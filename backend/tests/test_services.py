"""Service layer tests with an in-memory Redis (fakeredis) and stub QuestDB / registry."""

import asyncio

import fakeredis
import numpy as np
import orjson
import pytest

from app.config import Settings
from app.db.redis_store import LAST_KEY, STREAM_FIELD, WM_KEY, decode_packet, hot_key
from app.schemas import ChannelOut, IngestPacket
from app.services.ingest import IngestService
from app.services.live import LiveHub, Subscription
from app.services.series import SeriesService
from app.services.tiles import TileCache
from tsa_core.aggregates import AGG_DTYPE, aggregate

SEC = 1_000_000
T0 = 1_760_000_000 * SEC


class FakeRegistry:
    def __init__(self):
        self.channels: dict[str, ChannelOut] = {"A": ChannelOut(id="A", x_min=-10, x_max=10)}

    async def ensure(self, ids, source=None):
        new = [i for i in ids if i not in self.channels]
        for i in new:
            self.channels[i] = ChannelOut(id=i)
        return new

    async def all(self):
        return self.channels


class FakeQdb:
    """Serves aggregates computed from an in-memory raw series; counts queries."""

    def __init__(self, ts, x, deltas):
        self.ts, self.x, self.deltas = ts, x, deltas
        self.agg_queries = 0

    async def fetch_agg(self, channel, lvl, t_from, t_to):
        self.agg_queries += 1
        rows = aggregate(self.ts, self.x, self.deltas[lvl])
        return rows[(rows["ts"] >= t_from) & (rows["ts"] < t_to)]

    async def fetch_raw(self, channel, t_from, t_to, limit):
        m = (self.ts >= t_from) & (self.ts < t_to)
        out = np.zeros(int(m.sum()), dtype=[("ts", "<i8"), ("val", "<f8"), ("quality", "U12"), ("nd", "?"), ("otkl", "<i2")])
        out["ts"], out["val"], out["quality"] = self.ts[m], self.x[m], "good"
        return out[:limit]


@pytest.fixture
def settings():
    return Settings(hot_window_s=600, agg_delta0_ms=1000, agg_base=4, agg_levels=6, tile_buckets=64, late_ms=10_000)


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis()


def packet(seq, cid, ts, val, nd=None):
    return IngestPacket(seq=seq, source="test", channels=[{"id": cid, "ts": list(map(int, ts)), "val": val, "nd": nd}])


async def test_ingest_normalizes_and_publishes(settings, redis):
    svc = IngestService(settings, redis, FakeRegistry())
    ack = await svc.ingest(packet(1, "A", [T0 + 2 * SEC, T0, T0 + SEC, T0 + SEC], [1.0, None, 50.0, 2.0], [False, False, False, True]))
    assert ack.accepted == 3 and ack.rejected == 1 and ack.reordered == 4
    entries = await redis.xrange(settings.stream_key)
    body = decode_packet(entries[0][1][STREAM_FIELD])
    ch = body["ch"][0]
    assert ch["ts"] == [T0, T0 + SEC, T0 + 2 * SEC]
    # NaN -> bad, duplicate ts keeps the last (nd=True -> bad), 1.0 within range -> good
    assert ch["q"] == ["bad", "bad", "good"]
    assert int(await redis.hget(WM_KEY, "A")) == T0 + 2 * SEC
    assert orjson.loads(await redis.hget(LAST_KEY, "A"))["val"] == 1.0
    assert await redis.zcard(hot_key("A")) == 1

    ack = await svc.ingest(packet(2, "NEW", [T0], [1.0]))
    assert ack.new_channels == ["NEW"]
    ack = await svc.ingest(packet(3, "A", [123], [1.0]))  # seconds instead of us
    assert ack.rejected == 1 and ack.errors


async def test_series_hot_path_downsamples(settings, redis):
    svc = IngestService(settings, redis, FakeRegistry())
    n = 3000
    ts = T0 + np.arange(n) * (SEC // 10)  # 10 Hz, 300 s -> inside hot window
    x = np.sin(np.arange(n) / 30)
    for i in range(0, n, 100):
        await svc.ingest(packet(i, "A", ts[i : i + 100], x[i : i + 100].tolist()))
    series = SeriesService(settings, redis, qdb=None, tiles=None)
    out, _ = await series.get(["A"], int(ts[0]), int(ts[-1]) + 1, width_px=100, c=2)
    s = out[0]
    assert s.source == "hot" and s.n == n and len(s.t) == s.m == 200
    assert s.t[0] == ts[0] and s.t[-1] == ts[-1]
    assert max(s.v) == pytest.approx(x.max()) and min(s.v) == pytest.approx(x.min())


async def test_series_aggregate_path_and_tile_cache(settings, redis):
    g = settings.grid
    deltas = [g.delta(lvl) for lvl in range(g.n_levels)]
    n = 86_400
    ts = T0 + np.arange(n, dtype=np.int64) * SEC  # one day at 1 Hz
    x = np.sin(np.arange(n) / 600) + 0.01 * np.random.default_rng(0).standard_normal(n)
    x[50_000] = 9.0  # a short spike must survive the MinMax preselection
    qdb = FakeQdb(ts, x, deltas)
    await redis.hset(WM_KEY, "A", int(ts[-1]))
    tiles = TileCache(settings, redis, qdb)
    svc = SeriesService(settings, redis, qdb, tiles)

    out, _ = await svc.get(["A"], int(ts[0]), int(ts[-1]) + 1, width_px=800, c=2)
    s = out[0]
    assert s.source == "agg" and s.level is not None
    assert s.n == n and len(s.t) <= s.m == 1600
    assert 9.0 in s.v
    q1 = qdb.agg_queries
    stats = await redis.hgetall("metrics:tiles")
    assert int(stats[b"miss"]) > 0

    out2, _ = await svc.get(["A"], int(ts[0]), int(ts[-1]) + 1, width_px=800, c=2)
    assert out2[0].t == s.t
    # closed tiles come from Redis now; only the open tile at the end is re-read
    assert qdb.agg_queries - q1 <= 1
    assert int((await redis.hgetall("metrics:tiles"))[b"hit"]) > 0


async def test_concurrent_requests_share_one_query(settings, redis):
    g = settings.grid
    ts = T0 + np.arange(20_000, dtype=np.int64) * SEC
    x = np.cos(np.arange(20_000) / 100)
    qdb = FakeQdb(ts, x, [g.delta(lvl) for lvl in range(g.n_levels)])
    await redis.hset(WM_KEY, "A", int(ts[-1]) + 3600 * SEC)  # all tiles closed
    tiles = TileCache(settings, redis, qdb)
    lvl = 2
    res = await asyncio.gather(*[tiles.rows("A", lvl, int(ts[0]), int(ts[-1]), int(ts[-1]) + 3600 * SEC) for _ in range(10)])
    assert qdb.agg_queries == 1
    assert all(len(r[0]) == len(res[0][0]) for r in res)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_bytes(self, data):
        self.sent.append(orjson.loads(data))


async def test_live_hub_snapshot_and_incremental_updates(settings, redis):
    svc = IngestService(settings, redis, FakeRegistry())
    ts = T0 + np.arange(600) * (SEC // 10)
    x = np.sin(np.arange(600) / 20)
    await svc.ingest(packet(1, "A", ts[:300], x[:300].tolist()))

    hub = LiveHub(settings, redis)
    ws = FakeWS()
    sub = Subscription(ws, ["A"], window_s=30, width_px=100, c=2)
    await hub.subscribe(sub)
    snap = ws.sent[0]
    assert snap["type"] == "snapshot" and snap["delta_us"] == sub.delta
    first = snap["series"][0]
    assert len(first["t"]) > 0

    entries = []
    await svc.ingest(packet(2, "A", ts[300:], x[300:].tolist()))
    for _id, fields in await redis.xrange(settings.stream_key):
        entries.append(decode_packet(fields[STREAM_FIELD]))
    hub._dispatch(entries[-1])
    await hub._send_update(sub)
    upd = ws.sent[-1]
    assert upd["type"] == "update"
    t_new = upd["series"][0]["t"]
    assert t_new and t_new[0] > first["t"][-1]  # only new finalized points are sent
    cells = [t // sub.delta for t in first["t"] + t_new]
    assert len(cells) == len(set(cells))  # one point per absolute grid cell
