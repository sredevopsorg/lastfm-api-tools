"""FastAPI dependency wiring.

Constructor injection with plain functions, no container: two adapters and a
session do not need one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.adapters.lastfm.client import LastfmClient
from metaedit.archive.store import ArchiveStore
from metaedit.config import Settings, get_settings
from metaedit.db.session import get_session


async def jellyfin_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[JellyfinClient]:
    async with JellyfinClient(settings) as client:
        yield client


async def lastfm_client(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AsyncIterator[LastfmClient]:
    """A Last.fm client bound to this request's session.

    The session matters: harvesting archives what it fetches, so the store and the
    caller must share one transaction or the archive writes would be invisible to the
    candidate lookup that follows.
    """
    async with LastfmClient(settings, store=ArchiveStore(session, settings)) as client:
        yield client


JellyfinDep = Annotated[JellyfinClient, Depends(jellyfin_client)]
LastfmDep = Annotated[LastfmClient, Depends(lastfm_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
