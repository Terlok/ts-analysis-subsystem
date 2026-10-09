"""Analytics worker: causal Hampel filter -> window features -> combined diagnostics
(forecast residual + optional classifier) -> events and alarms. Runs on full-resolution
data before any downsampling, so diagnostics do not depend on the chart detail (1.16).

    python -m workers.analytics [--shard i --shards n]

Outputs:
  * QuestDB `point_flags` (run='online'): substituted / anomalous points with
    filtered value, forecast, residual, threshold, probability;
  * PostgreSQL `events`: episodes of consecutive flagged points, written when closed;
  * PostgreSQL `alarms` / `alarm_log`: ISA-18.2 alarm life cycle;
  * Redis Pub/Sub `events`, `alarms`: notifications for live clients;
  * latency samples (ingest -> result) and load metrics rho = lambda * tau.
"""

from __future__ import annotations

import logging
import time

import numpy as np
import orjson
import redis
from sqlalchemy import insert, select, update

from app.config import Settings
from app.db.models import Alarm as AlarmRow
from app.db.models import AlarmLog, Channel, Event, ModelVersion
from app.db.models import AlarmRule as AlarmRuleRow
from app.db.postgres import make_sync_engine, make_sync_sessionmaker
from app.db.questdb import IlpWriter
from app.db.redis_store import (
    ALARM_CMD_STREAM,
    CONFIG_VERSION_KEY,
    MODELS_VERSION_KEY,
    PUBSUB_ALARMS,
    PUBSUB_EVENTS,
    latency_key,
    packet_arrays,
)
from app.services.models_store import load_active
from tsa_core.alarms import AlarmKind, AlarmRule, AlarmRuntime, AlarmState, AlarmTransition, acknowledge, evaluate
from tsa_core.detectors import Episode
from tsa_core.pipeline import BatchResult, ChannelParams, ChannelProcessor
from workers.common import LoadMeter, StreamConsumer, settings, setup_logging, shard_of, worker_args

log = logging.getLogger("analytics")
RUN = "online"


def _num(v) -> float | None:
    v = float(v)
    return None if v != v or v in (np.inf, -np.inf) else v


def _now_us() -> int:
    return time.time_ns() // 1000


class AlarmManager:
    """Owns the runtime state of alarm rules of this shard and persists transitions."""

    def __init__(self, sm, r: redis.Redis):
        self.sm = sm
        self.r = r
        self.rules: dict[int, AlarmRule] = {}
        self.by_channel: dict[str, list[AlarmRule]] = {}
        self.runtime: dict[int, AlarmRuntime] = {}
        self.open_alarm: dict[int, int] = {}  # rule id -> alarm row id of the current occurrence

    def load(self, rows: list[AlarmRuleRow], in_shard) -> None:
        self.rules = {
            r.id: AlarmRule(
                id=r.id, channel=r.channel_id, kind=AlarmKind(r.kind), limit=r.limit, deadband=r.deadband or 0.0,
                on_delay_us=r.on_delay_ms * 1000, off_delay_us=r.off_delay_ms * 1000,
                min_repeat_us=r.min_repeat_ms * 1000, priority=r.priority, enabled=r.enabled,
            )
            for r in rows
            if in_shard(r.channel_id)
        }
        self.by_channel = {}
        for rule in self.rules.values():
            self.by_channel.setdefault(rule.channel, []).append(rule)
        # restore states of occurrences that are not finished yet
        with self.sm() as s:
            open_rows = s.execute(
                select(AlarmRow).where(AlarmRow.state != AlarmState.NORMAL.value, AlarmRow.rule_id.in_(list(self.rules)))
            ).scalars().all()
        for a in open_rows:
            rt = self.runtime.setdefault(a.rule_id, AlarmRuntime())
            rt.state = AlarmState(a.state)
            rt.condition = rt.state in (AlarmState.UNACK_ACTIVE, AlarmState.ACK_ACTIVE)
            rt.last_activation = a.ts_active
            self.open_alarm[a.rule_id] = a.id
        for rid in list(self.runtime):
            if rid not in self.rules:
                del self.runtime[rid]

    def evaluate(self, channel: str, res: BatchResult) -> None:
        rules = self.by_channel.get(channel)
        if not rules:
            return
        transitions: list[AlarmTransition] = []
        for rule in rules:
            rt = self.runtime.setdefault(rule.id, AlarmRuntime())
            # limits are checked on filtered values: single measurement spikes are handled
            # by the outlier/anomaly layer, not by limit alarms
            values = res.filtered
            for i in range(len(res.ts)):
                tr = evaluate(rule, rt, int(res.ts[i]), float(values[i]), bool(res.anomaly[i]))
                if tr:
                    transitions.append(tr)
        if transitions:
            self.persist(transitions)

    def command(self, cmd: dict) -> None:
        rule = self.rules.get(cmd.get("rule_id"))
        if rule is None:
            return  # rule of another shard
        rt = self.runtime.setdefault(rule.id, AlarmRuntime())
        tr = acknowledge(rule, rt, _now_us())
        if tr:
            self.persist([tr], user=cmd.get("user"))

    def persist(self, transitions: list[AlarmTransition], user: str | None = None) -> None:
        with self.sm() as s:
            for tr in transitions:
                rule = self.rules[tr.rule_id]
                alarm_id = self.open_alarm.get(tr.rule_id)
                if tr.reason == "activated" and tr.prev == AlarmState.NORMAL or alarm_id is None:
                    alarm_id = s.execute(
                        insert(AlarmRow).values(
                            rule_id=rule.id, channel_id=rule.channel, state=tr.new.value, priority=rule.priority,
                            ts_active=tr.ts, value=tr.value,
                        ).returning(AlarmRow.id)
                    ).scalar_one()
                    self.open_alarm[tr.rule_id] = alarm_id
                else:
                    vals: dict = {"state": tr.new.value}
                    if tr.reason == "returned":
                        vals["ts_return"] = tr.ts
                    elif tr.reason == "acknowledged":
                        vals["ts_ack"] = tr.ts
                        vals["acked_by"] = user
                    elif tr.reason == "activated":  # re-activation from UNACK_RTN
                        vals["ts_return"] = None
                    s.execute(update(AlarmRow).where(AlarmRow.id == alarm_id).values(**vals))
                s.execute(
                    insert(AlarmLog).values(
                        alarm_id=alarm_id, rule_id=rule.id, channel_id=rule.channel, ts=tr.ts,
                        prev_state=tr.prev.value, new_state=tr.new.value, reason=tr.reason, value=tr.value, user=user,
                    )
                )
                if tr.new == AlarmState.NORMAL:
                    self.open_alarm.pop(tr.rule_id, None)
                self.r.publish(PUBSUB_ALARMS, orjson.dumps({
                    "type": "alarm", "channel": rule.channel, "alarm_id": alarm_id, "rule_id": rule.id,
                    "kind": rule.kind.value, "priority": rule.priority, "ts": tr.ts, "state": tr.new.value,
                    "prev_state": tr.prev.value, "reason": tr.reason, "value": tr.value,
                }))
            s.commit()


class Analytics:
    def __init__(self, s: Settings, shard: int, shards: int, consumer_name: str):
        self.s = s
        self.shard, self.shards = shard, shards
        self.engine = make_sync_engine(s)
        self.sm = make_sync_sessionmaker(self.engine)
        self.writer = IlpWriter(s)
        self.processors: dict[str, ChannelProcessor] = {}
        self.channel_meta: dict[str, Channel] = {}
        self.models: dict = {}
        self.config_version = None
        self.models_version = None
        self.last_reload = 0.0
        group = f"analytics-{shard}of{shards}"
        self.consumer = StreamConsumer(s, group, consumer_name, self.handle, tick=self.tick, count=s.analytics_batch,
                                       on_error=self.on_error)
        self.r = self.consumer.r
        self.alarms = AlarmManager(self.sm, self.r)
        self.meter = LoadMeter(self.r, f"analytics:{shard}of{shards}")
        self.cmd_group = f"alarm-cmd-{shard}of{shards}"
        try:
            self.r.xgroup_create(ALARM_CMD_STREAM, self.cmd_group, id="$", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        self.reload(force=True)

    def in_shard(self, channel: str) -> bool:
        return shard_of(channel, self.shards) == self.shard

    # --- configuration ----------------------------------------------------------

    def params_for(self, ch: Channel | None) -> ChannelParams:
        d = self.s.default_channel_params()
        if ch is None:
            return d

        def pick(v, default):
            return default if v is None else v

        return ChannelParams(
            hampel_window=pick(ch.hampel_window, d.hampel_window),
            hampel_kappa=pick(ch.hampel_kappa, d.hampel_kappa),
            hampel_min_sigma=pick(ch.hampel_min_sigma, d.hampel_min_sigma),
            feature_window=pick(ch.feature_window, d.feature_window),
            residual_k=pick(ch.residual_k, d.residual_k),
            episode_gap_us=d.episode_gap_us,
        )

    def reload(self, force: bool = False) -> None:
        cfg_v = self.r.get(CONFIG_VERSION_KEY)
        mdl_v = self.r.get(MODELS_VERSION_KEY)
        periodic = time.monotonic() - self.last_reload > 60
        if not (force or periodic or cfg_v != self.config_version or mdl_v != self.models_version):
            return
        with self.sm() as s:
            self.channel_meta = {c.id: c for c in s.execute(select(Channel)).scalars().all() if self.in_shard(c.id)}
            rules = s.execute(select(AlarmRuleRow)).scalars().all()
            model_rows = s.execute(select(ModelVersion).where(ModelVersion.active.is_(True))).scalars().all()
        if force or cfg_v != self.config_version:
            self.alarms.load(list(rules), self.in_shard)
            for cid, proc in list(self.processors.items()):
                if proc.params != self.params_for(self.channel_meta.get(cid)):
                    del self.processors[cid]  # rebuilt with new parameters on next data
        if force or mdl_v != self.models_version:
            self.models = load_active([m for m in model_rows if self.in_shard(m.channel_id)], self.s.models_dir)
            for cid, proc in self.processors.items():
                proc.set_models(*self.models.get(cid, (None, None)))
            log.info("models loaded for %d channel(s)", len(self.models))
        self.config_version, self.models_version = cfg_v, mdl_v
        self.last_reload = time.monotonic()

    def processor(self, cid: str) -> ChannelProcessor | None:
        p = self.processors.get(cid)
        if p is None:
            meta = self.channel_meta.get(cid)
            if meta is not None and not meta.analytics_enabled:
                return None
            f, c = self.models.get(cid, (None, None))
            p = self.processors[cid] = ChannelProcessor(cid, self.params_for(meta), f, c)
        return p

    # --- processing -------------------------------------------------------------

    def handle(self, batch) -> None:
        t0 = time.perf_counter()
        n = 0
        episodes: list[tuple[str, Episode]] = []
        notify: list[bytes] = []
        t_ing_min = None
        for _id, pkt in batch:
            t_ing = pkt["t_ing"]
            t_ing_min = t_ing if t_ing_min is None else min(t_ing_min, t_ing)
            for ch in pkt["ch"]:
                cid = ch["id"]
                if not self.in_shard(cid):
                    continue
                proc = self.processor(cid)
                if proc is None:
                    continue
                ts, val, q, _nd, _otkl = packet_arrays(ch)
                valid = np.array([x != "bad" for x in q], dtype=bool) & ~np.isnan(val)
                res = proc.process(ts, val, valid)
                n += len(ts)
                self.write_flags(cid, res, notify)
                episodes += [(cid, e) for e in res.episodes]
                self.alarms.evaluate(cid, res)
        if episodes:
            self.write_episodes(episodes)
        self.writer.flush()
        for msg in notify:
            self.r.publish(PUBSUB_EVENTS, msg)
        busy = time.perf_counter() - t0
        self.meter.record(n, busy)
        if t_ing_min is not None:
            self.r.lpush(latency_key("proc"), _now_us() - t_ing_min)
            self.r.ltrim(latency_key("proc"), 0, 9_999)

    def write_flags(self, cid: str, res: BatchResult, notify: list[bytes]) -> None:
        idx = np.flatnonzero(res.flagged)
        if not len(idx):
            return
        rows = [
            {
                "ts": int(res.ts[i]), "val": res.raw[i], "val_f": res.filtered[i], "median": res.median[i],
                "pred": res.pred[i], "resid": res.resid[i], "thr": res.threshold[i], "prob": res.prob[i],
                "substituted": bool(res.substituted[i]), "anomaly": bool(res.anomaly[i]),
            }
            for i in idx
        ]
        self.writer.point_flags(cid, RUN, rows)
        notify.append(orjson.dumps({
            "type": "flags", "channel": cid,
            "points": [{"ts": r["ts"], "val": _num(r["val"]), "val_f": _num(r["val_f"]),
                        "substituted": r["substituted"], "anomaly": r["anomaly"]} for r in rows],
        }))

    def write_episodes(self, episodes: list[tuple[str, Episode]]) -> None:
        values = [
            {
                "channel_id": cid, "kind": e.kind, "ts_start": e.ts_start, "ts_end": e.ts_end,
                "n_points": e.n_points, "peak_ts": e.peak_ts, "peak_value": _num(e.peak_value),
                "score": _num(e.peak_score), "run": RUN, "model_version": e.details.get("model"),
                "details": e.details,
            }
            for cid, e in episodes
        ]
        with self.sm() as s:
            # executemany: SQLAlchemy batches the rows ("insertmanyvalues"), so a burst of
            # thousands of episodes (history import) stays within PostgreSQL's 65535 parameters
            ids = s.scalars(insert(Event).returning(Event.id, sort_by_parameter_order=True), values).all()
            s.commit()
        for eid, v in zip(ids, values):
            self.r.publish(PUBSUB_EVENTS, orjson.dumps({"type": "event", "channel": v["channel_id"], "id": eid, **v}))

    def tick(self) -> None:
        self.reload()
        resp = self.r.xreadgroup(self.cmd_group, self.consumer.consumer, {ALARM_CMD_STREAM: ">"}, count=50)
        for _stream, entries in resp or []:
            for entry_id, fields in entries:
                try:
                    self.alarms.command(orjson.loads(fields[b"c"]))
                finally:
                    self.r.xack(ALARM_CMD_STREAM, self.cmd_group, entry_id)

    def on_error(self, _e) -> None:
        # pending messages will be re-read: restart channel state so points are not fed twice
        self.processors.clear()
        try:
            self.writer.sender.close()
            self.writer = IlpWriter(self.s)
        except Exception:  # noqa: BLE001
            log.warning("QuestDB is still unavailable")

    def run(self) -> None:
        try:
            self.consumer.run()
        finally:
            closed = [(cid, e) for cid, p in self.processors.items() for e in p.flush()]
            if closed:
                try:
                    self.write_episodes(closed)
                except Exception:  # noqa: BLE001
                    log.exception("cannot write open episodes on shutdown")
            self.writer.close()


def main() -> None:
    setup_logging()
    args = worker_args("Analytics worker")
    Analytics(settings(), args.shard, args.shards, args.consumer).run()


if __name__ == "__main__":
    main()
