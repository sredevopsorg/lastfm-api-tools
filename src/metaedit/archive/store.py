"""Archive writes: the durable record of everything we ask Last.fm.

The ordering rule is deliberate and load-bearing: the request row and the
response body are committed **before** anything tries to interpret the payload.
A response we already paid a rate-limited request for must survive a parser bug,
a mapper crash, or a process restart.

Two tables, two jobs (ADR 0010):

* ``lastfm_response`` is content-addressed -- identical bodies are stored once.
* ``lastfm_request`` is an append-only observation log -- every attempt, every
  failure, in a monthly partition, so we can date when an entity changed or
  vanished without duplicating payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.lastfm.canonical import (
    api_key_fingerprint,
    body_bytes,
    canonical_params,
    content_id,
    is_error_body,
    params_hash,
)
from metaedit.config import Settings
from metaedit.db.models import LastfmRequest, LastfmResponse
from metaedit.domain.errors import ArchiveCapReached
from metaedit.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ArchivedResponse:
    """A stored response body plus what we know about its provenance."""

    response_id: str
    body: dict[str, Any]
    request_id: int | None
    first_seen_at: datetime
    last_seen_at: datetime
    observation_count: int


@dataclass(frozen=True, slots=True)
class Observation:
    """What to persist about one HTTP attempt."""

    method: str
    params: dict[str, Any]
    http_status: int | None
    duration_ms: int | None
    body: dict[str, Any] | None
    lastfm_error_code: int | None = None
    retry_count: int = 0
    served_from_archive: bool = False
    user_agent: str | None = None


class ArchiveStore:
    """Persists and looks up archive rows. Owns the ToS byte-cap policy."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------------ write

    async def record(self, observation: Observation) -> ArchivedResponse | None:
        """Persist one attempt and return the stored body when there was one.

        Returns ``None`` when the attempt produced no body (transport failure) or
        archiving is disabled. Raises ``ArchiveCapReached`` when storing a *new*
        distinct payload would breach the configured cap -- a very large increase
        since the last measurement, so the current total is re-read first.
        """
        if not self._settings.archive_enabled:
            return None

        body = observation.body
        response_id: str | None = None
        first_seen_at = datetime.now(UTC)
        last_seen_at = first_seen_at
        observation_count = 0

        if body is not None:
            response_id = content_id(body)
            first_seen_at, last_seen_at, observation_count = await self._store_body(
                response_id, body
            )

        request_id: int | None = None
        if self._settings.archive_log_requests:
            request_id = await self._store_request(observation, response_id)

        if body is None or response_id is None:
            return None
        return ArchivedResponse(
            response_id=response_id,
            body=body,
            request_id=request_id,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            observation_count=observation_count,
        )

    async def _store_body(
        self, response_id: str, body: dict[str, Any]
    ) -> tuple[datetime, datetime, int]:
        """Content-addressed upsert: store the bytes once, count every sighting.

        A repeat observation of an already-stored body costs no new payload bytes,
        so it is never blocked by the cap (ADR 0011).
        """
        now = datetime.now(UTC)
        exists = await self._session.scalar(
            select(LastfmResponse.id).where(LastfmResponse.id == response_id)
        )
        if exists is None:
            await self._enforce_cap(incoming_bytes=body_bytes(body))

        stmt = (
            pg_insert(LastfmResponse)
            .values(
                id=response_id,
                body=body,
                body_bytes=body_bytes(body),
                is_error=is_error_body(body),
                first_seen_at=now,
                last_seen_at=now,
                # 1, not 0: this insert *is* the first observation. ``RETURNING``
                # yields the pre-update row on conflict, so starting at 0 would
                # leave "how many times have we seen this" off by one forever.
                observation_count=1,
            )
            .on_conflict_do_update(
                index_elements=[LastfmResponse.id],
                set_={
                    "last_seen_at": now,
                    "observation_count": LastfmResponse.observation_count + 1,
                },
            )
            .returning(
                LastfmResponse.first_seen_at,
                LastfmResponse.last_seen_at,
                LastfmResponse.observation_count,
            )
        )
        row = (await self._session.execute(stmt)).one()
        return row[0], row[1], int(row[2])

    async def _store_request(self, observation: Observation, response_id: str | None) -> int:
        row = LastfmRequest(
            method=observation.method,
            params=canonical_params(observation.method, observation.params),
            params_hash=params_hash(observation.method, observation.params),
            requested_at=datetime.now(UTC),
            duration_ms=observation.duration_ms,
            http_status=observation.http_status,
            lastfm_error_code=observation.lastfm_error_code,
            retry_count=observation.retry_count,
            served_from_archive=observation.served_from_archive,
            user_agent=observation.user_agent,
            api_key_fingerprint=api_key_fingerprint(self._settings.lastfm_key()) or None,
            response_id=response_id,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def _enforce_cap(self, *, incoming_bytes: int) -> None:
        current = await self._session.scalar(
            select(func.coalesce(func.sum(LastfmResponse.body_bytes), 0))
        )
        used = int(current or 0)
        if used + incoming_bytes <= self._settings.archive_refuse_bytes:
            if used >= self._settings.archive_warn_bytes:
                log.warning(
                    "archive_cap_warning",
                    used_bytes=used,
                    cap_bytes=self._settings.archive_soft_cap_bytes,
                )
            return
        raise ArchiveCapReached(
            "Refusing to store a new Last.fm payload: the configured archive cap is "
            "reached. Repeat observations of already-stored data are unaffected.",
            used_bytes=used,
            cap_bytes=self._settings.archive_soft_cap_bytes,
        )

    # ------------------------------------------------------------------- read

    async def find_recent(
        self, method: str, params: dict[str, Any], *, max_age: timedelta
    ) -> ArchivedResponse | None:
        """The newest stored response for this request identity, if fresh enough.

        A hit here means the editor can answer without spending a request.
        """
        if not self._settings.archive_enabled:
            return None
        cutoff = datetime.now(UTC) - max_age
        stmt = (
            select(LastfmResponse, LastfmRequest.id)
            .join(
                LastfmRequest,
                LastfmRequest.response_id == LastfmResponse.id,
            )
            .where(LastfmRequest.params_hash == params_hash(method, params))
            .where(LastfmResponse.last_seen_at >= cutoff)
            .order_by(LastfmRequest.requested_at.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        response, request_id = row
        return ArchivedResponse(
            response_id=response.id,
            body=response.body,
            request_id=int(request_id),
            first_seen_at=response.first_seen_at,
            last_seen_at=response.last_seen_at,
            observation_count=response.observation_count,
        )

    async def history(
        self, method: str, params: dict[str, Any], *, limit: int = 50
    ) -> list[tuple[datetime, str | None, int | None, int | None]]:
        """Observation timeline for one request identity, newest first."""
        stmt = (
            select(
                LastfmRequest.requested_at,
                LastfmRequest.response_id,
                LastfmRequest.http_status,
                LastfmRequest.lastfm_error_code,
            )
            .where(LastfmRequest.params_hash == params_hash(method, params))
            .order_by(LastfmRequest.requested_at.desc())
            .limit(limit)
        )
        return [
            (row[0], row[1], row[2], row[3]) for row in (await self._session.execute(stmt)).all()
        ]

    async def get_body(self, response_id: str) -> dict[str, Any] | None:
        result = await self._session.scalar(
            select(LastfmResponse.body).where(LastfmResponse.id == response_id)
        )
        return result

    async def distinct_response_count(self, method: str, params: dict[str, Any]) -> int:
        """How many distinct bodies we have seen for this identity.

        More than one means the data changed under us at some point -- which is
        exactly what the archive exists to make visible.
        """
        stmt = (
            select(func.count(func.distinct(LastfmRequest.response_id)))
            .where(LastfmRequest.params_hash == params_hash(method, params))
            .where(LastfmRequest.response_id.is_not(None))
        )
        return int(await self._session.scalar(stmt) or 0)
