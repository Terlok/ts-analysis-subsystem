from __future__ import annotations

import csv
import io
import time

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
import orjson
from fastapi.responses import Response, StreamingResponse

from app.api.deps import get_state, parse_range, split_csv
from app.db.redis_store import QUALITY_CODES
from app.schemas import AlarmOut, EventOut, FlagPoint, RawPoint, SeriesResponse
from app.services.hot import read_hot, watermarks
from app.services.journal import list_alarms, significant_events
from app.state import AppState

router = APIRouter(prefix="/api", tags=["data"])

MAX_FLAGS = 5_000
MAX_EVENTS = 1_000


@router.get("/series", response_model=SeriesResponse)
async def get_series(
    channels: str = Query(..., description="comma separated channel ids"),
    t_from: str = Query(..., alias="from", description="us since epoch or ISO 8601"),
    t_to: str = Query(..., alias="to"),
    width: int = Query(1200, ge=10, description="chart width in pixels (W_px)"),
    c: float | None = Query(None, gt=0, le=10, description="detail coefficient"),
    events: bool = True,
    flags: bool = True,
    st: AppState = Depends(get_state),
):
    """Downsampled series for a chart: m = min(N, ceil(c*W_px)) points per channel plus the
    events layer (episodes, point flags, alarms) computed on full-resolution data."""
    a, b = parse_range(t_from, t_to)
    chs = split_csv(channels)
    if not chs:
        raise HTTPException(422, "no channels")
    width = min(width, st.settings.max_width_px)
    t0 = time.perf_counter()
    series, timing = await st.series.get(chs, a, b, width, c)
    resp = SeriesResponse(t_from=a, t_to=b, width_px=width, series=series, timing_ms=timing)
    if events:
        async with st.pg() as s:
            evs, resp.events_total = await significant_events(s, chs, a, b, limit=MAX_EVENTS)
            resp.events = [EventOut.model_validate(e) for e in evs]
            resp.alarms = [AlarmOut.model_validate(x) for x in await list_alarms(s, chs, a, b)]
    if flags:
        fl, resp.flags_total = await st.qdb.fetch_flags_info(chs, a, b, limit=MAX_FLAGS)
        resp.flags = [FlagPoint(**f) for f in fl]
    resp.timing_ms["total"] = round((time.perf_counter() - t0) * 1000, 2)
    return resp


@router.get("/raw")
async def get_raw(
    channel: str,
    t_from: str = Query(..., alias="from"),
    t_to: str = Query(..., alias="to"),
    format: str = Query("json", pattern="^(json|csv)$"),
    st: AppState = Depends(get_state),
):
    """Primary values with quality codes and substitution marks (details on demand, export)."""
    a, b = parse_range(t_from, t_to)
    s = st.settings
    wm = (await watermarks(st.redis, [channel])).get(channel)
    if wm is not None and a >= wm - s.hot_window_s * 1_000_000:
        ts, val, qc = await read_hot(st.redis, channel, a, b, s.hot_batch_max_span_s * 1_000_000)
        qnames = [QUALITY_CODES[i].value for i in qc]
        nd = np.zeros(len(ts), dtype=bool)
        otkl = np.zeros(len(ts), dtype=np.int16)
    else:
        raw = await st.qdb.fetch_raw(channel, a, b, s.max_raw_points)
        ts, val, qnames, nd, otkl = raw["ts"], raw["val"], list(raw["quality"]), raw["nd"], raw["otkl"]
    flags = {f["ts"]: f for f in await st.qdb.fetch_flags([channel], a, b, limit=s.max_raw_points) if f["substituted"]}

    def rows():
        for i in range(len(ts)):
            t = int(ts[i])
            f = flags.get(t)
            v = float(val[i])
            yield RawPoint(
                ts=t,
                val=None if v != v else v,
                quality="substituted" if f else str(qnames[i]),
                nd=bool(nd[i]),
                otkl=int(otkl[i]),
                substituted=f is not None,
                val_f=f["val_f"] if f else None,
            )

    if format == "json":
        return Response(orjson.dumps([r.model_dump() for r in rows()]), media_type="application/json")

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(RawPoint.model_fields.keys())
        for r in rows():
            w.writerow(r.model_dump().values())
            if buf.tell() > 1 << 16:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    filename = f"{channel}_{a}_{b}.csv"
    return StreamingResponse(gen(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/flags", response_model=list[FlagPoint])
async def get_flags(
    channels: str,
    t_from: str = Query(..., alias="from"),
    t_to: str = Query(..., alias="to"),
    run: str = "online",
    st: AppState = Depends(get_state),
):
    """Point-level diagnostics (substituted outliers, anomalous points) at full resolution."""
    a, b = parse_range(t_from, t_to)
    return [FlagPoint(**f) for f in await st.qdb.fetch_flags(split_csv(channels), a, b, run=run, limit=MAX_FLAGS)]
