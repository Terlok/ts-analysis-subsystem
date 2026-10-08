"""Per-channel streaming processing: Hampel -> window features -> combined diagnostics.

The same class is used by the online analytics worker and by offline re-analysis
of historical data (ФВ-3.5), so both produce identical results.

Inference is batched: features of a whole packet are computed first, then the model
is called once for the packet (per-point calls cost ~3.2 ms and give rho_load >> 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .detectors import (
    Classifier,
    Episode,
    EpisodeTracker,
    Forecaster,
    NaiveForecaster,
    ResidualDetector,
)
from .features import N_FEATURES, WindowFeatures
from .hampel import StreamingHampel


@dataclass
class ChannelParams:
    hampel_window: int = 15
    hampel_kappa: float = 3.0
    hampel_min_sigma: float = 0.0
    feature_window: int = 30
    residual_k: float = 3.0
    episode_gap_us: int = 5_000_000


@dataclass
class BatchResult:
    ts: np.ndarray
    raw: np.ndarray
    filtered: np.ndarray
    substituted: np.ndarray  # bool
    median: np.ndarray
    pred: np.ndarray  # forecast of x_t made at t-1 (NaN if none)
    resid: np.ndarray
    threshold: np.ndarray
    prob: np.ndarray  # classifier probability (NaN if no classifier)
    anomaly: np.ndarray  # bool, combined criterion
    episodes: list[Episode] = field(default_factory=list)

    @property
    def flagged(self) -> np.ndarray:
        return self.substituted | self.anomaly


class ChannelProcessor:
    def __init__(
        self,
        channel: str,
        params: ChannelParams | None = None,
        forecaster: Forecaster | None = None,
        classifier: Classifier | None = None,
    ):
        self.channel = channel
        self.params = params or ChannelParams()
        p = self.params
        self.hampel = StreamingHampel(p.hampel_window, p.hampel_kappa, p.hampel_min_sigma)
        self.features = WindowFeatures(p.feature_window)
        self.forecaster: Forecaster = forecaster or NaiveForecaster()
        self.classifier = classifier
        self.residual = ResidualDetector(p.residual_k, sigma_r=self.forecaster.sigma_r)
        self._next_pred = np.nan  # forecast for the next sample, made from the last X
        self.anomalies = EpisodeTracker("anomaly", p.episode_gap_us)
        self.outliers = EpisodeTracker("outlier", p.episode_gap_us)
        self.last_ts: int | None = None

    def set_models(self, forecaster: Forecaster | None, classifier: Classifier | None) -> None:
        self.forecaster = forecaster or NaiveForecaster()
        self.classifier = classifier
        self.residual = ResidualDetector(self.params.residual_k, sigma_r=self.forecaster.sigma_r)
        self._next_pred = np.nan

    def process(self, ts: np.ndarray, x: np.ndarray, valid: np.ndarray | None = None) -> BatchResult:
        """Process a time-ordered batch. `valid=False` points (bad quality) are passed through."""
        n = len(ts)
        if valid is None:
            valid = ~np.isnan(x)
        filtered = x.astype(np.float64).copy()
        substituted = np.zeros(n, dtype=bool)
        median = np.full(n, np.nan)
        X = np.empty((n, N_FEATURES))
        has_x = np.zeros(n, dtype=bool)

        for i in range(n):
            if not valid[i]:
                continue
            h = self.hampel.update(float(x[i]))
            filtered[i] = h.value
            substituted[i] = h.substituted
            median[i] = h.median
            fv = self.features.update(int(ts[i]), h.value)
            if fv is not None:
                X[i] = fv
                has_x[i] = True

        idx = np.flatnonzero(has_x)
        preds_next = self.forecaster.predict(X[idx]) if len(idx) else np.empty(0)
        prob = np.full(n, np.nan)
        if self.classifier is not None and len(idx):
            prob[idx] = self.classifier.predict_proba(X[idx])
        gamma = self.classifier.gamma if self.classifier is not None else np.inf

        pred = np.full(n, np.nan)
        resid = np.full(n, np.nan)
        thr = np.full(n, np.nan)
        anomaly = np.zeros(n, dtype=bool)
        episodes: list[Episode] = []
        j = 0  # position in preds_next
        for i in range(n):
            if not valid[i]:
                continue
            ti = int(ts[i])
            if self._next_pred == self._next_pred:  # forecast made at the previous step
                pred[i] = self._next_pred
                r = abs(float(x[i]) - self._next_pred)
                resid[i] = r
                thr[i], flag_r = self.residual.update(r)
            else:
                flag_r = False
            flag_p = prob[i] > gamma if prob[i] == prob[i] else False
            anomaly[i] = flag_r or flag_p
            if has_x[i]:
                self._next_pred = float(preds_next[j])
                j += 1
            if anomaly[i]:
                score = resid[i] / thr[i] if thr[i] and thr[i] == thr[i] and np.isfinite(thr[i]) else 1.0
                if flag_p:
                    score = max(score, float(prob[i]) / gamma if gamma > 0 else 1.0)
                closed = self.anomalies.add(ti, float(x[i]), float(score))
                if closed:
                    episodes.append(closed)
            if substituted[i]:
                closed = self.outliers.add(ti, float(x[i]), abs(float(x[i]) - median[i]))
                if closed:
                    episodes.append(closed)
            self.last_ts = ti

        if self.last_ts is not None:
            for tracker in (self.anomalies, self.outliers):
                closed = tracker.tick(self.last_ts)
                if closed:
                    episodes.append(closed)

        for ep in episodes:
            ep.details.setdefault("model", self.forecaster.version)
        return BatchResult(ts, x, filtered, substituted, median, pred, resid, thr, prob, anomaly, episodes)

    def flush(self) -> list[Episode]:
        return [e for e in (self.anomalies.flush(), self.outliers.flush()) if e is not None]
