"""Downsampling for visualization: LTTB, MinMax preselection, MinMaxLTTB and the
grid-anchored streaming LTTB used for live charts.

All functions return *indices* into the input arrays so the caller can carry along
any extra per-point columns (quality, flags, ...).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


def target_points(n: int, width_px: int, c: float = 2.0) -> int:
    """Formula (4): m = min(N, ceil(c * W_px))."""
    return min(n, math.ceil(c * width_px))


def lttb_indices(t: np.ndarray, x: np.ndarray, m: int) -> np.ndarray:
    """Largest-Triangle-Three-Buckets (Steinarsson 2013), formula (5).

    Keeps the first and last points, splits the rest into m-2 groups and picks in each
    group the point forming the largest triangle with the previously selected point
    and the centroid of the next group. O(N) time, O(m) result.
    """
    n = len(t)
    if m >= n or n < 3:
        return np.arange(n)
    if m < 3:
        return np.array([0, n - 1])[:m]

    tf = (t - t[0]).astype(np.float64)
    xf = x.astype(np.float64, copy=False)
    out = np.empty(m, dtype=np.int64)
    out[0] = 0
    out[-1] = n - 1
    every = (n - 2) / (m - 2)
    a = 0
    for i in range(m - 2):
        rs = int(i * every) + 1
        re = int((i + 1) * every) + 1
        ns = re
        ne = min(int((i + 2) * every) + 1, n)
        if ne <= ns:
            ns, ne = n - 1, n
        ct = tf[ns:ne].mean()
        cx = xf[ns:ne].mean()
        ta, xa = tf[a], xf[a]
        area = np.abs((ta - ct) * (xf[rs:re] - xa) - (ta - tf[rs:re]) * (cx - xa))
        a = rs + int(np.argmax(area))
        out[i + 1] = a
    return out


def minmax_indices(t: np.ndarray, x: np.ndarray, n_buckets: int) -> np.ndarray:
    """MinMax preselection over equal *time* segments (formula (13) of the thesis).

    For every non-empty segment returns the indices of its minimum and maximum
    (in time order); the first and last points of the series are always included.
    """
    n = len(t)
    if n == 0 or n_buckets <= 0 or 2 * n_buckets >= n:
        return np.arange(n)
    t0, t1 = int(t[0]), int(t[-1])
    span = max(t1 - t0, 1)
    bucket = ((t - t0) * n_buckets // (span + 1)).astype(np.int64)
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], n]
    idx = []
    for s, e in zip(starts, ends):
        seg = x[s:e]
        i_min = s + int(np.argmin(seg))
        i_max = s + int(np.argmax(seg))
        idx.append(min(i_min, i_max))
        idx.append(max(i_min, i_max))
    sel = np.unique(np.r_[0, np.asarray(idx, dtype=np.int64), n - 1])
    return sel


def minmax_lttb_indices(t: np.ndarray, x: np.ndarray, m: int, rho: float = 2.0) -> np.ndarray:
    """MinMaxLTTB (Van Der Donckt et al. 2023): MinMax preselection of ~rho*m points, then LTTB to m."""
    n = len(t)
    if m >= n:
        return np.arange(n)
    pre = minmax_indices(t, x, max(1, math.ceil(rho * m / 2)))
    if len(pre) <= m:
        return pre
    return pre[lttb_indices(t[pre], x[pre], m)]


def drop_nan(t: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ok = ~np.isnan(x)
    if ok.all():
        return t, x
    return t[ok], x[ok]


@dataclass
class _Bucket:
    t: list[int] = field(default_factory=list)
    x: list[float] = field(default_factory=list)


class StreamingGridLTTB:
    """LTTB for live charts with groups bound to an absolute time grid (requirement ФВ-4.4).

    Re-running LTTB over a window shifted by a few samples moves all group borders
    and makes the curve "jitter" from frame to frame. Here group k always covers
    [k*delta, (k+1)*delta) in absolute time, and its point is chosen once, as soon as
    the next group is complete (its centroid is needed for the triangle). Finalized
    points never change; only the unfinished tail is re-sent on every update.

    Input must be ordered by time; late points for already finalized groups are dropped.
    """

    def __init__(self, delta_us: int):
        if delta_us <= 0:
            raise ValueError("delta must be positive")
        self.delta = int(delta_us)
        self._buckets: dict[int, _Bucket] = {}
        self._a: tuple[int, float] | None = None
        self._finalized_through = -(2**62)
        self._latest_bucket = -(2**62)
        self._pending: list[tuple[int, float]] = []

    @staticmethod
    def grid_delta(window_us: int, m: int) -> int:
        """Bucket width rounded up to a "nice" 1-2-5 value so that clients with similar
        windows share the same grid."""
        raw = max(1, math.ceil(window_us / max(m, 1)))
        mag = 10 ** int(math.floor(math.log10(raw)))
        for step in (1, 2, 5, 10):
            if step * mag >= raw:
                return step * mag
        return 10 * mag

    def push(self, t: np.ndarray, x: np.ndarray) -> None:
        for ti, xi in zip(t.tolist(), x.tolist()):
            if xi != xi:
                continue
            k = ti // self.delta
            if k <= self._finalized_through:
                continue
            b = self._buckets.get(k)
            if b is None:
                b = self._buckets[k] = _Bucket()
            b.t.append(ti)
            b.x.append(xi)
            if k > self._latest_bucket:
                self._latest_bucket = k
        self._finalize()

    def _finalize(self) -> None:
        keys = sorted(self._buckets)
        # A bucket is complete once a later bucket has started (data is time ordered).
        complete = [k for k in keys if k < self._latest_bucket]
        for j, k in enumerate(complete):
            if j + 1 >= len(complete):
                break  # the next complete bucket is needed for the centroid
            nxt = self._buckets[complete[j + 1]]
            cur = self._buckets[k]
            if self._a is None:
                sel = 0
            else:
                ta, xa = self._a
                ct = sum(nxt.t) / len(nxt.t)
                cx = sum(nxt.x) / len(nxt.x)
                best, sel = -1.0, 0
                for i, (tb, xb) in enumerate(zip(cur.t, cur.x)):
                    area = abs((ta - ct) * (xb - xa) - (ta - tb) * (cx - xa))
                    if area > best:
                        best, sel = area, i
            self._a = (cur.t[sel], cur.x[sel])
            self._pending.append(self._a)
            self._finalized_through = k
            del self._buckets[k]

    def drain(self) -> list[tuple[int, float]]:
        """Points finalized since the previous call (stable, never re-sent)."""
        out, self._pending = self._pending, []
        return out

    def tail(self) -> list[tuple[int, float]]:
        """Provisional points of unfinished groups (min/max of each, time ordered)."""
        out: list[tuple[int, float]] = []
        for k in sorted(self._buckets):
            b = self._buckets[k]
            i_min = min(range(len(b.x)), key=b.x.__getitem__)
            i_max = max(range(len(b.x)), key=b.x.__getitem__)
            for i in sorted({i_min, i_max, len(b.x) - 1}):
                out.append((b.t[i], b.x[i]))
        return out
