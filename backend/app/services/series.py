"""Data selection module: level of detail, aggregation and downsampling (ФВ-4.1, ФВ-4.2).

For a request q = (C, t_s, t_e, W_px):
  1. m = min(N, ceil(c * W_px)).
  2. If the interval lies inside the Redis hot window, or is short (fewer than rho*m/2
     buckets of the finest level), raw points are read (hot window or QuestDB).
     N <= m -> returned as is, else MinMaxLTTB.
  3. Otherwise the coarsest level l* still giving >= rho*m MinMax candidates is
     chosen, its tiles are read through the cache, MinMax points of the buckets are
     expanded and LTTB reduces them to m points.
Diagnostics are NOT computed here: anomalies come from the separate events layer,
so they are identical for every m (formula (1.16)).
"""

from __future__ import annotations

import math
import time

import numpy as np
from redis.asyncio import Redis

from app.config import Settings
from app.db.questdb import QuestDBReader
from app.db.redis_store import QUALITY_CODES
from app.schemas import Series
from app.services.hot import read_hot, watermarks
from app.services.tiles import TileCache
from tsa_core.aggregates import minmax_points
from tsa_core.downsample import lttb_indices, minmax_lttb_indices
from tsa_core.levels import reduction_ratio

_QNAMES = np.array([q.value for q in QUALITY_CODES])


def _clean(v: np.ndarray) -> list[float | None]:
    return [None if x != x else x for x in v.tolist()]


class SeriesService:
    def __init__(self, settings: Settings, redis: Redis, qdb: QuestDBReader, tiles: TileCache):
        self.settings = settings
        self.grid = settings.grid
        self.redis = redis
        self.qdb = qdb
        self.tiles = tiles

    async def get(self, channels: list[str], t_from: int, t_to: int, width_px: int, c: float | None = None):
        s = self.settings
        c = c or s.detail_c
        m = max(3, math.ceil(c * width_px))
        wms = await watermarks(self.redis, channels)
        timing: dict[str, float] = {}
        out = []
        for ch in channels:
            t0 = time.perf_counter()
            out.append(await self._one(ch, t_from, t_to, m, wms.get(ch)))
            timing[ch] = round((time.perf_counter() - t0) * 1000, 2)
        return out, timing

    async def _one(self, ch: str, t_from: int, t_to: int, m: int, wm: int | None) -> Series:
        s = self.settings
        rho = s.preselect_rho
        in_hot = wm is not None and t_from >= wm - s.hot_window_s * 1_000_000
        # Recent intervals are served from raw hot-window points: exact and not delayed by
        # the aggregator flush; their size is bounded by T_hot * f.
        if in_hot or self.grid.buckets_in(0, t_from, t_to) * 2 < rho * m:
            return await self._raw(ch, t_from, t_to, m, wm)

        lvl = self.grid.select_level(t_from, t_to, m, rho)
        rows, _stats = await self.tiles.rows(ch, lvl, t_from, t_to, wm)
        if len(rows):  # tiles are wider than the request: keep overlapping buckets only
            rows = rows[(rows["ts"] + self.grid.delta(lvl) > t_from) & (rows["ts"] < t_to)]
        n =int(rows["cnt"].sum()) if len(rows) else 0
        if n <= m:
            return await self._raw(ch, t_from, t_to, m, wm)
        stats = {
            "vmin": float(rows["vmin"].min()),
            "vmax": float(rows["vmax"].max()),
            "mean": float(rows["vsum"].sum() / n),
        }
        t, x = minmax_points(rows, t_from, t_to)
        if len(t) > m:
            idx = lttb_indices(t, x, m)
            t, x = t[idx], x[idx]
        return Series(channel=ch, source="agg", level=lvl, n=n, m=m, eta=reduction_ratio(n, len(t)),
                      t=t.tolist(), v=_clean(x), **stats)

    async def _raw(self, ch: str, t_from: int, t_to: int, m: int, wm: int | None) -> Series:
        s = self.settings
        hot_from = wm - s.hot_window_s * 1_000_000 if wm is not None else None
        if hot_from is not None and t_from >= hot_from:
            ts, val, qc = await read_hot(self.redis, ch, t_from, t_to, s.hot_batch_max_span_s * 1_000_000)
            q = _QNAMES[qc] if len(qc) else np.empty(0, dtype=str)
            source = "hot"
        else:
            raw = await self.qdb.fetch_raw(ch, t_from, t_to, s.max_raw_points)
            ts, val, q = raw["ts"], raw["val"], raw["quality"]
            source = "raw"
        n = len(ts)
        if n == 0:
            return Series(channel=ch, source="empty", n=0, m=m, eta=0.0, t=[], v=[], q=[])
        finite = val[~np.isnan(val)]
        stats = (
            {"vmin": float(finite.min()), "vmax": float(finite.max()), "mean": float(finite.mean())}
            if len(finite)
            else {}
        )
        if n > m:
            ok = ~np.isnan(val)
            base = np.flatnonzero(ok)
            idx = base[minmax_lttb_indices(ts[ok], val[ok], m, s.preselect_rho)]
            ts, val, q = ts[idx], val[idx], q[idx]
        return Series(
            channel=ch,
            source=source,
            n=n,
            m=m,
            eta=reduction_ratio(n, len(ts)),
            t=ts.tolist(),
            v=_clean(val),
            q=[str(x) for x in q],
            **stats,
        )
