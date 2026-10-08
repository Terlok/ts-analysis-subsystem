"""Reading the hot window (last T_hot seconds of every channel) from Redis."""

from __future__ import annotations

from redis.asyncio import Redis

from app.db.redis_store import WM_KEY, hot_key, merge_hot


async def read_hot(redis: Redis, channel: str, t_from: int, t_to: int, max_span_us: int):
    """Points of [t_from, t_to) from the hot window -> (ts, val, qcodes)."""
    members = await redis.zrangebyscore(hot_key(channel), t_from - max_span_us, t_to)
    return merge_hot(members, t_from, t_to)


async def watermarks(redis: Redis, channels: list[str]) -> dict[str, int | None]:
    if not channels:
        return {}
    vals = await redis.hmget(WM_KEY, channels)
    return {c: (int(v) if v is not None else None) for c, v in zip(channels, vals)}


async def hot_start(redis: Redis, channel: str) -> int | None:
    """Timestamp of the oldest point still in the hot window."""
    first = await redis.zrange(hot_key(channel), 0, 0, withscores=True)
    return int(first[0][1]) if first else None
