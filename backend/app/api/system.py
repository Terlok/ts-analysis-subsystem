from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.api.deps import get_state
from app.services.metrics import collect
from app.state import AppState

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health(st: AppState = Depends(get_state)):
    status = {}
    try:
        status["redis"] = bool(await st.redis.ping())
    except Exception:  # noqa: BLE001
        status["redis"] = False
    try:
        async with st.pg() as s:
            await s.execute(text("SELECT 1"))
        status["postgres"] = True
    except Exception:  # noqa: BLE001
        status["postgres"] = False
    status["questdb"] = await st.qdb.ping()
    return {"ok": all(status.values()), **status}


@router.get("/metrics")
async def metrics(st: AppState = Depends(get_state)):
    """Latency percentiles (К1), worker load rho = lambda*tau/N_w (К2), cache hit ratio and
    Redis memory (К6), stream lag of consumer groups."""
    return await collect(st.redis, st.settings)


@router.get("/config")
async def client_config(st: AppState = Depends(get_state)):
    """Parameters the frontend needs to align with the server."""
    s = st.settings
    g = s.grid
    return {
        "detail_c": s.detail_c,
        "preselect_rho": s.preselect_rho,
        "max_width_px": s.max_width_px,
        "hot_window_s": s.hot_window_s,
        "live_rate_hz": s.live_rate_hz,
        "live_max_window_s": s.live_max_window_s,
        "grid": {
            "delta0_us": g.delta0_us,
            "base": g.base,
            "levels": [g.delta(lvl) for lvl in range(g.n_levels)],
            "tile_buckets": g.tile_buckets,
        },
    }
