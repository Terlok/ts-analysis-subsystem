"""FastAPI application: REST API (OpenAPI at /docs) and WebSocket endpoints.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import alarms, analysis, channels, events, ingest, live, series, system
from app.config import Settings, get_settings
from app.state import AppState


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    """`state` allows injecting prepared connections (tests)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = state or AppState.create(settings)
        app.state.tsa = st
        await st.start()
        try:
            yield
        finally:
            await st.close()

    app = FastAPI(title=settings.api_title, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=4096)
    for r in (system, channels, series, events, alarms, analysis, ingest, live):
        app.include_router(r.router)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
