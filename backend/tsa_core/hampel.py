"""Causal Hampel filter (formula (1) of the article).

    m_t = med(W_t),  sigma_t = 1.4826 * med(|W_t - m_t|)
    |x_t - m_t| > kappa * sigma_t  ->  x_t is replaced by m_t

W_t = {x_{t-w+1}, ..., x_t} contains the last w raw observations (including x_t),
so the decision for x_t never depends on future samples.
"""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass

import numpy as np

MAD_SCALE = 1.4826


@dataclass(slots=True)
class HampelResult:
    value: float  # filtered value (median if substituted)
    substituted: bool
    median: float
    sigma: float


class StreamingHampel:
    """Per-channel causal Hampel filter with O(w) update (w is small, typically 7..31).

    sigma = 0 (constant window, quantized signal) would make any deviation an outlier,
    so the scale is clamped from below by `min_sigma`; with `min_sigma == 0` no
    substitution is made while the window scale is zero.
    """

    def __init__(self, window: int = 15, kappa: float = 3.0, min_sigma: float = 0.0):
        if window < 3:
            raise ValueError("Hampel window must be >= 3")
        self.window = window
        self.kappa = kappa
        self.min_sigma = min_sigma
        self._fifo: deque[float] = deque()
        self._sorted: list[float] = []

    def reset(self) -> None:
        self._fifo.clear()
        self._sorted.clear()

    @property
    def ready(self) -> bool:
        return len(self._fifo) >= self.window

    def update(self, x: float) -> HampelResult:
        if x != x:  # NaN: not part of the window, passed through
            return HampelResult(x, False, float("nan"), float("nan"))
        self._fifo.append(x)
        bisect.insort(self._sorted, x)
        if len(self._fifo) > self.window:
            old = self._fifo.popleft()
            del self._sorted[bisect.bisect_left(self._sorted, old)]

        n = len(self._sorted)
        med = _median_sorted(self._sorted)
        if n < self.window:
            return HampelResult(x, False, med, float("nan"))

        dev = sorted(abs(v - med) for v in self._sorted)
        sigma = MAD_SCALE * _median_sorted(dev)
        sigma_eff = max(sigma, self.min_sigma)
        if sigma_eff > 0 and abs(x - med) > self.kappa * sigma_eff:
            return HampelResult(med, True, med, sigma)
        return HampelResult(x, False, med, sigma)


def _median_sorted(values: list[float]) -> float:
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return 0.5 * (values[mid - 1] + values[mid])


def hampel_batch(
    x: np.ndarray, window: int = 15, kappa: float = 3.0, min_sigma: float = 0.0
) -> tuple[np.ndarray, np.ndarray]:
    """Offline (but still causal) filter for a whole series: returns (filtered, substituted_mask)."""
    f = StreamingHampel(window, kappa, min_sigma)
    out = np.empty(len(x), dtype=np.float64)
    mask = np.zeros(len(x), dtype=bool)
    for i, v in enumerate(x):
        r = f.update(float(v))
        out[i] = r.value
        mask[i] = r.substituted
    return out, mask
