"""Composable aggregates on the hierarchical time grid (ФВ-2.3).

(min, max, first, last, sum, count) form a monoid with respect to merging buckets,
so aggregates can be built incrementally from a stream, merged across late data and
cached per tile — unlike LTTB results, which do not compose.

Times of the extrema (tmin/tmax) are kept as well: the MinMax preselection needs
real points (t, x) to feed into LTTB.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

AGG_DTYPE = np.dtype(
    [
        ("ts", "<i8"),  # bucket start, us
        ("vmin", "<f8"),
        ("tmin", "<i8"),
        ("vmax", "<f8"),
        ("tmax", "<i8"),
        ("vfirst", "<f8"),
        ("tfirst", "<i8"),
        ("vlast", "<f8"),
        ("tlast", "<i8"),
        ("vsum", "<f8"),
        ("cnt", "<i8"),
    ]
)


def level_deltas(delta0_us: int, base: int, n_levels: int) -> list[int]:
    """Bucket widths Delta_l = Delta_0 * b^l."""
    return [delta0_us * base**lvl for lvl in range(n_levels)]


def aggregate(ts: np.ndarray, x: np.ndarray, delta_us: int) -> np.ndarray:
    """Aggregate raw points into buckets of width delta (NaN values are ignored)."""
    ok = ~np.isnan(x)
    ts, x = ts[ok], x[ok]
    if len(ts) == 0:
        return np.empty(0, dtype=AGG_DTYPE)
    order = np.argsort(ts, kind="stable")
    ts, x = ts[order], x[order]
    b = (ts // delta_us) * delta_us
    starts = np.flatnonzero(np.r_[True, b[1:] != b[:-1]])
    ends = np.r_[starts[1:], len(ts)]
    out = np.empty(len(starts), dtype=AGG_DTYPE)
    out["ts"] = b[starts]
    out["vfirst"] = x[starts]
    out["tfirst"] = ts[starts]
    out["vlast"] = x[ends - 1]
    out["tlast"] = ts[ends - 1]
    out["vsum"] = np.add.reduceat(x, starts)
    out["cnt"] = ends - starts
    out["vmin"] = np.minimum.reduceat(x, starts)
    out["vmax"] = np.maximum.reduceat(x, starts)
    for i, (s, e) in enumerate(zip(starts, ends)):
        seg = x[s:e]
        out["tmin"][i] = ts[s + int(np.argmin(seg))]
        out["tmax"][i] = ts[s + int(np.argmax(seg))]
    return out


def merge_rows(a: np.void, b: np.void) -> np.ndarray:
    """Monoid operation on two aggregate rows of the same bucket."""
    r = np.empty(1, dtype=AGG_DTYPE)[0]
    r["ts"] = a["ts"]
    if b["vmin"] < a["vmin"]:
        r["vmin"], r["tmin"] = b["vmin"], b["tmin"]
    else:
        r["vmin"], r["tmin"] = a["vmin"], a["tmin"]
    if b["vmax"] > a["vmax"]:
        r["vmax"], r["tmax"] = b["vmax"], b["tmax"]
    else:
        r["vmax"], r["tmax"] = a["vmax"], a["tmax"]
    if b["tfirst"] < a["tfirst"]:
        r["vfirst"], r["tfirst"] = b["vfirst"], b["tfirst"]
    else:
        r["vfirst"], r["tfirst"] = a["vfirst"], a["tfirst"]
    if b["tlast"] >= a["tlast"]:
        r["vlast"], r["tlast"] = b["vlast"], b["tlast"]
    else:
        r["vlast"], r["tlast"] = a["vlast"], a["tlast"]
    r["vsum"] = a["vsum"] + b["vsum"]
    r["cnt"] = a["cnt"] + b["cnt"]
    return r


def rollup(rows: np.ndarray, delta_us: int) -> np.ndarray:
    """Merge aggregate rows of a finer level into buckets of width delta (rows sorted by ts)."""
    if len(rows) == 0:
        return rows
    b = (rows["ts"] // delta_us) * delta_us
    starts = np.flatnonzero(np.r_[True, b[1:] != b[:-1]])
    ends = np.r_[starts[1:], len(rows)]
    out = np.empty(len(starts), dtype=AGG_DTYPE)
    out["ts"] = b[starts]
    out["vsum"] = np.add.reduceat(rows["vsum"], starts)
    out["cnt"] = np.add.reduceat(rows["cnt"], starts)
    for i, (s, e) in enumerate(zip(starts, ends)):
        seg = rows[s:e]
        j = int(np.argmin(seg["vmin"]))
        out["vmin"][i], out["tmin"][i] = seg["vmin"][j], seg["tmin"][j]
        j = int(np.argmax(seg["vmax"]))
        out["vmax"][i], out["tmax"][i] = seg["vmax"][j], seg["tmax"][j]
        j = int(np.argmin(seg["tfirst"]))
        out["vfirst"][i], out["tfirst"][i] = seg["vfirst"][j], seg["tfirst"][j]
        j = int(np.argmax(seg["tlast"]))
        out["vlast"][i], out["tlast"][i] = seg["vlast"][j], seg["tlast"][j]
    return out


def minmax_points(rows: np.ndarray, t_from: int | None = None, t_to: int | None = None):
    """Expand aggregate rows into the (t, x) points of their extrema, time ordered.

    This is the MinMax preselection step served from cached aggregates.
    """
    if len(rows) == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    t = np.concatenate([rows["tmin"], rows["tmax"]])
    x = np.concatenate([rows["vmin"], rows["vmax"]])
    if t_from is not None:
        keep = (t >= t_from) & (t < t_to)
        t, x = t[keep], x[keep]
    order = np.lexsort((x, t))
    t, x = t[order], x[order]
    if len(t) > 1:  # min and max can be the same sample
        keep = np.r_[True, (t[1:] != t[:-1]) | (x[1:] != x[:-1])]
        t, x = t[keep], x[keep]
    return t, x


@dataclass
class _Open:
    row: np.void
    dirty: bool


class MultiLevelAggregator:
    """Streaming builder of aggregates for every level of the grid for one channel.

    Buckets stay in memory until they are older than the allowed lateness
    (`late_us`) relative to the watermark (max timestamp seen); late points for
    buckets that are still in memory are merged correctly. `pop_dirty()` returns
    changed buckets to be upserted (QuestDB DEDUP makes repeated writes idempotent).
    """

    def __init__(self, deltas_us: list[int], late_us: int):
        self.deltas = deltas_us
        self.late = late_us
        self.watermark = -(2**62)
        self._open: list[dict[int, _Open]] = [{} for _ in deltas_us]
        # buckets starting before this boundary were already evicted (per level)
        self._evicted_before: list[int] = [-(2**62)] * len(deltas_us)
        self.dropped_late = 0

    def add(self, ts: np.ndarray, x: np.ndarray) -> None:
        if len(ts) == 0:
            return
        self.watermark = max(self.watermark, int(ts.max()))
        for lvl, delta in enumerate(self.deltas):
            open_ = self._open[lvl]
            evicted_before = self._evicted_before[lvl]
            for row in aggregate(ts, x, delta):
                key = int(row["ts"])
                cur = open_.get(key)
                if cur is None:
                    if key < evicted_before:
                        self.dropped_late += int(row["cnt"])
                        continue
                    open_[key] = _Open(row.copy(), True)
                else:
                    cur.row = merge_rows(cur.row, row)
                    cur.dirty = True

    def seed(self, lvl: int, rows: np.ndarray) -> None:
        """Load already stored buckets (e.g. after a restart) so new points are merged into
        them instead of overwriting them with partial aggregates."""
        open_ = self._open[lvl]
        for row in rows:
            key = int(row["ts"])
            if key not in open_ and key >= self._evicted_before[lvl]:
                open_[key] = _Open(row.copy(), False)

    def pop_dirty(self) -> list[np.ndarray]:
        """Changed buckets per level (each an AGG_DTYPE array); evicts closed buckets."""
        result = []
        for lvl, delta in enumerate(self.deltas):
            open_ = self._open[lvl]
            rows = [o.row for o in open_.values() if o.dirty]
            for o in open_.values():
                o.dirty = False
            horizon = self.watermark - self.late - delta
            for key in [k for k in open_ if k < horizon]:
                del open_[key]
            self._evicted_before[lvl] = max(self._evicted_before[lvl], horizon)
            arr = np.array(rows, dtype=AGG_DTYPE) if rows else np.empty(0, dtype=AGG_DTYPE)
            result.append(arr)
        return result

    def flush_all(self) -> list[np.ndarray]:
        result = []
        for open_ in self._open:
            rows = [o.row for o in open_.values() if o.dirty]
            result.append(np.array(rows, dtype=AGG_DTYPE) if rows else np.empty(0, dtype=AGG_DTYPE))
            open_.clear()
        return result
