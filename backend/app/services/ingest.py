"""Ingestion module: validates packets, assigns quality codes, records t_ing and
publishes normalized packets to the Redis stream consumed by archiver/analytics/aggregator.

Only after the stream entry is written the packet is acknowledged to the sender,
so a confirmed packet survives a restart of any consumer (НФВ-7).
"""

from __future__ import annotations

import time

import numpy as np
import orjson
from redis.asyncio import Redis

from app.config import Settings
from app.db.redis_store import (
    LAST_KEY,
    QUALITY_TO_CODE,
    STREAM_FIELD,
    WM_KEY,
    WM_LUA,
    encode_packet,
    hot_key,
    latency_key,
    pack_batch,
    split_batches,
)
from app.schemas import IngestAck, IngestPacket
from app.services.registry import ChannelRegistry
from tsa_core.quality import quality_from_source

# Sanity range for source timestamps: 2000-01-01 .. 2100-01-01 (us)
TS_MIN = 946_684_800_000_000
TS_MAX = 4_102_444_800_000_000
LATENCY_SAMPLES = 10_000


def now_us() -> int:
    return time.time_ns() // 1000


class IngestService:
    def __init__(self, settings: Settings, redis: Redis, registry: ChannelRegistry):
        self.settings = settings
        self.redis = redis
        self.registry = registry

    async def ingest(self, packet: IngestPacket) -> IngestAck:
        t_ing = now_us()
        new_channels = await self.registry.ensure([c.id for c in packet.channels], source=packet.source)
        channels = await self.registry.all()

        accepted = rejected = reordered = 0
        errors: list[str] = []
        norm_channels = []
        for batch in packet.channels:
            n = len(batch.ts)
            if n == 0:
                continue
            ts = np.asarray(batch.ts, dtype=np.int64)
            if ts.min() < TS_MIN or ts.max() > TS_MAX:
                rejected += n
                errors.append(f"{batch.id}: timestamps out of range (expected microseconds since epoch)")
                continue
            val = np.array([np.nan if v is None else v for v in batch.val], dtype=np.float64)
            nd = np.asarray(batch.nd if batch.nd is not None else [False] * n, dtype=bool)
            otkl = np.asarray(batch.otkl if batch.otkl is not None else [0] * n, dtype=np.int16)

            if np.any(np.diff(ts) < 0):  # monotonicity check (ФВ-1.2): reorder instead of rejecting
                order = np.argsort(ts, kind="stable")
                ts, val, nd, otkl = ts[order], val[order], nd[order], otkl[order]
                reordered += n
            if n > 1:  # duplicate timestamps inside a packet: keep the last value
                keep = np.r_[ts[1:] != ts[:-1], True]
                if not keep.all():
                    rejected += int((~keep).sum())
                    ts, val, nd, otkl = ts[keep], val[keep], nd[keep], otkl[keep]

            meta = channels.get(batch.id)
            x_min = meta.x_min if meta else None
            x_max = meta.x_max if meta else None
            q = [
                quality_from_source(float(v), bool(d), int(o), x_min, x_max).value
                for v, d, o in zip(val, nd, otkl)
            ]
            accepted += len(ts)
            norm_channels.append((batch.id, ts, val, q, nd, otkl))

        if norm_channels:
            await self._publish(packet, t_ing, norm_channels)
        return IngestAck(
            seq=packet.seq,
            accepted=accepted,
            rejected=rejected,
            reordered=reordered,
            new_channels=new_channels,
            errors=errors,
        )

    async def _publish(self, packet: IngestPacket, t_ing: int, chans: list) -> None:
        s = self.settings
        hot_us = s.hot_window_s * 1_000_000
        span_us = s.hot_batch_max_span_s * 1_000_000
        body = {
            "seq": packet.seq,
            "src": packet.source,
            "sent_at": packet.sent_at,
            "t_ing": t_ing,
            "ch": [
                {
                    "id": cid,
                    "ts": ts,
                    "val": [None if v != v else v for v in val.tolist()],
                    "q": q,
                    "nd": nd,
                    "otkl": otkl,
                }
                for cid, ts, val, q, nd, otkl in chans
            ],
        }
        pipe = self.redis.pipeline(transaction=False)
        pipe.xadd(s.stream_key, {STREAM_FIELD: encode_packet(body)}, maxlen=s.stream_maxlen, approximate=True)
        for cid, ts, val, q, _nd, _otkl in chans:
            qcodes = np.fromiter((QUALITY_TO_CODE[x] for x in q), dtype=np.uint8, count=len(q))
            t_last = int(ts[-1])
            for sl in split_batches(ts, span_us):
                if ts[sl][-1] < t_last - hot_us:
                    continue  # too old for the hot window, goes only to the archive
                pipe.zadd(hot_key(cid), {pack_batch(ts[sl], val[sl], qcodes[sl]): int(ts[sl][0])})
            pipe.zremrangebyscore(hot_key(cid), "-inf", t_last - hot_us - span_us)
            last_v = float(val[-1])
            pipe.hset(
                LAST_KEY,
                cid,
                orjson.dumps({"ts": t_last, "val": None if last_v != last_v else last_v, "q": q[-1]}),
            )
            pipe.eval(WM_LUA, 1, WM_KEY, cid, t_last)
        # L_acq sample: delivery delay of the newest point (meaningful with synchronized clocks)
        newest = max(int(ts[-1]) for _, ts, *_ in chans)
        pipe.lpush(latency_key("acq"), t_ing - newest)
        pipe.ltrim(latency_key("acq"), 0, LATENCY_SAMPLES - 1)
        await pipe.execute()
