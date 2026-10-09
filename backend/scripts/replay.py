"""Telemetry replayer: imitates a robot gateway by sending measurements from a file
(exported from the `analog` table) to the ingestion endpoint of the backend.

    python -m scripts.replay data/analog.csv                         # real time, WebSocket
    python -m scripts.replay data/analog.csv --speed 10              # 10x faster
    python -m scripts.replay data/analog.csv --speed 0 --time-mode original   # bulk load of history
    python -m scripts.replay data/analog.csv --channels A1,A2 --loop
    python -m scripts.replay data/analog.csv --dry-run               # only statistics

How it works
  * the file is read in chunks and cut into packets of `--batch-ms` of *data* time
    (or `--max-points`), each packet carries all channels in columnar form;
    the batch size bounds the buffering delay L_batch <= (B_s - 1) / f_i;
  * packet k is sent at wall time  start + (t_end_k - t_first) / speed;
  * time modes:
      rebase   (default) timestamps are shifted so the first sample is "now":
               the stream looks live and the hot window / live charts work;
      original timestamps are sent as they are in the file (history import);
    with --compress-time and speed > 1 rebased timestamps are also compressed,
    i.e. data time follows the wall clock;
  * delivery: WebSocket with acknowledgements; unacknowledged packets are re-sent
    after a reconnect (the server deduplicates by (channel, ts)). HTTP POST is
    available with --http.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import orjson

from scripts.telemetry_file import Chunk, Columns, filter_chunk, read_chunks

log = logging.getLogger("replay")


def now_us() -> int:
    return time.time_ns() // 1000


def parse_time(s: str | None) -> int | None:
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000)


# --- packetizing ----------------------------------------------------------------------


@dataclass
class Packet:
    seq: int
    t_start: int  # original data time of the first sample
    t_end: int  # original data time of the last sample
    n: int
    channels: dict[str, dict] = field(default_factory=dict)


class Packetizer:
    """Cuts a time-ordered row stream into packets of `batch_us` data time / `max_points`."""

    def __init__(self, batch_us: int, max_points: int):
        self.batch_us = batch_us
        self.max_points = max_points
        self.seq = 0
        self._rows: list[Chunk] = []
        self._n = 0
        self._t0: int | None = None
        self.out_of_order = 0
        self._last_ts: int | None = None

    def feed(self, c: Chunk) -> list[Packet]:
        """Add rows; returns completed packets."""
        if len(c) == 0:
            return []
        if np.any(np.diff(c.ts) < 0):  # local disorder inside a chunk: sort it
            c = c.take(np.argsort(c.ts, kind="stable"))
        if self._last_ts is not None and c.ts[0] < self._last_ts:
            self.out_of_order += int((c.ts < self._last_ts).sum())
        self._last_ts = int(c.ts[-1]) if self._last_ts is None else max(self._last_ts, int(c.ts[-1]))

        packets = []
        i = 0
        n = len(c)
        while i < n:
            if self._t0 is None:
                self._t0 = int(c.ts[i])
            limit = self._t0 + self.batch_us
            # rows of this chunk that still belong to the current packet
            j = int(np.searchsorted(c.ts, limit, side="left", sorter=None))
            j = max(j, i)
            room = self.max_points - self._n
            j = min(j, i + room)
            if j > i:
                self._rows.append(c.take(slice(i, j)))
                self._n += j - i
            if j < n or self._n >= self.max_points:
                packets.append(self._emit())
            i = j
        return packets

    def flush(self) -> list[Packet]:
        return [self._emit()] if self._n else []

    def _emit(self) -> Packet:
        c = Chunk.concat(self._rows)
        self._rows, self._n, self._t0 = [], 0, None
        self.seq += 1
        p = Packet(self.seq, int(c.ts.min()), int(c.ts.max()), len(c))
        order = np.argsort(c.id, kind="stable")  # group by channel, keep time order inside
        c = c.take(order)
        ids, starts = np.unique(c.id, return_index=True)
        ends = list(starts[1:]) + [len(c)]
        for cid, s, e in zip(ids, starts, ends):
            p.channels[str(cid)] = {"ts": c.ts[s:e], "val": c.val[s:e], "nd": c.nd[s:e], "otkl": c.otkl[s:e]}
        return p


def encode(p: Packet, offset_us: int, compress: float, t_first: int, source: str) -> bytes:
    """Serialize a packet applying the time mapping ts' = t_first + offset + (ts - t_first) / compress."""
    chans = []
    for cid, d in p.channels.items():
        ts = d["ts"]
        if compress != 1.0:
            ts = t_first + np.round((ts - t_first) / compress).astype(np.int64)
        ts = ts + offset_us
        val = [None if v != v else v for v in d["val"].tolist()]
        chans.append({"id": cid, "ts": ts.tolist(), "val": val, "nd": d["nd"].tolist(), "otkl": d["otkl"].tolist()})
    return orjson.dumps({"seq": p.seq, "source": source, "sent_at": now_us(), "channels": chans})


# --- transport ------------------------------------------------------------------------


class Stats:
    def __init__(self):
        self.t0 = time.monotonic()
        self.packets = self.points = self.acked = self.rejected = self.resent = 0
        self.errors: collections.Counter[str] = collections.Counter()
        self.lag_s = 0.0
        self.new_channels: set[str] = set()

    def line(self, unacked: int) -> str:
        dt = max(time.monotonic() - self.t0, 1e-9)
        return (
            f"packets={self.packets} points={self.points} rate={self.points / dt:,.0f} pts/s "
            f"acked={self.acked} unacked={unacked} rejected={self.rejected} resent={self.resent} "
            f"lag={self.lag_s:.2f}s channels+={len(self.new_channels)}"
        )


class WsTransport:
    def __init__(self, url: str, stats: Stats, max_inflight: int, use_proxy: bool = False):
        self.url = url
        self.use_proxy = use_proxy
        self.stats = stats
        self.max_inflight = max_inflight
        self.unacked: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        self._ws = None
        self._space = asyncio.Event()
        self._space.set()
        self._connected = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._connection_loop())
        await asyncio.wait_for(self._connected.wait(), timeout=None)

    async def _connection_loop(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        kwargs = {"max_size": None, "ping_interval": 20}
        if not self.use_proxy:
            kwargs["proxy"] = None  # websockets >= 15 would otherwise honour HTTP(S)_PROXY
        backoff = 0.5
        while not self._closing:
            try:
                async with connect(self.url, **kwargs) as ws:
                    self._ws = ws
                    backoff = 0.5
                    for payload in list(self.unacked.values()):  # resend after reconnect
                        await ws.send(payload)
                        self.stats.resent += 1
                    self._connected.set()
                    log.info("connected to %s", self.url)
                    async for msg in ws:
                        self._on_ack(orjson.loads(msg))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure: reconnect and resend
                if self._closing:
                    break
                self._connected.clear()
                self._ws = None
                kind = "connection lost" if isinstance(e, ConnectionClosed) else "cannot connect"
                log.warning("%s (%s: %s), retrying in %.1fs", kind, e.__class__.__name__, str(e)[:120], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    def _on_ack(self, ack: dict) -> None:
        seq = ack.get("seq")
        if seq in self.unacked:
            del self.unacked[seq]
            self.stats.acked += 1
        self.stats.rejected += ack.get("rejected", 0)
        for e in ack.get("errors", []):
            self.stats.errors[e[:200]] += 1
        self.stats.new_channels.update(ack.get("new_channels", []))
        if len(self.unacked) < self.max_inflight:
            self._space.set()

    async def send(self, seq: int, payload: bytes) -> None:
        while len(self.unacked) >= self.max_inflight:
            self._space.clear()
            await self._space.wait()
        self.unacked[seq] = payload
        await self._connected.wait()
        try:
            await self._ws.send(payload)
        except Exception:  # noqa: BLE001 - will be resent after reconnect
            pass

    async def drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.unacked and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


class HttpTransport:
    def __init__(self, url: str, stats: Stats, use_proxy: bool = False):
        import httpx

        self.url = url
        self.stats = stats
        self.client = httpx.AsyncClient(timeout=30, trust_env=use_proxy)
        self.unacked: dict = {}

    async def start(self) -> None:
        pass

    async def send(self, seq: int, payload: bytes) -> None:
        backoff = 0.5
        while True:
            try:
                r = await self.client.post(self.url, content=payload, headers={"content-type": "application/json"})
                if r.status_code == 200:
                    ack = r.json()
                    self.stats.acked += 1
                    self.stats.rejected += ack.get("rejected", 0)
                    for e in ack.get("errors", []):
                        self.stats.errors[e[:200]] += 1
                    self.stats.new_channels.update(ack.get("new_channels", []))
                    return
                if 400 <= r.status_code < 500:
                    self.stats.errors[f"HTTP {r.status_code}: {r.text[:150]}"] += 1
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("POST failed (%s), retrying", e.__class__.__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)

    async def drain(self, timeout: float) -> None:
        pass

    async def close(self) -> None:
        await self.client.aclose()


class NullTransport:
    def __init__(self, stats: Stats):
        self.stats = stats
        self.unacked: dict = {}

    async def start(self) -> None:
        pass

    async def send(self, seq: int, payload: bytes) -> None:
        self.stats.acked += 1

    async def drain(self, timeout: float) -> None:
        pass

    async def close(self) -> None:
        pass


# --- main loop --------------------------------------------------------------------


async def replay(args: argparse.Namespace) -> int:
    stats = Stats()
    if args.dry_run:
        transport = NullTransport(stats)
    elif args.http:
        transport = HttpTransport(args.http, stats, args.use_proxy)
    else:
        transport = WsTransport(args.url, stats, args.max_inflight, args.use_proxy)
    await transport.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - windows
            pass

    cols = Columns(args.col_id, args.col_ts, args.col_val, args.col_nd, args.col_otkl)
    channels = set(args.channels.split(",")) if args.channels else None
    t_from, t_to = parse_time(args.start), parse_time(args.end)
    speed = args.speed
    compress = speed if (args.compress_time and speed > 0 and args.time_mode == "rebase") else 1.0

    async def reporter():
        while not stop.is_set():
            await asyncio.sleep(args.report_s)
            log.info(stats.line(len(transport.unacked)))

    rep = asyncio.create_task(reporter())
    clock = {"t_first": None, "offset": 0, "wall0": time.monotonic()}

    async def emit(packets: list[Packet], shift_us: int) -> None:
        """Send packets on schedule. shift_us: data-time shift of the current loop iteration."""
        for p in packets:
            if stop.is_set():
                return
            if clock["t_first"] is None:  # first packet ever: anchor the time mapping
                clock["t_first"] = p.t_start
                clock["offset"] = (now_us() - p.t_start) if args.time_mode == "rebase" else 0
                clock["wall0"] = time.monotonic()
            t_first = clock["t_first"]
            if speed > 0:
                due = clock["wall0"] + (p.t_end + shift_us - t_first) / 1e6 / speed
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                stats.lag_s = max(0.0, -delay)
            offset = clock["offset"] + (round(shift_us / compress) if args.time_mode == "rebase" else 0)
            await transport.send(p.seq, encode(p, offset, compress, t_first, args.source))
            stats.packets += 1
            stats.points += p.n

    iteration = 0
    t_lo = t_hi = None  # data time range of the file (after filters)
    try:
        while not stop.is_set():
            pk = Packetizer(args.batch_ms * 1000, args.max_points)
            pk.seq = iteration * 10_000_000  # sequence numbers stay unique across loops
            shift = 0
            if iteration > 0:
                shift = iteration * (t_hi - t_lo + args.batch_ms * 1000)
            rows = 0
            for chunk in read_chunks(args.file, cols, args.chunk_rows, args.format):
                chunk = filter_chunk(chunk, channels, t_from, t_to)
                if args.limit and rows + len(chunk) > args.limit:
                    chunk = chunk.take(slice(0, max(args.limit - rows, 0)))
                if len(chunk):
                    lo, hi = int(chunk.ts.min()), int(chunk.ts.max())
                    t_lo = lo if t_lo is None else min(t_lo, lo)
                    t_hi = hi if t_hi is None else max(t_hi, hi)
                await emit(pk.feed(chunk), shift)
                rows += len(chunk)
                if stop.is_set() or (args.limit and rows >= args.limit):
                    break
            await emit(pk.flush(), shift)
            if pk.out_of_order:
                log.warning("%d rows were older than already sent ones (file not sorted by ts?)", pk.out_of_order)
            if rows == 0:
                log.warning("no rows matched (check file, --channels, --start/--end)")
            iteration += 1
            if not args.loop or rows == 0:
                break
        await transport.drain(args.drain_timeout)
    finally:
        rep.cancel()
        await transport.close()
    log.info("done: %s", stats.line(len(transport.unacked)))
    for err, n in stats.errors.most_common(10):
        log.warning("server error x%d: %s", n, err)
    if transport.unacked:
        log.error("%d packet(s) were not acknowledged", len(transport.unacked))
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file", help="CSV / CSV.gz / Parquet file exported from the analog table")
    p.add_argument("--url", default="ws://localhost:8000/ws/ingest", help="WebSocket ingest endpoint")
    p.add_argument("--http", metavar="URL", help="use HTTP POST instead, e.g. http://localhost:8000/api/ingest")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed factor; 0 = as fast as possible")
    p.add_argument("--time-mode", choices=["rebase", "original"], default="rebase")
    p.add_argument("--compress-time", action="store_true", help="with rebase and speed>1: compress data time too")
    p.add_argument("--batch-ms", type=int, default=100, help="data time covered by one packet")
    p.add_argument("--max-points", type=int, default=5000, help="max measurements per packet")
    p.add_argument("--max-inflight", type=int, default=64, help="max unacknowledged packets (WebSocket)")
    p.add_argument("--channels", help="comma separated channel ids to send (default: all)")
    p.add_argument("--start", help="skip rows before this time (ISO 8601 or us)")
    p.add_argument("--end", help="skip rows from this time (ISO 8601 or us)")
    p.add_argument("--limit", type=int, default=0, help="stop after N rows (per loop)")
    p.add_argument("--loop", action="store_true", help="repeat the file endlessly (rebase mode shifts time)")
    p.add_argument("--source", default="replayer", help="source name stored with the channels")
    p.add_argument("--format", choices=["auto", "csv", "parquet"], default="auto")
    p.add_argument("--chunk-rows", type=int, default=200_000)
    p.add_argument("--col-id", default="id")
    p.add_argument("--col-ts", default="ts")
    p.add_argument("--col-val", default="val")
    p.add_argument("--col-nd", default="nd")
    p.add_argument("--col-otkl", default="otkl")
    p.add_argument("--report-s", type=float, default=5.0, help="statistics period")
    p.add_argument("--drain-timeout", type=float, default=30.0, help="wait for acknowledgements at the end")
    p.add_argument("--use-proxy", action="store_true", help="honour HTTP(S)_PROXY environment variables")
    p.add_argument("--dry-run", action="store_true", help="read and packetize without sending")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.speed < 0 or (args.speed > 0 and not math.isfinite(args.speed)):
        log.error("--speed must be >= 0")
        return 2
    try:
        return asyncio.run(replay(args))
    except FileNotFoundError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
