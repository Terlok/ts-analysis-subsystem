import asyncio
from pathlib import Path

import numpy as np
import orjson
import pytest

from scripts.replay import Packetizer, build_parser, encode, replay
from scripts.telemetry_file import Chunk, read_channel, read_chunks, to_micros

SEC = 1_000_000

CSV = """id,ts,val,nd,otkl
A,2025-01-01T00:00:00.000000Z,1.0,false,0
B,2025-01-01T00:00:00.000000Z,10.0,false,0
A,2025-01-01T00:00:00.050000Z,1.5,false,0
A,2025-01-01T00:00:01.000000Z,2.0,true,0
B,2025-01-01T00:00:01.000000Z,,false,3
A,2025-01-01T00:00:02.000000Z,3.0,false,0
"""


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    p = tmp_path / "analog.csv"
    p.write_text(CSV)
    return p


def test_read_questdb_csv(csv_file):
    (c,) = list(read_chunks(csv_file))
    assert list(c.id) == ["A", "B", "A", "A", "B", "A"]
    assert c.ts[0] == 1_735_689_600 * SEC and c.ts[2] - c.ts[0] == 50_000
    assert np.isnan(c.val[4]) and c.nd[3] and c.otkl[4] == 3
    ts, val, valid = read_channel(csv_file, "A")
    assert len(ts) == 4 and list(valid) == [True, True, False, True]


def test_numeric_timestamps_detected():
    import pandas as pd

    assert to_micros(pd.Series([1_735_689_600]))[0] == 1_735_689_600 * SEC
    assert to_micros(pd.Series([1_735_689_600_000]))[0] == 1_735_689_600 * SEC
    assert to_micros(pd.Series([1_735_689_600_000_000_000]))[0] == 1_735_689_600 * SEC


def test_packetizer_cuts_by_data_time_and_size():
    n = 1000
    c = Chunk(np.array(["A", "B"] * (n // 2), dtype=object), np.repeat(np.arange(n // 2) * 10_000, 2),
              np.arange(n, dtype=float), np.zeros(n, bool), np.zeros(n, np.int16))
    pk = Packetizer(batch_us=100_000, max_points=1000)
    packets = []
    for s in range(0, n, 77):  # chunk borders must not matter
        packets += pk.feed(c.take(slice(s, s + 77)))
    packets += pk.flush()
    assert sum(p.n for p in packets) == n
    assert all(p.t_end - p.t_start < 100_000 for p in packets)
    assert len(packets) == 50 and all(set(p.channels) == {"A", "B"} for p in packets)
    pk2 = Packetizer(batch_us=10**12, max_points=64)
    ps = pk2.feed(c) + pk2.flush()
    assert max(p.n for p in ps) == 64 and sum(p.n for p in ps) == n


def test_encode_time_mapping():
    c = Chunk(np.array(["A", "A"], dtype=object), np.array([10 * SEC, 20 * SEC]), np.array([1.0, np.nan]),
              np.zeros(2, bool), np.zeros(2, np.int16))
    pk = Packetizer(10**12, 10)
    (p,) = pk.feed(c) + pk.flush()
    body = orjson.loads(encode(p, offset_us=5 * SEC, compress=2.0, t_first=10 * SEC, source="t"))
    ch = body["channels"][0]
    assert ch["ts"] == [15 * SEC, 20 * SEC]  # 10 + (20-10)/2 + 5
    assert ch["val"] == [1.0, None]


async def _serve(received: list, drop_after: int):
    """Stub of /ws/ingest: acks every packet, drops the connection once without acking."""
    from websockets.asyncio.server import serve

    state = {"dropped": False}

    async def handler(ws):
        async for msg in ws:
            pkt = orjson.loads(msg)
            received.append(pkt["seq"])
            if not state["dropped"] and len(received) == drop_after:
                state["dropped"] = True
                await ws.close()
                return
            n = sum(len(c["ts"]) for c in pkt["channels"])
            await ws.send(orjson.dumps({"type": "ack", "seq": pkt["seq"], "accepted": n, "rejected": 0}))

    return await serve(handler, "127.0.0.1", 0)


async def test_replay_websocket_resends_unacked(tmp_path):
    from scripts.make_sample import generate
    from datetime import datetime, timezone

    df, _ = generate(3, 0.05, 1.0, 1, datetime(2025, 1, 1, tzinfo=timezone.utc))
    f = tmp_path / "s.csv"
    df.to_csv(f, index=False)

    received: list[int] = []
    server = await _serve(received, drop_after=20)
    port = server.sockets[0].getsockname()[1]
    args = build_parser().parse_args([str(f), "--url", f"ws://127.0.0.1:{port}", "--speed", "0", "--report-s", "60"])
    rc = await asyncio.wait_for(replay(args), timeout=30)
    server.close()
    assert rc == 0
    expected = set(range(1, 181))
    assert set(received) == expected  # every packet delivered at least once
    assert len(received) > len(expected)  # some were re-sent after the dropped connection
