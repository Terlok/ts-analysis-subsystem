"""HTTP / WebSocket level tests of ingest, series and live streaming with in-memory stores."""

import time

import fakeredis
import numpy as np
import orjson
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.ingest import IngestService
from app.services.live import LiveHub
from app.services.series import SeriesService
from app.services.tiles import TileCache
from app.services.training import ModelTrainer
from app.state import AppState
from tests.test_services import FakeQdb, FakeRegistry

SEC = 1_000_000


class FakeEngine:
    async def dispose(self):
        pass


class Qdb(FakeQdb):
    async def open(self):
        pass

    async def close(self):
        pass

    async def ping(self):
        return True


@pytest.fixture
def client():
    settings = Settings(live_rate_hz=50)
    redis = fakeredis.FakeAsyncRedis()
    reg = FakeRegistry()
    g = settings.grid
    qdb = Qdb(np.empty(0, np.int64), np.empty(0), [g.delta(i) for i in range(g.n_levels)])
    tiles = TileCache(settings, redis, qdb)
    st = AppState(
        settings=settings, redis=redis, pg_engine=FakeEngine(), pg=None, qdb=qdb, registry=reg,
        ingest=IngestService(settings, redis, reg), tiles=tiles,
        series=SeriesService(settings, redis, qdb, tiles), live=LiveHub(settings, redis),
        trainer=ModelTrainer(),
    )
    with TestClient(create_app(settings, st)) as c:
        yield c


def body(seq, n=50, t0=None, cid="A"):
    t0 = t0 or (time.time_ns() // 1000 - 60 * SEC)
    ts = [t0 + i * 100_000 for i in range(n)]
    return {"seq": seq, "source": "t", "channels": [{"id": cid, "ts": ts, "val": [float(np.sin(i / 5)) for i in range(n)]}]}


def test_ingest_http_and_series(client):
    b = body(1, n=600)
    r = client.post("/api/ingest", json=b)
    assert r.status_code == 200 and r.json()["accepted"] == 600
    t = b["channels"][0]["ts"]
    r = client.get("/api/series", params={"channels": "A", "from": t[0], "to": t[-1] + 1, "width": 50,
                                          "events": "false", "flags": "false"})
    assert r.status_code == 200
    s = r.json()["series"][0]
    assert s["source"] == "hot" and s["n"] == 600 and len(s["t"]) == 100
    assert client.get("/api/channels/state").json()[0]["id"] == "A"


def test_ingest_validation_errors(client):
    r = client.post("/api/ingest", json={"channels": [{"id": "A", "ts": [1, 2], "val": [1.0]}]})
    assert r.status_code == 422


def test_ingest_websocket_acks(client):
    with client.websocket_connect("/ws/ingest") as ws:
        ws.send_bytes(orjson.dumps(body(7)))
        ack = orjson.loads(ws.receive_bytes())
        assert ack["seq"] == 7 and ack["accepted"] == 50
        ws.send_text("{not json")
        err = orjson.loads(ws.receive_bytes())
        assert err["errors"]


def test_live_stream_subscription(client):
    client.post("/api/ingest", json=body(1, n=300))
    with client.websocket_connect("/ws/stream") as ws:
        ws.send_text(orjson.dumps({"op": "subscribe", "channels": ["A"], "window_s": 30, "width_px": 100}).decode())
        snap = orjson.loads(ws.receive_bytes())
        assert snap["type"] == "snapshot" and snap["series"][0]["t"]
        ws.send_text('{"op": "ping", "t": 1}')
        assert orjson.loads(ws.receive_bytes())["type"] == "pong"
        ws.send_text('{"op": "bogus"}')
        assert orjson.loads(ws.receive_bytes())["type"] == "error"


def test_config_endpoint(client):
    cfg = client.get("/api/config").json()
    assert cfg["grid"]["levels"][1] == cfg["grid"]["levels"][0] * cfg["grid"]["base"]
