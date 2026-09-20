"""Tests for ``lesvi.scanner``: walking shelves into artifact records."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import pytest

from lesvi.config import PRESET_CATEGORIES, PRESET_IGNORES, Config
from lesvi.scanner import (
    HTML_READ_LIMIT,
    SIDECAR_READ_LIMIT,
    _format_mtime,  # pyright: ignore[reportPrivateUsage]
    scan,
    scan_shelf,
)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _preset_shelf(root: Path) -> None:
    """A miniature shelf in the real layout, plus things that must be ignored."""
    _write(
        root,
        "lessons/0001-alpha.html",
        "<title>Lesson 1 — Alpha</title><p class='subtitle'>First lesson</p>",
    )
    _write(root, "lessons/0002-beta.html", "<title>Lesson 2 — Beta</title>")
    _write(root, "reference/cheatsheet.html", "<title>Cheatsheet</title>")
    _write(root, "reference/kafka/deep.html", "<title>Deep</title>")
    _write(root, "research/notes.md", "# notes")
    _write(root, "reference/research/notes.md", "# notes")
    _write(root, "lessons/notes.txt", "not an artifact")
    _write(root, "assets/styles.css", "body {}")
    _write(root, "learning-records/export.html", "<title>Export</title>")
    _write(root, "index.html", "<title>Hand-made</title>")
    _write(root, "node_modules/pkg/readme.html", "<title>Vendor</title>")
    _write(root, ".hidden/secret.html", "<title>Secret</title>")


def test_scan_shelf_matches_preset_globs_and_applies_ignores(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _preset_shelf(shelf)

    records = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)

    paths = [record.path for record in records]
    assert paths == sorted(paths)
    assert set(paths) == {
        "lessons/0001-alpha.html",
        "lessons/0002-beta.html",
        "reference/cheatsheet.html",
        "reference/kafka/deep.html",
        "reference/research/notes.md",
        "research/notes.md",
    }
    by_path = {record.path: record for record in records}
    assert by_path["lessons/0001-alpha.html"].category == "Lessons"
    assert by_path["reference/kafka/deep.html"].category == "Reference"
    assert by_path["research/notes.md"].category == "Research"


def test_scan_shelf_records_carry_url_size_mtime_and_source(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _preset_shelf(shelf)
    lesson = shelf / "lessons" / "0001-alpha.html"

    record = next(
        item
        for item in scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)
        if item.path == "lessons/0001-alpha.html"
    )

    assert record.shelf == "s"
    assert record.url == "/a/s/lessons/0001-alpha.html"
    assert record.number == 1
    assert record.title == "Alpha"
    assert record.description == "First lesson"
    assert record.tags == ("Lessons",)
    assert record.size == lesson.stat().st_size
    assert record.pinned is False
    assert record.meta_source == "heuristic"
    stamped = datetime.fromisoformat(record.mtime)
    assert stamped.tzinfo is not None
    assert abs(stamped.timestamp() - lesson.stat().st_mtime) < 1


def test_scan_shelf_percent_encodes_url_paths(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0003-café notes.html", "<p>x</p>")

    records = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)

    assert records[0].url == "/a/s/lessons/0003-caf%C3%A9%20notes.html"


def test_scan_shelf_uses_category_and_ignore_overrides(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "notes/one.md", "# one")
    _write(shelf, "notes/private/secret.md", "# secret")
    _write(shelf, "lessons/0001-x.html", "<p>x</p>")

    records = scan_shelf(
        "s",
        shelf,
        {"notes": ("notes/**/*.md",)},
        ("notes/private/**",),
    )

    assert [(record.path, record.category) for record in records] == [
        ("notes/one.md", "Notes")
    ]


def test_first_matching_category_wins(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-x.html", "<p>x</p>")

    records = scan_shelf(
        "s",
        shelf,
        {"first": ("lessons/*.html",), "second": ("lessons/**/*.html",)},
        (),
    )

    assert [record.category for record in records] == ["First"]


def test_scan_shelf_of_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert scan_shelf("s", tmp_path / "nope", PRESET_CATEGORIES, PRESET_IGNORES) == []


def test_scan_shelf_only_reads_the_first_64_kb_for_metadata(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    padding = "<!-- " + ("x" * (HTML_READ_LIMIT + 1024)) + " -->"
    _write(
        shelf,
        "lessons/0009-late-filename.html",
        f"{padding}<title>Lesson 9 — Late Title</title>",
    )

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.number == 9
    assert record.title == "Late Filename"  # the title sits beyond the cap


def test_scan_config_walks_every_shelf_and_skips_missing_roots(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    alpha = tmp_path / "alpha"
    _preset_shelf(alpha)
    config_file.write_text(
        f'[shelves.alpha]\npath = "{alpha}"\n\n[shelves.ghost]\npath = "{tmp_path / "ghost"}"\n'
    )

    records = scan(Config.load(config_file))

    assert {record.shelf for record in records} == {"alpha"}
    assert len(records) == 6


def test_scan_shelf_skips_names_that_cannot_be_url_encoded(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    lessons = shelf / "lessons"
    lessons.mkdir(parents=True)
    hostile = os.fsdecode(b"0001-bad-\xff.html")  # surrogateescape'd on Linux
    (lessons / hostile).write_text("<title>Hostile</title>")
    (lessons / "0002-good.html").write_text("<p>x</p>")

    records = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)

    assert [record.path for record in records] == ["lessons/0002-good.html"]


@pytest.mark.parametrize(
    "future", [253_402_300_800, 10**18]
)  # year 10000, then far beyond
def test_scan_shelf_degrades_unrepresentable_mtimes(
    tmp_path: Path, future: int
) -> None:
    shelf = tmp_path / "shelf"
    lesson = _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    try:
        os.utime(lesson, (future, future))
    except (OverflowError, OSError):
        pytest.skip("filesystem rejects the test timestamp")
    if lesson.stat().st_mtime != float(future):
        pytest.skip("filesystem clamped the test timestamp")

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    stamped = datetime.fromisoformat(record.mtime)
    assert stamped.tzinfo is not None
    assert stamped.timestamp() == 0  # degraded, not crashed


def test_format_mtime_degrades_unrepresentable_values() -> None:
    # Direct test so the degradation stays covered on filesystems (e.g. ext4)
    # that cannot store the out-of-range timestamps used above.
    for timestamp in (253_402_300_800.0, 10**18, 0.0):
        stamped = datetime.fromisoformat(_format_mtime(timestamp, Path("x.html")))
        assert stamped.tzinfo is not None
        assert stamped.timestamp() == 0

    formatted = _format_mtime(1_700_000_000.0, Path("x.html"))
    assert datetime.fromisoformat(formatted).timestamp() == 1_700_000_000


def test_scan_shelf_skips_symlinks_that_escape_the_shelf_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.html"
    outside.write_text("<title>Outside Secret</title>")
    shelf = tmp_path / "shelf"
    lessons = shelf / "lessons"
    lessons.mkdir(parents=True)
    os.symlink(outside, lessons / "0003-outside.html")
    os.symlink(tmp_path / "missing.html", lessons / "0004-broken.html")
    (lessons / "0005-inside.html").write_text("<p>inside</p>")

    records = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)

    assert [record.path for record in records] == ["lessons/0005-inside.html"]


def test_scan_shelf_keeps_symlinks_that_stay_inside_the_shelf_root(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    target = _write(shelf, "reference/1000-source.html", "<title>Source</title>")
    lessons = shelf / "lessons"
    lessons.mkdir(parents=True)
    os.symlink(target, lessons / "0006-alias.html")

    records = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)

    assert [record.path for record in records] == [
        "lessons/0006-alias.html",
        "reference/1000-source.html",
    ]


def test_scan_shelf_skips_non_regular_files(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    lessons = shelf / "lessons"
    lessons.mkdir(parents=True)
    os.mkfifo(lessons / "0007-pipe.html")  # opening it would block forever
    (lessons / "0008-real.html").write_text("<p>x</p>")

    records = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)

    assert [record.path for record in records] == ["lessons/0008-real.html"]


# --- metadata overrides from sidecars and lesvi:* meta tags -------------------


def test_scan_shelf_applies_a_sidecar_next_to_the_artifact(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    _write(
        shelf,
        "lessons/0001-alpha.html.meta.json",
        '{"title": "Alpha, enriched", "description": "From the sidecar", '
        '"tags": ["Track B"], "pin": true}',
    )

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.title == "Alpha, enriched"
    assert record.description == "From the sidecar"
    assert record.tags == ("Track B",)
    assert record.meta_source == "sidecar"
    assert record.pin_seed is True


def test_scan_shelf_applies_lesvi_meta_tags(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(
        shelf,
        "lessons/0001-alpha.html",
        "<title>Lesson 1 — Alpha</title>"
        "<meta name='lesvi:title' content='Alpha from meta'>"
        "<meta name='lesvi:tags' content='Track B, Kafka'>"
        "<meta name='lesvi:pin' content='yes'>",
    )

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.title == "Alpha from meta"
    assert record.tags == ("Track B", "Kafka")
    assert record.meta_source == "meta"
    assert record.pin_seed is True


def test_sidecar_files_are_never_indexed_even_when_globs_match_them(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>x</p>")
    _write(shelf, "lessons/0001-alpha.html.meta.json", '{"title": "Override"}')

    records = scan_shelf("s", shelf, {"all": ("lessons/**",)}, ())

    assert [record.path for record in records] == ["lessons/0001-alpha.html"]
    assert records[0].title == "Override"


def test_a_broken_sidecar_leaves_the_artifact_indexable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    _write(shelf, "lessons/0001-alpha.html.meta.json", "{broken")

    with caplog.at_level(logging.DEBUG, logger="lesvi.metadata"):
        record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.title == "Alpha"
    assert record.meta_source == "heuristic"
    assert caplog.records


def test_an_unreadable_sidecar_does_not_hide_the_artifact(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    sidecar = shelf / "lessons" / "0001-alpha.html.meta.json"
    sidecar.mkdir()  # a directory, not a file

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.title == "Alpha"


def test_an_oversized_sidecar_degrades_to_heuristics(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    _write(
        shelf,
        "lessons/0001-alpha.html.meta.json",
        '{"title": "' + "x" * (SIDECAR_READ_LIMIT + 100) + '"}',
    )

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.title == "Alpha"
    assert record.meta_source == "heuristic"


def test_a_sidecar_symlink_outside_the_shelf_root_is_ignored(tmp_path: Path) -> None:
    outside = tmp_path / "outside.meta.json"
    outside.write_text('{"title": "Outside"}')
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    os.symlink(outside, shelf / "lessons" / "0001-alpha.html.meta.json")

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.title == "Alpha"
    assert record.meta_source == "heuristic"


def test_a_sidecar_symlink_inside_the_shelf_root_is_followed(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    target = _write(shelf, "reference/shared.meta.json", '{"title": "Shared"}')
    os.symlink(target, shelf / "lessons" / "0001-alpha.html.meta.json")

    record = scan_shelf("s", shelf, PRESET_CATEGORIES, PRESET_IGNORES)[0]

    assert record.title == "Shared"
