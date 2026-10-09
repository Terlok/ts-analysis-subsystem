"""Create the schema of the subsystem in QuestDB and PostgreSQL (idempotent).

    python -m scripts.init_db              # both
    python -m scripts.init_db --questdb    # only QuestDB tables
    python -m scripts.init_db --postgres   # only PostgreSQL tables
"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import questdb_schema
from app.db.models import Base
from app.db.postgres import make_sync_engine
from app.db.questdb import QuestDBSyncReader


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--questdb", action="store_true")
    p.add_argument("--postgres", action="store_true")
    a = p.parse_args()
    both = not (a.questdb or a.postgres)
    s = get_settings()

    if both or a.questdb:
        q = QuestDBSyncReader(s)
        for ddl in questdb_schema.ALL:
            q.execute(ddl)
        # CREATE TABLE IF NOT EXISTS silently keeps a pre-existing table with the same name:
        # verify that every table really has our columns.
        with q.connect() as conn:
            for table, expected in questdb_schema.EXPECTED_COLUMNS.items():
                cols = {r[0] for r in conn.execute(f"SELECT \"column\" FROM table_columns('{table}')").fetchall()}
                missing = expected - cols
                if missing:
                    print(f"ERROR: QuestDB table {table} exists with a different schema, missing {sorted(missing)}")
                    return 1
        tables = ", ".join(questdb_schema.EXPECTED_COLUMNS)
        print(f"QuestDB {s.questdb_host}:{s.questdb_pg_port}: tables {tables} ready")
    if both or a.postgres:
        engine = make_sync_engine(s)
        Base.metadata.create_all(engine)
        print(f"PostgreSQL: tables {', '.join(sorted(Base.metadata.tables))} ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
