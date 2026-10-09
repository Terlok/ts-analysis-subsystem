"""Train the forecasting model of the normal regime (see app/services/training.py).

    python -m scripts.train_forecaster --file data/analog.csv --channel A001
    python -m scripts.train_forecaster --questdb --channel A001 --from 2025-05-01 --to 2025-05-08 --register
    python -m scripts.train_forecaster --questdb --all --to 2026-10-09T12:00 --register   # every archived channel
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.services.training import ALGORITHMS, TrainingError, load_archive, save_and_register, train_forecaster
from scripts.replay import parse_time
from tsa_core.detectors import save_model


def load_file(path: str, channel: str, t_from: int | None, t_to: int | None) -> tuple[np.ndarray, np.ndarray]:
    from scripts.telemetry_file import read_channel

    ts, val, valid = read_channel(path, channel)
    keep = valid.copy()
    if t_from is not None:
        keep &= ts >= t_from
    if t_to is not None:
        keep &= ts < t_to
    return ts[keep], val[keep]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="CSV/Parquet exported from the analog table")
    src.add_argument("--questdb", action="store_true", help="read from the telemetry archive")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--channel")
    which.add_argument("--all", action="store_true", help="every channel in the archive (with --questdb)")
    p.add_argument("--from", dest="t_from")
    p.add_argument("--to", dest="t_to")
    p.add_argument("--hampel-window", type=int)
    p.add_argument("--hampel-kappa", type=float)
    p.add_argument("--feature-window", type=int)
    p.add_argument("--algorithm", choices=list(ALGORITHMS), default="hgb")
    p.add_argument("--trees", type=int, default=100)
    p.add_argument("--depth", type=int, default=6)
    p.add_argument("--register", action="store_true", help="register in PostgreSQL")
    p.add_argument("--activate", choices=["auto", "always", "never"], default="auto",
                   help="auto: activate only if the model beats the naive forecast")
    a = p.parse_args()

    s = get_settings()
    hw = a.hampel_window or s.hampel_window
    hk = a.hampel_kappa or s.hampel_kappa
    fw = a.feature_window or s.feature_window
    t_from, t_to = parse_time(a.t_from), parse_time(a.t_to)

    if a.all:
        if not a.questdb:
            p.error("--all requires --questdb")
        from app.db.questdb import QuestDBSyncReader

        channels = sorted(QuestDBSyncReader(s).channels())
    else:
        channels = [a.channel]

    rc = 0
    for ch in channels:
        try:
            ts, x = load_archive(ch, t_from, t_to) if a.questdb else load_file(a.file, ch, t_from, t_to)
            model, metrics, params = train_forecaster(ts, x, hw, hk, fw, a.trees, a.depth, a.algorithm)
        except TrainingError as e:
            print(f"{ch}: {e}", file=sys.stderr)
            rc = 1
            continue
        if a.register:
            info = save_and_register(ch, model, metrics, params, a.activate)
        else:
            path = Path(s.models_dir) / ch / f"forecaster-{model.version}.joblib"
            save_model(model, path)
            info = {"path": str(path), "version": model.version, "metrics": metrics}
        print(json.dumps({"channel": ch, **info}, indent=2, ensure_ascii=False, default=float))
    return rc


if __name__ == "__main__":
    sys.exit(main())
