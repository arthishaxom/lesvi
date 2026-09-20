"""Tests for ``lesvi.config``: TOML round-trip, paths, and shelf registration."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from lesvi.config import (
    PRESET_CATEGORIES,
    PRESET_IGNORES,
    Config,
    ConfigError,
    category_counts,
    glob_match,
    is_sidecar,
    matches_preset,
    resolve_path,
    shelf_categories,
    shelf_ignores,
    slugify,
    store_path,
)

HANDWRITTEN_CONFIG = """\
# Example hand-edited config.
port = 9999
custom_key = "keep me"

[custom_table]
answer = 42

[shelves.data-engg]
path = "~/Learning/data-engg"
title = "Data Engineering"
note = "unknown key survives"

[shelves.data-engg.categories]
lessons = ["lessons/*.html"]
"""


def test_add_shelf_preserves_unknown_keys_and_existing_shelves(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(HANDWRITTEN_CONFIG)

    config = Config.load(config_file)
    config.add_shelf("new-shelf", tmp_path / "new-shelf")
    config.save()

    raw = tomllib.loads(config_file.read_text())
    assert raw["custom_key"] == "keep me"
    assert raw["custom_table"] == {"answer": 42}
    assert raw["port"] == 9999
    assert raw["shelves"]["data-engg"]["note"] == "unknown key survives"
    assert raw["shelves"]["data-engg"]["title"] == "Data Engineering"
    assert raw["shelves"]["data-engg"]["categories"] == {"lessons": ["lessons/*.html"]}
    assert raw["shelves"]["new-shelf"] == {"path": str(tmp_path / "new-shelf")}


def test_remove_shelf_deletes_only_that_entry_and_keeps_unknown_keys(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(HANDWRITTEN_CONFIG)

    config = Config.load(config_file)
    config.remove_shelf("data-engg")
    config.save()

    raw = tomllib.loads(config_file.read_text())
    assert raw.get("shelves", {}) == {}
    assert raw["custom_key"] == "keep me"
    assert raw["custom_table"] == {"answer": 42}


def test_remove_shelf_accepts_a_path_and_rejects_unknown_names(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config = Config.load(config_file)
    config.add_shelf("alpha", tmp_path / "alpha")
    config.save()

    reloaded = Config.load(config_file)
    reloaded.remove_shelf(str(tmp_path / "alpha"))
    reloaded.save()
    raw = tomllib.loads(config_file.read_text())
    assert raw.get("shelves", {}) == {}

    with pytest.raises(ConfigError):
        Config.load(config_file).remove_shelf("never-registered")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Data Engg", "data-engg"),
        ("GK", "gk"),
        ("oni", "oni"),
        ("My Shelf!", "my-shelf"),
        ("--weird--name--", "weird-name"),
        ("data_engg", "data-engg"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_resolve_path_expands_tilde_and_resolves_against_config_dir(
    tmp_path: Path,
) -> None:
    assert resolve_path("~/Learning", tmp_path) == Path.home() / "Learning"
    assert resolve_path("shelves/x", tmp_path) == (tmp_path / "shelves" / "x").resolve()
    assert resolve_path("/tmp/absolute", tmp_path) == Path("/tmp/absolute")


@pytest.mark.parametrize(
    ("pattern", "rel", "expected"),
    [
        ("lessons/*.html", "lessons/a.html", True),
        ("lessons/*.html", "lessons/a/b.html", False),
        ("lessons/*.html", "reference/a.html", False),
        ("reference/**/*.html", "reference/a.html", True),
        ("reference/**/*.html", "reference/deep/a.html", True),
        ("reference/**/*.html", "reference/a.md", False),
        ("lessons/a.html", "lessons/A.html", False),
    ],
)
def test_glob_match_segment_and_recursive_semantics(
    pattern: str, rel: str, expected: bool
) -> None:
    assert glob_match(pattern, rel) is expected


def _build_preset_shelf(root: Path) -> None:
    """A miniature shelf in the real layout, plus things that must be ignored."""
    for relative in (
        "lessons/0001-alpha.html",
        "lessons/0002-beta.html",
        "reference/cheatsheet.html",
        "reference/kafka/deep.html",
        "research/notes.md",
        "research/nested/deep.md",
        "assets/lesson.css",
        "learning-records/export.html",
        "index.html",
        ".hidden.html",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


def test_category_counts_use_preset_globs_and_skip_ignored_paths(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _build_preset_shelf(shelf)

    assert category_counts(shelf, PRESET_CATEGORIES) == {
        "lessons": 2,
        "reference": 2,
        "research": 2,
    }
    assert category_counts(shelf, {"all": ("**/*.html",)}, PRESET_IGNORES) == {"all": 4}


def test_category_counts_never_counts_sidecars(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    lessons = shelf / "lessons"
    lessons.mkdir(parents=True)
    (lessons / "0001-a.html").write_text("<p>x</p>")
    (lessons / "0001-a.html.meta.json").write_text('{"title": "A"}')

    # Even an ignore list that does not mention sidecars must not count them:
    # a sidecar is metadata about an artifact, never an artifact itself.
    counts = category_counts(shelf, {"all": ("lessons/**",)}, ("learning-records/**",))

    assert counts == {"all": 1}


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("lessons/0001-a.html.meta.json", True),
        ("a.meta.json", True),
        ("lessons/0001-a.html", False),
        ("lessons/meta.json", False),
    ],
)
def test_is_sidecar(rel: str, expected: bool) -> None:
    assert is_sidecar(rel) is expected


def test_matches_preset_distinguishes_shelves_from_plain_folders(
    tmp_path: Path,
) -> None:
    shelf = tmp_path / "shelf"
    _build_preset_shelf(shelf)
    plain = tmp_path / "plain"
    (plain / "assets").mkdir(parents=True)
    (plain / "assets" / "logo.svg").write_text("<svg/>")

    assert matches_preset(shelf) is True
    assert matches_preset(plain) is False
    assert matches_preset(tmp_path / "does-not-exist") is False


def test_category_and_ignore_overrides_replace_the_preset(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    (shelf / "notes").mkdir(parents=True)
    (shelf / "notes" / "a.md").write_text("x")
    (shelf / "lessons").mkdir()
    (shelf / "lessons" / "b.html").write_text("x")
    table: dict[str, object] = {
        "path": str(shelf),
        "categories": {"notes": "notes/*.md"},
        "ignore": ["notes/secret/**"],
    }

    assert shelf_categories(table) == {"notes": ("notes/*.md",)}
    assert shelf_ignores(table) == ("notes/secret/**",)
    assert category_counts(shelf, shelf_categories(table), shelf_ignores(table)) == {
        "notes": 1
    }


def test_absent_overrides_use_the_preset() -> None:
    assert shelf_categories({}) == dict(PRESET_CATEGORIES)
    assert shelf_ignores({}) == PRESET_IGNORES
    assert shelf_ignores({"ignore": "assets/**"}) == ("assets/**",)


def test_matches_preset_accepts_a_research_only_shelf(tmp_path: Path) -> None:
    research = tmp_path / "research-only"
    (research / "research" / "topic").mkdir(parents=True)
    (research / "research" / "topic" / "notes.md").write_text("x")

    assert matches_preset(research) is True


def test_store_path_prefers_tilde_when_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert store_path(tmp_path) == "~"
    assert store_path(tmp_path / "Learning" / "data-engg") == "~/Learning/data-engg"
    assert store_path(tmp_path.parent / "elsewhere") == str(
        tmp_path.parent / "elsewhere"
    )


def test_server_settings_default_and_override(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    default = Config.load(config_file)
    assert default.port() == 8787
    assert default.host() == "127.0.0.1"
    assert default.public_url() == ""

    config_file.write_text(
        'port = 9000\nhost = "0.0.0.0"\npublic_url = "https://lesvi.example.com"\n'
    )
    configured = Config.load(config_file)
    assert configured.port() == 9000
    assert configured.host() == "0.0.0.0"
    assert configured.public_url() == "https://lesvi.example.com"


@pytest.mark.parametrize(
    ("text", "method"),
    [
        ("port = 70000\n", "port"),
        ("port = -1\n", "port"),
        ("port = true\n", "port"),
        ('port = "8787"\n', "port"),
        ("host = 5\n", "host"),
        ('host = ""\n', "host"),
        ("host = '   '\n", "host"),
        ("public_url = 7\n", "public_url"),
    ],
)
def test_bad_server_settings_raise_config_error(
    tmp_path: Path, text: str, method: str
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(text)

    with pytest.raises(ConfigError):
        getattr(Config.load(config_file), method)()


def test_comments_and_hand_formatting_survive_add_and_remove(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "# top of file, user-owned\n"
        "port = 9999  # inline note\n"
        "\n"
        "# my shelf, do not lose this\n"
        "[shelves.data-engg]\n"
        'path = "~/Learning/data-engg"\n'
    )

    config = Config.load(config_file)
    config.add_shelf("new-shelf", tmp_path / "new-shelf")
    config.save()

    text = config_file.read_text()
    assert "# top of file, user-owned" in text
    assert "# inline note" in text
    assert "# my shelf, do not lose this" in text

    updated = Config.load(config_file)
    updated.remove_shelf("new-shelf")
    updated.save()

    text = config_file.read_text()
    assert "# top of file, user-owned" in text
    assert "# my shelf, do not lose this" in text


def test_unusual_values_survive_a_save(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "port = 8787\n"
        "hex = 0x10\n"
        'inline = { a = 1, b = "x" }\n'
        "note = 'literal \\ backslash'\n"
    )
    before = tomllib.loads(config_file.read_text())

    config = Config.load(config_file)
    config.add_shelf("x", tmp_path / "x")
    config.save()

    after = tomllib.loads(config_file.read_text())
    assert after["hex"] == before["hex"] == 16
    assert after["inline"] == before["inline"] == {"a": 1, "b": "x"}
    assert after["note"] == before["note"] == "literal \\ backslash"


def test_add_with_a_multiline_title_cannot_corrupt_or_inject_config(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config = Config.load(config_file)
    hostile = 'hello\nworld\n[shelves.pwned]\npath = "/"'
    config.add_shelf("x", tmp_path / "x", title=hostile)
    config.save()

    raw = tomllib.loads(config_file.read_text())
    assert set(raw["shelves"]) == {"x"}
    assert raw["shelves"]["x"]["title"] == hostile


def test_new_config_is_private_and_an_existing_mode_is_kept(tmp_path: Path) -> None:
    config_file = tmp_path / "cfg" / "config.toml"
    config = Config.load(config_file)
    config.add_shelf("x", tmp_path / "x")
    config.save()

    assert (config_file.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "cfg").stat().st_mode & 0o777) == 0o700

    config_file.chmod(0o640)
    second = Config.load(config_file)
    second.add_shelf("y", tmp_path / "y")
    second.save()
    assert (config_file.stat().st_mode & 0o777) == 0o640


@pytest.mark.parametrize(
    "text",
    [
        "shelves = 5\n",
        '[shelves.ghost]\ntitle = "no path"\n',
        "[shelves.ghost]\npath = 7\n",
    ],
)
def test_load_rejects_malformed_shelf_shapes(tmp_path: Path, text: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(text)

    with pytest.raises(ConfigError):
        Config.load(config_file)
