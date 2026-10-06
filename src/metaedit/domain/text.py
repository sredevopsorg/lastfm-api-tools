"""Turning Last.fm's HTML-laden prose into something safe to write as an overview.

Last.fm bios and track wikis arrive as HTML, typically a sentence or two followed by
``<a href="...">Read more on Last.fm</a>``. Jellyfin's ``Overview`` is plain text.

The stripping is done with :mod:`html.parser` rather than a regex. Regex-based tag
stripping is the classic way to mangle input, and while Last.fm's markup is simple,
"simple" is an assumption about data we do not control — the parser handles
unclosed tags, attributes containing ``>``, and entities correctly for the same
amount of code.

Two content decisions worth stating:

* The ``Read more on Last.fm`` link is **dropped from the text**, not kept as a URL.
  The canonical Last.fm URL belongs in ``ExternalUrls`` where a client can render it
  as a link; leaving the phrase inline would produce an overview ending in a
  sentence fragment with no target.
* The bio is Last.fm's own summary, which its interface truncates at roughly 300
  characters. We store what we were given and flag the truncation rather than
  pretending the text is complete — inventing or fetching more would be a different
  feature with different terms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Last.fm's own "read more" filler, in the several phrasings it uses.
_READ_MORE_PATTERNS = (
    re.compile(r"\s*<a[^>]*>\s*Read more.*?</a>\.?\s*", re.IGNORECASE | re.DOTALL),
    re.compile(r"\s*Read more on Last\.fm\.?\s*$", re.IGNORECASE),
    re.compile(r"\s*Read more\.?\s*$", re.IGNORECASE),
    # Last.fm's attribution footer, in the wording it actually uses. An earlier
    # version of this pattern guessed "CC-BY-SA" and silently matched nothing.
    re.compile(r"\s*User-contributed\s+text.*?$", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"\s*(available\s+)?under\s+the\s+Creative\s+Commons.*?$", re.IGNORECASE | re.DOTALL
    ),
)

# The interface truncates the summary at about this length.
LASTFM_SUMMARY_LIMIT = 300


class _TextExtractor(HTMLParser):
    """Collect text, treating block-level tags as line breaks."""

    _BREAKS = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag in self._BREAKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAKS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


@dataclass(frozen=True, slots=True)
class CleanText:
    """Plain text plus what we noticed about it."""

    text: str | None
    truncated_by_lastfm: bool = False
    dropped_read_more: bool = False

    @property
    def has_content(self) -> bool:
        return bool(self.text and self.text.strip())


def html_to_text(value: str | None) -> CleanText:
    """Convert a Last.fm bio or wiki summary to plain text.

    Returns ``text=None`` for input with no usable content, rather than an empty
    string: the caller distinguishes "no overview available" from "an overview that
    is empty", and the write payload must not clear a field because Last.fm sent
    whitespace.
    """
    if not value or not value.strip():
        return CleanText(text=None)

    dropped_read_more = False
    working = value
    for pattern in _READ_MORE_PATTERNS:
        if pattern.search(working):
            dropped_read_more = True
            working = pattern.sub(" ", working)

    parser = _TextExtractor()
    parser.feed(working)
    parser.close()
    text = parser.text()

    if "\n" in text:
        # Collapse the line breaks the block tags introduced, keeping paragraphs.
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    else:
        text = text.strip()
    text = re.sub(r"[ \t]+", " ", text).strip()

    if not text:
        return CleanText(text=None, dropped_read_more=dropped_read_more)

    return CleanText(
        text=text,
        # Last.fm truncates its own summary; this is reported, not repaired.
        truncated_by_lastfm=len(value.strip()) >= LASTFM_SUMMARY_LIMIT
        or "read more" in value.lower(),
        dropped_read_more=dropped_read_more,
    )


def is_thin_overview(text: str | None, *, minimum: int = 40) -> bool:
    """Whether a bio is too short to be worth writing over nothing.

    Last.fm has very short bios for obscure artists ("British band."). Replacing an
    empty overview with one of those is a wash, so it is worth being able to tell.
    """
    if not text:
        return True
    return len(text.strip()) < minimum
