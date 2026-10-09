"""DDL of the subsystem's own QuestDB tables.

The source table `analog` (id, ts, val, nd, otkl) is only read/replayed; measurements
of the subsystem go to `telemetry_raw` (not `telemetry`: QuestDB has a hidden system
table with that name, used for its own usage statistics). Dedup keys make every write idempotent
(ФВ-1.4, ФВ-2.2): re-delivered packets after a restart do not create duplicates.
"""

RAW_TABLE = "telemetry_raw"
FLAGS_TABLE = "point_flags"
AGG_TABLE = "telemetry_agg"

TELEMETRY = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    channel SYMBOL CAPACITY 4096 CACHE INDEX,
    ts TIMESTAMP,
    val DOUBLE,
    quality SYMBOL CAPACITY 8 CACHE,
    nd BOOLEAN,
    otkl SHORT,
    t_ing TIMESTAMP
) TIMESTAMP(ts) PARTITION BY DAY WAL
DEDUP UPSERT KEYS(ts, channel)
"""

# Sparse table: only points that were substituted by the Hampel filter or flagged as
# anomalous. `run` = 'online' for the stream, or the id of an offline re-analysis run.
POINT_FLAGS = """
CREATE TABLE IF NOT EXISTS point_flags (
    channel SYMBOL CAPACITY 4096 CACHE INDEX,
    run SYMBOL CAPACITY 256 CACHE,
    ts TIMESTAMP,
    val DOUBLE,
    val_f DOUBLE,
    median DOUBLE,
    pred DOUBLE,
    resid DOUBLE,
    thr DOUBLE,
    prob DOUBLE,
    substituted BOOLEAN,
    anomaly BOOLEAN
) TIMESTAMP(ts) PARTITION BY DAY WAL
DEDUP UPSERT KEYS(ts, channel, run)
"""

# Composable aggregates of every level of the time grid (ФВ-2.3).
TELEMETRY_AGG = """
CREATE TABLE IF NOT EXISTS telemetry_agg (
    channel SYMBOL CAPACITY 4096 CACHE INDEX,
    lvl INT,
    ts TIMESTAMP,
    vmin DOUBLE,
    tmin TIMESTAMP,
    vmax DOUBLE,
    tmax TIMESTAMP,
    vfirst DOUBLE,
    tfirst TIMESTAMP,
    vlast DOUBLE,
    tlast TIMESTAMP,
    vsum DOUBLE,
    cnt LONG
) TIMESTAMP(ts) PARTITION BY MONTH WAL
DEDUP UPSERT KEYS(ts, channel, lvl)
"""

ALL = [TELEMETRY, POINT_FLAGS, TELEMETRY_AGG]

# Columns every table must have after creation (guards against silently reusing an
# existing table of the same name).
EXPECTED_COLUMNS = {
    RAW_TABLE: {"channel", "ts", "val", "quality", "nd", "otkl", "t_ing"},
    FLAGS_TABLE: {"channel", "run", "ts", "val_f", "substituted", "anomaly"},
    AGG_TABLE: {"channel", "lvl", "ts", "vmin", "tmin", "vmax", "tmax", "cnt"},
}

# Optional retention policies (separate for raw data and aggregates, ФВ-2.2), e.g.:
#   ALTER TABLE telemetry_raw SET TTL 8 WEEKS;
#   ALTER TABLE point_flags SET TTL 8 WEEKS;
