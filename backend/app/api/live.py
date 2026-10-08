"""Live subscription for browsers (ФВ-5.2).

Client -> server (JSON):
    {"op": "subscribe", "channels": ["A1", "A2"], "window_s": 60, "width_px": 1200, "c": 2}
    {"op": "unsubscribe"}
    {"op": "ping", "t": <client us>}
Server -> client (JSON in binary frames):
    {"type": "snapshot", "delta_us", "window_us", "m", "series": [{channel, t, v, tail_t, tail_v}], "t_send"}
    {"type": "update", "series": [...], "events": [...], "t_send", "t_ing"}
    {"type": "pong", "t", "server_t"} | {"type": "error", "detail"}
`t`/`v` are finalized points (append them), `tail_t`/`tail_v` replace the previous tail.
"""

from __future__ import annotations

import time

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.api.deps import ws_state
from app.schemas import Ping, Subscribe, Unsubscribe
from app.services.live import Subscription

router = APIRouter(tags=["live"])
_cmd = TypeAdapter(Subscribe | Unsubscribe | Ping)


@router.websocket("/ws/stream")
async def stream_ws(ws: WebSocket):
    st = ws_state(ws)
    s = st.settings
    await ws.accept()
    sub: Subscription | None = None
    try:
        while True:
            data = await ws.receive_text()
            try:
                cmd = _cmd.validate_python(orjson.loads(data))
            except (orjson.JSONDecodeError, ValidationError) as e:
                await ws.send_bytes(orjson.dumps({"type": "error", "detail": str(e)[:1000]}))
                continue
            if isinstance(cmd, Ping):
                await ws.send_bytes(orjson.dumps({"type": "pong", "t": cmd.t, "server_t": time.time_ns() // 1000}))
            elif isinstance(cmd, Unsubscribe):
                if sub:
                    st.live.unsubscribe(sub)
                    sub = None
            else:
                if sub:
                    st.live.unsubscribe(sub)
                window_s = min(cmd.window_s, s.live_max_window_s, s.hot_window_s)
                width = min(cmd.width_px, s.max_width_px)
                sub = Subscription(ws, cmd.channels, window_s, width, cmd.c or s.detail_c)
                await st.live.subscribe(sub)
    except WebSocketDisconnect:
        pass
    finally:
        if sub:
            st.live.unsubscribe(sub)
