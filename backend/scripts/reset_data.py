"""Delete all measurements and diagnostic results, keeping the schema (and, by default,
the channel registry, alarm rules and model versions).

    python -m scripts.reset_data --yes                  # data only
    python -m scripts.reset_data --yes --registry       # also channels, alarm rules, models

Cleared:
  QuestDB     telemetry_raw, point_flags, telemetry_agg (TRUNCATE)
  PostgreSQL  events, alarms, alarm_log, mode_changes
  Redis       ingest stream, hot window, last values, watermarks, tiles, metrics
Running workers recreate their consumer groups automatically; restarting them is
still recommended so that no in-memory state of the old data is kept.
"""

from __future__ import annotations

import argparse
import sys

import redis
from sqlalchemy import text

from app.config import get_settings
from app.db import questdb_schema
from app.db.postgres import make_sync_engine
from app.db.questdb import QuestDBSyncReader
from app.db.redis_store import LAST_KEY, WM_KEY

REDIS_PATTERNS = ["hot:*", "tile:*", "lease:*", "metrics:*"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.add_argument("--registry", action="store_true", help="also delete channels, alarm rules and model versions")
    a = p.parse_args()
    if not a.yes:
        print("Nothing done: add --yes to delete the data.", file=sys.stderr)
        return 1
    s = get_settings()

    q = QuestDBSyncReader(s)
    for table in questdb_schema.EXPECTED_COLUMNS:
        q.execute(f"TRUNCATE TABLE {table}")
    print(f"QuestDB: truncated {', '.join(questdb_schema.EXPECTED_COLUMNS)}")

    tables = ["alarm_log", "alarms", "events", "mode_changes"]
    if a.registry:
        tables += ["alarm_rules", "model_versions", "channels"]
    with make_sync_engine(s).begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    print(f"PostgreSQL: truncated {', '.join(tables)}")

    r = redis.Redis.from_url(s.redis_url)
    keys = [s.stream_key, LAST_KEY, WM_KEY]
    for pattern in REDIS_PATTERNS:
        keys += list(r.scan_iter(match=pattern, count=1000))
    deleted = r.delete(*keys) if keys else 0
    r.incr("config:version")
    r.incr("models:version")
    print(f"Redis: deleted {deleted} key(s)")
    print("Done. Restart ./run_dev.sh so that workers drop in-memory state of the old data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
