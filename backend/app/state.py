"""Shared application state (connections and services), created in the FastAPI lifespan."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.postgres import make_async_engine, make_async_sessionmaker
from app.db.questdb import QuestDBReader
from app.services.ingest import IngestService
from app.services.live import LiveHub
from app.services.registry import ChannelRegistry
from app.services.series import SeriesService
from app.services.tiles import TileCache


@dataclass
class AppState:
    settings: Settings
    redis: Redis
    pg_engine: AsyncEngine
    pg: async_sessionmaker[AsyncSession]
    qdb: QuestDBReader
    registry: ChannelRegistry
    ingest: IngestService
    tiles: TileCache
    series: SeriesService
    live: LiveHub

    @classmethod
    def create(cls, settings: Settings) -> "AppState":
        redis = Redis.from_url(settings.redis_url)
        engine = make_async_engine(settings)
        pg = make_async_sessionmaker(engine)
        qdb = QuestDBReader(settings)
        registry = ChannelRegistry(pg)
        tiles = TileCache(settings, redis, qdb)
        return cls(
            settings=settings,
            redis=redis,
            pg_engine=engine,
            pg=pg,
            qdb=qdb,
            registry=registry,
            ingest=IngestService(settings, redis, registry),
            tiles=tiles,
            series=SeriesService(settings, redis, qdb, tiles),
            live=LiveHub(settings, redis),
        )

    async def start(self) -> None:
        await self.qdb.open()
        await self.live.start()

    async def close(self) -> None:
        await self.live.stop()
        await self.qdb.close()
        await self.pg_engine.dispose()
        await self.redis.aclose()
