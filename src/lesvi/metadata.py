"""Metadata resolution: sidecar > ``lesvi:*`` meta tags > heuristics.

ADR-0005, field by field. The heuristic layer reads the numbered filename, the
``<title>`` element, a leading ``.subtitle``/``<p>`` and the first
``.lesson-tag``/``.badge`` element. Agents can enrich an artifact through either
of two channels, each of which wins field by field over the layer below:
a ``<file>.meta.json`` sidecar and ``lesvi:title|description|tags|pin`` meta
tags in the artifact's ``<head>``.

Parsing is defensive by contract: broken sidecar JSON or malformed HTML degrades
to the next source and logs at debug level, never raises into the scanner.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import TypeVar

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 160
ELLIPSIS = "…"

SOURCE_SIDECAR = "sidecar"
SOURCE_META = "meta"
SOURCE_HEURISTIC = "heuristic"

_META_NAME_PREFIX = "lesvi:"
_META_KEYS = frozenset({"title", "description", "tags", "pin"})
_PIN_TRUE = frozenset({"true", "1", "yes", "on"})
_PIN_FALSE = frozenset({"false", "0", "no", "off"})

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

_T = TypeVar("_T")


@dataclass(frozen=True)
class Heuristics:
    """What the heuristic layer could extract, before any overrides."""

    number: int | None
    title: str
    description: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Metadata:
    """A resolved record: heuristics with any overrides already applied."""

    number: int | None
    title: str
    description: str
    tags: tuple[str, ...]
    meta_source: str = SOURCE_HEURISTIC
    pin_seed: bool = False


@dataclass(frozen=True)
class HtmlFacts:
    """The raw facts a single HTML prefix parse produces."""

    title: str = ""
    subtitle: str = ""
    paragraph: str = ""
    tag_text: str = ""
    meta: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Overrides:
    """One enrichment layer's validated fields; ``None`` means "absent"."""

    title: str | None = None
    description: str | None = None
    tags: tuple[str, ...] | None = None
    pin: bool | None = None

    @property
    def provides_display_metadata(self) -> bool:
        """Whether this layer supplied title, description or tags."""
        return (
            self.title is not None
            or self.description is not None
            or self.tags is not None
        )


def heuristic_metadata(
    relative_path: str,
    category_label: str,
    html: str | None = None,
) -> Heuristics:
    """Derive heuristic metadata for *relative_path*; never raises.

    *html* is the artifact's leading text (the scanner caps it at 64 KB) or
    ``None`` for unreadable/non-HTML artifacts. The category label always leads
    the tag list (ADR-0005).
    """
    try:
        facts = _parse_html(html) if html else HtmlFacts()
        return _heuristics_from(relative_path, category_label, facts)
    except Exception:  # pragma: no cover - the fallback is the contract
        log.debug("heuristics failed for %s", relative_path, exc_info=True)
        return _filename_heuristics(relative_path, category_label)


def resolve_metadata(
    relative_path: str,
    category_label: str,
    html: str | None = None,
    sidecar_text: str | None = None,
) -> Metadata:
    """Resolve metadata for *relative_path*; never raises.

    Precedence is field by field (ADR-0005): the sidecar's ``title``,
    ``description``, ``tags`` and ``pin`` win over the ``lesvi:*`` meta tags,
    which win over the heuristics. ``number`` only ever comes from the filename;
    ``pin`` is a *seed* the caller resolves against the reader's pin state.
    """
    try:
        facts = _parse_html(html) if html else HtmlFacts()
        heuristics = _heuristics_from(relative_path, category_label, facts)
        sidecar = _sidecar_overrides(sidecar_text, relative_path)
        meta = _meta_tag_overrides(facts.meta, relative_path)
        return _resolve(heuristics, sidecar, meta)
    except Exception:  # pragma: no cover - the fallback is the contract
        log.debug("metadata resolution failed for %s", relative_path, exc_info=True)
        return _resolved_fallback(relative_path, category_label)


def _resolve(heuristics: Heuristics, sidecar: _Overrides, meta: _Overrides) -> Metadata:
    title = _pick(sidecar.title, meta.title, default=heuristics.title)
    description = _pick(
        sidecar.description, meta.description, default=heuristics.description
    )
    tags = _pick(sidecar.tags, meta.tags, default=heuristics.tags)
    if sidecar.provides_display_metadata:
        source = SOURCE_SIDECAR
    elif meta.provides_display_metadata:
        source = SOURCE_META
    else:
        source = SOURCE_HEURISTIC
    pin = sidecar.pin if sidecar.pin is not None else meta.pin
    return Metadata(
        number=heuristics.number,
        title=title,
        description=description,
        tags=tags,
        meta_source=source,
        pin_seed=pin is True,
    )


def _pick(first: _T | None, second: _T | None, *, default: _T) -> _T:
    """The first non-``None`` value, or *default* when both layers are silent."""
    if first is not None:
        return first
    if second is not None:
        return second
    return default


def _heuristics_from(
    relative_path: str, category_label: str, facts: HtmlFacts
) -> Heuristics:
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


def _filename_heuristics(relative_path: str, category_label: str) -> Heuristics:
    number, title = _filename_metadata(relative_path)
    return Heuristics(
        number=number,
        title=title,
        description="",
        tags=(category_label,),
    )


def _resolved_fallback(relative_path: str, category_label: str) -> Metadata:
    heuristics = _filename_heuristics(relative_path, category_label)
    return Metadata(
        number=heuristics.number,
        title=heuristics.title,
        description=heuristics.description,
        tags=heuristics.tags,
    )


def _sidecar_overrides(text: str | None, relative_path: str) -> _Overrides:
    """Validate a sidecar's JSON; anything unusable degrades to no overrides."""
    if text is None:
        return _Overrides()
    try:
        raw = json.loads(text.lstrip("\ufeff"))  # editors add BOMs; JSON forbids them
    except ValueError:
        log.debug("unreadable sidecar JSON for %s", relative_path, exc_info=True)
        return _Overrides()
    if not isinstance(raw, dict):
        log.debug("sidecar for %s is not a JSON object", relative_path)
        return _Overrides()
    pin = raw.get("pin")
    if pin is not None and not isinstance(pin, bool):
        log.debug("sidecar pin for %s is not a boolean", relative_path)
        pin = None
    return _Overrides(
        title=_clean_text(raw.get("title"), "sidecar title", relative_path),
        description=_clean_text(
            raw.get("description"), "sidecar description", relative_path
        ),
        tags=_clean_tags(raw.get("tags"), "sidecar tags", relative_path),
        pin=pin,
    )


def _meta_tag_overrides(meta: Mapping[str, str], relative_path: str) -> _Overrides:
    """Validate the ``lesvi:*`` meta tags the HTML parser collected."""
    return _Overrides(
        title=_clean_text(meta.get("title"), "lesvi:title", relative_path),
        description=_clean_text(
            meta.get("description"), "lesvi:description", relative_path
        ),
        tags=_comma_tags(meta.get("tags"), "lesvi:tags", relative_path),
        pin=_pin_flag(meta.get("pin"), relative_path),
    )


def _clean_text(value: object, kind: str, relative_path: str) -> str | None:
    """A collapsed, non-empty string; anything else is absent."""
    if isinstance(value, str):
        cleaned = _collapse(value)
        if cleaned:
            return cleaned
    if value is not None:
        log.debug("ignoring unusable %s for %s: %r", kind, relative_path, value)
    return None


def _clean_tags(value: object, kind: str, relative_path: str) -> tuple[str, ...] | None:
    """A list of distinct non-empty strings; anything else is absent.

    An explicitly empty list is a declaration of "no tags" and clears the
    heuristic tag list; a list with no usable entries is broken input and
    degrades to the next source.
    """
    if not isinstance(value, list):
        if value is not None:
            log.debug("ignoring unusable %s for %s: %r", kind, relative_path, value)
        return None
    if not value:
        return ()
    tokens: list[str] = []
    for item in value:
        token = _clean_text(item, kind, relative_path)
        if token and token not in tokens:
            tokens.append(token)
    if not tokens:
        log.debug("ignoring unusable %s for %s: %r", kind, relative_path, value)
        return None
    return tuple(tokens)


def _comma_tags(
    value: str | None, kind: str, relative_path: str
) -> tuple[str, ...] | None:
    """A ``lesvi:tags`` content value: comma-separated, collapsed, deduped."""
    if value is None:
        return None
    tokens: list[str] = []
    for raw in value.split(","):
        token = _collapse(raw)
        if token and token not in tokens:
            tokens.append(token)
    if not tokens:
        log.debug("ignoring unusable %s for %s: %r", kind, relative_path, value)
        return None
    return tuple(tokens)


def _pin_flag(value: str | None, relative_path: str) -> bool | None:
    """A ``lesvi:pin`` content value; unrecognised values are ignored."""
    if value is None:
        return None
    token = value.strip().lower()
    if token in _PIN_TRUE:
        return True
    if token in _PIN_FALSE:
        return False
    log.debug("ignoring lesvi:pin value %r for %s", value, relative_path)
    return None


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
    """Collect the first title/subtitle/paragraph/tag-element texts and meta tags."""

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
        self._meta: dict[str, str] = {}

    def facts(self) -> HtmlFacts:
        return HtmlFacts(
            title=_collapse("".join(self._title_parts or [])),
            subtitle=_collapse("".join(self._subtitle_parts or [])),
            paragraph=_collapse("".join(self._paragraph_parts or [])),
            tag_text=_collapse("".join(self._tag_parts or [])),
            meta=dict(self._meta),
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag in _BLOCK_ELEMENTS:
            self._stop_block_captures(tag)
        if tag == "meta":
            self._capture_meta(attrs)
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

    def _capture_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        """Remember the first ``lesvi:*`` meta tag of each name."""
        name = ""
        content = ""
        for attr_name, value in attrs:
            lowered = attr_name.lower()
            if lowered == "name" and value:
                name = value
            elif lowered == "content":
                content = value or ""
        key = name.strip().lower()
        if not key.startswith(_META_NAME_PREFIX):
            return
        key = key[len(_META_NAME_PREFIX) :]
        if key not in _META_KEYS:
            if key:
                log.debug("ignoring unknown lesvi:* meta tag %r", key)
            return
        if not content:
            # An empty declaration is not a decision: a later tag may still win.
            log.debug("ignoring empty lesvi:%s meta tag", key)
            return
        if key not in self._meta:
            self._meta[key] = content

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
