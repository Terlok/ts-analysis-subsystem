"""Tile cache of composable aggregates in Redis (ФВ-4.3).

A tile (channel, level l, k) holds the aggregate rows of K buckets of width Delta_l,
aligned to the absolute time grid, so panning/zooming requests of different users
hit the same keys. A tile is immutable once t_wm - t_end > Delta_late (wm = channel
watermark); only such tiles are cached. The open tile is read from QuestDB each time.

Stampede protection: concurrent requests for the same missing tile range within a
process share one query (single flight); across processes a short Redis lease makes
the others wait for the cache to be filled.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from redis.asyncio import Redis

from app.config import Settings
from app.db.questdb import QuestDBReader
from app.db.redis_store import tile_key
from tsa_core.aggregates import AGG_DTYPE

log = logging.getLogger(__name__)

LEASE_MS = 5000
LEASE_WAIT_S = 2.0


class TileCache:
    def __init__(self, settings: Settings, redis: Redis, qdb: QuestDBReader):
        self.settings = settings
        self.grid = settings.grid
        self.redis = redis
        self.qdb = qdb
        self._inflight: dict[tuple, asyncio.Future] = {}

    async def rows(self, channel: str, lvl: int, t_from: int, t_to: int, watermark: int | None) -> tuple[np.ndarray, dict]:
        """Aggregate rows of level `lvl` overlapping [t_from, t_to), sorted by bucket start."""
        g = self.grid
        span = g.tile_span(lvl)
        tiles = list(g.tiles_covering(lvl, t_from, t_to))
        closed_before = (watermark - self.settings.late_us) if watermark is not None else None
        is_closed = [closed_before is not None and (k + 1) * span <= closed_before for k in tiles]

        stats = {"tiles": len(tiles), "hit": 0, "miss": 0, "open": 0}
        parts: dict[int, np.ndarray] = {}
        closed = [k for k, c in zip(tiles, is_closed) if c]
        if closed:
            cached = await self.redis.mget([tile_key(channel, lvl, k) for k in closed])
            for k, buf in zip(closed, cached):
                if buf is not None:
                    parts[k] = np.frombuffer(buf, dtype=AGG_DTYPE)
            stats["hit"] = len(parts)

        missing = [k for k in tiles if k not in parts]
        stats["miss"] = sum(1 for k in missing if k in closed)
        stats["open"] = len(missing) - stats["miss"]
        for run in _contiguous(missing):
            fetched = await self._fetch_run(channel, lvl, run)
            for k in run:
                rows = fetched[k]
                parts[k] = rows
                if k in closed:
                    await self.redis.set(tile_key(channel, lvl, k), rows.tobytes(), ex=self.settings.tile_ttl_s)

        if stats["hit"] or stats["miss"]:
            await self.redis.hincrby("metrics:tiles", "hit", stats["hit"])
            await self.redis.hincrby("metrics:tiles", "miss", stats["miss"])
        if not parts:
            return np.empty(0, dtype=AGG_DTYPE), stats
        rows = np.concatenate([parts[k] for k in tiles])
        return rows, stats

    async def _fetch_run(self, channel: str, lvl: int, run: list[int]) -> dict[int, np.ndarray]:
        key = (channel, lvl, run[0], run[-1])
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await self._fetch_with_lease(channel, lvl, run)
            fut.set_result(result)
            return result
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved
            raise
        finally:
            del self._inflight[key]

    async def _fetch_with_lease(self, channel: str, lvl: int, run: list[int]) -> dict[int, np.ndarray]:
        lease = f"lease:{tile_key(channel, lvl, run[0])}:{run[-1]}"
        got = await self.redis.set(lease, b"1", nx=True, px=LEASE_MS)
        if not got:  # another process is filling these tiles: wait for the cache
            deadline = asyncio.get_running_loop().time() + LEASE_WAIT_S
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
                cached = await self.redis.mget([tile_key(channel, lvl, k) for k in run])
                if all(c is not None for c in cached):
                    return {k: np.frombuffer(c, dtype=AGG_DTYPE) for k, c in zip(run, cached)}
        try:
            span = self.grid.tile_span(lvl)
            rows = await self.qdb.fetch_agg(channel, lvl, run[0] * span, (run[-1] + 1) * span)
        finally:
            if got:
                await self.redis.delete(lease)
        tile_of = rows["ts"] // span
        return {k: rows[tile_of == k] for k in run}

    async def invalidate(self, channel: str) -> int:
        n = 0
        async for key in self.redis.scan_iter(match=f"tile:{channel}:*", count=1000):
            await self.redis.delete(key)
            n += 1
        return n


def _contiguous(ks: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for k in ks:
        if runs and runs[-1][-1] == k - 1:
            runs[-1].append(k)
        else:
            runs.append([k])
    return runs
