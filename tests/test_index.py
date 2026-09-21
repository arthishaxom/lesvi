"""Tests for ``lesvi.index``: the in-memory store, sorted views and JSON payload."""

from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from lesvi.config import Config
from lesvi.index import Index
from lesvi.state import PinState


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


# --- pins: stored decisions and declared seeds (issue #8) ---------------------


def test_build_applies_stored_pins_and_declared_seeds(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "lessons/0001-alpha.html.meta.json", '{"pin": true}')
    _write(shelf, "lessons/0002-beta.html", "<p>b</p>")
    state = PinState.load(tmp_path / "state.json")
    state.set_pin("shelf/lessons/0002-beta.html", True)

    index = Index.build(_shelf_config(tmp_path, shelf=shelf), state)

    pins = {artifact.path: artifact.pinned for artifact in index.by_number("shelf")}
    assert pins == {"lessons/0001-alpha.html": True, "lessons/0002-beta.html": True}


def test_an_explicit_unpin_wins_over_a_seed_across_restarts(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "lessons/0001-alpha.html.meta.json", '{"pin": true}')
    config = _shelf_config(tmp_path, shelf=shelf)
    state_file = tmp_path / "state.json"

    seeded = Index.build(config, PinState.load(state_file))
    assert seeded.by_number("shelf")[0].pinned is True

    state = PinState.load(state_file)
    state.set_pin("shelf/lessons/0001-alpha.html", False)
    state.save()

    after_restart = Index.build(config, PinState.load(state_file))
    assert after_restart.by_number("shelf")[0].pinned is False


def test_build_without_pin_state_still_reports_declared_seeds(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "lessons/0001-alpha.html.meta.json", '{"pin": true}')

    index = Index.build(_shelf_config(tmp_path, shelf=shelf))

    assert index.by_number("shelf")[0].pinned is True


def test_to_json_reports_the_metadata_source_and_never_the_seed(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    _write(shelf, "lessons/0001-alpha.html.meta.json", '{"title": "From sidecar"}')
    _write(
        shelf,
        "reference/cheatsheet.html",
        "<title>Cheatsheet</title><meta name='lesvi:tags' content='Track B'>",
    )

    payload = Index.build(_shelf_config(tmp_path, shelf=shelf)).to_json()
    records = {record["path"]: record for record in payload["artifacts"]}

    assert records["lessons/0001-alpha.html"]["meta_source"] == "sidecar"
    assert records["reference/cheatsheet.html"]["meta_source"] == "meta"
    assert "pin_seed" not in records["lessons/0001-alpha.html"]


# --- the pin toggle (issue #7) -------------------------------------------------


def test_set_pin_persists_the_decision_and_updates_the_snapshot(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    config = _shelf_config(tmp_path, shelf=shelf)
    state_file = tmp_path / "state.json"
    index = Index.build(config, PinState.load(state_file))

    updated = index.set_pin("shelf", "lessons/0001-alpha.html", True)

    assert updated is not index
    assert updated.by_number("shelf")[0].pinned is True
    assert index.by_number("shelf")[0].pinned is False  # the old snapshot is intact
    assert PinState.load(state_file).pins == {"shelf/lessons/0001-alpha.html"}
    # A restart rebuilds from the same state file and still sees the pin.
    restarted = Index.build(config, PinState.load(state_file))
    assert [artifact.path for artifact in restarted.pinned()] == [
        "lessons/0001-alpha.html"
    ]


def test_set_pin_unpins_a_declared_seed_for_good(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "lessons/0001-alpha.html.meta.json", '{"pin": true}')
    config = _shelf_config(tmp_path, shelf=shelf)
    state_file = tmp_path / "state.json"
    index = Index.build(config, PinState.load(state_file))
    assert index.by_number("shelf")[0].pinned is True

    updated = index.set_pin("shelf", "lessons/0001-alpha.html", False)

    assert updated.by_number("shelf")[0].pinned is False
    restarted = Index.build(config, PinState.load(state_file))
    assert restarted.by_number("shelf")[0].pinned is False


def test_set_pin_rejects_an_unknown_artifact_and_writes_nothing(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    state_file = tmp_path / "state.json"
    index = Index.build(_shelf_config(tmp_path, shelf=shelf), PinState.load(state_file))

    with pytest.raises(KeyError):
        index.set_pin("shelf", "lessons/missing.html", True)
    with pytest.raises(KeyError):
        index.set_pin("ghost", "lessons/0001-alpha.html", True)

    assert index.pinned() == ()
    assert not state_file.exists()


def test_set_pin_without_an_attached_state_uses_the_documented_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    state_file = tmp_path / "state" / "state.json"
    monkeypatch.setenv("LESVI_STATE", str(state_file))
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))  # no state attached

    updated = index.set_pin("shelf", "lessons/0001-alpha.html", True)

    assert updated.by_number("shelf")[0].pinned is True
    assert PinState.load(state_file).pins == {"shelf/lessons/0001-alpha.html"}


def test_changed_self_heals_a_pin_decided_after_the_snapshot(
    tmp_path: Path,
) -> None:
    """The watcher holds a stale snapshot while the server toggles pins."""
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "lessons/0002-beta.html", "<p>b</p>")
    state_file = tmp_path / "state.json"
    state = PinState.load(state_file)
    stale = Index.build(_shelf_config(tmp_path, shelf=shelf), state)
    # The server records a decision against its own snapshot...
    updated = stale.set_pin("shelf", "lessons/0001-alpha.html", True)
    assert [artifact.path for artifact in updated.pinned()] == [
        "lessons/0001-alpha.html"
    ]

    # ...and the watcher, still on the pre-toggle snapshot, rebuilds the shelf.
    beta = next(item for item in stale.artifacts if item.path.endswith("beta.html"))
    healed = stale.changed("shelf", upsert=[replace(beta, title="Beta, revised")])

    pins = {artifact.path: artifact.pinned for artifact in healed.by_number("shelf")}
    assert pins == {"lessons/0001-alpha.html": True, "lessons/0002-beta.html": False}
    assert healed.pinned()[0].path == "lessons/0001-alpha.html"


# --- incremental updates (issue #10) ------------------------------------------


def test_changed_upserts_one_record_and_resorts_the_views(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))
    record = next(iter(index.artifacts))
    updated = replace(record, title="Alpha, revised")

    changed = index.changed("shelf", upsert=[updated])

    assert changed is not index
    assert index.by_number("shelf")[0].title == "Alpha"  # the old snapshot is intact
    assert changed.by_number("shelf")[0].title == "Alpha, revised"
    counts = {item.key: item.count for item in changed.shelves["shelf"].categories}
    assert counts["lessons"] == 1
    assert changed.to_json()["artifacts"][0]["title"] == "Alpha, revised"


def test_changed_removes_records_and_updates_the_category_counts(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "reference/cheatsheet.html", "<p>c</p>")
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))

    changed = index.changed("shelf", remove=["lessons/0001-alpha.html"])

    assert [artifact.path for artifact in changed.by_number("shelf")] == [
        "reference/cheatsheet.html"
    ]
    counts = {item.key: item.count for item in changed.shelves["shelf"].categories}
    assert counts == {"lessons": 0, "reference": 1, "research": 0}
    assert changed.shelves["shelf"].recency[0].path == "reference/cheatsheet.html"


def test_changed_keeps_recency_order_after_a_modification(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    first = _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    second = _write(shelf, "lessons/0002-beta.html", "<p>b</p>")
    os.utime(first, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_700_000_100, 1_700_000_100))
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))
    record = next(item for item in index.artifacts if item.path.endswith("alpha.html"))

    touched = replace(record, mtime="2026-09-20T12:00:00+05:30")
    changed = index.changed("shelf", upsert=[touched])

    assert [artifact.path for artifact in changed.by_recency("shelf")] == [
        "lessons/0001-alpha.html",
        "lessons/0002-beta.html",
    ]


def test_changed_is_a_no_op_when_nothing_would_differ(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    index = Index.build(_shelf_config(tmp_path, shelf=shelf))

    assert index.changed("shelf") is index
    assert index.changed("shelf", upsert=list(index.artifacts)) is index
    assert index.changed("shelf", remove=["lessons/missing.html"]) is index
    assert index.changed("ghost", remove=["lessons/0001-alpha.html"]) is index


def test_changed_applies_pin_state_and_seeds_to_new_records(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<p>a</p>")
    _write(shelf, "lessons/0002-beta.html", "<p>b</p>")
    _write(shelf, "lessons/0002-beta.html.meta.json", '{"pin": true}')
    state = PinState.load(tmp_path / "state.json")
    state.set_pin("shelf/lessons/0001-alpha.html", True)
    index = Index.build(_shelf_config(tmp_path, shelf=shelf), state)

    beta = next(item for item in index.artifacts if item.path.endswith("beta.html"))
    changed = index.changed("shelf", upsert=[replace(beta, title="Beta, revised")])

    pins = {artifact.path: artifact.pinned for artifact in changed.by_number("shelf")}
    assert pins == {"lessons/0001-alpha.html": True, "lessons/0002-beta.html": True}


def test_changed_recomputes_the_cross_shelf_views(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    _write(one, "lessons/0001-alpha.html", "<p>a</p>")
    _write(two, "lessons/0002-beta.html", "<p>b</p>")
    index = Index.build(_shelf_config(tmp_path, one=one, two=two))
    record = next(item for item in index.artifacts if item.shelf == "one")

    changed = index.changed("one", upsert=[replace(record, title="Alpha, revised")])
    added_elsewhere = index.changed(
        "two",
        upsert=[
            replace(
                record,
                shelf="two",
                path="reference/new.html",
                url="/a/two/reference/new.html",
                category="Reference",
                category_key="reference",
            )
        ],
    )

    assert changed.visible_artifacts == changed.artifacts
    assert len(changed.visible_artifacts) == 2
    assert len(added_elsewhere.visible_artifacts) == 3
    assert "reference/new.html" in [
        artifact.path for artifact in added_elsewhere.by_number("two")
    ]


def test_changed_is_fast_at_five_thousand_artifacts(tmp_path: Path) -> None:
    """One incremental change must stay under the 50 ms scale target."""
    shelf = tmp_path / "scale"
    for number in range(5000):
        directory = shelf / ("lessons" if number % 3 else "reference/deep")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{number:04d}-topic-{number}.html").write_text(
            f"<title>Lesson {number} — Topic {number}</title>"
        )

    index = Index.build(_shelf_config(tmp_path, scale=shelf))
    record = next(
        item
        for item in index.artifacts
        if item.path == "lessons/0001-topic-1.html"
    )
    new = replace(
        record, path="lessons/5000-new.html", url="/a/scale/lessons/5000-new.html"
    )

    started = time.perf_counter()
    changed = index.changed(
        "scale", upsert=[new], remove=["reference/deep/0000-topic-0.html"]
    )
    elapsed = time.perf_counter() - started

    assert len(changed.artifacts) == 5000
    assert elapsed < 0.05, f"incremental update took {elapsed * 1000:.1f} ms"
