"""Queries of the events layer and the alarm journal (PostgreSQL)."""

from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alarm, AlarmLog, Event


async def list_events(
    s: AsyncSession,
    channels: list[str] | None,
    t_from: int | None,
    t_to: int | None,
    run: str = "online",
    kinds: list[str] | None = None,
    limit: int = 1000,
) -> list[Event]:
    q = select(Event).where(Event.run == run)
    if channels:
        q = q.where(Event.channel_id.in_(channels))
    if t_from is not None:
        q = q.where(Event.ts_end >= t_from)
    if t_to is not None:
        q = q.where(Event.ts_start < t_to)
    if kinds:
        q = q.where(Event.kind.in_(kinds))
    q = q.order_by(Event.ts_start.desc()).limit(limit)
    return list((await s.execute(q)).scalars().all())


async def significant_events(
    s: AsyncSession, channels: list[str], t_from: int, t_to: int, run: str = "online", limit: int = 1000
) -> tuple[list[Event], int]:
    """Events of an interval for the chart layer: all of them if they fit into `limit`,
    otherwise the `limit` events with the highest score (spread over the whole interval)."""
    from sqlalchemy import func

    cond = and_(Event.run == run, Event.channel_id.in_(channels), Event.ts_end >= t_from, Event.ts_start < t_to)
    total = (await s.execute(select(func.count()).select_from(Event).where(cond))).scalar_one()
    if total <= limit:
        rows = list((await s.execute(select(Event).where(cond))).scalars().all())
    else:
        # scores of different channels are not comparable: every channel gets its share
        per = max(1, limit // len(channels))
        rows = []
        for ch in channels:
            q = select(Event).where(cond, Event.channel_id == ch).order_by(Event.score.desc().nulls_last()).limit(per)
            rows += list((await s.execute(q)).scalars().all())
    rows.sort(key=lambda e: e.ts_start)
    return rows, int(total)


async def list_alarms(
    s: AsyncSession,
    channels: list[str] | None = None,
    t_from: int | None = None,
    t_to: int | None = None,
    states: list[str] | None = None,
    limit: int = 500,
) -> list[Alarm]:
    q = select(Alarm)
    if channels:
        q = q.where(Alarm.channel_id.in_(channels))
    if states:
        q = q.where(Alarm.state.in_(states))
    if t_to is not None:
        q = q.where(Alarm.ts_active < t_to)
    if t_from is not None:
        q = q.where(or_(Alarm.ts_return.is_(None), Alarm.ts_return >= t_from))
    q = q.order_by(Alarm.ts_active.desc()).limit(limit)
    return list((await s.execute(q)).scalars().all())


async def alarm_log(
    s: AsyncSession, alarm_id: int | None = None, channels: list[str] | None = None, limit: int = 500
) -> list[AlarmLog]:
    conds = []
    if alarm_id is not None:
        conds.append(AlarmLog.alarm_id == alarm_id)
    if channels:
        conds.append(AlarmLog.channel_id.in_(channels))
    q = select(AlarmLog)
    if conds:
        q = q.where(and_(*conds))
    q = q.order_by(AlarmLog.id.desc()).limit(limit)
    return list((await s.execute(q)).scalars().all())
