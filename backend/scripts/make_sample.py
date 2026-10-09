"""Synthetic telemetry in the format of the `analog` table (id, ts, val, nd, otkl) with
injected anomalies of known types — impulses, level shifts and slow drifts — and a
label file, for testing the pipeline and for evaluating diagnostics (criterion К5).

    python -m scripts.make_sample data/sample.csv --channels 8 --hours 2 --hz 1
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


def generate(n_channels: int, hours: float, hz: float, seed: int, start: datetime):
    rng = np.random.default_rng(seed)
    n = int(hours * 3600 * hz)
    t0 = int(start.timestamp() * 1e6)
    base_t = t0 + np.round(np.arange(n) / hz * 1e6).astype(np.int64)
    frames, labels = [], []
    for c in range(n_channels):
        cid = f"A{c + 1:03d}"
        t = base_t.copy()
        # irregular sampling: ~3% of intervals are doubled (missing samples)
        keep = np.ones(n, dtype=bool)
        keep[rng.random(n) < 0.03] = False
        keep[0] = True
        level = rng.uniform(10, 100)
        amp = rng.uniform(0.5, 5)
        period = rng.uniform(300, 1800) * hz
        x = level + amp * np.sin(2 * np.pi * np.arange(n) / period) + rng.normal(0, amp * 0.03, n)
        # impulses
        margin = min(100, n // 10)
        for i in rng.choice(np.arange(margin, n - margin), size=max(1, n // 2000), replace=False):
            x[i] += rng.choice([-1, 1]) * amp * rng.uniform(1.5, 3)
            labels.append((cid, int(t[i]), int(t[i]), "impulse"))
        # level shift
        i = int(rng.integers(n // 4, 3 * n // 4))
        L = max(1, min(int(rng.integers(30, 300) * hz), n // 10))
        x[i : i + L] += amp * rng.uniform(1, 2)
        labels.append((cid, int(t[i]), int(t[min(i + L, n - 1)]), "level_shift"))
        # slow drift in the last part
        i = int(0.8 * n)
        x[i:] += np.linspace(0, amp * 2, n - i)
        labels.append((cid, int(t[i]), int(t[-1]), "drift"))
        nd = rng.random(n) < 0.002
        otkl = np.zeros(n, dtype=np.int16)
        frames.append(pd.DataFrame({"id": cid, "ts": t, "val": np.round(x, 4), "nd": nd, "otkl": otkl})[keep])
    df = pd.concat(frames).sort_values(["ts", "id"], kind="stable")
    df["ts"] = pd.to_datetime(df["ts"], unit="us", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    df["nd"] = np.where(df["nd"], "true", "false")
    return df, labels


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("out", help="output CSV path")
    p.add_argument("--channels", type=int, default=8)
    p.add_argument("--hours", type=float, default=1.0)
    p.add_argument("--hz", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--start", default=None, help="ISO start time (default: now - hours)")
    a = p.parse_args()
    start = (
        datetime.fromisoformat(a.start.replace("Z", "+00:00"))
        if a.start
        else datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - a.hours * 3600, timezone.utc)
    )
    df, labels = generate(a.channels, a.hours, a.hz, a.seed, start)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    with open(out.with_suffix(".labels.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "ts_start", "ts_end", "type"])
        w.writerows(labels)
    print(f"{len(df)} rows, {a.channels} channels -> {out}; {len(labels)} labels -> {out.with_suffix('.labels.csv')}")


if __name__ == "__main__":
    main()
