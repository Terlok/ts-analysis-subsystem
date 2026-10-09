"""Reading telemetry files exported from the `analog` table.

Supported formats: CSV (optionally .gz) as produced by QuestDB `/exp` or
`scripts/export_questdb.py`, and Parquet. Columns (names configurable):

    id    SYMBOL     channel id
    ts    TIMESTAMP  ISO 8601 (e.g. 2025-05-01T10:00:00.123456Z) or a number
                     (s / ms / us / ns since epoch, detected by magnitude)
    val   FLOAT      value (empty / NaN allowed)
    nd    BOOLEAN    "invalid data" flag (optional)
    otkl  SHORT      deviation code (optional)

Files are read in chunks, so their size is not limited by memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd


@dataclass
class Columns:
    id: str = "id"
    ts: str = "ts"
    val: str = "val"
    nd: str = "nd"
    otkl: str = "otkl"


@dataclass
class Chunk:
    """Rows of a file chunk as numpy arrays, timestamps in us."""

    id: np.ndarray  # object (str)
    ts: np.ndarray  # int64
    val: np.ndarray  # float64 (NaN = missing)
    nd: np.ndarray  # bool
    otkl: np.ndarray  # int16

    def __len__(self) -> int:
        return len(self.ts)

    def take(self, idx) -> "Chunk":
        return Chunk(self.id[idx], self.ts[idx], self.val[idx], self.nd[idx], self.otkl[idx])

    @staticmethod
    def concat(chunks: list["Chunk"]) -> "Chunk":
        return Chunk(*(np.concatenate([getattr(c, f) for c in chunks]) for f in ("id", "ts", "val", "nd", "otkl")))


def to_micros(col: pd.Series) -> np.ndarray:
    if pd.api.types.is_numeric_dtype(col):
        v = col.to_numpy(dtype=np.float64)
        mag = np.nanmax(np.abs(v)) if len(v) else 0
        if mag < 1e11:  # seconds
            scale = 1e6
        elif mag < 1e14:  # milliseconds
            scale = 1e3
        elif mag < 1e17:  # microseconds
            scale = 1.0
        else:  # nanoseconds
            scale = 1e-3
        return np.round(v * scale).astype(np.int64)
    dt = pd.to_datetime(col, utc=True, format="ISO8601").dt.tz_localize(None)
    return dt.to_numpy(dtype="datetime64[us]").astype(np.int64)


def _bool(col: pd.Series | None, n: int) -> np.ndarray:
    if col is None:
        return np.zeros(n, dtype=bool)
    if col.dtype == bool:
        return col.to_numpy()
    s = col.astype(str).str.strip().str.lower()
    return s.isin(["true", "t", "1", "yes"]).to_numpy()


def frame_to_chunk(df: pd.DataFrame, cols: Columns) -> Chunk:
    n = len(df)
    val = pd.to_numeric(df[cols.val], errors="coerce").to_numpy(dtype=np.float64)
    otkl = (
        pd.to_numeric(df[cols.otkl], errors="coerce").fillna(0).to_numpy(dtype=np.int16)
        if cols.otkl in df
        else np.zeros(n, dtype=np.int16)
    )
    return Chunk(
        id=df[cols.id].astype(str).to_numpy(dtype=object),
        ts=to_micros(df[cols.ts]),
        val=val,
        nd=_bool(df[cols.nd] if cols.nd in df else None, n),
        otkl=otkl,
    )


def detect_format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".parquet", ".pq")):
        return "parquet"
    return "csv"


def read_chunks(path: str | Path, cols: Columns | None = None, chunk_rows: int = 200_000, fmt: str = "auto") -> Iterator[Chunk]:
    path = Path(path)
    cols = cols or Columns()
    fmt = detect_format(path) if fmt == "auto" else fmt
    if fmt == "parquet":
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=chunk_rows):
            yield frame_to_chunk(batch.to_pandas(), cols)
        return
    reader = pd.read_csv(
        path,
        chunksize=chunk_rows,
        dtype={cols.id: str},
        keep_default_na=True,
        compression="infer",
    )
    for df in reader:
        yield frame_to_chunk(df, cols)


def merged_chunks(
    paths: list[str | Path], cols: Columns | None = None, chunk_rows: int = 200_000, fmt: str = "auto"
) -> Iterator[Chunk]:
    """K-way merge of several time-ordered files into one time-ordered stream of chunks.

    Rows are released only up to the smallest "last timestamp" among the buffers of files
    that still have unread data, so the output is ordered across files without loading
    them into memory.
    """
    iters = [read_chunks(p, cols, chunk_rows, fmt) for p in paths]
    bufs: list[Chunk | None] = [None] * len(iters)
    done = [False] * len(iters)

    def refill(i: int) -> None:
        while not done[i] and (bufs[i] is None or len(bufs[i]) == 0):
            try:
                c = next(iters[i])
            except StopIteration:
                done[i] = True
                return
            if np.any(np.diff(c.ts) < 0):
                c = c.take(np.argsort(c.ts, kind="stable"))
            bufs[i] = c if bufs[i] is None or len(bufs[i]) == 0 else Chunk.concat([bufs[i], c])

    while True:
        for i in range(len(iters)):
            refill(i)
        live = [i for i in range(len(iters)) if bufs[i] is not None and len(bufs[i])]
        if not live:
            return
        open_files = [i for i in live if not done[i]]
        boundary = min(int(bufs[i].ts[-1]) for i in open_files) if open_files else None
        parts = []
        for i in live:
            b = bufs[i]
            k = len(b) if boundary is None else int(np.searchsorted(b.ts, boundary, side="right"))
            if k:
                parts.append(b.take(slice(0, k)))
                bufs[i] = b.take(slice(k, None))
        out = Chunk.concat(parts)
        if len(parts) > 1:
            out = out.take(np.argsort(out.ts, kind="stable"))
        yield out


def first_timestamp(path: str | Path, cols: Columns | None = None, fmt: str = "auto") -> int | None:
    """Timestamp (us) of the first row of a file, without reading the whole file."""
    for c in read_chunks(path, cols, chunk_rows=1000, fmt=fmt):
        if len(c):
            return int(c.ts.min())
    return None


def filter_chunk(c: Chunk, channels: set[str] | None, t_from: int | None, t_to: int | None) -> Chunk:
    mask = np.ones(len(c), dtype=bool)
    if channels:
        mask &= np.isin(c.id, list(channels))
    if t_from is not None:
        mask &= c.ts >= t_from
    if t_to is not None:
        mask &= c.ts < t_to
    return c if mask.all() else c.take(mask)


def read_channel(path: str | Path, channel: str, cols: Columns | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Whole series of one channel (ts, val, valid), sorted by time — for offline analysis."""
    parts = [filter_chunk(c, {channel}, None, None) for c in read_chunks(path, cols)]
    parts = [p for p in parts if len(p)]
    if not parts:
        e = np.empty(0)
        return e.astype(np.int64), e, e.astype(bool)
    c = Chunk.concat(parts)
    order = np.argsort(c.ts, kind="stable")
    c = c.take(order)
    valid = ~c.nd & ~np.isnan(c.val)
    return c.ts, c.val, valid
