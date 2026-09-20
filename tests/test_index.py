"""Tests for ``lesvi.index``: the in-memory store, sorted views and JSON payload."""

from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from lesvi.config import Config
from lesvi.index import Index


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _shelf_config(tmp_path: Path, **shelves: Path) -> Config:
    """Write and load a config with one ``[shelves.<name>]`` per keyword."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "".join(
            f'[shelves.{name}]\npath = "{path}"\n' for name, path in shelves.items()
        )
    )
    return Config.load(config_file)


def test_build_sorts_curriculum_and_recency(tmp_path: Path) -> None:
    shelf = tmp_path / "data-engg"
    oldest = _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    newest = _write(shelf, "lessons/0002-beta.html", "<title>Lesson 2 — Beta</title>")
    middle = _write(shelf, "reference/cheatsheet.html", "<title>Cheatsheet</title>")
    os.utime(oldest, (1_700_000_000, 1_700_000_000))
    os.utime(middle, (1_700_000_100, 1_700_000_100))
    os.utime(newest, (1_700_000_200, 1_700_000_200))
    index = Index.build(_shelf_config(tmp_path, **{"data-engg": shelf}))

    assert [artifact.path for artifact in index.by_number("data-engg")] == [
        "lessons/0001-alpha.html",
        "lessons/0002-beta.html",
        "reference/cheatsheet.html",
    ]
    assert [artifact.path for artifact in index.by_recency("data-engg")] == [
        "lessons/0002-beta.html",
        "reference/cheatsheet.html",
        "lessons/0001-alpha.html",
    ]


def test_unnumbered_artifacts_sort_after_numbered_ones(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0002-beta.html", "<p>b</p>")
    _write(shelf, "reference/cheatsheet.html", "<p>c</p>")
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))

    assert [artifact.number for artifact in index.by_number("shelf")] == [1, 2, None]


def test_build_counts_categories_and_marks_research_hidden(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "lessons/0002-beta.html", "<p>b</p>")
    _write(shelf, "reference/cheatsheet.html", "<p>c</p>")
    _write(shelf, "research/notes.md", "# n")
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))

    payload = index.to_json()
    categories = {
        item["key"]: item for item in payload["shelves"]["shelf"]["categories"]
    }

    assert categories["lessons"] == {
        "key": "lessons",
        "label": "Lessons",
        "count": 2,
        "hidden": False,
    }
    assert categories["reference"]["count"] == 1
    assert categories["research"] == {
        "key": "research",
        "label": "Research",
        "count": 1,
        "hidden": True,
    }
    assert payload["shelves"]["shelf"]["total"] == 4


def test_to_json_uses_the_documented_artifact_record_shape(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(
        shelf,
        "lessons/0024-apache-kafka-fundamentals.html",
        "<title>Lesson 24 — Apache Kafka Fundamentals</title><p>Topics and offsets.</p>",
    )
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))

    payload = index.to_json()
    record = payload["artifacts"][0]

    assert set(record) == {
        "shelf",
        "path",
        "url",
        "category",
        "number",
        "title",
        "description",
        "tags",
        "mtime",
        "size",
        "pinned",
        "meta_source",
    }
    assert record["shelf"] == "shelf"
    assert record["number"] == 24
    assert record["title"] == "Apache Kafka Fundamentals"
    assert record["tags"] == ["Lessons"]
    assert payload["shelves"]["shelf"]["curriculum"] == [
        "lessons/0024-apache-kafka-fundamentals.html"
    ]
    assert payload["shelves"]["shelf"]["recency"] == [
        "lessons/0024-apache-kafka-fundamentals.html"
    ]


def test_shelf_title_override_wins_and_defaults_to_the_name(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[shelves.custom]\npath = "{shelf}"\ntitle = "Data Engineering"\n'
        f'\n[shelves.plain]\npath = "{shelf}"\n'
    )
    index = Index.build(Config.load(config_file))

    shelves = index.to_json()["shelves"]
    assert shelves["custom"]["title"] == "Data Engineering"
    assert shelves["plain"]["title"] == "plain"


def test_a_missing_shelf_root_stays_in_the_index_with_zero_counts(
    tmp_path: Path,
) -> None:
    index = Index.build(_shelf_config(tmp_path, ghost=tmp_path / "does-not-exist"))

    payload = index.to_json()
    assert payload["shelves"]["ghost"]["total"] == 0
    assert payload["shelves"]["ghost"]["curriculum"] == []
    assert {item["count"] for item in payload["shelves"]["ghost"]["categories"]} == {0}
    assert payload["artifacts"] == []


def test_category_label_collisions_do_not_mix_counts(tmp_path: Path) -> None:
    """Two keys whose labels normalise identically keep separate counts."""
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "reference/cheatsheet.html", "<p>c</p>")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[shelves.shelf]\npath = "{shelf}"\n'
        f"[shelves.shelf.categories]\n"
        f'data-notes = ["lessons/*.html"]\ndata_notes = ["reference/*.html"]\n'
    )
    index = Index.build(Config.load(config_file))

    payload = index.to_json()
    counts = {
        item["key"]: item["count"] for item in payload["shelves"]["shelf"]["categories"]
    }

    assert counts == {"data-notes": 1, "data_notes": 1}


def test_cross_shelf_recency_view_is_newest_first_and_capped(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    oldest = _write(one, "lessons/0001-alpha.html", "<p>a</p>")
    newest = _write(two, "lessons/0002-beta.html", "<p>b</p>")
    middle = _write(one, "reference/cheatsheet.html", "<p>c</p>")
    research = _write(one, "research/notes.md", "# notes")
    os.utime(oldest, (1_700_000_000, 1_700_000_000))
    os.utime(middle, (1_700_000_050, 1_700_000_050))
    os.utime(newest, (1_700_000_100, 1_700_000_100))
    os.utime(research, (1_700_000_200, 1_700_000_200))  # hidden, must stay out
    index = Index.build(_shelf_config(tmp_path, one=one, two=two))

    assert [artifact.path for artifact in index.recent()] == [
        "lessons/0002-beta.html",
        "reference/cheatsheet.html",
        "lessons/0001-alpha.html",
    ]
    assert [artifact.path for artifact in index.recent(limit=2)] == [
        "lessons/0002-beta.html",
        "reference/cheatsheet.html",
    ]


def test_cross_shelf_pinned_view_keeps_only_pins_newest_first(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    first = _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    second = _write(shelf, "lessons/0002-beta.html", "<p>b</p>")
    os.utime(first, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_700_000_100, 1_700_000_100))
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))
    records = index.shelves["shelf"].curriculum
    pinned = replace(records[0], pinned=True)  # the older one
    index = Index(
        {"shelf": replace(index.shelves["shelf"], curriculum=(pinned, records[1]))}
    )

    assert [artifact.path for artifact in index.pinned()] == ["lessons/0001-alpha.html"]
    assert [artifact.path for artifact in index.recent()] == [
        "lessons/0002-beta.html",
        "lessons/0001-alpha.html",
    ]


def test_scale_target_five_thousand_artifacts(tmp_path: Path) -> None:
    """Full scan < 2 s; the JSON the server sends gzipped is < 1 MB."""
    shelf = tmp_path / "scale"
    tracks = ("Foundations", "Processing", "Storage", "Operations")
    for number in range(5000):
        track = tracks[number % len(tracks)]
        directory = shelf / ("lessons" if number % 3 else "reference/deep")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{number:04d}-topic-{number}.html").write_text(
            f"<title>Lesson {number} — Topic {number} in {track}</title>"
            f"<p class='subtitle'>A ground-up explanation of topic {number} with "
            "worked examples, failure modes and the trade-offs that matter in "
            "production.</p>"
            f"<span class='lesson-tag'>Lesson {number} · {track} · 15 minutes</span>"
        )

    config = _shelf_config(tmp_path, scale=shelf)
    started = time.perf_counter()
    index = Index.build(config)
    elapsed = time.perf_counter() - started

    assert len(index.artifacts) == 5000
    assert elapsed < 2.0
    payload = json.dumps(
        index.to_json(), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    # The documented record shape at this size is why the server gzips when the
    # client accepts it; the transport budget is what has to stay under 1 MB.
    assert len(payload) > 1_000_000
    assert len(gzip.compress(payload, mtime=0)) < 1_000_000
