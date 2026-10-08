"""Reading and writing the operator's genre blacklist.

Keeps two things out of the API layer: the merge of the three sources a blacklist entry
can come from, and the transaction around a replace-all save.

Nothing here computes a diff or writes to Jellyfin. Changing the blacklist changes what a
*future* plan proposes; it never edits a library by itself. That separation is deliberate:
"blacklisting a genre" and "deleting a genre from 500 items" are different operations with
different blast radii, and the second must never be a side effect of the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.config import Settings
from metaedit.db.models import GenreBlacklist
from metaedit.domain.genre_blacklist import (
    Conflict,
    ParseResult,
    normalise,
    parse,
)
from metaedit.domain.tags import DEFAULT_BLACKLIST
from metaedit.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BlacklistState:
    """The blacklist as the API and the UI need to see it.

    The three parts are reported separately *and* merged. Separately, because an operator
    who blacklisted something and then did not see it take effect needs to know which
    source supplied what; merged, because that is what the tag policy actually uses.
    """

    # The built-in list every deployment starts with.
    defaults: frozenset[str]
    # From TAG_BLACKLIST_EXTRA, per deployment.
    env: frozenset[str]
    # From the database, operator-editable at runtime.
    stored: list[str] = field(default_factory=list)

    @property
    def stored_normalised(self) -> frozenset[str]:
        return frozenset(normalise(value) for value in self.stored)

    @property
    def effective(self) -> frozenset[str]:
        """Everything that will be enforced, normalised.

        The single merge point. Every caller that needs to enforce the blacklist reads
        this rather than assembling its own union -- three sources with three
        normalisations is exactly how the env var and the table would come to disagree.
        """
        return self.defaults | self.env | self.stored_normalised

    @property
    def counts(self) -> dict[str, int]:
        return {
            "defaults": len(self.defaults),
            "env": len(self.env),
            "stored": len(self.stored),
            "effective": len(self.effective),
        }


async def load(session: AsyncSession, settings: Settings) -> BlacklistState:
    """Read the stored entries and merge them with the configured sources."""
    rows = (
        (await session.execute(select(GenreBlacklist).order_by(GenreBlacklist.value_norm)))
        .scalars()
        .all()
    )
    return BlacklistState(
        defaults=DEFAULT_BLACKLIST,
        env=settings.extra_tag_blacklist,
        stored=[row.value for row in rows],
    )


async def effective(session: AsyncSession, settings: Settings) -> frozenset[str]:
    """Just the enforced set, for callers that only need to apply it.

    A function rather than a property on a cached object: the blacklist is mutable at
    runtime, so anything holding it across requests would serve a stale policy -- and the
    symptom would be an entry that appears saved but does not apply until a restart,
    which is the exact problem this feature exists to remove.
    """
    return (await load(session, settings)).effective


async def replace(
    session: AsyncSession, *, raw: str, allow_commas: bool = False
) -> tuple[ParseResult, list[Conflict]]:
    """Replace the whole stored list from operator input.

    Replace, not merge, because the UI presents a textarea: what an operator sees when
    they press save is what they mean, and a hidden union with previous entries would
    make deleting an entry impossible.

    ``allow_commas`` is threaded through rather than decided here. Splitting
    ``"Rock, Reggae"`` on the operator's behalf is the one irreversible reading of their
    input, so it happens only when they have said which they meant.

    Returns the parse result and, separately, any conflicts. A conflict is *not* an error
    here: the caller decides whether to refuse (``parse_strict``) or to accept the
    unambiguous lines and report the rest. Saving the unambiguous entries and saying
    plainly what was skipped beats rejecting the whole block because one line was
    ambiguous.

    Conflict ``whole`` values are the operator's own text rather than a live library
    spelling, because this layer has no Jellyfin client and should not grow one. The API
    endpoint enriches them when it can; see ``api.settings``.
    """
    result = parse(raw, allow_commas=allow_commas)

    rows = [GenreBlacklist(value=entry.value, value_norm=entry.norm) for entry in result.entries]
    # Delete-then-insert inside one transaction. An upsert would preserve ids, but the
    # ids are not part of any contract and a partial replace that left an entry the
    # operator removed would be worse than a changed id.
    await session.execute(delete(GenreBlacklist))
    session.add_all(rows)
    try:
        await session.flush()
    except IntegrityError:
        # Reachable only if two entries normalise the same but differ in spelling -- which
        # `parse` already deduplicates. Kept because the alternative is an unhandled 500
        # on a uniqueness violation, and because it fails the transaction rather than
        # silently dropping the offending entry.
        await session.rollback()
        log.warning("genre_blacklist_duplicate_after_dedup")
        raise
    await session.commit()

    log.info(
        "genre_blacklist_replaced",
        stored=len(rows),
        conflicts=len(result.conflicts),
        allow_commas=allow_commas,
    )
    return result, result.conflicts


async def add(session: AsyncSession, *, value: str, note: str | None = None) -> GenreBlacklist:
    """Add one entry, idempotent on the normalised value.

    Returns the existing row when the value is already present rather than raising: from
    the operator's seat, adding ``rock`` when ``Rock`` is already blacklisted has achieved
    what they asked for, and an error would be noise about a distinction they cannot see.
    """
    norm = normalise(value)
    existing = (
        await session.execute(select(GenreBlacklist).where(GenreBlacklist.value_norm == norm))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = GenreBlacklist(value=value.strip(), value_norm=norm, note=note)
    session.add(row)
    await session.commit()
    log.info("genre_blacklist_added", value=value.strip())
    return row


async def remove(session: AsyncSession, *, value: str) -> bool:
    """Remove one entry. Returns whether anything was removed.

    Select-then-delete rather than reading ``rowcount``: the count on a delete statement
    is untyped in SQLAlchemy 2's ``Result`` (mypy rejects it), and this also makes the
    "was it there" decision explicit rather than inferred from a driver-driven count.
    """
    norm = normalise(value)
    row = (
        await session.execute(select(GenreBlacklist).where(GenreBlacklist.value_norm == norm))
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    log.info("genre_blacklist_removed", value=row.value)
    return True


def known_from(values: list[str]) -> frozenset[str]:
    """Normalise a live genre vocabulary for conflict reporting."""
    return frozenset(normalise(value) for value in values if value)
