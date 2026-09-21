"""Tests for ``lesvi.render``: the home feed and shelf pages, as HTML.

Rendered pages are read back through :func:`parse_page`, a small test-side HTML
reader, so assertions are about the document a browser would see rather than
about the template's formatting.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from lesvi.config import Config
from lesvi.index import Index
from lesvi.render import HOME_PAGE_SIZE, relative_time, render_home, render_shelf

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

LESSON = (
    "<!doctype html><html><head>"
    "<title>Lesson 24 — Apache Kafka Fundamentals</title></head>"
    "<body><p class='subtitle'>Topics, partitions and consumer groups.</p>"
    "<span class='lesson-tag'>Track B · Streaming · Deep Dive</span>"
    "</body></html>"
)

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

_CARD_FIELDS = {
    "card-shelf": "shelf",
    "card-number": "number",
    "card-title": "title",
    "card-description": "description",
}


@dataclass
class PageCard:
    href: str = ""
    shelf: str = ""
    number: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    time: str = ""
    datetime: str = ""
    search: str = ""
    pin_pressed: bool = False
    pin_shelf: str = ""
    pin_path: str = ""
    pin_label: str = ""
    pin_hidden: bool = False


@dataclass
class PageHeading:
    level: int
    text: str


@dataclass
class PageLink:
    href: str
    text: str
    classes: tuple[str, ...]
    current: bool


@dataclass
class _Element:
    tag: str
    classes: tuple[str, ...]
    card: PageCard | None = None
    heading: PageHeading | None = None
    link: PageLink | None = None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[PageCard] = []
        self.headings: list[PageHeading] = []
        self.links: list[PageLink] = []
        self._open: list[_Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = tuple((attributes.get("class") or "").split())
        element = _Element(tag=tag, classes=classes)
        card = self._open_card()
        if tag == "article" and "card" in classes:
            element.card = PageCard(search=attributes.get("data-search") or "")
        if card is not None and tag == "button" and "pin-toggle" in classes:
            card.pin_pressed = attributes.get("aria-pressed") == "true"
            card.pin_shelf = attributes.get("data-shelf") or ""
            card.pin_path = attributes.get("data-path") or ""
            card.pin_label = attributes.get("aria-label") or ""
            card.pin_hidden = "hidden" in attributes
        if tag in ("h1", "h2", "h3") and card is None:
            element.heading = PageHeading(int(tag[1]), "")
        href = attributes.get("href")
        if tag == "a" and href is not None:
            element.link = PageLink(
                href=href,
                text="",
                classes=classes,
                current=attributes.get("aria-current") == "page",
            )
        if card is not None:
            if element.link is not None and "card-link" in classes:
                card.href = href or ""
            if tag == "time" and "card-time" in classes:
                card.datetime = attributes.get("datetime") or ""
            if "card-tag" in classes:
                card.tags.append("")
        if tag not in _VOID_ELEMENTS:
            self._open.append(element)

    def handle_endtag(self, tag: str) -> None:
        index = next(
            (i for i in range(len(self._open) - 1, -1, -1) if self._open[i].tag == tag),
            None,
        )
        if index is None:
            return
        while len(self._open) > index:
            self._close(self._open.pop())

    def handle_data(self, data: str) -> None:
        field_name: str | None = None
        for element in reversed(self._open):
            if element.link is not None:
                element.link.text += data
            if element.heading is not None:
                element.heading.text += data
            if "card-time" in element.classes:
                field_name = "time"
            if "card-tag" in element.classes:
                field_name = "card-tag"
            for class_name, name in _CARD_FIELDS.items():
                if class_name in element.classes:
                    field_name = name
            if element.card is not None:
                if field_name == "card-tag":
                    element.card.tags[-1] += data
                elif field_name == "time":
                    element.card.time += data
                elif field_name is not None:
                    setattr(
                        element.card,
                        field_name,
                        getattr(element.card, field_name) + data,
                    )
                return

    def _open_card(self) -> PageCard | None:
        for element in reversed(self._open):
            if element.card is not None:
                return element.card
        return None

    def _close(self, element: _Element) -> None:
        if element.card is not None:
            self.cards.append(element.card)
        if element.heading is not None:
            self.headings.append(element.heading)
        if element.link is not None:
            self.links.append(element.link)


@dataclass
class Page:
    """A rendered page as a test would inspect it: cards, headings, links."""

    cards: list[PageCard]
    headings: list[PageHeading]
    links: list[PageLink]

    def link(self, class_name: str) -> PageLink | None:
        return next((link for link in self.links if class_name in link.classes), None)

    def links_with(self, class_name: str) -> list[PageLink]:
        return [link for link in self.links if class_name in link.classes]

    def heading_texts(self) -> list[str]:
        return [heading.text for heading in self.headings]


def parse_page(html: str) -> Page:
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    return Page(cards=parser.cards, headings=parser.headings, links=parser.links)


def _write(root: Path, relative: str, text: str, *, moments_ago: timedelta) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = (NOW - moments_ago).timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def index(tmp_path: Path) -> Index:
    """Two shelves with a few artifacts each; research stays hidden in v1."""
    data = tmp_path / "data-engg"
    writing = tmp_path / "writing"
    _write(
        data,
        "lessons/0005-glossary.html",
        "<title>Glossary</title>",
        moments_ago=timedelta(days=5),
    )
    _write(
        data,
        "lessons/0024-apache-kafka-fundamentals.html",
        LESSON,
        moments_ago=timedelta(hours=2),
    )
    _write(
        data,
        "reference/kafka-cheatsheet.html",
        "<title>Kafka Cheatsheet</title>",
        moments_ago=timedelta(days=3),
    )
    _write(
        writing,
        "lessons/0003-tight-sentences.html",
        "<title>Tight Sentences</title>",
        moments_ago=timedelta(minutes=10),
    )
    _write(data, "research/notes.md", "# notes", moments_ago=timedelta(minutes=1))
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[shelves.data-engg]\npath = "{data}"\n[shelves.writing]\npath = "{writing}"\n'
    )
    return Index.build(Config.load(config_file))


# --- relative time ------------------------------------------------------------


@pytest.mark.parametrize(
    ("ago", "expected"),
    [
        (timedelta(seconds=30), "just now"),
        (timedelta(minutes=5), "5m ago"),
        (timedelta(minutes=59), "59m ago"),
        (timedelta(hours=1), "1h ago"),
        (timedelta(hours=23), "23h ago"),
        (timedelta(days=1), "1d ago"),
        (timedelta(days=6), "6d ago"),
        (timedelta(days=10), "1w ago"),
        (timedelta(days=29), "4w ago"),
        (timedelta(days=30), "1mo ago"),
        (timedelta(days=364), "12mo ago"),
        (timedelta(days=365), "1y ago"),
        (timedelta(days=800), "2y ago"),
    ],
)
def test_relative_time_reads_naturally(ago: timedelta, expected: str) -> None:
    mtime = (NOW - ago).isoformat()

    assert relative_time(mtime, now=NOW) == expected


def test_relative_time_never_goes_negative_or_raises() -> None:
    assert relative_time((NOW + timedelta(hours=1)).isoformat(), now=NOW) == "just now"
    assert relative_time("not a timestamp", now=NOW) == ""


def test_relative_time_tolerates_a_naive_now() -> None:
    naive_now = datetime.fromisoformat("2026-09-20T12:00:00")

    assert (
        relative_time((naive_now - timedelta(hours=2)).isoformat(), now=naive_now)
        == "2h ago"
    )


# --- the home feed ------------------------------------------------------------


def test_home_lists_recent_cards_across_shelves_newest_first(index: Index) -> None:
    page = parse_page(render_home(index, now=NOW))

    assert [card.href for card in page.cards] == [
        "/a/writing/lessons/0003-tight-sentences.html",
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
        "/a/data-engg/reference/kafka-cheatsheet.html",
        "/a/data-engg/lessons/0005-glossary.html",
    ]


def test_home_card_carries_shelf_number_title_description_tags_and_time(
    index: Index,
) -> None:
    page = parse_page(render_home(index, now=NOW))
    kafka = page.cards[1]

    assert kafka.shelf == "data-engg"
    assert kafka.number == "24"
    assert kafka.title == "Apache Kafka Fundamentals"
    assert kafka.description == "Topics, partitions and consumer groups."
    assert kafka.tags == ["Lessons", "Track B", "Streaming"]  # capped at three
    assert kafka.time == "2h ago"
    assert kafka.datetime == (NOW - timedelta(hours=2)).astimezone().isoformat(
        timespec="seconds"
    )


def test_cards_carry_the_search_text_and_a_hidden_pin_toggle(index: Index) -> None:
    kafka = parse_page(render_home(index, now=NOW)).cards[1]

    assert "Apache Kafka Fundamentals" in kafka.search
    assert "Topics, partitions and consumer groups." in kafka.search
    assert "Track B" in kafka.search
    assert kafka.pin_pressed is False
    assert kafka.pin_shelf == "data-engg"
    assert kafka.pin_path == "lessons/0024-apache-kafka-fundamentals.html"
    assert kafka.pin_label == "Pin Apache Kafka Fundamentals"
    assert kafka.pin_hidden is True  # revealed by app.js


def test_a_seeded_pin_renders_a_pressed_unpin_toggle(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(
        shelf,
        "lessons/0001-alpha.html",
        "<title>Alpha</title>",
        moments_ago=timedelta(minutes=1),
    )
    (shelf / "lessons" / "0001-alpha.html.meta.json").write_text('{"pin": true}')
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[shelves.shelf]\npath = "{shelf}"\n')
    index = Index.build(Config.load(config_file))

    card = parse_page(render_home(index, now=NOW)).cards[0]

    assert card.pin_pressed is True
    assert card.pin_label == "Unpin Alpha"


def test_both_pages_carry_a_hidden_search_box_and_a_live_result_count(
    index: Index,
) -> None:
    rendered_pages = (
        render_home(index, now=NOW),
        render_shelf(index, "data-engg", now=NOW),
    )

    for rendered in rendered_pages:
        assert re.search(r'<form class="search"[^>]*hidden', rendered)
        assert 'name="q"' in rendered
        assert 'type="search"' in rendered
        assert 'for="search-input"' in rendered
        assert 'id="search-input"' in rendered
        assert 'role="status" aria-live="polite"' in rendered
        assert 'class="no-results" hidden' in rendered


def test_cards_without_a_number_omit_the_badge(index: Index) -> None:
    cheatsheet = parse_page(render_home(index, now=NOW)).cards[2]

    assert cheatsheet.title == "Kafka Cheatsheet"
    assert cheatsheet.number == ""


def test_home_keeps_research_out_of_the_feed(index: Index) -> None:
    page = parse_page(render_home(index, now=NOW))

    assert not any("research" in card.href for card in page.cards)


def test_home_hides_the_pinned_section_when_nothing_is_pinned(index: Index) -> None:
    page = parse_page(render_home(index, now=NOW))

    assert page.heading_texts() == ["lesvi", "Recently updated"]


def test_home_shows_pinned_cards_before_the_recent_feed(index: Index) -> None:
    shelves = dict(index.shelves)
    writing = shelves["writing"]
    record = replace(writing.curriculum[0], pinned=True)
    shelves["writing"] = replace(writing, curriculum=(record,))
    index = Index(shelves)
    page = parse_page(render_home(index, now=NOW))

    assert page.heading_texts() == ["lesvi", "Pinned", "Recently updated"]
    assert [card.href for card in page.cards] == [
        "/a/writing/lessons/0003-tight-sentences.html",
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
        "/a/data-engg/reference/kafka-cheatsheet.html",
        "/a/data-engg/lessons/0005-glossary.html",
    ]


def test_home_limits_the_feed_and_links_to_show_more(index: Index) -> None:
    page = parse_page(render_home(index, limit=1, now=NOW))
    show_more = page.link("show-more")

    assert len(page.cards) == 1
    assert show_more is not None
    assert show_more.href == "/?limit=2"
    assert show_more.text == "Show more"


def test_home_has_no_show_more_link_when_the_feed_is_complete(index: Index) -> None:
    page = parse_page(render_home(index, limit=HOME_PAGE_SIZE, now=NOW))

    assert len(page.cards) == 4
    assert page.link("show-more") is None


def test_home_escapes_artifact_metadata(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(
        shelf,
        "lessons/0001-fish-and-chips.html",
        "<title>Lesson 1 — Fish &amp; Chips <script>alert(1)</script></title>"
        "<p class='subtitle'>A \"quoted\" &amp; <b>bold</b> bit</p>"
        "<span class='lesson-tag'>A \"tag\" · Two</span>",
        moments_ago=timedelta(minutes=1),
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[shelves.shelf]\npath = "{shelf}"\n')
    index = Index.build(Config.load(config_file))

    rendered = render_home(index, now=NOW)

    assert "<script>alert(1)</script>" not in rendered
    assert "Fish &amp; Chips" in rendered
    assert "A &quot;quoted&quot; &amp; bold bit" in rendered
    assert "A &quot;tag&quot;" in rendered


def test_home_documents_the_shelves_in_the_header(index: Index) -> None:
    page = parse_page(render_home(index, now=NOW))

    shelf_links = {link.href: link.text for link in page.links_with("shelf-link")}

    assert shelf_links == {
        "/s/data-engg/": "data-engg",
        "/s/writing/": "writing",
    }


def test_home_without_artifacts_shows_guidance(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[shelves.ghost]\npath = "{tmp_path / "does-not-exist"}"\n')
    index = Index.build(Config.load(config_file))

    page = parse_page(render_home(index, now=NOW))

    assert page.cards == []
    assert "lesvi add" in render_home(index, now=NOW)


# --- shelf pages --------------------------------------------------------------


def test_shelf_page_orders_cards_by_curriculum_number(index: Index) -> None:
    page = parse_page(render_shelf(index, "data-engg", now=NOW))

    assert page.heading_texts() == ["data-engg", "Lessons", "Reference"]
    assert [card.href for card in page.cards] == [
        "/a/data-engg/lessons/0005-glossary.html",
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
        "/a/data-engg/reference/kafka-cheatsheet.html",
    ]


def test_shelf_page_cards_carry_the_same_search_and_pin_hooks(index: Index) -> None:
    card = parse_page(render_shelf(index, "data-engg", now=NOW)).cards[0]

    assert "Glossary" in card.search
    assert card.pin_shelf == "data-engg"
    assert card.pin_path == "lessons/0005-glossary.html"
    assert card.pin_hidden is True


def test_card_search_and_pin_attributes_escape_metadata(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(
        shelf,
        'lessons/0001-a"b.html',
        "<title>Lesson 1 — A &quot;quoted&quot; title</title>"
        '<p class="subtitle">Say &quot;hi&quot;.</p>',
        moments_ago=timedelta(minutes=1),
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[shelves.shelf]\npath = "{shelf}"\n')
    index = Index.build(Config.load(config_file))

    card = parse_page(render_home(index, now=NOW)).cards[0]

    assert card.search == 'A "quoted" title Say "hi". Lessons'
    assert card.pin_path == 'lessons/0001-a"b.html'
    assert card.pin_label == 'Pin A "quoted" title'


def test_shelf_page_recent_sort_orders_each_section_by_mtime(index: Index) -> None:
    page = parse_page(render_shelf(index, "data-engg", sort="recent", now=NOW))

    assert page.heading_texts() == ["data-engg", "Lessons", "Reference"]
    assert [card.href for card in page.cards] == [
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
        "/a/data-engg/lessons/0005-glossary.html",
        "/a/data-engg/reference/kafka-cheatsheet.html",
    ]


def test_shelf_page_hides_research_and_counts_only_visible_categories(
    index: Index,
) -> None:
    rendered = render_shelf(index, "data-engg", now=NOW)

    assert "research" not in rendered
    assert "3 artifacts" in rendered
    assert "Lessons 2" in rendered
    assert "Reference 1" in rendered


def test_shelf_page_offers_both_sorts_and_marks_the_active_one(
    index: Index,
) -> None:
    curriculum = parse_page(render_shelf(index, "data-engg", now=NOW))
    recent = parse_page(render_shelf(index, "data-engg", sort="recent", now=NOW))

    curriculum_links = {link.text: link for link in curriculum.links_with("sort-link")}
    recent_links = {link.text: link for link in recent.links_with("sort-link")}

    assert set(curriculum_links) == {"Curriculum", "Recent"}
    assert curriculum_links["Curriculum"].href == "/s/data-engg/"
    assert curriculum_links["Curriculum"].current
    assert curriculum_links["Recent"].href == "/s/data-engg/?sort=recent"
    assert recent_links["Recent"].current
    assert not recent_links["Curriculum"].current


def test_shelf_page_marks_the_active_shelf_in_the_header(index: Index) -> None:
    page = parse_page(render_shelf(index, "data-engg", now=NOW))

    active = [link for link in page.links_with("shelf-link") if link.current]

    assert [link.href for link in active] == ["/s/data-engg/"]


def test_shelf_page_for_an_empty_shelf_shows_guidance(tmp_path: Path) -> None:
    shelf = tmp_path / "empty"
    shelf.mkdir()
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[shelves.empty]\npath = "{shelf}"\n')
    index = Index.build(Config.load(config_file))

    page = parse_page(render_shelf(index, "empty", now=NOW))

    assert page.cards == []
    assert "No visible artifacts" in render_shelf(index, "empty", now=NOW)
