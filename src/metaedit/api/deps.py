"""FastAPI dependency wiring.

Constructor injection with plain functions, no container: two adapters and a
session do not need one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.config import Settings, get_settings


async def jellyfin_client(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[JellyfinClient]:
    async with JellyfinClient(settings) as client:
        yield client


JellyfinDep = Annotated[JellyfinClient, Depends(jellyfin_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
