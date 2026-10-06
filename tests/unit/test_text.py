"""Overview text handling.

Last.fm sends HTML; Jellyfin's ``Overview`` is plain text. The interesting cases are
the ones that break naive stripping: attributes containing ``>``, unclosed tags,
entities, and embedded script content.
"""

from __future__ import annotations

import pytest

from metaedit.domain.text import (
    LASTFM_SUMMARY_LIMIT,
    html_to_text,
    is_thin_overview,
)


def test_plain_text_passes_through() -> None:
    result = html_to_text("Cher is an American singer.")
    assert result.text == "Cher is an American singer."
    assert result.dropped_read_more is False


def test_the_read_more_link_is_dropped_not_kept() -> None:
    """The canonical URL belongs in ExternalUrls, not inline in prose."""
    result = html_to_text(
        'Cher is a singer. <a href="https://www.last.fm/music/Cher/+wiki">Read more on Last.fm</a>'
    )
    assert result.text == "Cher is a singer."
    assert result.dropped_read_more is True
    assert "last.fm" not in (result.text or "").lower()


@pytest.mark.parametrize(
    "tail",
    [
        "Read more on Last.fm.",
        "Read more on Last.fm",
        "read more on last.fm",
        "Read more.",
    ],
)
def test_read_more_phrasings_are_recognised(tail: str) -> None:
    result = html_to_text(f"A band. {tail}")
    assert "read more" not in (result.text or "").lower()


@pytest.mark.parametrize(
    "boilerplate",
    [
        "User-contributed text is available under the Creative Commons By-SA License; "
        "additional terms may apply.",
        "User-contributed text is available under the Creative Commons By-SA License.",
        "Available under the Creative Commons By-SA License; additional terms may apply.",
    ],
)
def test_user_contributed_boilerplate_is_dropped(boilerplate: str) -> None:
    """The wording Last.fm actually uses, not a guess at it.

    An earlier pattern matched a hypothetical "CC-BY-SA" abbreviation and therefore
    matched nothing, leaving the footer in every overview.
    """
    result = html_to_text(f"A band. {boilerplate}")
    assert result.text == "A band."
    assert "creative commons" not in (result.text or "").lower()
    assert "user-contributed" not in (result.text or "").lower()


def test_html_entities_are_decoded() -> None:
    result = html_to_text("Sigur R&oacute;s are Icelandic &amp; loud &lt;live&gt;.")
    assert result.text == "Sigur Rós are Icelandic & loud <live>."


def test_numeric_entities_are_decoded() -> None:
    assert html_to_text("caf&#233;").text == "café"


def test_block_tags_become_line_breaks() -> None:
    result = html_to_text("<p>First para.</p><p>Second para.</p>")
    assert result.text == "First para.\nSecond para."


def test_br_becomes_a_line_break() -> None:
    assert html_to_text("Line one<br>Line two").text == "Line one\nLine two"


def test_paragraph_breaks_are_not_collapsed_into_one_line() -> None:
    """A two-paragraph bio should not read as one run-on sentence."""
    result = html_to_text("<p>One.</p><p>Two.</p>")
    assert result.text is not None
    assert "\n" in result.text


def test_attributes_containing_a_greater_than_sign_survive() -> None:
    """The case that makes regex-based stripping wrong.

    ``<a title="a > b" href="x">text</a>`` is valid HTML; a regex stopping at the
    first ``>`` would emit the rest of the attribute as visible text.
    """
    result = html_to_text('<a title="a > b" href="https://x">visible text</a>')
    assert result.text == "visible text"
    assert "b" not in (result.text or "").replace("visible text", "")


def test_unclosed_tags_do_not_leak_markup() -> None:
    result = html_to_text('<b>Bold without close and a <a href="x">link')
    assert result.text == "Bold without close and a link"


def test_script_content_is_excluded() -> None:
    result = html_to_text("Real text<script>var hidden = 'noise';</script> trailing")
    assert result.text == "Real text trailing"
    assert "hidden" not in (result.text or "")


def test_style_content_is_excluded() -> None:
    result = html_to_text("Text<style>.a { color: red }</style> more")
    assert "color" not in (result.text or "")


def test_whitespace_is_normalised() -> None:
    assert html_to_text("  lots    of \t space  ").text == "lots of space"


def test_blank_lines_are_removed() -> None:
    result = html_to_text("<p>One.</p><p></p><p>Two.</p>")
    assert result.text == "One.\nTwo."


# ------------------------------------------------------------------ emptiness


@pytest.mark.parametrize(
    "empty", [None, "", "   ", "\n\t", "<p></p>", "<br/>", "<script>x</script>"]
)
def test_empty_input_yields_none_not_an_empty_string(empty: str | None) -> None:
    """The distinction matters: an empty string would clear an existing overview."""
    result = html_to_text(empty)
    assert result.text is None
    assert result.has_content is False


def test_none_and_empty_are_distinguishable_from_real_text() -> None:
    assert html_to_text("A band.").has_content is True
    assert html_to_text("").has_content is False


# -------------------------------------------------------------- truncation


def test_long_text_is_flagged_as_interface_truncated() -> None:
    """Last.fm truncates its own summary; we report that rather than hide it."""
    result = html_to_text("x" * (LASTFM_SUMMARY_LIMIT + 10))
    assert result.truncated_by_lastfm is True


def test_short_text_is_not_flagged_as_truncated() -> None:
    result = html_to_text("A short bio.")
    assert result.truncated_by_lastfm is False


def test_a_read_more_link_implies_truncation() -> None:
    """Its presence means Last.fm had more text than it showed."""
    result = html_to_text("Short. <a href='x'>Read more on Last.fm</a>")
    assert result.truncated_by_lastfm is True


# ------------------------------------------------------------- thin overviews


@pytest.mark.parametrize("thin", [None, "", "British band.", "Band."])
def test_thin_overviews_are_recognisable(thin: str | None) -> None:
    """Replacing nothing with "British band." is not worth a write."""
    assert is_thin_overview(thin) is True


def test_substantial_overviews_are_not_thin() -> None:
    assert is_thin_overview("Cher is an American singer and actress, born in 1946.") is False


def test_thin_threshold_is_configurable() -> None:
    assert is_thin_overview("British band.", minimum=5) is False
