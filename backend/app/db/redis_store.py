"""Redis layout (operational DB, message bus and cache).

    telem:in                 STREAM   normalized ingest packets (consumed by workers)
    hot:{ch}                 ZSET     hot window T_hot, stored in batches:
                                      score = first ts of batch (us), member = packed batch
    last                     HASH     ch -> last value  {"ts","val","q"}
    wm                       HASH     ch -> watermark (max ts seen, us)
    tile:{ch}:{lvl}:{k}      STRING   packed aggregate rows of a closed tile
    lease:{tile key}         STRING   lease of a tile being computed (anti-stampede)
    alarms:cmd               STREAM   commands to the analytics worker (ack)
    events / alarms          PUBSUB   diagnostic results for live clients
    metrics:*                LIST/HASH latency samples and counters
    models:version           STRING   bumped when active models change
    config:version           STRING   bumped when channels / alarm rules change
"""

from __future__ import annotations

import struct

import numpy as np
import orjson

from tsa_core.quality import Quality

STREAM_FIELD = b"p"
ALARM_CMD_STREAM = "alarms:cmd"
PUBSUB_EVENTS = "events"
PUBSUB_ALARMS = "alarms"
LAST_KEY = "last"
WM_KEY = "wm"
MODELS_VERSION_KEY = "models:version"
CONFIG_VERSION_KEY = "config:version"

QUALITY_CODES = [Quality.GOOD, Quality.UNCERTAIN, Quality.BAD, Quality.SUBSTITUTED]
QUALITY_TO_CODE = {q.value: i for i, q in enumerate(QUALITY_CODES)}

# Atomically raise the watermark of a channel: HSET only if greater.
WM_LUA = """
local cur = redis.call('HGET', KEYS[1], ARGV[1])
if (not cur) or tonumber(cur) < tonumber(ARGV[2]) then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
  return ARGV[2]
end
return cur
"""


def hot_key(channel: str) -> str:
    return f"hot:{channel}"


def tile_key(channel: str, lvl: int, k: int) -> str:
    return f"tile:{channel}:{lvl}:{k}"


def latency_key(stage: str) -> str:
    return f"metrics:lat:{stage}"


# --- hot window batches ---------------------------------------------------------

_HDR = struct.Struct("<I")


def pack_batch(ts: np.ndarray, val: np.ndarray, qcodes: np.ndarray) -> bytes:
    n = len(ts)
    return (
        _HDR.pack(n)
        + np.ascontiguousarray(ts, dtype="<i8").tobytes()
        + np.ascontiguousarray(val, dtype="<f8").tobytes()
        + np.ascontiguousarray(qcodes, dtype="u1").tobytes()
    )


def unpack_batch(buf: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    (n,) = _HDR.unpack_from(buf, 0)
    o = _HDR.size
    ts = np.frombuffer(buf, dtype="<i8", count=n, offset=o)
    val = np.frombuffer(buf, dtype="<f8", count=n, offset=o + 8 * n)
    q = np.frombuffer(buf, dtype="u1", count=n, offset=o + 16 * n)
    return ts, val, q


def split_batches(ts: np.ndarray, max_span_us: int) -> list[slice]:
    """Split a time-ordered batch so that no stored batch spans more than max_span."""
    if len(ts) == 0:
        return []
    out, start = [], 0
    t0 = ts[0]
    for i in range(1, len(ts)):
        if ts[i] - t0 > max_span_us:
            out.append(slice(start, i))
            start, t0 = i, ts[i]
    out.append(slice(start, len(ts)))
    return out


def merge_hot(members: list[bytes], t_from: int, t_to: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Concatenate hot-window batches, keep [t_from, t_to), sort and drop duplicate timestamps."""
    if not members:
        e = np.empty(0)
        return e.astype(np.int64), e, e.astype(np.uint8)
    parts = [unpack_batch(m) for m in members]
    ts = np.concatenate([p[0] for p in parts])
    val = np.concatenate([p[1] for p in parts])
    q = np.concatenate([p[2] for p in parts])
    keep = (ts >= t_from) & (ts < t_to)
    ts, val, q = ts[keep], val[keep], q[keep]
    order = np.argsort(ts, kind="stable")
    ts, val, q = ts[order], val[order], q[order]
    if len(ts) > 1:  # re-delivered packets: keep the last version of each timestamp
        last = np.r_[ts[1:] != ts[:-1], True]
        ts, val, q = ts[last], val[last], q[last]
    return ts, val, q


# --- stream packets ---------------------------------------------------------------


def encode_packet(packet: dict) -> bytes:
    return orjson.dumps(packet, option=orjson.OPT_SERIALIZE_NUMPY)


def decode_packet(raw: bytes) -> dict:
    return orjson.loads(raw)


def packet_arrays(ch: dict) -> tuple[np.ndarray, np.ndarray, list[str], np.ndarray, np.ndarray]:
    """Arrays of one channel entry of a normalized packet (null values -> NaN)."""
    ts = np.asarray(ch["ts"], dtype=np.int64)
    val = np.array([np.nan if v is None else v for v in ch["val"]], dtype=np.float64)
    n = len(ts)
    q = ch.get("q") or [Quality.GOOD.value] * n
    nd = np.asarray(ch.get("nd") or [False] * n, dtype=bool)
    otkl = np.asarray(ch.get("otkl") or [0] * n, dtype=np.int16)
    return ts, val, q, nd, otkl
