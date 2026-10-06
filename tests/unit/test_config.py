"""Configuration behaviour that the rest of the app depends on."""

from __future__ import annotations

import pytest

from metaedit.config import LASTFM_TOS_CAP_BYTES, Settings


def test_defaults_are_loopback_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "BIND_HOST",
        "ARCHIVE_SOFT_CAP_BYTES",
        "ARCHIVE_WARN_RATIO",
        "TAG_BLACKLIST_EXTRA",
        "WEB_DIST_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.bind_host == "127.0.0.1", "an admin Jellyfin key must not be exposed by default"
    assert settings.archive_soft_cap_bytes == LASTFM_TOS_CAP_BYTES == 104857600
    assert settings.extra_tag_blacklist == frozenset()
    assert settings.web_dist_dir == ""


def test_urls_are_normalised() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        JELLYFIN_URL="http://jellyfin:8096/",
        LASTFM_API_ROOT="http://ws.audioscrobbler.com/2.0/",
    )
    assert settings.jellyfin_base_url == "http://jellyfin:8096"
    assert settings.lastfm_base_url == "http://ws.audioscrobbler.com/2.0/"


def test_cap_thresholds() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ARCHIVE_SOFT_CAP_BYTES=1000,
        ARCHIVE_WARN_RATIO=0.8,
        ARCHIVE_REFUSE_RATIO=1.0,
    )
    assert settings.archive_warn_bytes == 800
    assert settings.archive_refuse_bytes == 1000


def test_tag_blacklist_extra_is_parsed_and_normalised() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        TAG_BLACKLIST_EXTRA=" Seen Live , Favourites ,, ",
    )
    assert settings.extra_tag_blacklist == frozenset({"seen live", "favourites"})


def test_secrets_are_not_in_repr() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        LASTFM_API_KEY="super-secret-value",
        JELLYFIN_API_KEY="another-secret",
    )
    assert "super-secret-value" not in repr(settings)
    assert "another-secret" not in repr(settings)
    assert settings.lastfm_key() == "super-secret-value"


@pytest.mark.parametrize("ratio", [0, -0.5, 1.5, 11])
def test_ratios_are_fractions_of_the_cap(ratio: float) -> None:
    """Above 1.0 a "ratio" is not a fraction of the cap and the arithmetic lies."""
    with pytest.raises(ValueError, match="at most 1"):
        Settings(_env_file=None, ARCHIVE_WARN_RATIO=ratio)  # type: ignore[call-arg]


def test_warning_must_fire_before_refusal() -> None:
    """A warning threshold past the refusal threshold is unreachable dead code.

    The warning exists to give an operator time to act before writes start being
    refused. A per-field range check happily accepts warn > refuse, so this is
    rejected by a cross-field validation instead.
    """
    with pytest.raises(ValueError, match="must not exceed archive_refuse_ratio"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            ARCHIVE_SOFT_CAP_BYTES=1000,
            ARCHIVE_WARN_RATIO=0.9,
            ARCHIVE_REFUSE_RATIO=0.5,
        )


@pytest.mark.parametrize(("warn", "refuse"), [(0.1, 1.0), (0.8, 0.8), (1.0, 1.0), (0.5, 0.6)])
def test_valid_ratio_pairings_are_accepted(warn: float, refuse: float) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ARCHIVE_SOFT_CAP_BYTES=1000,
        ARCHIVE_WARN_RATIO=warn,
        ARCHIVE_REFUSE_RATIO=refuse,
    )
    assert settings.archive_warn_bytes <= settings.archive_refuse_bytes


def test_default_ratios_warn_before_refusing() -> None:
    """The shipped defaults must leave room to react."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert 0 < settings.archive_warn_bytes < settings.archive_refuse_bytes
    assert settings.archive_refuse_bytes == settings.archive_soft_cap_bytes


def test_negative_cap_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        Settings(_env_file=None, ARCHIVE_SOFT_CAP_BYTES=-1)  # type: ignore[call-arg]
