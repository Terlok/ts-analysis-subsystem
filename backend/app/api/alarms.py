"""Alarm rules, alarm list/journal and acknowledgement (ФВ-3.4, ФВ-2.4).

Alarm state is owned by the analytics worker (it evaluates the rules on the stream),
so acknowledgement is sent to it as a command through a Redis stream; the worker
applies the ISA-18.2 transition, writes the journal and publishes the new state.
"""

from __future__ import annotations

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.api.deps import get_state, parse_ts, split_csv
from app.db.models import Alarm, AlarmRule, Channel
from app.db.redis_store import ALARM_CMD_STREAM, CONFIG_VERSION_KEY
from app.schemas import AckRequest, AlarmLogOut, AlarmOut, AlarmRuleIn, AlarmRuleOut
from app.services.journal import alarm_log, list_alarms
from app.state import AppState
from tsa_core.alarms import AlarmState

router = APIRouter(prefix="/api/alarms", tags=["alarms"])


@router.get("", response_model=list[AlarmOut])
async def get_alarms(
    channels: str | None = None,
    state: str | None = Query(None, description="normal,unack_active,ack_active,unack_rtn"),
    active: bool = Query(False, description="only alarms that are not normal"),
    t_from: str | None = Query(None, alias="from"),
    t_to: str | None = Query(None, alias="to"),
    limit: int = Query(500, le=5000),
    st: AppState = Depends(get_state),
):
    states = split_csv(state)
    if active:
        states = [s.value for s in AlarmState if s != AlarmState.NORMAL]
    async with st.pg() as s:
        return await list_alarms(s, split_csv(channels), parse_ts(t_from, "from"), parse_ts(t_to, "to"), states, limit)


@router.get("/log", response_model=list[AlarmLogOut])
async def get_alarm_log(
    alarm_id: int | None = None,
    channels: str | None = None,
    limit: int = Query(500, le=5000),
    st: AppState = Depends(get_state),
):
    async with st.pg() as s:
        return await alarm_log(s, alarm_id, split_csv(channels), limit)


@router.post("/{alarm_id}/ack", status_code=202)
async def ack_alarm(alarm_id: int, body: AckRequest, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        alarm = await s.get(Alarm, alarm_id)
    if alarm is None:
        raise HTTPException(404, "alarm not found")
    if alarm.state not in (AlarmState.UNACK_ACTIVE, AlarmState.UNACK_RTN):
        raise HTTPException(409, f"alarm is in state {alarm.state}, nothing to acknowledge")
    cmd_id = await st.redis.xadd(
        ALARM_CMD_STREAM,
        {b"c": orjson.dumps({"op": "ack", "alarm_id": alarm_id, "rule_id": alarm.rule_id, "user": body.user})},
        maxlen=10_000,
        approximate=True,
    )
    return {"accepted": True, "command_id": cmd_id.decode() if isinstance(cmd_id, bytes) else cmd_id}


# --- rules --------------------------------------------------------------------------


@router.get("/rules", response_model=list[AlarmRuleOut])
async def get_rules(channels: str | None = None, st: AppState = Depends(get_state)):
    q = select(AlarmRule).order_by(AlarmRule.id)
    if chs := split_csv(channels):
        q = q.where(AlarmRule.channel_id.in_(chs))
    async with st.pg() as s:
        return (await s.execute(q)).scalars().all()


@router.post("/rules", response_model=AlarmRuleOut, status_code=201)
async def create_rule(body: AlarmRuleIn, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        if await s.get(Channel, body.channel_id) is None:
            raise HTTPException(404, "channel not found")
        rule = AlarmRule(**body.model_dump())
        s.add(rule)
        await s.commit()
        await s.refresh(rule)
    await st.redis.incr(CONFIG_VERSION_KEY)
    return rule


@router.put("/rules/{rule_id}", response_model=AlarmRuleOut)
async def update_rule(rule_id: int, body: AlarmRuleIn, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        rule = await s.get(AlarmRule, rule_id)
        if rule is None:
            raise HTTPException(404, "rule not found")
        for k, v in body.model_dump().items():
            setattr(rule, k, v)
        await s.commit()
        await s.refresh(rule)
    await st.redis.incr(CONFIG_VERSION_KEY)
    return rule


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        rule = await s.get(AlarmRule, rule_id)
        if rule is None:
            raise HTTPException(404, "rule not found")
        await s.delete(rule)
        await s.commit()
    await st.redis.incr(CONFIG_VERSION_KEY)
