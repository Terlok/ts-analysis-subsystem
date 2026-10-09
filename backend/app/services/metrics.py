"""Evaluation metrics of the running subsystem (criteria К1, К2, К6 of the thesis)."""

from __future__ import annotations

import numpy as np
from redis.asyncio import Redis

from app.config import Settings
from app.db.redis_store import latency_key

STAGES = {
    "acq": "L_acq: source timestamp -> ingest (needs synchronized clocks / rebased replay)",
    "proc": "L_proc + L_inf: ingest -> analytics result",
    "ing_to_send": "ingest -> WebSocket send (server part of L)",
}


def _percentiles(samples: list[bytes]) -> dict | None:
    if not samples:
        return None
    a = np.array([int(x) for x in samples], dtype=np.float64) / 1000.0  # us -> ms
    p50, p95, p99 = np.percentile(a, [50, 95, 99])
    return {"n": len(a), "p50_ms": round(p50, 3), "p95_ms": round(p95, 3), "p99_ms": round(p99, 3), "max_ms": round(a.max(), 3)}


async def collect(redis: Redis, settings: Settings) -> dict:
    out: dict = {"latency": {}, "workers": {}, "stream": {}, "tiles": {}, "redis": {}}
    for stage, desc in STAGES.items():
        out["latency"][stage] = {"description": desc, "stats": _percentiles(await redis.lrange(latency_key(stage), 0, -1))}

    async for key in redis.scan_iter(match="metrics:worker:*", count=100):
        name = key.decode() if isinstance(key, bytes) else key
        h = await redis.hgetall(key)
        out["workers"][name.removeprefix("metrics:worker:")] = {
            (k.decode() if isinstance(k, bytes) else k): float(v) for k, v in h.items()
        }

    try:
        out["stream"]["length"] = await redis.xlen(settings.stream_key)
        groups = await redis.xinfo_groups(settings.stream_key)
        out["stream"]["groups"] = [
            {"name": g["name"].decode() if isinstance(g["name"], bytes) else g["name"],
             "pending": g.get("pending"), "lag": g.get("lag")}
            for g in groups
        ]
    except Exception:  # noqa: BLE001 - stream does not exist yet
        out["stream"]["groups"] = []

    t = await redis.hgetall("metrics:tiles")
    hit = int(t.get(b"hit", 0))
    miss = int(t.get(b"miss", 0))
    out["tiles"] = {"hit": hit, "miss": miss, "hit_ratio": round(hit / (hit + miss), 4) if hit + miss else None}

    mem = await redis.info("memory")
    out["redis"] = {"used_memory_bytes": mem.get("used_memory"), "maxmemory_bytes": mem.get("maxmemory")}
    return out
