from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.api.deps import get_state, parse_ts, split_csv
from app.db.models import Event, ModeChange
from app.schemas import EventOut, ModeChangeIn, ModeChangeOut
from app.services.journal import list_events
from app.state import AppState

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events", response_model=list[EventOut])
async def get_events(
    channels: str | None = None,
    t_from: str | None = Query(None, alias="from"),
    t_to: str | None = Query(None, alias="to"),
    kind: str | None = Query(None, description="anomaly,outlier"),
    run: str = "online",
    limit: int = Query(1000, le=10_000),
    st: AppState = Depends(get_state),
):
    async with st.pg() as s:
        return await list_events(
            s, split_csv(channels), parse_ts(t_from, "from"), parse_ts(t_to, "to"), run, split_csv(kind), limit
        )


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: int, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        ev = await s.get(Event, event_id)
    if ev is None:
        raise HTTPException(404, "event not found")
    return ev


@router.get("/modes", response_model=list[ModeChangeOut])
async def get_modes(
    robot: str | None = None,
    t_from: str | None = Query(None, alias="from"),
    t_to: str | None = Query(None, alias="to"),
    st: AppState = Depends(get_state),
):
    q = select(ModeChange)
    if robot:
        q = q.where(ModeChange.robot == robot)
    if (a := parse_ts(t_from, "from")) is not None:
        q = q.where(ModeChange.ts >= a)
    if (b := parse_ts(t_to, "to")) is not None:
        q = q.where(ModeChange.ts < b)
    async with st.pg() as s:
        return (await s.execute(q.order_by(ModeChange.ts))).scalars().all()


@router.post("/modes", response_model=ModeChangeOut, status_code=201)
async def add_mode(body: ModeChangeIn, st: AppState = Depends(get_state)):
    async with st.pg() as s:
        m = ModeChange(**body.model_dump())
        s.add(m)
        await s.commit()
        await s.refresh(m)
    return m
