from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request, WebSocket

from app.state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.tsa


def ws_state(ws: WebSocket) -> AppState:
    return ws.app.state.tsa


def parse_ts(value: str | int | None, name: str) -> int | None:
    """Accepts microseconds since epoch or an ISO 8601 string (naive = UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    s = value.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(422, f"{name}: expected microseconds or ISO 8601, got {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000)


def parse_range(t_from: str | None, t_to: str | None) -> tuple[int, int]:
    a, b = parse_ts(t_from, "from"), parse_ts(t_to, "to")
    if a is None or b is None:
        raise HTTPException(422, "both 'from' and 'to' are required")
    if b <= a:
        raise HTTPException(422, "'to' must be greater than 'from'")
    return a, b


def split_csv(value: str | None) -> list[str]:
    return [v for v in (value or "").split(",") if v]
