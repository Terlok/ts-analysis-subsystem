"""Shared machinery of the stream workers.

Every worker is a separate OS process (no GIL contention with the API event loop)
reading the ingest stream through its own Redis consumer group:

    archiver           group "archiver"           stateless, may run N copies
    analytics --shard  group "analytics-{i}of{n}" stateful per channel: each shard
    aggregator --shard group "aggregator-{i}of{n}" handles channels with crc32(id) % n == i

Messages are acknowledged only after their results are durably written, and pending
(unacknowledged) messages are re-processed after a restart, so no confirmed packet is
lost (НФВ-7); idempotent writes (QuestDB DEDUP) make re-processing safe.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import time
import zlib
from typing import Callable

import redis

from app.config import Settings, get_settings
from app.db.redis_store import STREAM_FIELD, decode_packet

log = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def shard_of(channel: str, shards: int) -> int:
    return zlib.crc32(channel.encode()) % shards


def worker_args(description: str, sharded: bool = True) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=description)
    if sharded:
        p.add_argument("--shard", type=int, default=0, help="index of this shard (0-based)")
        p.add_argument("--shards", type=int, default=1, help="total number of shards N_w")
    p.add_argument("--consumer", default=f"{socket.gethostname()}-{os.getpid()}", help="consumer name in the group")
    return p.parse_args()


Handler = Callable[[list[tuple[bytes, dict]]], None]


class StreamConsumer:
    def __init__(
        self,
        settings: Settings,
        group: str,
        consumer: str,
        handler: Handler,
        tick: Callable[[], None] | None = None,
        count: int = 200,
        block_ms: int = 500,
        ack_after_handler: bool = True,
        on_error: Callable[[Exception], None] | None = None,
    ):
        self.settings = settings
        self.r = redis.Redis.from_url(settings.redis_url)
        self.stream = settings.stream_key
        self.group = group
        self.consumer = consumer
        self.handler = handler
        self.tick = tick
        self.count = count
        self.block_ms = block_ms
        self.ack_after_handler = ack_after_handler
        self.on_error = on_error
        self._stop = False

    def ensure_group(self) -> None:
        try:
            # "0": a new group starts from the beginning of the retained stream
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
            log.info("created consumer group %s", self.group)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def ack(self, ids: list[bytes]) -> None:
        if ids:
            self.r.xack(self.stream, self.group, *ids)

    def stop(self, *_):
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.ensure_group()
        cursor = "0"  # first re-process own pending messages, then switch to new ones
        log.info("consumer %s/%s started on %s", self.group, self.consumer, self.stream)
        while not self._stop:
            try:
                resp = self.r.xreadgroup(
                    self.group, self.consumer, {self.stream: cursor}, count=self.count,
                    block=None if cursor == "0" else self.block_ms,
                )
            except redis.ConnectionError:
                log.warning("redis unavailable, retrying")
                time.sleep(1)
                continue
            entries = resp[0][1] if resp else []
            if cursor == "0" and not entries:
                cursor = ">"
            batch = []
            for entry_id, fields in entries:
                if not fields:  # pending entry already trimmed from the stream
                    self.ack([entry_id])
                    continue
                batch.append((entry_id, decode_packet(fields[STREAM_FIELD])))
            try:
                if batch:
                    self.handler(batch)
                    if self.ack_after_handler:
                        self.ack([e for e, _ in batch])
                if self.tick:
                    self.tick()
            except Exception as e:  # noqa: BLE001 - storage outage: keep messages pending and retry
                log.exception("%s: processing failed, will re-read pending messages", self.group)
                if self.on_error:
                    self.on_error(e)
                time.sleep(2)
                cursor = "0"
        log.info("consumer %s/%s stopping", self.group, self.consumer)


class LoadMeter:
    """Measures lambda (points/s), tau (s per point) and rho_load = lambda*tau (per worker)."""

    def __init__(self, r: redis.Redis, name: str, period_s: float = 5.0):
        self.r = r
        self.key = f"metrics:worker:{name}"
        self.period = period_s
        self._t0 = time.monotonic()
        self._points = 0
        self._busy = 0.0
        self.total = 0

    def record(self, points: int, busy_s: float) -> None:
        self._points += points
        self._busy += busy_s
        self.total += points
        now = time.monotonic()
        if now - self._t0 >= self.period:
            dt = now - self._t0
            lam = self._points / dt
            tau = self._busy / self._points if self._points else 0.0
            self.r.hset(self.key, mapping={
                "lambda_pts_s": round(lam, 2),
                "tau_us": round(tau * 1e6, 3),
                "rho_load": round(lam * tau, 4),
                "points_total": self.total,
                "updated_at": time.time(),
            })
            self.r.expire(self.key, 300)
            self._t0, self._points, self._busy = now, 0, 0.0


def settings() -> Settings:
    return get_settings()
