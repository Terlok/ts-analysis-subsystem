"""Channel registry with an in-process cache and dynamic registration (ФВ-1.1)."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Channel
from app.schemas import ChannelOut

log = logging.getLogger(__name__)


class ChannelRegistry:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], refresh_s: float = 30.0):
        self.sessionmaker = sessionmaker
        self.refresh_s = refresh_s
        self._cache: dict[str, ChannelOut] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        async with self.sessionmaker() as s:
            rows = (await s.execute(select(Channel))).scalars().all()
        self._cache = {r.id: ChannelOut.model_validate(r) for r in rows}
        self._loaded_at = time.monotonic()

    async def all(self) -> dict[str, ChannelOut]:
        if time.monotonic() - self._loaded_at > self.refresh_s:
            async with self._lock:
                if time.monotonic() - self._loaded_at > self.refresh_s:
                    try:
                        await self.refresh()
                    except Exception:  # noqa: BLE001 - keep serving the stale cache
                        log.exception("registry refresh failed")
                        self._loaded_at = time.monotonic()
        return self._cache

    async def get(self, channel_id: str) -> ChannelOut | None:
        return (await self.all()).get(channel_id)

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def ensure(self, ids: list[str], source: str | None = None) -> list[str]:
        """Register unknown channels; returns ids that were new for this process."""
        known = await self.all()
        new = [i for i in dict.fromkeys(ids) if i not in known]
        if not new:
            return []
        async with self.sessionmaker() as s:
            await s.execute(
                insert(Channel)
                .values([{"id": i, "name": i, "source": source, "auto_registered": True} for i in new])
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await s.commit()
        for i in new:
            self._cache[i] = ChannelOut(id=i, name=i, source=source, auto_registered=True)
        log.info("registered %d new channel(s): %s", len(new), ", ".join(new[:10]))
        return new
