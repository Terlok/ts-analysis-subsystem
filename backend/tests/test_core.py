import math

import numpy as np
import pytest

from tsa_core.aggregates import (
    AGG_DTYPE,
    MultiLevelAggregator,
    aggregate,
    level_deltas,
    minmax_points,
    rollup,
)
from tsa_core.alarms import AlarmKind, AlarmRule, AlarmRuntime, AlarmState, acknowledge, evaluate
from tsa_core.downsample import (
    StreamingGridLTTB,
    lttb_indices,
    minmax_indices,
    minmax_lttb_indices,
    target_points,
)
from tsa_core.features import WindowFeatures, features_batch
from tsa_core.hampel import hampel_batch
from tsa_core.levels import Grid, reduction_ratio
from tsa_core.pipeline import ChannelParams, ChannelProcessor

SEC = 1_000_000


def series(n=5000, seed=0, step=SEC):
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.int64) * step + 1_700_000_000 * SEC
    x = np.sin(np.arange(n) / 50) + 0.05 * rng.standard_normal(n)
    return t, x


# --- downsampling -------------------------------------------------------------


def test_target_points_and_eta():
    assert target_points(10_000, 1600, 2) == 3200
    assert target_points(100, 1600, 2) == 100
    assert reduction_ratio(3486, 120) == pytest.approx(0.9656, abs=1e-4)


def test_lttb_keeps_endpoints_and_size():
    t, x = series()
    idx = lttb_indices(t, x, 120)
    assert len(idx) == 120
    assert idx[0] == 0 and idx[-1] == len(t) - 1
    assert np.all(np.diff(idx) > 0)


def test_lttb_returns_all_when_small():
    t, x = series(50)
    assert np.array_equal(lttb_indices(t, x, 100), np.arange(50))


def test_lttb_reference_implementation():
    """Compare with a straightforward pure-python LTTB."""
    t, x = series(997, seed=3)

    def ref(data, threshold):
        n = len(data)
        every = (n - 2) / (threshold - 2)
        a, out = 0, [0]
        for i in range(threshold - 2):
            avg_s = int((i + 1) * every) + 1
            avg_e = min(int((i + 2) * every) + 1, n)
            avg_t = sum(p[0] for p in data[avg_s:avg_e]) / (avg_e - avg_s)
            avg_x = sum(p[1] for p in data[avg_s:avg_e]) / (avg_e - avg_s)
            rs, re = int(i * every) + 1, int((i + 1) * every) + 1
            best, best_i = -1, rs
            for j in range(rs, re):
                area = abs((data[a][0] - avg_t) * (data[j][1] - data[a][1]) - (data[a][0] - data[j][0]) * (avg_x - data[a][1]))
                if area > best:
                    best, best_i = area, j
            out.append(best_i)
            a = best_i
        out.append(n - 1)
        return out

    data = [((ti - t[0]) / 1.0, xi) for ti, xi in zip(t, x)]
    assert list(lttb_indices(t, x, 77)) == ref(data, 77)


def test_minmax_preserves_extrema():
    t, x = series()
    x[1234] = 50.0
    x[4321] = -50.0
    idx = minmax_indices(t, x, 100)
    assert 1234 in idx and 4321 in idx
    sel = minmax_lttb_indices(t, x, 300)
    assert len(sel) == 300


# --- aggregates ---------------------------------------------------------------


def test_aggregate_matches_numpy():
    t, x = series(1000)
    rows = aggregate(t, x, 10 * SEC)
    assert len(rows) == 100
    assert rows["cnt"].sum() == 1000
    np.testing.assert_allclose(rows["vmax"], x.reshape(100, 10).max(axis=1))
    np.testing.assert_allclose(rows["vsum"], x.reshape(100, 10).sum(axis=1))
    assert np.all(x[(rows["tmin"] - t[0]) // SEC] == rows["vmin"])


def test_rollup_equals_direct_aggregation():
    t, x = series(1000)
    fine = aggregate(t, x, 10 * SEC)
    coarse = rollup(fine, 100 * SEC)
    direct = aggregate(t, x, 100 * SEC)
    for name in AGG_DTYPE.names:
        np.testing.assert_allclose(coarse[name], direct[name])


def test_streaming_aggregator_with_late_data():
    t, x = series(1000)
    agg = MultiLevelAggregator(level_deltas(SEC, 4, 3), late_us=60 * SEC)
    # deliver in chunks; 500..520 arrives after 520..540, i.e. up to 39 s late (allowed: 60 s)
    written = [[] for _ in range(3)]
    for s, e in [(0, 500), (520, 540), (500, 520), (540, 1000)]:
        agg.add(t[s:e], x[s:e])
        for lvl, rows in enumerate(agg.pop_dirty()):
            written[lvl].append(rows)
    for lvl, rows in enumerate(agg.flush_all()):
        written[lvl].append(rows)
    for lvl in range(3):
        # dedup by bucket keeping the last version (as QuestDB DEDUP UPSERT does)
        last = {int(r["ts"]): r for r in np.concatenate(written[lvl])}
        assert sum(int(r["cnt"]) for r in last.values()) == 1000
    assert agg.dropped_late == 0

    # data later than the allowed lateness is not merged into evicted buckets
    agg2 = MultiLevelAggregator([SEC], late_us=30 * SEC)
    agg2.add(t[:500], x[:500])
    agg2.pop_dirty()
    agg2.add(t[:10], x[:10])
    assert agg2.dropped_late == 10


def test_minmax_points_from_aggregates():
    t, x = series(1000)
    rows = aggregate(t, x, 50 * SEC)
    pt, px = minmax_points(rows)
    assert np.all(np.diff(pt) >= 0)
    assert px.max() == x.max() and px.min() == x.min()


# --- levels -------------------------------------------------------------------


def test_level_selection():
    g = Grid(delta0_us=SEC, base=4, n_levels=8, tile_buckets=1024)
    day = 86_400 * SEC
    lvl = g.select_level(0, day, m=3200, rho=2.0)
    assert g.buckets_in(lvl, 0, day) >= 3200
    assert g.buckets_in(lvl + 1, 0, day) < 3200
    assert g.select_level(0, 60 * SEC, m=3200) == 0
    tiles = g.tiles_covering(lvl, 5, day)
    assert len(tiles) <= math.ceil(day / g.tile_span(lvl)) + 1


# --- hampel / features ---------------------------------------------------------


def test_hampel_replaces_spike_and_keeps_signal():
    t, x = series(500)
    x[300] += 5.0
    filt, mask = hampel_batch(x, window=15, kappa=3.0)
    assert mask[300]
    assert abs(filt[300] - x[299]) < 0.5
    assert mask.mean() < 0.05


def test_hampel_constant_signal_no_substitution():
    x = np.ones(100)
    x[50] = 1.0001
    _, mask = hampel_batch(x, window=15)
    assert not mask.any()  # sigma = 0 and min_sigma = 0 -> no decision


def test_features_match_numpy():
    t, x = series(200, step=SEC // 2)
    t = t.copy()
    t[100:] += SEC  # irregular sampling
    feats, valid = features_batch(t, x, window=30)
    i = 150
    w = x[i - 29 : i + 1]
    tw = (t[i - 29 : i + 1] - t[i - 29]) / 1e6
    assert valid[i]
    np.testing.assert_allclose(feats[i, 1], w.mean())
    np.testing.assert_allclose(feats[i, 2], w.std(), rtol=1e-6)
    np.testing.assert_allclose(feats[i, 3], w.min())
    np.testing.assert_allclose(feats[i, 4], w.max())
    np.testing.assert_allclose(feats[i, 5], math.sqrt((w**2).mean()))
    np.testing.assert_allclose(feats[i, 6], np.polyfit(tw, w, 1)[0], rtol=1e-6)


def test_features_rebase_long_series():
    wf = WindowFeatures(10)
    v = None
    for i in range(20_000):
        v = wf.update(i * SEC, 2.0 * i)  # slope 2 per second
    assert v[6] == pytest.approx(2.0, rel=1e-9)


# --- pipeline -----------------------------------------------------------------


def test_pipeline_flags_spike_and_produces_episodes():
    t, x = series(3000)
    x[2000] += 3.0
    proc = ChannelProcessor("c1", ChannelParams(episode_gap_us=5 * SEC))
    res1 = proc.process(t[:1500], x[:1500])
    res2 = proc.process(t[1500:], x[1500:])
    assert res2.anomaly[500] and res2.substituted[500]
    eps = res1.episodes + res2.episodes + proc.flush()
    assert any(e.kind == "anomaly" and e.ts_start <= t[2000] <= e.ts_end for e in eps)
    # causality: predictions exist and are made one step ahead
    assert np.isnan(res1.pred[:30]).all() and not np.isnan(res2.pred).any()


def test_streaming_pipeline_equals_single_batch():
    t, x = series(2000, seed=5)
    a = ChannelProcessor("c").process(t, x)
    p = ChannelProcessor("c")
    parts = [p.process(t[s : s + 137], x[s : s + 137]) for s in range(0, 2000, 137)]
    np.testing.assert_array_equal(a.anomaly, np.concatenate([r.anomaly for r in parts]))
    np.testing.assert_allclose(a.filtered, np.concatenate([r.filtered for r in parts]))


# --- streaming LTTB -----------------------------------------------------------


def test_streaming_grid_lttb_is_stable():
    t, x = series(3000, step=SEC // 10)
    delta = StreamingGridLTTB.grid_delta(60 * SEC, 300)
    assert delta == 200_000
    s1 = StreamingGridLTTB(delta)
    s1.push(t, x)
    full = s1.drain()
    s2 = StreamingGridLTTB(delta)
    inc = []
    for i in range(0, 3000, 7):
        s2.push(t[i : i + 7], x[i : i + 7])
        inc += s2.drain()
    assert full == inc  # independent of how data was chunked
    ts = [p[0] for p in full]
    assert all(b - a > 0 for a, b in zip(ts, ts[1:]))
    assert len({p[0] // delta for p in full}) == len(full)  # one point per grid cell


# --- alarms -------------------------------------------------------------------


def test_alarm_lifecycle_with_hysteresis():
    rule = AlarmRule(1, "c", AlarmKind.HI, limit=10.0, deadband=1.0)
    rt = AlarmRuntime()
    assert evaluate(rule, rt, 1, 9.0) is None
    tr = evaluate(rule, rt, 2, 10.5)
    assert tr.new == AlarmState.UNACK_ACTIVE
    assert evaluate(rule, rt, 3, 9.5) is None  # inside deadband: stays active
    tr = evaluate(rule, rt, 4, 8.9)
    assert tr.new == AlarmState.UNACK_RTN
    tr = acknowledge(rule, rt, 5)
    assert tr.new == AlarmState.NORMAL


def test_alarm_on_delay_and_ack_active():
    rule = AlarmRule(2, "c", AlarmKind.LO, limit=0.0, on_delay_us=3)
    rt = AlarmRuntime()
    assert evaluate(rule, rt, 10, -1) is None
    assert evaluate(rule, rt, 12, -1) is None
    assert evaluate(rule, rt, 13, -1).new == AlarmState.UNACK_ACTIVE
    assert acknowledge(rule, rt, 14).new == AlarmState.ACK_ACTIVE
    assert evaluate(rule, rt, 15, 1).new == AlarmState.NORMAL
