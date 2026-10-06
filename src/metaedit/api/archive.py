"""Archive inspection endpoints.

The archive is a feature, not an implementation detail: these endpoints expose
what we have accumulated and how close we are to the ToS storage cap.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.archive.stats import measure, partition_usage
from metaedit.config import Settings, get_settings
from metaedit.db.session import get_session

router = APIRouter(prefix="/archive", tags=["archive"])


@router.get("/stats")
async def archive_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    stats = await measure(session, settings)
    stats["partitions"] = await partition_usage(session)
    return stats
