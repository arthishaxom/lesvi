"""Tests for ``lesvi.metadata``: the ADR-0005 resolution layers.

``heuristic_metadata`` is a pure function over the relative path, the category
label and (optionally) the first 64 KB of the artifact's text.
``resolve_metadata`` layers the two enrichment channels on top, field by
field: sidecar JSON > ``lesvi:*`` meta tags > heuristics. Every layer is
defensive: broken input degrades to the next source, never raises.
"""

from __future__ import annotations

import json
import logging

import pytest

import lesvi.metadata as metadata_module
from lesvi.metadata import heuristic_metadata, resolve_metadata


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
    def boom(_html: str) -> metadata_module.HtmlFacts:
        raise RuntimeError("boom")

    monkeypatch.setattr(metadata_module, "_parse_html", boom)

    metadata = metadata_module.heuristic_metadata(
        "lessons/0008-kafka-basics.html", "Lessons", "<p>ignored</p>"
    )

    assert metadata.number == 8
    assert metadata.title == "Kafka Basics"
    assert metadata.description == ""
    assert metadata.tags == ("Lessons",)


# --- sidecar JSON and ``lesvi:*`` meta tags -----------------------------------


def test_sidecar_overrides_title_description_and_tags() -> None:
    html = (
        "<title>Lesson 24 — Kafka Basics</title>"
        "<p class='subtitle'>Heuristic description</p>"
        "<span class='badge'>Track A</span>"
    )
    sidecar = json.dumps(
        {
            "title": "Kafka, end to end",
            "description": "Consumer groups and offsets",
            "tags": ["Track B", "Kafka"],
        }
    )

    metadata = resolve_metadata(
        "lessons/0024-kafka.html", "Lessons", html, sidecar_text=sidecar
    )

    assert metadata.number == 24
    assert metadata.title == "Kafka, end to end"
    assert metadata.description == "Consumer groups and offsets"
    assert metadata.tags == ("Track B", "Kafka")
    assert metadata.meta_source == "sidecar"
    assert metadata.pin_seed is False


def test_meta_tags_override_heuristics_when_no_sidecar_exists() -> None:
    html = (
        "<title>Lesson 24 — Kafka Basics</title>"
        "<meta name='lesvi:title' content='Kafka, end to end'>"
        "<meta name='lesvi:description' content='Consumer groups and offsets'>"
        "<meta name='lesvi:tags' content='Track B, Kafka'>"
    )

    metadata = resolve_metadata("lessons/0024-kafka.html", "Lessons", html)

    assert metadata.number == 24
    assert metadata.title == "Kafka, end to end"
    assert metadata.description == "Consumer groups and offsets"
    assert metadata.tags == ("Track B", "Kafka")
    assert metadata.meta_source == "meta"


def test_precedence_is_field_by_field_not_all_or_nothing() -> None:
    html = "<title>Lesson 24 — Kafka Basics</title><p>Heuristic description.</p>"
    sidecar = '{"title": "From the sidecar"}'

    metadata = resolve_metadata(
        "lessons/0024-kafka.html", "Lessons", html, sidecar_text=sidecar
    )

    assert metadata.title == "From the sidecar"  # sidecar
    assert metadata.description == "Heuristic description."  # heuristics
    assert metadata.tags == ("Lessons",)  # heuristics
    assert metadata.meta_source == "sidecar"


def test_sidecar_beats_meta_tags_field_by_field() -> None:
    html = (
        "<title>Lesson 24 — Kafka Basics</title>"
        "<meta name='lesvi:title' content='From the meta tag'>"
        "<meta name='lesvi:description' content='From the meta tag'>"
        "<meta name='lesvi:tags' content='From the meta tag'>"
    )
    sidecar = '{"title": "From the sidecar", "tags": ["Sidecar Tag"]}'

    metadata = resolve_metadata(
        "lessons/0024-kafka.html", "Lessons", html, sidecar_text=sidecar
    )

    assert metadata.title == "From the sidecar"
    assert metadata.description == "From the meta tag"
    assert metadata.tags == ("Sidecar Tag",)
    assert metadata.meta_source == "sidecar"


def test_meta_tags_fall_back_to_heuristics_field_by_field() -> None:
    html = (
        "<title>Lesson 24 — Kafka Basics</title>"
        "<p class='subtitle'>Heuristic description</p>"
        "<meta name='lesvi:tags' content='Track B'>"
    )

    metadata = resolve_metadata("lessons/0024-kafka.html", "Lessons", html)

    assert metadata.title == "Kafka Basics"
    assert metadata.description == "Heuristic description"
    assert metadata.tags == ("Track B",)
    assert metadata.meta_source == "meta"


def test_an_invalid_sidecar_degrades_to_meta_tags_then_heuristics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    html = (
        "<title>Lesson 24 — Kafka Basics</title>"
        "<meta name='lesvi:tags' content='Track B'>"
    )

    with caplog.at_level(logging.DEBUG, logger="lesvi.metadata"):
        metadata = resolve_metadata(
            "lessons/0024-kafka.html",
            "Lessons",
            html,
            sidecar_text="{not json",
        )

    assert metadata.title == "Kafka Basics"
    assert metadata.tags == ("Track B",)
    assert metadata.meta_source == "meta"
    assert caplog.records


@pytest.mark.parametrize(
    "sidecar",
    ["[1, 2]", '"title"', "null", "5", '{"title": "  "}'],
)
def test_a_broken_sidecar_never_overrides(
    sidecar: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="lesvi.metadata"):
        metadata = resolve_metadata(
            "lessons/0001-x.html",
            "Lessons",
            "<title>Real Title</title>",
            sidecar_text=sidecar,
        )

    assert metadata.title == "Real Title"
    assert metadata.meta_source == "heuristic"
    assert caplog.records


def test_a_null_sidecar_field_is_absent_not_broken() -> None:
    metadata = resolve_metadata(
        "lessons/0001-x.html",
        "Lessons",
        "<title>Real Title</title>",
        '{"title": null}',
    )

    assert metadata.title == "Real Title"
    assert metadata.meta_source == "heuristic"


def test_a_bom_prefixed_sidecar_is_still_read() -> None:
    metadata = resolve_metadata(
        "lessons/0001-x.html", "Lessons", "", '\ufeff{"title": "BOM Title"}'
    )

    assert metadata.title == "BOM Title"
    assert metadata.meta_source == "sidecar"


def test_an_empty_sidecar_tag_list_declares_no_tags() -> None:
    metadata = resolve_metadata(
        "lessons/0001-x.html",
        "Lessons",
        "<title>Alpha</title><span class='badge'>Track A</span>",
        '{"tags": []}',
    )

    assert metadata.tags == ()
    assert metadata.meta_source == "sidecar"


def test_a_list_of_only_unusable_sidecar_tags_degrades() -> None:
    metadata = resolve_metadata(
        "lessons/0001-x.html", "Lessons", "", '{"tags": [42, "  ", null]}'
    )

    assert metadata.tags == ("Lessons",)
    assert metadata.meta_source == "heuristic"


def test_a_pin_only_sidecar_seeds_without_changing_display_metadata() -> None:
    metadata = resolve_metadata(
        "lessons/0001-x.html", "Lessons", "<title>Alpha</title>", '{"pin": true}'
    )

    assert metadata.title == "Alpha"
    assert metadata.meta_source == "heuristic"
    assert metadata.pin_seed is True


def test_an_invalid_sidecar_field_degrades_without_dropping_valid_ones() -> None:
    sidecar = '{"title": "Sidecar Title", "tags": "not a list"}'

    metadata = resolve_metadata(
        "lessons/0001-x.html", "Lessons", "<title>Real Title</title>", sidecar
    )

    assert metadata.title == "Sidecar Title"
    assert metadata.tags == ("Lessons",)
    assert metadata.meta_source == "sidecar"


def test_sidecar_tags_keep_the_valid_entries_and_drop_the_rest() -> None:
    sidecar = '{"tags": ["Track B", "", 42, "Track B ", " Kafka ", null]}'

    metadata = resolve_metadata("lessons/0001-x.html", "Lessons", "", sidecar)

    assert metadata.tags == ("Track B", "Kafka")


def test_sidecar_unknown_keys_are_ignored() -> None:
    sidecar = '{"title": "Sidecar Title", "number": 99, "extra": true}'

    metadata = resolve_metadata(
        "lessons/0024-kafka.html", "Lessons", "<title>Lesson 24 — Kafka</title>", sidecar
    )

    assert metadata.number == 24  # number has no override channel
    assert metadata.title == "Sidecar Title"


def test_sidecar_description_is_respected_whole_not_capped() -> None:
    long_description = "x" * 200

    metadata = resolve_metadata(
        "lessons/0001-x.html",
        "Lessons",
        "",
        json.dumps({"description": long_description}),
    )

    assert metadata.description == long_description


def test_only_the_first_meta_tag_of_each_name_counts() -> None:
    html = (
        "<meta name='lesvi:title' content='First'>"
        "<meta name='lesvi:title' content='Second'>"
    )

    assert resolve_metadata("lessons/0001-x.html", "Lessons", html).title == "First"


def test_an_empty_first_meta_tag_does_not_block_a_later_one() -> None:
    html = (
        "<meta name='lesvi:title'>"
        "<meta name='lesvi:title' content='The real one'>"
    )

    assert resolve_metadata("lessons/0001-x.html", "Lessons", html).title == "The real one"


def test_meta_tag_names_are_case_insensitive_and_trimmed() -> None:
    html = "<meta NAME=' Lesvi:Title ' content='Tolerant'>"

    assert resolve_metadata("lessons/0001-x.html", "Lessons", html).title == "Tolerant"


def test_meta_tags_work_with_content_before_name() -> None:
    html = "<meta content='Kafka' name='lesvi:title'>"

    assert resolve_metadata("lessons/0001-x.html", "Lessons", html).title == "Kafka"


@pytest.mark.parametrize(
    "meta", ["<meta name='lesvi:title'>", "<meta name='lesvi:other' content='x'>"]
)
def test_meta_tags_without_usable_content_are_ignored(
    meta: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="lesvi.metadata"):
        metadata = resolve_metadata(
            "lessons/0001-x.html", "Lessons", f"<title>Alpha</title>{meta}"
        )

    assert metadata.title == "Alpha"
    assert metadata.meta_source == "heuristic"
    assert caplog.records


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_truthy_lesvi_pin_values_seed_a_pin(value: str) -> None:
    html = f"<title>Alpha</title><meta name='lesvi:pin' content='{value}'>"

    metadata = resolve_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.pin_seed is True
    assert metadata.meta_source == "heuristic"  # pin is not display metadata


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_falsey_lesvi_pin_values_do_not_seed_a_pin(value: str) -> None:
    html = f"<title>Alpha</title><meta name='lesvi:pin' content='{value}'>"

    assert resolve_metadata("lessons/0001-x.html", "Lessons", html).pin_seed is False


def test_a_garbage_lesvi_pin_value_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    html = "<title>Alpha</title><meta name='lesvi:pin' content='maybe'>"

    with caplog.at_level(logging.DEBUG, logger="lesvi.metadata"):
        metadata = resolve_metadata("lessons/0001-x.html", "Lessons", html)

    assert metadata.pin_seed is False
    assert caplog.records


def test_a_sidecar_pin_overrides_a_meta_tag_pin() -> None:
    html = "<title>Alpha</title><meta name='lesvi:pin' content='true'>"

    metadata = resolve_metadata(
        "lessons/0001-x.html", "Lessons", html, '{"pin": false}'
    )

    assert metadata.pin_seed is False


def test_a_sidecar_pin_must_be_a_boolean() -> None:
    metadata = resolve_metadata(
        "lessons/0001-x.html", "Lessons", "<title>Alpha</title>", '{"pin": "true"}'
    )

    assert metadata.pin_seed is False


def test_resolve_metadata_never_raises_when_the_parser_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_html: str) -> metadata_module.HtmlFacts:
        raise RuntimeError("boom")

    monkeypatch.setattr(metadata_module, "_parse_html", boom)

    metadata = resolve_metadata(
        "lessons/0008-kafka-basics.html",
        "Lessons",
        "<p>ignored</p>",
        '{"title": "Sidecar Title"}',
    )

    assert metadata.number == 8
    assert metadata.title == "Kafka Basics"
    assert metadata.tags == ("Lessons",)
