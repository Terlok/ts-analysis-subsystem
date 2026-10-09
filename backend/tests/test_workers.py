"""Smoke tests of the worker handlers with stubbed storage."""

import fakeredis
import numpy as np
import orjson

from app.config import Settings
from tsa_core.aggregates import MultiLevelAggregator
from workers.analytics import Analytics
from workers.common import LoadMeter, shard_of

SEC = 1_000_000


class FakeWriter:
    def __init__(self):
        self.flags = []

    def point_flags(self, channel, run, rows):
        self.flags += [(channel, run, r) for r in rows]

    def flush(self):
        pass


class NoAlarms:
    def evaluate(self, channel, res):
        pass


def make_analytics(shards=1, shard=0):
    a = object.__new__(Analytics)
    a.s = Settings()
    a.shard, a.shards = shard, shards
    a.writer = FakeWriter()
    a.r = fakeredis.FakeRedis()
    a.meter = LoadMeter(a.r, "test", period_s=0)
    a.alarms = NoAlarms()
    a.processors, a.channel_meta, a.models = {}, {}, {}
    a.episodes = []
    a.write_episodes = lambda eps: a.episodes.extend(eps)
    return a


def pkt(cid, ts, val, q=None):
    return {"t_ing": int(ts[-1]), "ch": [{"id": cid, "ts": ts.tolist(), "val": val.tolist(), "q": q or ["good"] * len(ts)}]}


def test_analytics_handler_flags_spike_and_publishes():
    a = make_analytics()
    ps = a.r.pubsub()
    ps.subscribe("events")
    ps.get_message()
    n = 2000
    ts = 1_760_000_000 * SEC + np.arange(n) * SEC
    x = np.sin(np.arange(n) / 40) + 0.01 * np.random.default_rng(1).standard_normal(n)
    x[1500] += 2.0
    batch = [(f"{i}-0".encode(), pkt("A", ts[i : i + 100], x[i : i + 100])) for i in range(0, n, 100)]
    a.handle(batch)
    flagged = {r["ts"]: r for _c, _run, r in a.writer.flags}
    assert int(ts[1500]) in flagged and flagged[int(ts[1500])]["anomaly"]
    msg = ps.get_message(timeout=1)
    assert msg and orjson.loads(msg["data"])["type"] == "flags"
    assert a.r.llen("metrics:lat:proc") == 1
    assert float(a.r.hget("metrics:worker:test", "rho_load")) >= 0


def test_analytics_skips_other_shards_and_bad_points():
    a = make_analytics(shards=2, shard=0)
    other = next(c for c in ("A", "B", "C", "D") if shard_of(c, 2) == 1)
    ts = np.arange(10) * SEC + 1_760_000_000 * SEC
    a.handle([(b"1-0", pkt(other, ts, np.ones(10)))])
    assert other not in a.processors
    mine = next(c for c in ("A", "B", "C", "D") if shard_of(c, 2) == 0)
    a.handle([(b"2-0", pkt(mine, ts, np.ones(10), q=["bad"] * 10))])
    assert a.processors[mine].hampel._fifo == type(a.processors[mine].hampel._fifo)()  # nothing analysed


def test_aggregator_seed_merges_with_stored_buckets():
    agg = MultiLevelAggregator([10 * SEC], late_us=60 * SEC)
    first = MultiLevelAggregator([10 * SEC], late_us=60 * SEC)
    ts = np.arange(20) * SEC
    first.add(ts[:5], np.arange(5.0))
    stored = first.pop_dirty()[0]  # written before a "restart"
    agg.seed(0, stored)
    agg.add(ts[5:20], np.arange(5.0, 20.0))
    rows = {int(r["ts"]): r for r in agg.flush_all()[0]}
    assert rows[0]["cnt"] == 10 and rows[0]["vmin"] == 0.0 and rows[10 * SEC]["cnt"] == 10
