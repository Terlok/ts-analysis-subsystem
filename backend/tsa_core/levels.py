"""Level-of-detail selection and tile addressing (formulas (1.8), (1.9) of the thesis)."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Grid:
    delta0_us: int  # Delta_0
    base: int  # b
    n_levels: int
    tile_buckets: int  # K, buckets per tile

    def delta(self, level: int) -> int:
        return self.delta0_us * self.base**level

    def tile_span(self, level: int) -> int:
        return self.tile_buckets * self.delta(level)

    def tile_index(self, level: int, t_us: int) -> int:
        """k = floor(t / (K * Delta_l))."""
        return t_us // self.tile_span(level)

    def tiles_covering(self, level: int, t_from: int, t_to: int) -> range:
        """Tiles covering [t_from, t_to): ceil((te-ts)/(K*Delta_l)) + 1 at most."""
        return range(self.tile_index(level, t_from), self.tile_index(level, max(t_to - 1, t_from)) + 1)

    def select_level(self, t_from: int, t_to: int, m: int, rho: float = 2.0) -> int:
        """Coarsest level whose MinMax preselection still yields >= rho*m candidate points.

        Every bucket contributes two points (min, max), so we need
        (te - ts) / Delta_l >= rho*m/2, i.e.
            l* = max(0, floor(log_b(2 (te - ts) / (rho m Delta_0)))),
        clamped to the available levels. This is formula (1.8) with c*W_px replaced by
        rho*m/2 buckets so the final LTTB step always has enough candidates.
        """
        span = max(t_to - t_from, 1)
        need_buckets = max(rho * m / 2.0, 1.0)
        ratio = span / (need_buckets * self.delta0_us)
        if ratio < 1:
            return 0
        lvl = int(math.floor(math.log(ratio, self.base) + 1e-9))
        return max(0, min(lvl, self.n_levels - 1))

    def buckets_in(self, level: int, t_from: int, t_to: int) -> float:
        return (t_to - t_from) / self.delta(level)


def reduction_ratio(n: int, m: int) -> float:
    """eta = 1 - m/N, share of removed points."""
    return 0.0 if n == 0 else max(0.0, 1.0 - m / n)
