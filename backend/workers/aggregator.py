"""Aggregator: builds composable multi-level aggregates (min/max/first/last/sum/count with
times of extrema) on the grid Delta_l = Delta_0 * b^l and upserts them to `telemetry_agg`.

    python -m workers.aggregator [--shard i --shards n]

Changed buckets are written every `agg_flush_interval_ms`; stream messages are
acknowledged only after that write. When a channel is seen for the first time after a
start, already stored buckets of its current interval are loaded so that new points are
merged into them. Exactly-once is not guaranteed if the process dies between the write
and XACK (re-processed points would be counted twice in the open buckets); use
`scripts/rebuild_aggregates.py` to recompute aggregates from raw data if needed.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from app.db.questdb import IlpWriter, QuestDBSyncReader
from app.db.redis_store import packet_arrays
from tsa_core.aggregates import MultiLevelAggregator
from workers.common import LoadMeter, StreamConsumer, settings, setup_logging, shard_of, worker_args

log = logging.getLogger("aggregator")


def main() -> None:
    setup_logging()
    args = worker_args("Multi-level aggregator")
    s = settings()
    grid = s.grid
    deltas = [grid.delta(lvl) for lvl in range(grid.n_levels)]
    reader = QuestDBSyncReader(s)
    state = {"writer": IlpWriter(s)}
    aggs: dict[str, MultiLevelAggregator] = {}
    pending: list[bytes] = []
    last_flush = time.monotonic()
    meter: LoadMeter | None = None
    consumer: StreamConsumer | None = None

    def agg_for(channel: str, t_first: int) -> MultiLevelAggregator:
        a = aggs.get(channel)
        if a is None:
            a = aggs[channel] = MultiLevelAggregator(deltas, s.late_us)
            for lvl, d in enumerate(deltas):
                start = (t_first // d) * d
                try:
                    a.seed(lvl, reader.fetch_agg(channel, lvl, start - s.late_us, start + d + s.late_us))
                except Exception:  # noqa: BLE001 - table may not exist yet
                    log.warning("cannot seed %s level %d", channel, lvl)
        return a

    def handle(batch):
        t0 = time.perf_counter()
        n = 0
        for entry_id, pkt in batch:
            for ch in pkt["ch"]:
                cid = ch["id"]
                if shard_of(cid, args.shards) != args.shard:
                    continue
                ts, val, q, _nd, _otkl = packet_arrays(ch)
                good = np.array([x != "bad" for x in q], dtype=bool) & ~np.isnan(val)
                if good.any():
                    agg_for(cid, int(ts[good][0])).add(ts[good], val[good])
                    n += int(good.sum())
            pending.append(entry_id)
        meter.record(n, time.perf_counter() - t0)

    def tick():
        nonlocal last_flush
        if time.monotonic() - last_flush < s.agg_flush_interval_ms / 1000:
            return
        writer = state["writer"]
        rows = 0
        for cid, a in aggs.items():
            for lvl, dirty in enumerate(a.pop_dirty()):
                if len(dirty):
                    writer.aggregates(cid, lvl, dirty)
                    rows += len(dirty)
        if rows:
            writer.flush()
        consumer.ack(pending)
        pending.clear()
        last_flush = time.monotonic()

    def on_error(_e):
        # Buckets popped in the failed tick are lost from memory: drop all state and let the
        # pending messages rebuild it on top of what is stored (seeding).
        aggs.clear()
        pending.clear()
        try:
            state["writer"].sender.close()
            state["writer"] = IlpWriter(s)
        except Exception:  # noqa: BLE001
            log.warning("QuestDB is still unavailable")

    group = f"aggregator-{args.shard}of{args.shards}"
    consumer = StreamConsumer(s, group, args.consumer, handle, tick=tick, ack_after_handler=False, on_error=on_error)
    meter = LoadMeter(consumer.r, f"aggregator:{args.shard}of{args.shards}")
    try:
        consumer.run()
    finally:
        w = state["writer"]
        for cid, a in aggs.items():
            for lvl, dirty in enumerate(a.flush_all()):
                if len(dirty):
                    w.aggregates(cid, lvl, dirty)
        w.close()
        consumer.ack(pending)


if __name__ == "__main__":
    main()
