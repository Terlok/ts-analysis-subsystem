"""Ingestion endpoints for the robot gateway / telemetry replayer (ФВ-1.2).

WebSocket protocol (/ws/ingest): the client sends IngestPacket JSON messages
(text or binary frames) and receives an IngestAck for every packet. A packet is
confirmed only after it has been written to the Redis stream, so the sender may
safely drop it from its resend buffer.
"""

from __future__ import annotations

import logging

import orjson
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.deps import get_state, ws_state
from app.schemas import IngestAck, IngestPacket
from app.state import AppState

log = logging.getLogger(__name__)
router = APIRouter(tags=["ingest"])


@router.post("/api/ingest", response_model=IngestAck)
async def ingest_http(packet: IngestPacket, st: AppState = Depends(get_state)):
    return await st.ingest.ingest(packet)


@router.websocket("/ws/ingest")
async def ingest_ws(ws: WebSocket):
    st = ws_state(ws)
    await ws.accept()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or msg.get("text")
            if data is None:
                continue
            try:
                packet = IngestPacket.model_validate(orjson.loads(data))
            except (orjson.JSONDecodeError, ValidationError) as e:
                seq = None
                try:
                    seq = orjson.loads(data).get("seq")
                except Exception:  # noqa: BLE001
                    pass
                err = IngestAck(seq=seq, accepted=0, rejected=0, errors=[str(e)[:2000]])
                await ws.send_bytes(orjson.dumps(err.model_dump()))
                continue
            ack = await st.ingest.ingest(packet)
            await ws.send_bytes(orjson.dumps(ack.model_dump()))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ingest websocket failed")
        await ws.close(code=1011)
