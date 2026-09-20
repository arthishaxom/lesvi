"""Heuristic metadata extraction: number, title, description and tags.

This is the heuristic layer of ADR-0005: the numbered filename, the ``<title>``
element, a leading ``.subtitle``/``<p>`` and the first ``.lesson-tag``/``.badge``
element. Sidecar files and ``lesvi:*`` meta tags are separate, higher-precedence
sources layered on top by a later ticket.

Parsing is defensive by contract: a malformed artifact degrades to
filename-derived metadata, never raises into the scanner.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 160
ELLIPSIS = "…"

_NUMBER_PREFIX = re.compile(r"^(\d+)[-_]")
_LESSON_TITLE = re.compile(r"^\s*Lesson\s+(\d+)\s*[—–:-]\s*(.+)$", re.IGNORECASE)
_SLUG_SEPARATOR = re.compile(r"[-_\s]+")
_WHITESPACE = re.compile(r"\s+")
_TAG_SEPARATOR = re.compile(r"[·•]")
_NOISE_TAG = re.compile(
    r"^(?:\W*Lesson\s*\d+\W*"
    r"|\d+\s*(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes))\s*$",
    re.IGNORECASE,
)

# HTML tags that never have an end tag, so they must not enter the open stack.
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_TAG_CLASSES = frozenset({"lesson-tag", "badge"})

# Block-level starts end an unterminated inline capture, so a malformed
# subtitle/badge cannot swallow the rest of the document. Block *containers*
# (div, li, section, ...) may legitimately wrap headings/lists inside a
# captured element and therefore do not end the capture by themselves.
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "ul",
    }
)

_BLOCK_CONTAINERS = _BLOCK_ELEMENTS - frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "hr", "p", "pre"}
)


@dataclass(frozen=True)
class Heuristics:
    """What the heuristic layer could extract, before any overrides."""

    number: int | None
    title: str
    description: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class HtmlFacts:
    """The raw facts a single HTML prefix parse produces."""

    title: str = ""
    subtitle: str = ""
    paragraph: str = ""
    tag_text: str = ""


def heuristic_metadata(
    relative_path: str,
    category_label: str,
    html: str | None = None,
) -> Heuristics:
    """Derive metadata for *relative_path*; never raises.

    *html* is the artifact's leading text (the scanner caps it at 64 KB) or
    ``None`` for unreadable/non-HTML artifacts. The category label always leads
    the tag list (ADR-0005).
    """
    try:
        return _extract(relative_path, category_label, html)
    except Exception:  # pragma: no cover - the fallback is the contract
        log.debug("heuristics failed for %s", relative_path, exc_info=True)
        number, title = _filename_metadata(relative_path)
        return Heuristics(
            number=number,
            title=title,
            description="",
            tags=(category_label,),
        )


def _extract(relative_path: str, category_label: str, html: str | None) -> Heuristics:
    facts = _parse_html(html) if html else HtmlFacts()

    title = ""
    title_number: int | None = None
    lesson_match = _LESSON_TITLE.match(facts.title)
    if lesson_match:
        title_number = int(lesson_match.group(1))
        title = _collapse(lesson_match.group(2))
    else:
        title = facts.title

    filename_number, filename_title = _filename_metadata(relative_path)
    number = filename_number if filename_number is not None else title_number

    if not title:
        title = filename_title

    description = facts.subtitle or facts.paragraph
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT].rstrip() + ELLIPSIS

    return Heuristics(
        number=number,
        title=title,
        description=description,
        tags=(category_label, *_tag_tokens(facts.tag_text, category_label)),
    )


def _parse_html(html: str) -> HtmlFacts:
    parser = _FactsParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is tolerant already
        log.debug("HTML prefix parse failed", exc_info=True)
    return parser.facts()


def _tag_tokens(text: str, category_label: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for raw in _TAG_SEPARATOR.split(text):
        token = _collapse(raw)
        if not token or token == category_label or _NOISE_TAG.match(token):
            continue
        if token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _filename_title(stem: str) -> str:
    slug = _NUMBER_PREFIX.sub("", stem)
    return " ".join(
        word[:1].upper() + word[1:] for word in _SLUG_SEPARATOR.split(slug) if word
    )


def _filename_metadata(relative_path: str) -> tuple[int | None, str]:
    """Number and title as derived from the filename alone."""
    stem = PurePosixPath(relative_path).stem
    match = _NUMBER_PREFIX.match(stem)
    number = int(match.group(1)) if match else None
    return number, _filename_title(stem)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


class _FactsParser(HTMLParser):
    """Collect the first title/subtitle/paragraph/tag-element texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._open: list[str] = []
        self._title_parts: list[str] | None = None
        self._title_done: bool = False
        self._subtitle_element: str | None = None
        self._subtitle_parts: list[str] | None = None
        self._paragraph_element: str | None = None
        self._paragraph_parts: list[str] | None = None
        self._tag_element: str | None = None
        self._tag_parts: list[str] | None = None

    def facts(self) -> HtmlFacts:
        return HtmlFacts(
            title=_collapse("".join(self._title_parts or [])),
            subtitle=_collapse("".join(self._subtitle_parts or [])),
            paragraph=_collapse("".join(self._paragraph_parts or [])),
            tag_text=_collapse("".join(self._tag_parts or [])),
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag in _BLOCK_ELEMENTS:
            self._stop_block_captures(tag)
        if self._title_parts is None and not self._title_done and tag == "title":
            self._title_parts = []
        if self._subtitle_parts is None and "subtitle" in classes:
            self._subtitle_element = tag
            self._subtitle_parts = []
        if self._paragraph_parts is None and tag == "p":
            self._paragraph_element = tag
            self._paragraph_parts = []
        if self._tag_parts is None and classes & _TAG_CLASSES:
            self._tag_element = tag
            self._tag_parts = []
        if tag not in _VOID_ELEMENTS:
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title_done = True
        if tag in self._open:
            del self._open[self._open.index(tag) :]
        if self._subtitle_element == tag:
            self._subtitle_element = None
        if self._paragraph_element == tag:
            self._paragraph_element = None
        if self._tag_element == tag:
            self._tag_element = None

    def handle_data(self, data: str) -> None:
        if "title" in self._open and not self._title_done:
            if self._title_parts is None:
                self._title_parts = []
            self._title_parts.append(data)
        if self._subtitle_element in self._open and self._subtitle_parts is not None:
            self._subtitle_parts.append(data)
        if self._paragraph_element in self._open and self._paragraph_parts is not None:
            self._paragraph_parts.append(data)
        if self._tag_element in self._open and self._tag_parts is not None:
            self._tag_parts.append(data)

    def _stop_block_captures(self, tag: str) -> None:
        if tag not in _BLOCK_ELEMENTS:
            return
        # A capture whose element cannot contain block content (p, span, ...)
        # ends here; a container element (div, li, ...) keeps capturing.
        if self._subtitle_element not in _BLOCK_CONTAINERS:
            self._subtitle_element = None
        if self._paragraph_element not in _BLOCK_CONTAINERS:
            self._paragraph_element = None
        if self._tag_element not in _BLOCK_CONTAINERS:
            self._tag_element = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name.lower() == "class" and value:
                return set(value.split())
        return set()
