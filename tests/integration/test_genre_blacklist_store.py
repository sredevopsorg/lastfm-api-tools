"""The genre blacklist against a real Postgres.

Two things here can only be tested against the database rather than in a unit test: the
uniqueness constraint, and whether a change made through the service is actually visible
to the *next* read. The second is the whole point of the feature -- the previous
implementation was an env var, so "saved but not in effect" was the normal state and the
only signal was a restart.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import requires_postgres

from metaedit.config import Settings
from metaedit.db.models import GenreBlacklist
from metaedit.domain.genre_blacklist import normalise
from metaedit.domain.tags import DEFAULT_BLACKLIST, TagInput, TagPolicy
from metaedit.service import genre_blacklist as service

pytestmark = requires_postgres


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "LOG_JSON": False,
        "TAG_BLACKLIST_EXTRA": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def session(database_url: str) -> AsyncSession:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as open_session:
        yield open_session
    await engine.dispose()


# ------------------------------------------------------------------- loading


async def test_an_empty_table_reports_the_defaults(session: AsyncSession) -> None:
    state = await service.load(session, _settings())
    assert state.stored == []
    assert state.defaults == DEFAULT_BLACKLIST
    assert "seen live" in state.effective


async def test_the_env_var_and_the_table_both_contribute(session: AsyncSession) -> None:
    await service.replace(session, raw="shoegaze")
    state = await service.load(session, _settings(TAG_BLACKLIST_EXTRA="dream pop"))

    assert state.stored == ["shoegaze"]
    assert state.env == {"dream pop"}
    # All three sources, which is the merge that has to be right.
    assert {"shoegaze", "dream pop"} <= state.effective
    assert "seen live" in state.effective


async def test_effective_is_normalised(session: AsyncSession) -> None:
    await service.replace(session, raw="  Gothic   Rock  ")
    loaded = await service.effective(session, _settings())
    assert "gothic rock" in loaded


async def test_counts_describe_each_source_separately(session: AsyncSession) -> None:
    await service.replace(session, raw="a\nb")
    state = await service.load(session, _settings(TAG_BLACKLIST_EXTRA="c"))
    assert state.counts["stored"] == 2
    assert state.counts["env"] == 1
    assert state.counts["effective"] >= 3


# ------------------------------------------------------------------ replacing


async def test_replace_saves_the_entries_with_their_typed_spelling(session: AsyncSession) -> None:
    result, conflicts = await service.replace(session, raw="Rock\nGothic Rock")
    assert result.values == ["Rock", "Gothic Rock"]
    assert conflicts == []

    rows = (
        await session.execute(text("select value, value_norm from genre_blacklist order by id"))
    ).all()
    assert [(r.value, r.value_norm) for r in rows] == [
        ("Rock", "rock"),
        ("Gothic Rock", "gothic rock"),
    ]


async def test_replace_is_a_replace_not_a_merge(session: AsyncSession) -> None:
    """The UI is a textarea: what the operator sees on save is what they mean.

    A union would make removing an entry impossible, and the entry would keep applying
    with nothing on screen to explain why.
    """
    await service.replace(session, raw="Rock\nReggae")
    await service.replace(session, raw="Ska")

    state = await service.load(session, _settings())
    assert state.stored == ["Ska"]


async def test_replace_deduplicates_case_insensitively(session: AsyncSession) -> None:
    await service.replace(session, raw="Rock\nrock\nROCK")
    state = await service.load(session, _settings())
    assert state.stored == ["Rock"]


async def test_replace_with_empty_input_clears_the_list(session: AsyncSession) -> None:
    await service.replace(session, raw="Rock")
    await service.replace(session, raw="   \n\n ")
    state = await service.load(session, _settings())
    assert state.stored == []
    # The built-in list is not removable: clearing the operator's list must not
    # accidentally re-enable "seen live" as a genre.
    assert "seen live" in state.effective


async def test_a_comma_line_is_reported_and_not_saved(session: AsyncSession) -> None:
    """Saving the unambiguous lines and saying what was skipped beats rejecting the
    whole block because one line was ambiguous."""
    result, conflicts = await service.replace(session, raw="Gothic\nRock, Reggae\nSka")

    assert result.values == ["Gothic", "Ska"]
    assert len(conflicts) == 1
    assert conflicts[0].whole == "Rock, Reggae"
    assert conflicts[0].fragments == ["Rock", "Reggae"]

    state = await service.load(session, _settings())
    assert state.stored == ["Gothic", "Ska"]


async def test_allow_commas_saves_the_fragments(session: AsyncSession) -> None:
    """Only when the operator has said which reading they meant."""
    result, conflicts = await service.replace(session, raw="rock, reggae", allow_commas=True)
    assert result.values == ["rock", "reggae"]
    assert conflicts == []
    # `load` orders by value_norm, not insertion: a list that reshuffles itself between
    # reads would make the UI's chip order unstable for no reason.
    assert sorted((await service.load(session, _settings())).stored) == ["reggae", "rock"]


async def test_stored_entries_have_a_stable_order(session: AsyncSession) -> None:
    """Sorted by the comparison key, so the UI is not at the mercy of insert order."""
    await service.replace(session, raw="Zebra\nApple\nmango")
    first = (await service.load(session, _settings())).stored
    second = (await service.load(session, _settings())).stored
    assert first == second == ["Apple", "mango", "Zebra"]


# -------------------------------------------------------------------- adding


async def test_add_is_idempotent_across_case(session: AsyncSession) -> None:
    first = await service.add(session, value="Rock")
    second = await service.add(session, value="rock")

    assert first.id == second.id
    assert first.value == "Rock", "the original spelling is kept"
    assert (await service.load(session, _settings())).stored == ["Rock"]


async def test_add_keeps_a_note(session: AsyncSession) -> None:
    await service.add(session, value="Rock", note="too vague to be useful")
    row = (await session.execute(text("select note from genre_blacklist"))).scalar_one()
    assert row == "too vague to be useful"


async def test_remove_reports_whether_it_removed_anything(session: AsyncSession) -> None:
    await service.add(session, value="Rock")
    assert await service.remove(session, value="rock") is True
    assert await service.remove(session, value="rock") is False


async def test_remove_matches_case_insensitively(session: AsyncSession) -> None:
    await service.add(session, value="Gothic Rock")
    assert await service.remove(session, value="GOTHIC ROCK") is True
    assert (await service.load(session, _settings())).stored == []


# ------------------------------------------------------------------ constraint


async def test_the_database_refuses_two_spellings_of_one_entry(session: AsyncSession) -> None:
    """The backstop for a caller that skips the parser.

    Without it, ``Rock`` and ``rock`` would occupy two rows and the second would silently
    do nothing -- an operator-visible no-op with no error anywhere.
    """
    session.add(GenreBlacklist(value="Rock", value_norm=normalise("Rock")))
    await session.commit()

    session.add(GenreBlacklist(value="rock", value_norm=normalise("rock")))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


# ------------------------------------------------------------------- applying


async def test_a_stored_entry_changes_what_the_policy_proposes(session: AsyncSession) -> None:
    """End to end from the operator's save to the mapping decision.

    This is the assertion that makes the feature real: the row reaches `TagPolicy` and
    changes which genres would be written.
    """
    await service.replace(session, raw="Gothic Rock")

    blacklist = await service.effective(session, _settings())
    policy = TagPolicy(
        genre_limit=5, style_limit=5, blacklist=frozenset(), extra_blacklist=blacklist
    )
    outcome = policy.apply([TagInput("Gothic Rock", 90), TagInput("Ska", 80)])

    assert outcome.genres == ["Ska"]
    assert [drop.reason for drop in outcome.dropped] == ["on the blacklist"]


async def test_case_insensitivity_survives_the_database_round_trip(session: AsyncSession) -> None:
    """``Rock`` is stored; Last.fm returns ``ROCK``; both must fold to the same key."""
    await service.replace(session, raw="Rock")

    blacklist = await service.effective(session, _settings())
    policy = TagPolicy(
        genre_limit=5, style_limit=5, blacklist=frozenset(), extra_blacklist=blacklist
    )
    for spelling in ("rock", "ROCK", "RoCk"):
        assert policy.apply([TagInput(spelling, 50)]).genres == [], spelling


async def test_the_env_var_still_applies_when_the_table_is_emptied(
    session: AsyncSession,
) -> None:
    """The two sources are independent, so clearing one cannot silently disable the other."""
    settings = _settings(TAG_BLACKLIST_EXTRA="shoegaze")
    await service.replace(session, raw="Rock")
    await service.replace(session, raw="")

    blacklist = await service.effective(session, settings)
    policy = TagPolicy(
        genre_limit=5, style_limit=5, blacklist=frozenset(), extra_blacklist=blacklist
    )
    assert policy.apply([TagInput("shoegaze", 50)]).genres == []
