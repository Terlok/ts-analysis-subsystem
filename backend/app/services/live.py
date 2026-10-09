"""Live delivery to browsers over WebSocket (сценарій С1, ФВ-4.4, ФВ-5.2).

One hub per API process:
  * reads the ingest stream with plain XREAD (fan-out, no consumer group) and feeds
    the per-subscription grid-anchored streaming LTTB of every channel;
  * relays diagnostic results from Redis Pub/Sub (events, alarms);
  * every 1/rate seconds sends each subscriber only what changed: finalized points
    (stable, sent once) and the provisional tail.
Slow clients are conflated: nothing is queued per tick, the next tick simply sends
the accumulated state, and finalized points older than the window are dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time

import orjson
from fastapi import WebSocket
from redis.asyncio import Redis

from app.config import Settings
from app.db.redis_store import (
    PUBSUB_ALARMS,
    PUBSUB_EVENTS,
    STREAM_FIELD,
    decode_packet,
    latency_key,
    packet_arrays,
)
from app.services.hot import read_hot, watermarks
from tsa_core.downsample import StreamingGridLTTB

log = logging.getLogger(__name__)


def _now_us() -> int:
    return time.time_ns() // 1000


class _ChannelView:
    def __init__(self, delta: int, window_us: int):
        self.lttb = StreamingGridLTTB(delta)
        self.window_us = window_us
        self.t_ing_new: int | None = None  # earliest ingest time of data not yet sent

    def pending(self) -> tuple[list, list]:
        fin = self.lttb.drain()
        if fin and len(fin) > 1:  # conflation: never send more than the window
            horizon = fin[-1][0] - self.window_us
            fin = [p for p in fin if p[0] >= horizon]
        return fin, self.lttb.tail()


class Subscription:
    def __init__(self, ws: WebSocket, channels: list[str], window_s: float, width_px: int, c: float):
        self.ws = ws
        self.channels = channels
        self.window_us = int(window_s * 1_000_000)
        self.m = max(3, math.ceil(c * width_px))
        self.delta = StreamingGridLTTB.grid_delta(self.window_us, self.m)
        self.views = {ch: _ChannelView(self.delta, self.window_us) for ch in channels}
        self.outbox: list[dict] = []  # events/alarms to send on the next tick
        self.dirty = False
        self.sending = False


class LiveHub:
    def __init__(self, settings: Settings, redis: Redis):
        self.settings = settings
        self.redis = redis
        self.subs: set[Subscription] = set()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._stream_loop(), name="live-stream"),
            asyncio.create_task(self._pubsub_loop(), name="live-pubsub"),
            asyncio.create_task(self._tick_loop(), name="live-tick"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # --- subscriptions --------------------------------------------------------------

    async def subscribe(self, sub: Subscription) -> None:
        """Seed views from the hot window and send the initial snapshot."""
        s = self.settings
        wms = await watermarks(self.redis, sub.channels)
        snapshot = []
        for ch, view in sub.views.items():
            wm = wms.get(ch)
            if wm is not None:
                ts, val, _q = await read_hot(self.redis, ch, wm - sub.window_us, wm + 1, s.hot_batch_max_span_s * 1_000_000)
                view.lttb.push(ts, val)
            fin, tail = view.pending()
            snapshot.append({"channel": ch, "t": [p[0] for p in fin], "v": [p[1] for p in fin],
                             "tail_t": [p[0] for p in tail], "tail_v": [p[1] for p in tail]})
        self.subs.add(sub)
        await sub.ws.send_bytes(orjson.dumps({
            "type": "snapshot", "delta_us": sub.delta, "window_us": sub.window_us, "m": sub.m,
            "series": snapshot, "t_send": _now_us(),
        }))

    def unsubscribe(self, sub: Subscription) -> None:
        self.subs.discard(sub)

    # --- background loops -------------------------------------------------------

    async def _stream_loop(self) -> None:
        last_id = "$"
        while True:
            try:
                resp = await self.redis.xread({self.settings.stream_key: last_id}, count=500, block=1000)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("live: XREAD failed")
                await asyncio.sleep(1)
                continue
            for _stream, entries in resp or []:
                for entry_id, fields in entries:
                    last_id = entry_id
                    self._dispatch(decode_packet(fields[STREAM_FIELD]))

    def _dispatch(self, packet: dict) -> None:
        if not self.subs:
            return
        t_ing = packet.get("t_ing")
        for ch in packet["ch"]:
            cid = ch["id"]
            arrays = None
            for sub in self.subs:
                view = sub.views.get(cid)
                if view is None:
                    continue
                if arrays is None:
                    arrays = packet_arrays(ch)
                view.lttb.push(arrays[0], arrays[1])
                if t_ing is not None and view.t_ing_new is None:
                    view.t_ing_new = t_ing
                sub.dirty = True

    async def _pubsub_loop(self) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(PUBSUB_EVENTS, PUBSUB_ALARMS)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    data = orjson.loads(msg["data"])
                except orjson.JSONDecodeError:
                    continue
                ch = data.get("channel")
                for sub in self.subs:
                    if ch in sub.views:
                        sub.outbox.append(data)
                        sub.dirty = True
        finally:
            await pubsub.aclose()

    async def _tick_loop(self) -> None:
        period = 1.0 / self.settings.live_rate_hz
        while True:
            await asyncio.sleep(period)
            for sub in list(self.subs):
                if sub.dirty and not sub.sending:
                    sub.sending = True
                    asyncio.create_task(self._send_update(sub))

    async def _send_update(self, sub: Subscription) -> None:
        try:
            sub.dirty = False
            series = []
            t_ing_min = None
            for ch, view in sub.views.items():
                fin, tail = view.pending()
                if not fin and not tail:
                    continue
                series.append({"channel": ch, "t": [p[0] for p in fin], "v": [p[1] for p in fin],
                               "tail_t": [p[0] for p in tail], "tail_v": [p[1] for p in tail]})
                # latency of this update: oldest data in it, not the last packet of a slow channel
                if view.t_ing_new is not None:
                    t_ing_min = view.t_ing_new if t_ing_min is None else min(t_ing_min, view.t_ing_new)
                    view.t_ing_new = None
            events, sub.outbox = sub.outbox, []
            t_send = _now_us()
            msg = {"type": "update", "series": series, "events": events, "t_send": t_send, "t_ing": t_ing_min}
            await asyncio.wait_for(sub.ws.send_bytes(orjson.dumps(msg, option=orjson.OPT_SERIALIZE_NUMPY)), timeout=5)
            if t_ing_min is not None:  # server-side part of the end-to-end latency
                await self.redis.lpush(latency_key("ing_to_send"), t_send - t_ing_min)
                await self.redis.ltrim(latency_key("ing_to_send"), 0, 9_999)
        except Exception:  # noqa: BLE001 - client gone or too slow
            self.unsubscribe(sub)
        finally:
            sub.sending = False

