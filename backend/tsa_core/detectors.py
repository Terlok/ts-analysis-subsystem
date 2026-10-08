"""Combined anomaly diagnostics (formula (3) of the article):

    r_t = |x_t - f(X_{t-1}; theta)|,    a_t = 1{p_t > gamma  or  r_t > eps_i},  eps_i = k * sigma_r,i

f is a forecasting model of the normal regime (residual criterion, needs no labels),
g is a classifier of known abnormal states (needs labels, optional).
Models are plain objects with `predict` / `predict_proba` (scikit-learn compatible),
so this module does not depend on any ML library.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .features import FEATURE_NAMES


class Forecaster(Protocol):
    version: str
    sigma_r: float | None  # residual std on validation data of the normal regime

    def predict(self, X: np.ndarray) -> np.ndarray:
        """X: (n, n_features) feature rows X_t -> forecasts of x_{t+1}."""
        ...


class Classifier(Protocol):
    version: str
    gamma: float

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """X: (n, n_features) -> probability of the abnormal state, shape (n,)."""
        ...


class NaiveForecaster:
    """Baseline: x_{t+1} = x_t. Used when no trained model exists for a channel."""

    version = "naive"
    sigma_r = None

    def predict(self, X: np.ndarray) -> np.ndarray:
        return X[:, 0].copy()


@dataclass
class TreeForecaster:
    """Tree ensemble predicting the *increment* x_{t+1} - x_t from standardized features.

    Trees cannot extrapolate beyond the range of training targets, so predicting the
    level directly fails after a level shift; predicting the increment does not.
    """

    model: Any
    feature_mu: np.ndarray
    feature_sd: np.ndarray
    target_scale: float
    version: str
    sigma_r: float | None = None
    meta: dict = field(default_factory=dict)

    def standardize(self, X: np.ndarray) -> np.ndarray:
        return (X - self.feature_mu) / self.feature_sd

    def predict(self, X: np.ndarray) -> np.ndarray:
        if len(X) == 0:
            return np.empty(0)
        return X[:, 0] + self.model.predict(self.standardize(X)) * self.target_scale


@dataclass
class TreeClassifier:
    model: Any
    feature_mu: np.ndarray
    feature_sd: np.ndarray
    version: str
    gamma: float = 0.5

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if len(X) == 0:
            return np.empty(0)
        return self.model.predict_proba((X - self.feature_mu) / self.feature_sd)[:, 1]


def save_model(obj: TreeForecaster | TreeClassifier, path: str | Path) -> None:
    import joblib

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"kind": type(obj).__name__, "features": FEATURE_NAMES, "obj": obj}, path)


def load_model(path: str | Path) -> TreeForecaster | TreeClassifier:
    import joblib

    payload = joblib.load(path)
    if tuple(payload.get("features", ())) != FEATURE_NAMES:
        raise ValueError(f"{path}: feature set mismatch {payload.get('features')} != {FEATURE_NAMES}")
    return payload["obj"]


class ResidualDetector:
    """Threshold eps = k * sigma_r on the absolute forecast residual.

    sigma_r comes from validation data of the trained model. Without it, sigma_r is
    estimated online and robustly: an exponential average of |r| over non-anomalous
    points, scaled by sqrt(pi/2) (mean absolute deviation -> std for a normal law).
    """

    def __init__(self, k: float = 3.0, sigma_r: float | None = None, warmup: int = 100, alpha: float = 0.01):
        self.k = k
        self.fixed_sigma = sigma_r
        self.warmup = warmup
        self.alpha = alpha
        self._ema_abs = 0.0
        self._n = 0

    @property
    def sigma(self) -> float | None:
        if self.fixed_sigma is not None:
            return self.fixed_sigma
        if self._n < self.warmup:
            return None
        return math.sqrt(math.pi / 2) * self._ema_abs

    def update(self, r: float) -> tuple[float, bool]:
        """Returns (threshold, is_anomaly) for an absolute residual r."""
        sigma = self.sigma
        thr = self.k * sigma if sigma is not None else math.inf
        flag = sigma is not None and sigma > 0 and r > thr
        if self.fixed_sigma is None and not flag and r == r:
            self._n += 1
            if self._n == 1:
                self._ema_abs = r
            else:
                a = max(self.alpha, 1.0 / self._n)  # plain mean during warm-up
                self._ema_abs += a * (r - self._ema_abs)
        return thr, flag


@dataclass
class Episode:
    """Consecutive flagged points of one channel merged into one event."""

    kind: str  # "anomaly" | "outlier"
    ts_start: int
    ts_end: int
    n_points: int
    peak_ts: int
    peak_value: float
    peak_score: float
    details: dict = field(default_factory=dict)


class EpisodeTracker:
    """Merges flagged points separated by less than `max_gap_us` into episodes."""

    def __init__(self, kind: str, max_gap_us: int):
        self.kind = kind
        self.max_gap = max_gap_us
        self.current: Episode | None = None

    def add(self, ts: int, value: float, score: float) -> Episode | None:
        """Register a flagged point; returns a previous episode if it got closed."""
        cur = self.current
        if cur is not None and ts - cur.ts_end <= self.max_gap:
            cur.ts_end = ts
            cur.n_points += 1
            if score > cur.peak_score:
                cur.peak_ts, cur.peak_value, cur.peak_score = ts, value, score
            return None
        self.current = Episode(self.kind, ts, ts, 1, ts, value, score)
        return cur

    def tick(self, now_ts: int) -> Episode | None:
        """Close the open episode if no flagged point arrived for longer than the gap."""
        cur = self.current
        if cur is not None and now_ts - cur.ts_end > self.max_gap:
            self.current = None
            return cur
        return None

    def flush(self) -> Episode | None:
        cur, self.current = self.current, None
        return cur
