"""Incremental window features (formula (2) of the article).

For every channel the feature vector over the last w samples contains
mean, standard deviation, minimum, maximum, RMS and the slope of the local linear
trend. Sampling is non-uniform, so the slope is estimated by least squares over
timestamps (seconds), not over sample indices:

    beta = sum (t_k - t_mean)(x_k - x_mean) / sum (t_k - t_mean)^2

Sums are updated incrementally (O(1)), extrema are kept in monotonic deques
(amortized O(1), Lemire 2006).
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

FEATURE_NAMES = ("x", "mean", "std", "min", "max", "rms", "slope")
N_FEATURES = len(FEATURE_NAMES)

_REBASE_SECONDS = 3600.0  # re-anchor time origin to keep sums numerically well-conditioned
_RESYNC_EVERY = 10_000  # recompute sums from scratch to stop floating point drift


class WindowFeatures:
    def __init__(self, window: int = 30):
        if window < 2:
            raise ValueError("feature window must be >= 2")
        self.window = window
        self._t: deque[float] = deque()  # seconds relative to self._t0
        self._x: deque[float] = deque()
        self._t0_us: int | None = None
        self._min: deque[tuple[int, float]] = deque()  # (seq, value), increasing values
        self._max: deque[tuple[int, float]] = deque()  # (seq, value), decreasing values
        self._seq = 0
        self._sx = self._sxx = self._st = self._stt = self._stx = 0.0
        self._since_resync = 0

    @property
    def ready(self) -> bool:
        return len(self._x) >= self.window

    def update(self, ts_us: int, x: float) -> np.ndarray | None:
        """Add a sample; returns the feature vector (FEATURE_NAMES order) or None while warming up."""
        if x != x:
            return None
        if self._t0_us is None:
            self._t0_us = ts_us
        t = (ts_us - self._t0_us) / 1e6
        if t > _REBASE_SECONDS:
            self._rebase(ts_us)
            t = 0.0

        self._t.append(t)
        self._x.append(x)
        self._sx += x
        self._sxx += x * x
        self._st += t
        self._stt += t * t
        self._stx += t * x

        seq = self._seq
        self._seq += 1
        while self._min and self._min[-1][1] >= x:
            self._min.pop()
        self._min.append((seq, x))
        while self._max and self._max[-1][1] <= x:
            self._max.pop()
        self._max.append((seq, x))

        if len(self._x) > self.window:
            ot = self._t.popleft()
            ox = self._x.popleft()
            self._sx -= ox
            self._sxx -= ox * ox
            self._st -= ot
            self._stt -= ot * ot
            self._stx -= ot * ox
        oldest_seq = seq - len(self._x) + 1
        while self._min[0][0] < oldest_seq:
            self._min.popleft()
        while self._max[0][0] < oldest_seq:
            self._max.popleft()

        self._since_resync += 1
        if self._since_resync >= _RESYNC_EVERY:
            self._resync()

        if len(self._x) < self.window:
            return None
        return self._vector(x)

    def _vector(self, x: float) -> np.ndarray:
        n = len(self._x)
        mean = self._sx / n
        var = max(self._sxx / n - mean * mean, 0.0)
        rms = math.sqrt(max(self._sxx / n, 0.0))
        t_mean = self._st / n
        denom = self._stt - n * t_mean * t_mean
        slope = (self._stx - n * t_mean * mean) / denom if denom > 1e-12 else 0.0
        return np.array(
            [x, mean, math.sqrt(var), self._min[0][1], self._max[0][1], rms, slope],
            dtype=np.float64,
        )

    def _rebase(self, ts_us: int) -> None:
        shift = (ts_us - self._t0_us) / 1e6
        self._t = deque(t - shift for t in self._t)
        self._t0_us = ts_us
        self._resync()

    def _resync(self) -> None:
        self._since_resync = 0
        self._sx = sum(self._x)
        self._sxx = sum(v * v for v in self._x)
        self._st = sum(self._t)
        self._stt = sum(t * t for t in self._t)
        self._stx = sum(t * v for t, v in zip(self._t, self._x))


def features_batch(ts_us: np.ndarray, x: np.ndarray, window: int = 30) -> tuple[np.ndarray, np.ndarray]:
    """Offline helper: returns (feature_matrix, valid_mask) for every sample."""
    wf = WindowFeatures(window)
    out = np.full((len(x), N_FEATURES), np.nan)
    valid = np.zeros(len(x), dtype=bool)
    for i in range(len(x)):
        v = wf.update(int(ts_us[i]), float(x[i]))
        if v is not None:
            out[i] = v
            valid[i] = True
    return out, valid
