"""Tests for ``lesvi.metadata``: heuristic number/title/description/tags parsing.

``heuristic_metadata`` is a pure function over the relative path, the category
label and (optionally) the first 64 KB of the artifact's text. These tests
pin the ADR-0005 heuristic layer; sidecar and ``lesvi:*`` meta tags arrive
with their own ticket.
"""

from __future__ import annotations

import pytest

import lesvi.metadata as metadata_module
from lesvi.metadata import heuristic_metadata


def test_filename_number_and_slug_title_without_html() -> None:
    metadata = heuristic_metadata(
        "lessons/0024-apache-kafka-fundamentals.html", "Lessons"
    )

    assert metadata.number == 24
    assert metadata.title == "Apache Kafka Fundamentals"
    assert metadata.description == ""
    assert metadata.tags == ("Lessons",)


def test_lesson_prefixed_title_is_split_into_number_and_title() -> None:
    html = (
        "<html><head><title>Lesson 24 — Apache Kafka Fundamentals</title></head></html>"
    )

    metadata = heuristic_metadata("lessons/kafka.html", "Lessons", html)

    assert metadata.number == 24
    assert metadata.title == "Apache Kafka Fundamentals"


@pytest.mark.parametrize("separator", ["—", "–", "-", ":"])
def test_lesson_title_prefix_accepts_dash_and_colon_variants(separator: str) -> None:
    html = f"<title>Lesson 0001{separator} Windows to Linux</title>"

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.title == "Windows to Linux"


def test_filename_number_wins_over_a_conflicting_title_number() -> None:
    html = "<title>Lesson 24 — Something Else</title>"

    metadata = heuristic_metadata("lessons/0007-something.html", "Lessons", html)

    assert metadata.number == 7
    assert metadata.title == "Something Else"


def test_bare_lesson_title_is_kept_and_a_filename_number_still_wins() -> None:
    # "Lesson 7" has no separator, so it is not a prefix to strip; the number
    # heuristic only reads the filename prefix (spec section 9).
    metadata = heuristic_metadata(
        "lessons/x.html", "Lessons", "<title>Lesson 7</title>"
    )

    assert metadata.number is None
    assert metadata.title == "Lesson 7"


def test_plain_title_is_used_as_is() -> None:
    metadata = heuristic_metadata(
        "reference/cheatsheet.html", "Reference", "<title>Kafka Cheatsheet</title>"
    )

    assert metadata.title == "Kafka Cheatsheet"
    assert metadata.number is None


def test_empty_html_title_falls_back_to_the_filename() -> None:
    metadata = heuristic_metadata(
        "reference/0100-cheat-sheet.html", "Reference", "<title>   </title>"
    )

    assert metadata.title == "Cheat Sheet"
    assert metadata.number == 100


def test_absent_html_falls_back_to_the_filename() -> None:
    metadata = heuristic_metadata("research/field-notes.md", "Research", None)

    assert metadata.title == "Field Notes"
    assert metadata.number is None
    assert metadata.tags == ("Research",)


def test_description_prefers_the_subtitle_element() -> None:
    html = "<p class='subtitle'>  The   gap </p><p>Ignore me</p>"

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.description == "The gap"


def test_subtitle_text_includes_nested_markup() -> None:
    html = "<p class='subtitle'>A <em>nested</em> subtitle</p>"

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.description == "A nested subtitle"


def test_description_falls_back_to_the_first_paragraph() -> None:
    html = "<h1>Title</h1><p>First <em>paragraph</em> here.</p><p>Second</p>"

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.description == "First paragraph here."


def test_description_collapses_whitespace_and_caps_at_160_with_an_ellipsis() -> None:
    html = f"<p>{'a' * 200}</p>"

    description = heuristic_metadata("lessons/0001-x.html", "Lessons", html).description

    assert description == "a" * 160 + "…"
    assert len(description) == 161


def test_description_that_exactly_fits_is_not_truncated() -> None:
    text = "b" * 160

    description = heuristic_metadata(
        "lessons/0001-x.html", "Lessons", f"<p>{text}</p>"
    ).description

    assert description == text


def test_tags_lead_with_the_category_label_and_split_on_the_middle_dot() -> None:
    html = '<span class="lesson-tag">Lesson 01 · Project Research</span>'

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.tags == ("Lessons", "Project Research")


def test_tags_parse_badges_with_bullets_and_emoji_dropping_noise_tokens() -> None:
    html = '<span class="badge">&#10024; Lesson 0001 &bull; Track B &bull; 15 minutes</span>'

    metadata = heuristic_metadata("ricing/lessons/0001-x.html", "Lessons", html)

    assert metadata.tags == ("Lessons", "Track B")


@pytest.mark.parametrize(
    "noise",
    ["10 minutes", "15 min", "1 hour", "2 hrs", "45 mins", "Lesson 12", "Lesson 0012"],
)
def test_tags_drop_duration_and_lesson_tokens(noise: str) -> None:
    html = f'<span class="lesson-tag">Something · {noise}</span>'

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.tags == ("Lessons", "Something")


def test_tags_use_only_the_first_tag_element() -> None:
    html = (
        '<span class="badge">First Tag</span>'
        '<span class="lesson-tag">Lesson 01 · Second Tag</span>'
    )

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.tags == ("Lessons", "First Tag")


def test_tags_do_not_repeat_the_category_label() -> None:
    html = '<span class="lesson-tag">Lessons · Track B</span>'

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.tags == ("Lessons", "Track B")


def test_tags_without_a_tag_element_are_the_category_label_alone() -> None:
    html = "<h1>No tags here</h1><p>Body</p>"

    metadata = heuristic_metadata("reference/cheatsheet.html", "Reference", html)

    assert metadata.tags == ("Reference",)


def test_malformed_html_never_raises_and_degrades_gracefully() -> None:
    html = "<html><head><title>Lesson 3 — Broken</title><body><p class='subtitle'>oops"

    metadata = heuristic_metadata("lessons/0009-x.html", "Lessons", html)

    assert metadata.number == 9  # filename wins over the half-parsed title
    assert metadata.title == "Broken"
    assert metadata.description == "oops"


def test_garbage_html_never_raises_and_falls_back_to_the_filename() -> None:
    metadata = heuristic_metadata("lessons/0005-x.html", "Lessons", "<html><<<>>>")

    assert metadata.number == 5
    assert metadata.title == "X"
    assert metadata.description == ""


def test_only_the_first_title_element_is_used() -> None:
    html = "<title>Lesson 1 — First</title><title>Second</title>"

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.title == "First"


def test_an_unclosed_subtitle_stops_at_the_next_block_element() -> None:
    html = "<p class='subtitle'>Only this<p>Later paragraph</p>"

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.description == "Only this"


def test_an_unclosed_tag_element_stops_at_the_next_block_element() -> None:
    html = '<span class="badge">Lesson 1 · Track A<div><p>Body</p></div>'

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.tags == ("Lessons", "Track A")


def test_a_block_subtitle_may_wrap_block_content() -> None:
    html = "<div class='subtitle'><h2>Topics, partitions</h2></div><p>Body</p>"

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.description == "Topics, partitions"


def test_a_block_badge_may_wrap_block_content() -> None:
    html = '<div class="badge"><h3>Track B</h3></div>'

    metadata = heuristic_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.tags == ("Lessons", "Track B")


def test_an_exploding_parser_degrades_to_filename_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(html: str) -> metadata_module.HtmlFacts:
        raise RuntimeError("boom")

    monkeypatch.setattr(metadata_module, "_parse_html", boom)

    metadata = metadata_module.heuristic_metadata(
        "lessons/0008-kafka-basics.html", "Lessons", "<p>ignored</p>"
    )

    assert metadata.number == 8
    assert metadata.title == "Kafka Basics"
    assert metadata.description == ""
    assert metadata.tags == ("Lessons",)
