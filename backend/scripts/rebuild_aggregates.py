"""Recompute multi-level aggregates from raw `telemetry_raw` (exact, idempotent upserts).

Use after a bulk history import, after late data beyond Delta_late, or if the
aggregator was interrupted. Clear the Redis tile cache afterwards (--clear-cache).

    python -m scripts.rebuild_aggregates                      # all channels, whole archive
    python -m scripts.rebuild_aggregates --channels A1,A2 --from 2025-05-01 --to 2025-05-02
"""

from __future__ import annotations

import argparse
import sys
import time

import redis

from app.config import get_settings
from app.db.questdb import IlpWriter, QuestDBSyncReader
from scripts.replay import parse_time
from tsa_core.aggregates import MultiLevelAggregator


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channels")
    p.add_argument("--from", dest="t_from")
    p.add_argument("--to", dest="t_to")
    p.add_argument("--chunk-hours", type=float, default=6)
    p.add_argument("--clear-cache", action="store_true", help="delete cached tiles of the channels in Redis")
    a = p.parse_args()
    s = get_settings()
    reader = QuestDBSyncReader(s)
    writer = IlpWriter(s)
    grid = s.grid
    deltas = [grid.delta(lvl) for lvl in range(grid.n_levels)]
    channels = a.channels.split(",") if a.channels else reader.channels()
    # buckets of the coarsest level must be complete: align the range to it
    coarse = deltas[-1]
    for ch in channels:
        bounds = reader.time_bounds(ch)
        if bounds is None:
            continue
        t_from = parse_time(a.t_from) if a.t_from else bounds[0]
        t_to = parse_time(a.t_to) if a.t_to else bounds[1] + 1
        t_from = (t_from // coarse) * coarse
        t_to = -(-t_to // coarse) * coarse
        agg = MultiLevelAggregator(deltas, late_us=0)
        t0 = time.monotonic()
        n = rows = 0
        for ts, val in reader.iter_raw(ch, t_from, t_to, int(a.chunk_hours * 3600e6)):
            agg.add(ts, val)
            n += len(ts)
            for lvl, dirty in enumerate(agg.pop_dirty()):
                writer.aggregates(ch, lvl, dirty)
                rows += len(dirty)
            writer.flush()
        for lvl, dirty in enumerate(agg.flush_all()):
            writer.aggregates(ch, lvl, dirty)
            rows += len(dirty)
        writer.flush()
        print(f"{ch}: {n} points -> {rows} aggregate rows ({time.monotonic() - t0:.1f}s)")
        if a.clear_cache:
            r = redis.Redis.from_url(s.redis_url)
            keys = list(r.scan_iter(match=f"tile:{ch}:*", count=1000))
            if keys:
                r.delete(*keys)
    writer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
