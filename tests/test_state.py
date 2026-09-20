"""Tests for ``lesvi.state``: the pin store outside shelf folders.

Pin decisions live in ``~/.local/state/lesvi/state.json`` (``$LESVI_STATE`` or
``$XDG_STATE_HOME`` respected). A pin declared by a sidecar or a
``lesvi:pin`` meta tag is only a seed: it applies until the reader toggles that
artifact, after which the stored decision wins. A broken state file is safe —
it degrades to "no decisions", never raises.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from lesvi.state import PinState, artifact_key, state_path


def test_state_path_honours_the_lesvi_state_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom" / "state.json"
    monkeypatch.setenv("LESVI_STATE", str(override))

    assert state_path() == override


def test_state_path_honours_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LESVI_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert state_path() == tmp_path / "state" / "lesvi" / "state.json"


def test_state_path_defaults_under_the_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LESVI_STATE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    home = Path.home()

    assert state_path() == home / ".local" / "state" / "lesvi" / "state.json"


def test_artifact_key_is_shelf_slash_path() -> None:
    assert (
        artifact_key("data-engg", "lessons/0024-kafka.html")
        == "data-engg/lessons/0024-kafka.html"
    )


def test_load_without_a_file_is_empty(tmp_path: Path) -> None:
    state = PinState.load(tmp_path / "state.json")

    assert state.pins == set()
    assert state.explicit == set()
    assert state.is_pinned("shelf/lessons/0001-x.html") is False


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = PinState.load(path)
    state.set_pin("shelf/lessons/0001-a.html", True)
    state.set_pin("shelf/lessons/0002-b.html", False)

    assert state.to_json() == {
        "pins": ["shelf/lessons/0001-a.html"],
        "explicit": ["shelf/lessons/0001-a.html", "shelf/lessons/0002-b.html"],
    }

    state.save()
    loaded = PinState.load(path)

    assert loaded.pins == {"shelf/lessons/0001-a.html"}
    assert loaded.explicit == {
        "shelf/lessons/0001-a.html",
        "shelf/lessons/0002-b.html",
    }


def test_save_is_restricted_to_the_owner(tmp_path: Path) -> None:
    path = tmp_path / "state" / "state.json"

    PinState.load(path).save()

    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_set_pin_does_not_write_until_save(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = PinState.load(path)

    state.set_pin("shelf/lessons/0001-a.html", True)

    assert not path.exists()


def test_is_pinned_prefers_the_stored_decision_over_a_seed(tmp_path: Path) -> None:
    state = PinState.load(tmp_path / "state.json")

    assert state.is_pinned("shelf/a.html", seed=True) is True  # seeded
    state.set_pin("shelf/a.html", False)  # the reader toggled it off
    assert state.is_pinned("shelf/a.html", seed=True) is False
    state.set_pin("shelf/a.html", True)
    assert state.is_pinned("shelf/a.html", seed=False) is True


def test_a_hand_edited_pin_still_counts(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"pins": ["shelf/a.html"]}\n')

    state = PinState.load(path)

    assert state.is_pinned("shelf/a.html") is True
    assert state.explicit == set()  # nothing was explicitly toggled


def test_a_pin_without_a_decision_and_without_a_seed_is_unpinned(
    tmp_path: Path,
) -> None:
    state = PinState.load(tmp_path / "state.json")

    assert state.is_pinned("shelf/a.html") is False


def test_a_broken_state_file_degrades_to_no_decisions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")

    with caplog.at_level(logging.DEBUG, logger="lesvi.state"):
        state = PinState.load(path)

    assert state.pins == set()
    assert state.explicit == set()
    assert caplog.records


def test_a_state_file_that_is_not_an_object_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text("[1, 2]")

    with caplog.at_level(logging.DEBUG, logger="lesvi.state"):
        state = PinState.load(path)

    assert state.pins == set()
    assert caplog.records


def test_non_string_state_entries_are_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"pins": ["a", 5, "", null], "explicit": "not a list"}')

    with caplog.at_level(logging.DEBUG, logger="lesvi.state"):
        state = PinState.load(path)

    assert state.pins == {"a"}
    assert state.explicit == set()
    assert caplog.records


def test_save_is_atomic_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = PinState.load(path)
    state.set_pin("a", True)

    state.save()

    assert [entry.name for entry in tmp_path.iterdir()] == ["state.json"]
    assert json.loads(path.read_text())["pins"] == ["a"]


def test_a_failed_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.mkdir()  # replacing a directory always fails
    state = PinState(path=path)
    state.set_pin("a", True)

    with pytest.raises(OSError):
        state.save()

    assert [entry.name for entry in tmp_path.iterdir()] == ["state.json"]


def test_a_bom_prefixed_state_file_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('\ufeff{"pins": ["a"]}', encoding="utf-8")

    state = PinState.load(path)

    assert state.pins == {"a"}


def test_deeply_nested_state_json_degrades_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[" * 100_000)

    state = PinState.load(path)

    assert state.pins == set()
    assert state.explicit == set()
