"""Tests for ``lesvi.watch``: incremental index updates from filesystem events."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from lesvi.config import Config
from lesvi.index import Index
from lesvi.watch import (
    ADDED,
    DELETED,
    MODIFIED,
    Event,
    Watcher,
    watchfiles_installed,
)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _config(tmp_path: Path, root: Path, name: str = "shelf") -> Config:
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[shelves.{name}]\npath = "{root}"\n')
    return Config.load(config_file)


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


StartWatcher = Callable[..., tuple[Watcher, list[Index]]]


@pytest.fixture
def running() -> Iterator[StartWatcher]:
    """Start polling watchers that record every snapshot; stop them all after."""

    started: list[Watcher] = []

    def start(
        config: Config,
        index: Index,
        *,
        debounce: float = 0.05,
        poll_interval: float = 0.02,
        prefer_watchfiles: bool = False,
        on_update: Callable[[Index], None] | None = None,
    ) -> tuple[Watcher, list[Index]]:
        updates: list[Index] = []
        watcher = Watcher(
            config,
            index,
            debounce=debounce,
            poll_interval=poll_interval,
            prefer_watchfiles=prefer_watchfiles,
            on_update=on_update or updates.append,
        )
        watcher.start()
        started.append(watcher)
        return watcher, updates

    try:
        yield start
    finally:
        for watcher in started:
            watcher.stop()


# --- the synchronous core (handle) --------------------------------------------


def test_sidecar_events_update_the_sibling_and_never_add_a_card(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)
    sidecar = shelf / "lessons" / "0001-alpha.html.meta.json"

    sidecar.write_text('{"title": "From the sidecar"}')
    current = watcher.handle([Event(ADDED, sidecar)])
    assert [(record.path, record.title, record.meta_source) for record in current.by_number("shelf")] == [
        ("lessons/0001-alpha.html", "From the sidecar", "sidecar")
    ]

    sidecar.write_text('{"title": "Sidecar, revised"}')
    current = watcher.handle([Event(MODIFIED, sidecar)])
    assert current.by_number("shelf")[0].title == "Sidecar, revised"

    sidecar.unlink()
    current = watcher.handle([Event(DELETED, sidecar)])
    assert current.by_number("shelf")[0].title == "Alpha"
    assert len(current.by_number("shelf")) == 1


def test_a_sidecar_without_a_matching_sibling_stays_ignored(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    config = _config(tmp_path, shelf)
    index = Index.build(config)
    updates: list[Index] = []
    watcher = Watcher(config, index, on_update=updates.append, prefer_watchfiles=False)

    other = _write(shelf, "lessons/notes.txt.meta.json", '{"title": "Nothing"}')
    ignored = _write(shelf, "assets/extra.html.meta.json", '{"title": "Ignored"}')

    assert watcher.handle([Event(ADDED, other)]) is index
    assert watcher.handle([Event(ADDED, ignored)]) is index
    assert updates == []
    assert [record.path for record in index.artifacts] == ["lessons/0001-alpha.html"]


def test_ignored_paths_never_touch_the_index(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    config = _config(tmp_path, shelf)
    index = Index.build(config)
    updates: list[Index] = []
    watcher = Watcher(config, index, on_update=updates.append, prefer_watchfiles=False)

    for relative in (
        "assets/extra.html",
        "learning-records/notes.html",
        "index.html",
        "node_modules/pkg/readme.html",
        ".hidden/secret.html",
        "lessons/.hidden.html",
    ):
        path = _write(shelf, relative, "<title>Must stay out</title>")
        assert watcher.handle([Event(ADDED, path)]) is index, relative
        assert watcher.handle([Event(MODIFIED, path)]) is index, relative

    assert updates == []
    assert [record.path for record in index.artifacts] == ["lessons/0001-alpha.html"]


def test_a_deleted_file_event_removes_only_that_record(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "reference/a.html", "<title>A</title>")
    _write(shelf, "reference/ab.html", "<title>AB</title>")
    config = _config(tmp_path, shelf)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)

    (shelf / "reference" / "a.html").unlink()
    current = watcher.handle([Event(DELETED, shelf / "reference" / "a.html")])

    assert [record.path for record in current.by_number("shelf")] == [
        "reference/ab.html"
    ]


def test_directory_events_rescan_or_drop_a_whole_subtree(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    config = _config(tmp_path, shelf)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)
    reference = shelf / "reference"

    _write(shelf, "reference/cheatsheet.html", "<title>Cheatsheet</title>")
    _write(shelf, "reference/deep/dive.html", "<title>Dive</title>")
    current = watcher.handle([Event(ADDED, reference)])
    assert [record.path for record in current.by_number("shelf")] == [
        "lessons/0001-alpha.html",
        "reference/cheatsheet.html",
        "reference/deep/dive.html",
    ]

    shutil.rmtree(reference)
    current = watcher.handle([Event(DELETED, reference)])
    assert [record.path for record in current.by_number("shelf")] == [
        "lessons/0001-alpha.html"
    ]


def test_rename_events_swap_the_record(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    old = _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    config = _config(tmp_path, shelf)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)
    new = shelf / "lessons" / "0002-beta.html"
    os.rename(old, new)

    current = watcher.handle([Event(DELETED, old), Event(ADDED, new)])

    assert [record.path for record in current.by_number("shelf")] == [
        "lessons/0002-beta.html"
    ]
    assert current.by_number("shelf")[0].number == 2
    assert current.by_number("shelf")[0].title == "Alpha"  # the in-page title wins


def test_events_are_scoped_to_the_shelf_they_belong_to(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    _write(one, "lessons/0001-alpha.html", "<title>Alpha</title>")
    _write(two, "lessons/0001-beta.html", "<title>Beta</title>")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[shelves.one]\npath = "{one}"\n\n[shelves.two]\npath = "{two}"\n'
    )
    config = Config.load(config_file)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)

    path = _write(one, "lessons/0002-gamma.html", "<title>Gamma</title>")
    current = watcher.handle([Event(ADDED, path)])

    assert [record.path for record in current.by_number("one")] == [
        "lessons/0001-alpha.html",
        "lessons/0002-gamma.html",
    ]
    assert [record.path for record in current.by_number("two")] == [
        "lessons/0001-beta.html"
    ]


def test_nested_shelves_both_see_a_change_under_the_inner_root(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "learning"
    inner = outer / "sub"
    _write(outer, "lessons/0001-top.html", "<p>top</p>")
    _write(inner, "lessons/0002-inner.html", "<p>inner</p>")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[shelves.outer]\npath = "{outer}"\n'
        f'[shelves.outer.categories]\nall = ["**/*.html"]\n'
        f'[shelves.inner]\npath = "{inner}"\n'
        f'[shelves.inner.categories]\nall = ["**/*.html"]\n'
    )
    config = Config.load(config_file)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)

    path = _write(inner, "lessons/0003-new.html", "<p>new</p>")
    current = watcher.handle([Event(ADDED, path)])

    assert [record.path for record in current.by_number("outer")] == [
        "lessons/0001-top.html",
        "sub/lessons/0002-inner.html",
        "sub/lessons/0003-new.html",
    ]
    assert [record.path for record in current.by_number("inner")] == [
        "lessons/0002-inner.html",
        "lessons/0003-new.html",
    ]


def test_a_file_replaced_by_a_directory_drops_the_stale_card(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    lesson = _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    config = _config(tmp_path, shelf)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)

    lesson.unlink()
    lesson.mkdir()
    current = watcher.handle([Event(MODIFIED, lesson)])

    assert current.artifacts == ()
    assert Index.build(config).artifacts == ()


def test_a_directory_replaced_by_a_file_is_indexed(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    config = _config(tmp_path, shelf)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)
    target = shelf / "lessons" / "0002-beta.html"
    target.mkdir()
    watcher.handle([Event(ADDED, target)])
    assert [record.path for record in watcher.index.by_number("shelf")] == [
        "lessons/0001-alpha.html"
    ]

    target.rmdir()
    _write(shelf, "lessons/0002-beta.html", "<title>Lesson 2 — Beta</title>")
    current = watcher.handle([Event(ADDED, target)])

    assert [record.path for record in current.by_number("shelf")] == [
        "lessons/0001-alpha.html",
        "lessons/0002-beta.html",
    ]


def test_one_incremental_event_stays_under_fifty_milliseconds(
    tmp_path: Path,
) -> None:
    """The scale target: a single change at 5,000 artifacts."""
    shelf = tmp_path / "scale"
    for number in range(5000):
        bucket = "gone" if number < 2500 else "kept"
        _write(
            shelf, f"lessons/{bucket}/{number:04d}-topic-{number}.html", "<p>x</p>"
        )
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[shelves.scale]\npath = "{shelf}"\n'
        f'[shelves.scale.categories]\nall = ["lessons/**/*.html"]\n'
    )
    config = Config.load(config_file)
    watcher = Watcher(config, Index.build(config), prefer_watchfiles=False)
    lesson = _write(shelf, "lessons/5001-new.html", "<title>Lesson 5001 — New</title>")

    started = time.perf_counter()
    current = watcher.handle([Event(ADDED, lesson)])
    elapsed = time.perf_counter() - started

    assert len(current.artifacts) == 5001
    assert elapsed < 0.05, f"incremental event took {elapsed * 1000:.1f} ms"

    # A mass delete (what polling produces for a removed directory: one event
    # per file) must stay linear, not rescale the shelf once per event.
    doomed = [
        record
        for record in current.artifacts
        if record.path.startswith("lessons/gone/")
    ]
    assert len(doomed) == 2500
    shutil.rmtree(shelf / "lessons" / "gone")
    events = [Event(DELETED, shelf / record.path) for record in doomed]

    started = time.perf_counter()
    after = watcher.handle(events)
    elapsed = time.perf_counter() - started

    assert len(after.artifacts) == 2501
    # 2,500 delete events in one burst; the per-event scale target is 50 ms,
    # and this bound still fails the old per-prefix shelf scan (several times
    # slower) with room for slow CI.
    assert elapsed < 0.2, f"mass delete took {elapsed * 1000:.1f} ms"


# --- the polling loop (threaded) ----------------------------------------------


def test_polling_keeps_the_index_current_without_a_restart(
    tmp_path: Path, running: StartWatcher
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)
    index = Index.build(config)

    watcher, updates = running(config, index)

    _write(shelf, "lessons/0002-beta.html", "<title>Lesson 2 — Beta</title>")
    assert _wait_for(lambda: len(watcher.index.by_number("shelf")) == 2)
    beta = watcher.index.by_number("shelf")[1]
    assert (beta.number, beta.title) == (2, "Beta")
    assert watcher.index.to_json()["shelves"]["shelf"]["curriculum"] == [
        "lessons/0001-alpha.html",
        "lessons/0002-beta.html",
    ]

    _write(
        shelf,
        "lessons/0002-beta.html",
        "<title>Lesson 2 — Beta, revised</title>",
    )
    assert _wait_for(
        lambda: watcher.index.by_number("shelf")[1].title == "Beta, revised"
    )

    os.rename(shelf / "lessons/0002-beta.html", shelf / "lessons/0003-gamma.html")
    assert _wait_for(
        lambda: [record.path for record in watcher.index.by_number("shelf")]
        == ["lessons/0001-alpha.html", "lessons/0003-gamma.html"]
    )

    (shelf / "lessons" / "0001-alpha.html").unlink()
    assert _wait_for(
        lambda: [record.path for record in watcher.index.by_number("shelf")]
        == ["lessons/0003-gamma.html"]
    )
    counts = {
        category.key: category.count
        for category in watcher.index.shelves["shelf"].categories
    }
    assert counts["lessons"] == 1
    assert updates


def test_a_polling_sidecar_change_reaches_the_threaded_loop(
    tmp_path: Path, running: StartWatcher
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)

    watcher, _updates = running(config, Index.build(config))

    _write(
        shelf,
        "lessons/0001-alpha.html.meta.json",
        '{"title": "Published from the sidecar"}',
    )
    assert _wait_for(
        lambda: watcher.index.by_number("shelf")[0].title
        == "Published from the sidecar"
    )


def test_a_burst_of_writes_debounces_into_one_update(
    tmp_path: Path, running: StartWatcher
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)

    watcher, updates = running(
        config, Index.build(config), debounce=0.2, poll_interval=0.02
    )
    for number in range(2, 7):
        _write(shelf, f"lessons/{number:04d}-burst.html", "<p>x</p>")
    assert _wait_for(lambda: len(watcher.index.by_number("shelf")) == 6)
    time.sleep(0.3)

    assert len(updates) == 1, "the burst should be parsed as one settled batch"


def test_a_chunked_write_is_parsed_once_it_settles(
    tmp_path: Path, running: StartWatcher
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)
    lesson = shelf / "lessons" / "0002-beta.html"

    watcher, updates = running(
        config, Index.build(config), debounce=0.2, poll_interval=0.02
    )
    lesson.write_text("<title>Lesson 2 — Be")
    time.sleep(0.05)
    lesson.write_text("<title>Lesson 2 — Beta settled</title>")
    assert _wait_for(
        lambda: any(
            record.title == "Beta settled" for record in watcher.index.artifacts
        )
    )
    time.sleep(0.3)

    assert len(updates) == 1, "the two chunks should settle into one parse"
    assert watcher.index.by_number("shelf")[1].title == "Beta settled"


def test_a_deleted_and_recreated_shelf_root_converges(
    tmp_path: Path, running: StartWatcher
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)

    watcher, _updates = running(config, Index.build(config))

    shutil.rmtree(shelf)
    assert _wait_for(lambda: watcher.index.artifacts == ())

    _write(shelf, "lessons/0002-back.html", "<title>Lesson 2 — Back</title>")
    assert _wait_for(
        lambda: [record.path for record in watcher.index.artifacts]
        == ["lessons/0002-back.html"]
    )


# --- the watchfiles backend ---------------------------------------------------


def test_the_watchfiles_backend_maps_native_changes_and_debounce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Alpha</title>")
    config = _config(tmp_path, shelf)
    watcher = Watcher(config, Index.build(config), debounce=0.25)
    added = shelf / "lessons" / "0002-beta.html"
    calls: dict[str, Any] = {}

    class FakeChange:
        added: str = "added"
        modified: str = "modified"
        deleted: str = "deleted"

    def fake_watch(*paths: object, **kwargs: object) -> Iterator[object]:
        calls["paths"] = paths
        calls["kwargs"] = kwargs
        yield {(FakeChange.added, str(added))}

    monkeypatch.setitem(
        sys.modules,
        "watchfiles",
        types.SimpleNamespace(Change=FakeChange, watch=fake_watch),
    )

    batch = next(watcher._watchfiles_events())  # pyright: ignore[reportPrivateUsage]

    assert [(event.kind, event.path) for event in batch] == [(ADDED, added)]
    assert [Path(path).resolve() for path in calls["paths"]] == [shelf.resolve()]
    assert calls["kwargs"]["debounce"] == 250
    assert calls["kwargs"]["watch_filter"] is None
    assert calls["kwargs"]["stop_event"] is watcher._stop  # pyright: ignore[reportPrivateUsage]


@pytest.mark.skipif(
    not watchfiles_installed(), reason="the optional watchfiles extra is not installed"
)
def test_native_events_update_the_index_without_polling(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)
    updates: list[Index] = []
    watcher = Watcher(
        config,
        Index.build(config),
        on_update=updates.append,
        debounce=0.1,
        poll_interval=30.0,  # a fallback here would time the test out
    )
    watcher.start()
    try:
        _write(shelf, "lessons/0002-beta.html", "<title>Lesson 2 — Beta</title>")
        assert _wait_for(
            lambda: len(watcher.index.by_number("shelf")) == 2, timeout=10
        )
    finally:
        watcher.stop()

    assert updates


@pytest.mark.parametrize("prefer_watchfiles", [False, True])
def test_startup_reconciles_changes_made_before_the_watcher_attached(
    tmp_path: Path, running: StartWatcher, prefer_watchfiles: bool
) -> None:
    if prefer_watchfiles and not watchfiles_installed():
        pytest.skip("the optional watchfiles extra is not installed")
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)
    index = Index.build(config)

    # The change lands after the startup scan but before the watcher attaches.
    _write(shelf, "lessons/0002-lost.html", "<title>Lesson 2 — Lost</title>")
    watcher, _updates = running(
        config, index, prefer_watchfiles=prefer_watchfiles
    )

    assert [record.path for record in watcher.index.by_number("shelf")] == [
        "lessons/0001-alpha.html",
        "lessons/0002-lost.html",
    ]


def test_a_native_failure_warns_once_and_falls_back_to_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    config = _config(tmp_path, shelf)
    monkeypatch.setattr("lesvi.watch.watchfiles_installed", lambda: True)

    class FakeChange:
        added: str = "added"
        modified: str = "modified"
        deleted: str = "deleted"

    def broken_watch(*_paths: object, **_kwargs: object) -> Iterator[object]:
        raise RuntimeError("inotify watch limit reached")

    monkeypatch.setitem(
        sys.modules,
        "watchfiles",
        types.SimpleNamespace(Change=FakeChange, watch=broken_watch),
    )

    watcher = Watcher(
        config,
        Index.build(config),
        debounce=0.05,
        poll_interval=0.02,
        prefer_watchfiles=True,
    )
    with caplog.at_level(logging.WARNING, logger="lesvi.watch"):
        watcher.start()
        try:
            assert _wait_for(
                lambda: any("falling back" in record.message for record in caplog.records)
            )
            _write(shelf, "lessons/0002-beta.html", "<title>Lesson 2 — Beta</title>")
            assert _wait_for(lambda: len(watcher.index.by_number("shelf")) == 2)
        finally:
            watcher.stop()

    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


def test_a_native_failure_after_a_batch_still_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shelf = tmp_path / "shelf"
    _write(shelf, "lessons/0001-alpha.html", "<title>Lesson 1 — Alpha</title>")
    lost = _write(shelf, "lessons/0002-lost.html", "<title>Lesson 2 — Lost</title>")
    config = _config(tmp_path, shelf)
    monkeypatch.setattr("lesvi.watch.watchfiles_installed", lambda: True)

    class FakeChange:
        added: str = "added"
        modified: str = "modified"
        deleted: str = "deleted"

    def flaky_watch(*_paths: object, **_kwargs: object) -> Iterator[object]:
        yield {(FakeChange.added, str(lost))}
        raise RuntimeError("inotify watch limit reached")

    monkeypatch.setitem(
        sys.modules,
        "watchfiles",
        types.SimpleNamespace(Change=FakeChange, watch=flaky_watch),
    )

    watcher = Watcher(
        config,
        Index.build(config),
        debounce=0.05,
        poll_interval=0.02,
        prefer_watchfiles=True,
    )
    with caplog.at_level(logging.WARNING, logger="lesvi.watch"):
        watcher.start()
        try:
            assert [record.path for record in watcher.index.by_number("shelf")] == [
                "lessons/0001-alpha.html",
                "lessons/0002-lost.html",
            ]
            assert _wait_for(
                lambda: any("falling back" in record.message for record in caplog.records)
            )
            _write(shelf, "lessons/0003-after.html", "<title>Lesson 3 — After</title>")
            assert _wait_for(lambda: len(watcher.index.by_number("shelf")) == 3)
        finally:
            watcher.stop()

    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert watcher.using_watchfiles is False
