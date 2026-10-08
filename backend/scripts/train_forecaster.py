"""Train the forecasting model of the normal regime for one channel (residual criterion).

Procedure (as in the article): causal Hampel filter -> window features X_t ->
tree ensemble predicting the next value; chronological 60/20/20 split with gaps of
w samples between parts (no leakage through overlapping windows); MAE on the test
part compared with the naive forecast x_{t+1} = x_t; sigma_r for eps = k*sigma_r
estimated on the validation part.

The model predicts the increment x_{t+1} - x_t (trees cannot extrapolate levels).
sigma_r is estimated robustly (1.4826 * MAD of residuals against raw values), because
validation data may still contain impulses.

    python -m scripts.train_forecaster --file data/analog.csv --channel A001
    python -m scripts.train_forecaster --questdb --channel A001 --from 2025-05-01 --to 2025-05-08 --register
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.config import get_settings
from scripts.replay import parse_time
from tsa_core.detectors import TreeForecaster, save_model
from tsa_core.features import FEATURE_NAMES, features_batch
from tsa_core.hampel import hampel_batch


def load_series(a, s) -> tuple[np.ndarray, np.ndarray]:
    if a.file:
        from scripts.telemetry_file import read_channel

        ts, val, valid = read_channel(a.file, a.channel)
    else:
        from app.db.questdb import QuestDBSyncReader

        reader = QuestDBSyncReader(s)
        bounds = reader.time_bounds(a.channel)
        if bounds is None:
            raise SystemExit(f"no data for {a.channel} in QuestDB")
        t_from = parse_time(a.t_from) or bounds[0]
        t_to = parse_time(a.t_to) or bounds[1] + 1
        parts = list(reader.iter_raw(a.channel, t_from, t_to, 24 * 3600 * 1_000_000))
        if not parts:
            raise SystemExit("empty interval")
        ts = np.concatenate([p[0] for p in parts])
        val = np.concatenate([p[1] for p in parts])
        valid = ~np.isnan(val)
    if a.t_from and a.file:
        keep = ts >= parse_time(a.t_from)
        ts, val, valid = ts[keep], val[keep], valid[keep]
    if a.t_to and a.file:
        keep = ts < parse_time(a.t_to)
        ts, val, valid = ts[keep], val[keep], valid[keep]
    return ts[valid], val[valid]


def robust_sigma(r: np.ndarray) -> float:
    r = r[~np.isnan(r)]
    return float(1.4826 * np.median(np.abs(r - np.median(r)))) if len(r) else float("nan")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="CSV/Parquet exported from the analog table")
    src.add_argument("--questdb", action="store_true", help="read from the telemetry archive")
    p.add_argument("--channel", required=True)
    p.add_argument("--from", dest="t_from")
    p.add_argument("--to", dest="t_to")
    p.add_argument("--hampel-window", type=int)
    p.add_argument("--hampel-kappa", type=float)
    p.add_argument("--feature-window", type=int)
    p.add_argument("--trees", type=int, default=100)
    p.add_argument("--depth", type=int, default=6)
    p.add_argument("--register", action="store_true", help="register and activate in PostgreSQL")
    a = p.parse_args()

    from sklearn.ensemble import HistGradientBoostingRegressor

    s = get_settings()
    hw = a.hampel_window or s.hampel_window
    hk = a.hampel_kappa or s.hampel_kappa
    fw = a.feature_window or s.feature_window

    ts, x = load_series(a, s)
    if len(ts) < 10 * fw:
        print(f"too few points: {len(ts)}", file=sys.stderr)
        return 1
    xf, substituted = hampel_batch(x, hw, hk)
    X, has = features_batch(ts, xf, fw)

    i = np.flatnonzero(has[:-1])  # rows with features and a next sample
    y = xf[i + 1] - xf[i]
    n = len(i)
    gap = max(hw, fw)
    n_tr, n_va = int(0.6 * n), int(0.2 * n)
    tr = np.arange(0, n_tr)
    va = np.arange(n_tr + gap, n_tr + n_va)
    te = np.arange(n_tr + n_va + gap, n)
    if min(len(tr), len(va), len(te)) < 10:
        print("not enough data for a 60/20/20 split", file=sys.stderr)
        return 1

    mu = X[i[tr]].mean(axis=0)
    sd = X[i[tr]].std(axis=0)
    sd[sd == 0] = 1.0
    y_scale = float(y[tr].std()) or 1.0
    model = HistGradientBoostingRegressor(max_iter=a.trees, max_depth=a.depth, learning_rate=0.1, random_state=0)
    t0 = time.perf_counter()
    model.fit((X[i[tr]] - mu) / sd, y[tr] / y_scale)
    fit_s = time.perf_counter() - t0

    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    f = TreeForecaster(model, mu, sd, y_scale, version=f"hgb-{version}")

    def evaluate(part):
        pred = f.predict(X[i[part]])
        target = xf[i[part] + 1]
        raw_next = x[i[part] + 1]
        naive = xf[i[part]]
        return {
            "mae": float(np.mean(np.abs(target - pred))),
            "mae_naive": float(np.mean(np.abs(target - naive))),
            "resid_raw": raw_next - pred,
        }

    ev_va, ev_te = evaluate(va), evaluate(te)
    f.sigma_r = robust_sigma(ev_va["resid_raw"])
    t0 = time.perf_counter()
    f.predict(X[i[te]])
    infer_us = (time.perf_counter() - t0) / len(te) * 1e6

    metrics = {
        "n_points": int(len(ts)),
        "substituted_share": round(float(substituted.mean()), 5),
        "mae_test": ev_te["mae"],
        "mae_test_naive": ev_te["mae_naive"],
        "mae_gain": 1 - ev_te["mae"] / ev_te["mae_naive"] if ev_te["mae_naive"] else None,
        "sigma_r": f.sigma_r,
        "eps_k3": 3 * f.sigma_r,
        "fit_s": round(fit_s, 3),
        "batch_inference_us_per_point": round(infer_us, 3),
        "split": {"train": len(tr), "val": len(va), "test": len(te), "gap": gap},
    }
    params = {"hampel_window": hw, "hampel_kappa": hk, "feature_window": fw, "trees": a.trees, "depth": a.depth,
              "features": list(FEATURE_NAMES), "target": "increment"}
    f.meta = {"channel": a.channel, "params": params, "metrics": metrics}

    rel = Path(a.channel) / f"forecaster-{version}.joblib"
    path = Path(s.models_dir) / rel
    save_model(f, path)
    print(json.dumps({"model": str(path), "version": f.version, **metrics}, indent=2, default=float))

    if a.register:
        import redis
        from sqlalchemy import update

        from app.db.models import ModelVersion
        from app.db.postgres import make_sync_engine, make_sync_sessionmaker
        from app.db.redis_store import MODELS_VERSION_KEY

        sm = make_sync_sessionmaker(make_sync_engine(s))
        with sm() as sess:
            sess.execute(
                update(ModelVersion)
                .where(ModelVersion.channel_id == a.channel, ModelVersion.kind == "forecaster")
                .values(active=False)
            )
            mv = ModelVersion(channel_id=a.channel, kind="forecaster", version=f.version, path=str(rel),
                              params=params, metrics=json.loads(json.dumps(metrics, default=float)), active=True)
            sess.add(mv)
            sess.commit()
        redis.Redis.from_url(s.redis_url).incr(MODELS_VERSION_KEY)
        print(f"registered and activated model id={mv.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
