"""Export the `analog` table from a (remote) QuestDB to a CSV file via the HTTP /exp endpoint.

The interval is exported in time slices (the table is partitioned by hour), so large
ranges do not produce one huge query; the output is the input format of scripts.replay.

    python -m scripts.export_questdb --url http://remote-host:9000 \\
        --from 2025-05-01T00:00:00Z --to 2025-05-02T00:00:00Z --out data/analog.csv.gz
    python -m scripts.export_questdb --url http://remote-host:9000 --last 6h --ids P1,P2 --out data/p.csv
"""

from __future__ import annotations

import argparse
import gzip
import io
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_duration(s: str) -> timedelta:
    unit = s[-1]
    n = float(s[:-1])
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def exec_json(client: httpx.Client, base: str, sql: str) -> dict:
    r = client.get(f"{base}/exec", params={"query": sql})
    r.raise_for_status()
    return r.json()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="QuestDB HTTP endpoint, e.g. http://host:9000")
    p.add_argument("--table", default="analog")
    p.add_argument("--columns", default="id, ts, val, nd, otkl")
    p.add_argument("--from", dest="t_from", help="ISO start (default: first row / now - --last)")
    p.add_argument("--to", dest="t_to", help="ISO end (default: last row / now)")
    p.add_argument("--last", help="export the last period instead, e.g. 6h, 2d, 1w")
    p.add_argument("--ids", help="comma separated ids to export (default: all)")
    p.add_argument("--slice", default="1h", help="time slice per request, e.g. 30m, 1h, 1d")
    p.add_argument("--out", required=True, help="output .csv or .csv.gz")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--timeout", type=float, default=300)
    a = p.parse_args()

    base = a.url.rstrip("/")
    auth = (a.user, a.password or "") if a.user else None
    client = httpx.Client(timeout=a.timeout, auth=auth)

    if a.last:
        t_to = parse_dt(a.t_to) if a.t_to else datetime.now(timezone.utc)
        t_from = t_to - parse_duration(a.last)
    else:
        bounds = exec_json(client, base, f"SELECT min(ts), max(ts) FROM {a.table}")["dataset"][0]
        if bounds[0] is None:
            print("table is empty", file=sys.stderr)
            return 1
        t_from = parse_dt(a.t_from) if a.t_from else parse_dt(bounds[0])
        t_to = parse_dt(a.t_to) if a.t_to else parse_dt(bounds[1]) + timedelta(microseconds=1)

    where_ids = ""
    if a.ids:
        where_ids = " AND id IN (" + ", ".join(quote(i) for i in a.ids.split(",")) + ")"
    step = parse_duration(a.slice)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if out.name.endswith(".gz") else open
    rows = 0
    header_written = False
    t0 = time.monotonic()
    with opener(out, "wt", newline="") as f:
        cur = t_from
        while cur < t_to:
            nxt = min(cur + step, t_to)
            sql = f"SELECT {a.columns} FROM {a.table} WHERE ts >= {quote(iso(cur))} AND ts < {quote(iso(nxt))}{where_ids}"
            with client.stream("GET", f"{base}/exp", params={"query": sql}) as r:
                r.raise_for_status()
                text = io.StringIO()
                for part in r.iter_text():
                    text.write(part)
            lines = text.getvalue().splitlines(keepends=True)
            if lines:
                if not header_written:
                    f.write(lines[0].replace('"', ""))
                    header_written = True
                body = lines[1:]
                f.writelines(body)
                rows += len(body)
            print(f"{iso(cur)} .. {iso(nxt)}: total {rows} rows ({time.monotonic() - t0:.1f}s)", file=sys.stderr)
            cur = nxt
    print(f"exported {rows} rows to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
