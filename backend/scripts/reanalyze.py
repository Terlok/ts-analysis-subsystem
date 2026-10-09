"""Offline re-analysis of historical data with given parameters (ФВ-3.5) and evaluation
of diagnostics against labels (criterion К5: precision, recall, F1, detection delay).

Uses exactly the same ChannelProcessor as the online analytics worker.

    python -m scripts.reanalyze --file data/sample.csv --labels data/sample.labels.csv
    python -m scripts.reanalyze --questdb --channels A001 --from 2025-05-01 --to 2025-05-02 \\
        --hampel-kappa 4 --run-id kappa4 --write
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict

import numpy as np

from app.config import get_settings
from scripts.replay import parse_time
from tsa_core.detectors import load_model
from tsa_core.pipeline import ChannelParams, ChannelProcessor


def load_labels(path: str) -> dict[str, list[tuple[int, int, str]]]:
    out: dict[str, list] = defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            out[r["id"]].append((int(r["ts_start"]), int(r["ts_end"]), r.get("type", "")))
    return out


def evaluate(episodes, labels: list[tuple[int, int, str]], tol_us: int) -> dict:
    """Event-level metrics: a label is detected if an anomaly episode overlaps
    [start - tol, end + tol]; an episode is a true positive if it overlaps any label."""
    eps = [(e.ts_start, e.ts_end) for e in episodes if e.kind == "anomaly"]
    detected, delays = 0, []
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s, e, typ in labels:
        hits = [a for a, b in eps if a <= e + tol_us and b >= s - tol_us]
        by_type[typ][1] += 1
        if hits:
            detected += 1
            by_type[typ][0] += 1
            delays.append(max(0, min(hits) - s) / 1e6)
    tp = sum(1 for a, b in eps if any(a <= e + tol_us and b >= s - tol_us for s, e, _ in labels))
    precision = tp / len(eps) if eps else None
    recall = detected / len(labels) if labels else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else 0.0
    return {
        "episodes": len(eps),
        "labels": len(labels),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mean_delay_s": float(np.mean(delays)) if delays else None,
        "recall_by_type": {t: f"{d}/{n}" for t, (d, n) in by_type.items()},
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--questdb", action="store_true")
    p.add_argument("--channels", help="comma separated (default: all in file / archive)")
    p.add_argument("--from", dest="t_from")
    p.add_argument("--to", dest="t_to")
    p.add_argument("--hampel-window", type=int)
    p.add_argument("--hampel-kappa", type=float)
    p.add_argument("--feature-window", type=int)
    p.add_argument("--residual-k", type=float)
    p.add_argument("--model", help="path of a forecaster .joblib (for a single channel)")
    p.add_argument("--labels", help="labels CSV (id, ts_start, ts_end, type) to compute P/R/F1")
    p.add_argument("--tolerance-s", type=float, default=5.0, help="matching tolerance for labels")
    p.add_argument("--run-id", default=None, help="name of the run when writing results")
    p.add_argument("--write", action="store_true", help="write flags to QuestDB and events to PostgreSQL")
    a = p.parse_args()

    s = get_settings()
    d = s.default_channel_params()
    params = ChannelParams(
        hampel_window=a.hampel_window or d.hampel_window,
        hampel_kappa=a.hampel_kappa or d.hampel_kappa,
        feature_window=a.feature_window or d.feature_window,
        residual_k=a.residual_k or d.residual_k,
        episode_gap_us=d.episode_gap_us,
    )
    t_from, t_to = parse_time(a.t_from), parse_time(a.t_to)

    # --- load data ----------------------------------------------------------------
    series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    if a.file:
        from scripts.telemetry_file import Chunk, filter_chunk, read_chunks

        wanted = set(a.channels.split(",")) if a.channels else None
        parts = [filter_chunk(c, wanted, t_from, t_to) for c in read_chunks(a.file)]
        c = Chunk.concat([x for x in parts if len(x)])
        for cid in np.unique(c.id):
            sub = c.take(c.id == cid)
            order = np.argsort(sub.ts, kind="stable")
            sub = sub.take(order)
            series[str(cid)] = (sub.ts, sub.val, ~sub.nd & ~np.isnan(sub.val))
    else:
        from app.db.questdb import QuestDBSyncReader

        reader = QuestDBSyncReader(s)
        for cid in a.channels.split(",") if a.channels else reader.channels():
            b = reader.time_bounds(cid)
            if b is None:
                continue
            chunks = list(reader.iter_raw(cid, t_from or b[0], t_to or b[1] + 1, 24 * 3600 * 1_000_000))
            if chunks:
                ts = np.concatenate([x[0] for x in chunks])
                val = np.concatenate([x[1] for x in chunks])
                series[cid] = (ts, val, ~np.isnan(val))

    labels = load_labels(a.labels) if a.labels else {}
    forecaster = load_model(a.model) if a.model else None
    writer = sm = None
    run = a.run_id or f"re-{time.strftime('%Y%m%dT%H%M%S')}"
    if a.write:
        from app.db.postgres import make_sync_engine, make_sync_sessionmaker
        from app.db.questdb import IlpWriter

        writer = IlpWriter(s)
        sm = make_sync_sessionmaker(make_sync_engine(s))

    report = {"run": run, "params": params.__dict__, "channels": {}}
    all_eps, all_labels = [], []
    for cid, (ts, val, valid) in sorted(series.items()):
        proc = ChannelProcessor(cid, params, forecaster)
        t0 = time.perf_counter()
        res = proc.process(ts, val, valid)
        eps = res.episodes + proc.flush()
        elapsed = time.perf_counter() - t0
        info = {
            "n": int(len(ts)),
            "substituted": int(res.substituted.sum()),
            "anomaly_points": int(res.anomaly.sum()),
            "episodes": {"anomaly": sum(e.kind == "anomaly" for e in eps), "outlier": sum(e.kind == "outlier" for e in eps)},
            "us_per_point": round(elapsed / max(len(ts), 1) * 1e6, 2),
            "model": proc.forecaster.version,
        }
        if cid in labels:
            info["eval"] = evaluate(eps, labels[cid], int(a.tolerance_s * 1e6))
            all_eps += eps
            all_labels += labels[cid]
        report["channels"][cid] = info

        if a.write:
            from sqlalchemy import insert

            from app.db.models import Event

            idx = np.flatnonzero(res.flagged)
            rows = [
                {"ts": int(res.ts[i]), "val": res.raw[i], "val_f": res.filtered[i], "median": res.median[i],
                 "pred": res.pred[i], "resid": res.resid[i], "thr": res.threshold[i], "prob": res.prob[i],
                 "substituted": bool(res.substituted[i]), "anomaly": bool(res.anomaly[i])}
                for i in idx
            ]
            writer.point_flags(cid, run, rows)
            writer.flush()
            if eps:
                with sm() as sess:
                    sess.execute(insert(Event).values([
                        {"channel_id": cid, "kind": e.kind, "ts_start": e.ts_start, "ts_end": e.ts_end,
                         "n_points": e.n_points, "peak_ts": e.peak_ts, "peak_value": float(e.peak_value),
                         "score": float(e.peak_score), "run": run, "model_version": e.details.get("model"),
                         "details": {**e.details, "params": params.__dict__}}
                        for e in eps
                    ]))
                    sess.commit()

    if all_labels:
        report["overall"] = evaluate(all_eps, all_labels, int(a.tolerance_s * 1e6))
    if writer:
        writer.close()
    print(json.dumps(report, indent=2, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
