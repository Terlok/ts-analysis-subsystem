from __future__ import annotations

import orjson
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.api.deps import get_state
from app.db.models import Channel
from app.db.redis_store import CONFIG_VERSION_KEY, LAST_KEY
from app.schemas import ChannelCreate, ChannelIn, ChannelOut, ChannelState, ChannelStats
from app.state import AppState

router = APIRouter(prefix="/api/channels", tags=["channels"])


@router.get("", response_model=list[ChannelOut])
async def list_channels(st: AppState = Depends(get_state)):
    async with st.pg() as s:
        rows = (await s.execute(select(Channel).order_by(Channel.id))).scalars().all()
    return rows


@router.get("/state", response_model=list[ChannelState])
async def channels_state(st: AppState = Depends(get_state)):
    """Last value of every tag (operational DB)."""
    raw = await st.redis.hgetall(LAST_KEY)
    out = []
    for k, v in raw.items():
        d = orjson.loads(v)
        out.append(ChannelState(id=k.decode(), last_ts=d.get("ts"), last_val=d.get("val"), last_quality=d.get("q")))
    return sorted(out, key=lambda c: c.id)


@router.get("/stats", response_model=list[ChannelStats])
async def channels_stats(st: AppState = Depends(get_state)):
    """Time range and number of archived points of every channel (overview navigator, archive mode)."""
    rows = await st.qdb.channel_stats()
    return [ChannelStats(id=c, count=n, first_ts=a, last_ts=b) for c, n, a, b in sorted(rows)]


@router.get("/{channel_id}", response_model=ChannelOut)
async def get_channel(channel_id: str, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        ch = await s.get(Channel, channel_id)
    if ch is None:
        raise HTTPException(404, "channel not found")
    return ch


@router.post("", response_model=ChannelOut, status_code=201)
async def create_channel(body: ChannelCreate, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        if await s.get(Channel, body.id) is not None:
            raise HTTPException(409, "channel already exists")
        ch = Channel(**body.model_dump(exclude_none=True))
        s.add(ch)
        await s.commit()
        await s.refresh(ch)
    st.registry.invalidate()
    await st.redis.incr(CONFIG_VERSION_KEY)
    return ch


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(channel_id: str, body: ChannelIn, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        ch = await s.get(Channel, channel_id)
        if ch is None:
            raise HTTPException(404, "channel not found")
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(ch, k, v)
        ch.auto_registered = False
        await s.commit()
        await s.refresh(ch)
    st.registry.invalidate()
    await st.redis.incr(CONFIG_VERSION_KEY)
    return ch


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(channel_id: str, st: AppState = Depends(get_state)):
    """Removes the channel from the registry only; archived data stays in QuestDB."""
    async with st.pg() as s:
        ch = await s.get(Channel, channel_id)
        if ch is None:
            raise HTTPException(404, "channel not found")
        await s.delete(ch)
        await s.commit()
    st.registry.invalidate()
    await st.redis.incr(CONFIG_VERSION_KEY)
