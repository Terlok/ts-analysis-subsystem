"""QuestDB access.

Reads go through the PostgreSQL wire protocol (psycopg 3), writes through the
InfluxDB Line Protocol over HTTP (official `questdb` client), which is the
recommended high-throughput ingestion path.

Timestamps are selected as `cast(ts AS long)` (microseconds) to avoid datetime
conversions. Time bounds are integers formatted into SQL (they are validated ints,
so this is injection-safe); channel ids are always bound parameters.
"""

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import psycopg
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from tsa_core.aggregates import AGG_DTYPE

try:  # questdb >= 5
    from questdb import Sender, TimestampMicros
except ImportError:  # pragma: no cover - older client
    from questdb.ingress import Sender, TimestampMicros

log = logging.getLogger(__name__)

RAW_DTYPE = np.dtype([("ts", "<i8"), ("val", "<f8"), ("quality", "U12"), ("nd", "?"), ("otkl", "<i2")])
FLAG_COLUMNS = ("val", "val_f", "median", "pred", "resid", "thr", "prob")


def _ts(t: int) -> str:
    return f"cast({int(t)} AS timestamp)"


class QuestDBReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = AsyncConnectionPool(
            settings.questdb_pg_conninfo,
            min_size=1,
            max_size=settings.questdb_pool_size,
            kwargs={"autocommit": True},
            open=False,
        )

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        await self.pool.close()

    async def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    async def ping(self) -> bool:
        try:
            await self._fetch("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def fetch_raw(self, channel: str, t_from: int, t_to: int, limit: int) -> np.ndarray:
        rows = await self._fetch(
            "SELECT cast(ts AS long), val, quality, nd, otkl FROM telemetry "
            f"WHERE channel = %s AND ts >= {_ts(t_from)} AND ts < {_ts(t_to)} LIMIT {int(limit)}",
            (channel,),
        )
        out = np.empty(len(rows), dtype=RAW_DTYPE)
        for i, (t, v, q, nd, otkl) in enumerate(rows):
            out[i] = (t, np.nan if v is None else v, q or "", bool(nd), otkl or 0)
        return out

    async def count_raw(self, channel: str, t_from: int, t_to: int) -> int:
        rows = await self._fetch(
            f"SELECT count() FROM telemetry WHERE channel = %s AND ts >= {_ts(t_from)} AND ts < {_ts(t_to)}",
            (channel,),
        )
        return int(rows[0][0]) if rows else 0

    async def fetch_agg(self, channel: str, lvl: int, t_from: int, t_to: int) -> np.ndarray:
        rows = await self._fetch(
            "SELECT cast(ts AS long), vmin, cast(tmin AS long), vmax, cast(tmax AS long), "
            "vfirst, cast(tfirst AS long), vlast, cast(tlast AS long), vsum, cnt "
            f"FROM telemetry_agg WHERE channel = %s AND lvl = {int(lvl)} "
            f"AND ts >= {_ts(t_from)} AND ts < {_ts(t_to)} ORDER BY ts",
            (channel,),
        )
        return np.array([tuple(r) for r in rows], dtype=AGG_DTYPE) if rows else np.empty(0, AGG_DTYPE)

    async def fetch_flags(self, channels: list[str], t_from: int, t_to: int, run: str = "online", limit: int = 100_000):
        if not channels:
            return []
        marks = ",".join(["%s"] * len(channels))
        rows = await self._fetch(
            "SELECT channel, cast(ts AS long), " + ", ".join(FLAG_COLUMNS) + ", substituted, anomaly "
            f"FROM point_flags WHERE channel IN ({marks}) AND run = %s "
            f"AND ts >= {_ts(t_from)} AND ts < {_ts(t_to)} LIMIT {int(limit)}",
            (*channels, run),
        )
        keys = ("channel", "ts", *FLAG_COLUMNS, "substituted", "anomaly")
        return [dict(zip(keys, r)) for r in rows]

    async def channel_stats(self) -> list[tuple[str, int, int, int]]:
        """(channel, count, first_ts, last_ts) for every channel present in the archive."""
        rows = await self._fetch(
            "SELECT channel, count(), cast(min(ts) AS long), cast(max(ts) AS long) FROM telemetry GROUP BY channel"
        )
        return [(r[0], int(r[1]), int(r[2]), int(r[3])) for r in rows]


class QuestDBSyncReader:
    """Synchronous reader for workers and offline scripts."""

    def __init__(self, settings: Settings):
        self.conninfo = settings.questdb_pg_conninfo

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, autocommit=True)

    def iter_raw(
        self, channel: str, t_from: int, t_to: int, chunk_us: int, include_bad: bool = False
    ) -> Iterable[tuple[np.ndarray, np.ndarray]]:
        """Raw (ts, val) of a channel in time chunks; bad-quality points are skipped by default."""
        quality = "" if include_bad else " AND quality != 'bad'"
        with self.connect() as conn:
            t = t_from
            while t < t_to:
                t_end = min(t + chunk_us, t_to)
                rows = conn.execute(
                    f"SELECT cast(ts AS long), val FROM telemetry WHERE channel = %s "
                    f"AND ts >= {_ts(t)} AND ts < {_ts(t_end)}{quality}",
                    (channel,),
                ).fetchall()
                if rows:
                    arr = np.array(rows, dtype=object)
                    yield arr[:, 0].astype(np.int64), np.array([np.nan if v is None else v for v in arr[:, 1]], dtype=np.float64)
                t = t_end

    def time_bounds(self, channel: str | None = None) -> tuple[int, int] | None:
        where = "WHERE channel = %s" if channel else ""
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT cast(min(ts) AS long), cast(max(ts) AS long) FROM telemetry {where}",
                (channel,) if channel else (),
            ).fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0]), int(row[1])

    def fetch_agg(self, channel: str, lvl: int, t_from: int, t_to: int) -> np.ndarray:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT cast(ts AS long), vmin, cast(tmin AS long), vmax, cast(tmax AS long), "
                "vfirst, cast(tfirst AS long), vlast, cast(tlast AS long), vsum, cnt "
                f"FROM telemetry_agg WHERE channel = %s AND lvl = {int(lvl)} "
                f"AND ts >= {_ts(t_from)} AND ts < {_ts(t_to)} ORDER BY ts",
                (channel,),
            ).fetchall()
        return np.array([tuple(r) for r in rows], dtype=AGG_DTYPE) if rows else np.empty(0, AGG_DTYPE)

    def channels(self) -> list[str]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT channel FROM telemetry").fetchall()]

    def execute(self, sql: str) -> None:
        with self.connect() as conn:
            conn.execute(sql)


class IlpWriter:
    """Batched writer over ILP/HTTP. Call `flush()` before acknowledging the source messages."""

    def __init__(self, settings: Settings):
        self.sender = Sender.from_conf(settings.questdb_ilp_conf)
        self.sender.establish()
        self.rows = 0

    def close(self) -> None:
        try:
            self.sender.flush()
        finally:
            self.sender.close()

    def flush(self) -> None:
        self.sender.flush()

    def telemetry(self, channel: str, ts: np.ndarray, val: np.ndarray, quality: list[str], nd, otkl, t_ing: int) -> None:
        row = self.sender.row
        ing = TimestampMicros(int(t_ing))
        for i in range(len(ts)):
            cols = {"nd": bool(nd[i]), "otkl": int(otkl[i]), "t_ing": ing}
            v = float(val[i])
            if v == v:
                cols["val"] = v
            row(
                "telemetry",
                symbols={"channel": channel, "quality": quality[i]},
                columns=cols,
                at=TimestampMicros(int(ts[i])),
            )
        self.rows += len(ts)

    def point_flags(self, channel: str, run: str, rows: list[dict]) -> None:
        for r in rows:
            cols = {k: float(r[k]) for k in FLAG_COLUMNS if r.get(k) is not None and r[k] == r[k]}
            cols["substituted"] = bool(r["substituted"])
            cols["anomaly"] = bool(r["anomaly"])
            self.sender.row(
                "point_flags",
                symbols={"channel": channel, "run": run},
                columns=cols,
                at=TimestampMicros(int(r["ts"])),
            )
        self.rows += len(rows)

    def aggregates(self, channel: str, lvl: int, rows: np.ndarray) -> None:
        for r in rows:
            self.sender.row(
                "telemetry_agg",
                symbols={"channel": channel},
                columns={
                    "lvl": int(lvl),
                    "vmin": float(r["vmin"]),
                    "tmin": TimestampMicros(int(r["tmin"])),
                    "vmax": float(r["vmax"]),
                    "tmax": TimestampMicros(int(r["tmax"])),
                    "vfirst": float(r["vfirst"]),
                    "tfirst": TimestampMicros(int(r["tfirst"])),
                    "vlast": float(r["vlast"]),
                    "tlast": TimestampMicros(int(r["tlast"])),
                    "vsum": float(r["vsum"]),
                    "cnt": int(r["cnt"]),
                },
                at=TimestampMicros(int(r["ts"])),
            )
        self.rows += len(rows)
