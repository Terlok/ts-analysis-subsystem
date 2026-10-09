"""Training of the normal-regime forecasting model (residual criterion of the diagnostics).

Procedure (article, section "Results"): causal Hampel filter -> window features X_t ->
tree ensemble predicting the next value; chronological 60/20/20 split with gaps of
max(w) samples between the parts (no leakage through overlapping windows); MAE on the
test part compared with the naive forecast x_{t+1} = x_t; sigma_r for eps = k*sigma_r
from the validation part.

The model predicts the increment x_{t+1} - x_t (trees cannot extrapolate levels).
sigma_r is estimated robustly (1.4826 * MAD of residuals against raw values), because
the data may contain impulses.

`run_training_job` is executed in a separate process by the API (see ModelTrainer).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path

import numpy as np

from tsa_core.detectors import TreeForecaster, save_model
from tsa_core.features import FEATURE_NAMES, features_batch
from tsa_core.hampel import hampel_batch

log = logging.getLogger(__name__)


class TrainingError(Exception):
    pass


def robust_sigma(r: np.ndarray) -> float:
    r = r[~np.isnan(r)]
    return float(1.4826 * np.median(np.abs(r - np.median(r)))) if len(r) else float("nan")


ALGORITHMS = {
    "hgb": "градієнтний бустинг (HistGradientBoosting)",
    "rf": "випадковий ліс (RandomForest)",
    "ridge": "лінійна регресія (Ridge)",
}


def make_regressor(algorithm: str, trees: int, depth: int, n_rows: int):
    """Tree ensembles of the thesis (random forest [9], gradient boosting [10]) and a linear
    baseline. All predict the standardized increment from the window features."""
    if algorithm == "hgb":
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(max_iter=trees, max_depth=depth, learning_rate=0.1, random_state=0)
    if algorithm == "rf":
        from sklearn.ensemble import RandomForestRegressor

        # bootstrap subsamples keep fitting time bounded on millions of rows
        return RandomForestRegressor(
            n_estimators=trees, max_depth=depth, max_samples=min(1.0, 200_000 / max(n_rows, 1)),
            min_samples_leaf=20, n_jobs=-1, random_state=0,
        )
    if algorithm == "ridge":
        from sklearn.linear_model import Ridge

        return Ridge(alpha=1.0)
    raise TrainingError(f"unknown algorithm {algorithm!r}, expected one of {', '.join(ALGORITHMS)}")


def train_forecaster(
    ts: np.ndarray,
    x: np.ndarray,
    hampel_window: int,
    hampel_kappa: float,
    feature_window: int,
    trees: int = 100,
    depth: int = 6,
    algorithm: str = "hgb",
) -> tuple[TreeForecaster, dict, dict]:
    """Returns (model, metrics, params). ts in us, x without NaN."""

    if len(ts) < 10 * feature_window:
        raise TrainingError(f"too few points: {len(ts)} (need at least {10 * feature_window})")
    xf, substituted = hampel_batch(x, hampel_window, hampel_kappa)
    X, has = features_batch(ts, xf, feature_window)

    i = np.flatnonzero(has[:-1])  # rows with features and a next sample
    y = xf[i + 1] - xf[i]
    n = len(i)
    gap = max(hampel_window, feature_window)
    n_tr, n_va = int(0.6 * n), int(0.2 * n)
    tr = np.arange(0, n_tr)
    va = np.arange(n_tr + gap, n_tr + n_va)
    te = np.arange(n_tr + n_va + gap, n)
    if min(len(tr), len(va), len(te)) < 10:
        raise TrainingError("not enough data for a 60/20/20 split")

    mu = X[i[tr]].mean(axis=0)
    sd = X[i[tr]].std(axis=0)
    sd[sd == 0] = 1.0
    y_scale = float(y[tr].std()) or 1.0
    model = make_regressor(algorithm, trees, depth, len(tr))
    t0 = time.perf_counter()
    model.fit((X[i[tr]] - mu) / sd, y[tr] / y_scale)
    fit_s = time.perf_counter() - t0

    version = f"{algorithm}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"
    f = TreeForecaster(model, mu, sd, y_scale, version=version)

    def evaluate(part):
        pred = f.predict(X[i[part]])
        target = xf[i[part] + 1]
        return {
            "mae": float(np.mean(np.abs(target - pred))),
            "mae_naive": float(np.mean(np.abs(target - xf[i[part]]))),
            "resid_raw": x[i[part] + 1] - pred,
        }

    ev_va, ev_te = evaluate(va), evaluate(te)
    f.sigma_r = robust_sigma(ev_va["resid_raw"])
    t0 = time.perf_counter()
    f.predict(X[i[te]])
    infer_us = (time.perf_counter() - t0) / len(te) * 1e6

    metrics = {
        "n_points": int(len(ts)),
        "t_from": int(ts[0]),
        "t_to": int(ts[-1]),
        "substituted_share": round(float(substituted.mean()), 5),
        "mae_test": ev_te["mae"],
        "mae_test_naive": ev_te["mae_naive"],
        "mae_gain": (1 - ev_te["mae"] / ev_te["mae_naive"]) if ev_te["mae_naive"] else None,
        "sigma_r": f.sigma_r,
        "fit_s": round(fit_s, 3),
        "batch_inference_us_per_point": round(infer_us, 3),
        "split": {"train": int(len(tr)), "val": int(len(va)), "test": int(len(te)), "gap": int(gap)},
    }
    params = {
        "hampel_window": hampel_window,
        "hampel_kappa": hampel_kappa,
        "feature_window": feature_window,
        "algorithm": algorithm,
        "trees": trees if algorithm != "ridge" else None,
        "depth": depth if algorithm != "ridge" else None,
        "features": list(FEATURE_NAMES),
        "target": "increment",
    }
    f.meta = {"params": params, "metrics": metrics}
    return f, metrics, params


def should_activate(mode: str, metrics: dict, active_gain: float | None = None) -> bool:
    """'auto': the model must beat the naive forecast on its test part and, if the channel
    already has an active model, show a larger gain than that one (gains are relative to the
    naive forecast, so models trained on different intervals stay comparable)."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    gain = metrics.get("mae_gain")
    if gain is None or gain <= 0:
        return False
    return active_gain is None or gain > active_gain


def save_and_register(channel: str, model: TreeForecaster, metrics: dict, params: dict, activate: bool | str = True) -> dict:
    """Save the model file and register it in PostgreSQL; `activate` is a bool or an activation
    mode ("auto" / "always" / "never", see should_activate). Notifies workers."""
    import redis
    from sqlalchemy import select, update

    from app.config import get_settings
    from app.db.models import ModelVersion
    from app.db.postgres import make_sync_engine, make_sync_sessionmaker
    from app.db.redis_store import MODELS_VERSION_KEY

    s = get_settings()
    model.meta["channel"] = channel
    rel = Path(channel) / f"forecaster-{model.version}.joblib"
    save_model(model, Path(s.models_dir) / rel)
    clean_metrics = json.loads(json.dumps(metrics, default=float))
    sm = make_sync_sessionmaker(make_sync_engine(s))
    with sm() as sess:
        if isinstance(activate, str):
            current = sess.execute(
                select(ModelVersion).where(ModelVersion.channel_id == channel, ModelVersion.active.is_(True))
            ).scalars().first()
            active_gain = (current.metrics or {}).get("mae_gain") if current is not None else None
            activate = should_activate(activate, metrics, active_gain)
        if activate:
            sess.execute(
                update(ModelVersion)
                .where(ModelVersion.channel_id == channel, ModelVersion.kind == "forecaster")
                .values(active=False)
            )
        mv = ModelVersion(
            channel_id=channel, kind="forecaster", version=model.version, path=str(rel),
            params=params, metrics=clean_metrics, active=activate,
        )
        sess.add(mv)
        sess.commit()
        model_id = mv.id
    if activate:
        redis.Redis.from_url(s.redis_url).incr(MODELS_VERSION_KEY)  # analytics workers reload models
    return {"model_id": model_id, "version": model.version, "path": str(rel), "metrics": clean_metrics, "active": activate}


def load_archive(channel: str, t_from: int | None, t_to: int | None) -> tuple[np.ndarray, np.ndarray]:
    from app.config import get_settings
    from app.db.questdb import QuestDBSyncReader

    reader = QuestDBSyncReader(get_settings())
    bounds = reader.time_bounds(channel)
    if bounds is None:
        raise TrainingError(f"no data for {channel} in the archive")
    a = t_from if t_from is not None else bounds[0]
    b = t_to if t_to is not None else bounds[1] + 1
    parts = list(reader.iter_raw(channel, a, b, 24 * 3600 * 1_000_000))
    if not parts:
        raise TrainingError("no data in the requested interval")
    ts = np.concatenate([p[0] for p in parts])
    x = np.concatenate([p[1] for p in parts])
    ok = ~np.isnan(x)
    return ts[ok], x[ok]


def run_training_job(
    channel: str, t_from: int | None, t_to: int | None, trees: int, depth: int, activate: str, algorithm: str = "hgb"
) -> dict:
    """Entry point executed in a worker process: archive -> model -> registry."""
    from app.config import get_settings

    s = get_settings()
    ts, x = load_archive(channel, t_from, t_to)
    model, metrics, params = train_forecaster(ts, x, s.hampel_window, s.hampel_kappa, s.feature_window, trees, depth, algorithm)
    return save_and_register(channel, model, metrics, params, activate)


def delete_model_file(models_dir: str, rel_path: str) -> bool:
    path = Path(rel_path)
    if not path.is_absolute():
        path = Path(models_dir) / path
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


@dataclass
class TrainingJob:
    id: str
    channel: str
    t_from: int | None
    t_to: int | None
    algorithm: str = "hgb"
    status: str = "queued"  # queued | running | done | error
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None


class ModelTrainer:
    """Runs training jobs in a separate process (CPU-bound work must not block the API)."""

    def __init__(self, max_workers: int = 1):
        self.max_workers = max_workers
        self._pool: ProcessPoolExecutor | None = None
        self.jobs: dict[str, TrainingJob] = {}

    def _executor(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self.max_workers, mp_context=get_context("spawn"))
        return self._pool

    def submit(
        self, channel: str, t_from: int | None, t_to: int | None, trees: int = 100, depth: int = 6,
        activate: str = "auto", algorithm: str = "hgb",
    ) -> TrainingJob:
        if algorithm not in ALGORITHMS:
            raise TrainingError(f"unknown algorithm {algorithm!r}")
        job = TrainingJob(uuid.uuid4().hex[:12], channel, t_from, t_to, algorithm)
        self.jobs[job.id] = job
        fut = self._executor().submit(run_training_job, channel, t_from, t_to, trees, depth, activate, algorithm)
        job.status = "running"

        def done(f: Future) -> None:
            job.finished_at = time.time()
            try:
                job.result = f.result()
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                job.status = "error"
                job.error = str(e) or type(e).__name__
                log.warning("training of %s failed: %s", channel, job.error)

        fut.add_done_callback(done)
        return job

    def list(self) -> list[TrainingJob]:
        return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
