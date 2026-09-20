"""Tests for the ``lesvi`` command-line shell."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import entry_points
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

import lesvi
from lesvi.cli import main


def _run_cli(
    *args: str, config: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m lesvi`` against an isolated ``LESVI_CONFIG``."""
    env = {**os.environ, "LESVI_CONFIG": str(config)}
    return subprocess.run(
        [sys.executable, "-m", "lesvi", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
    )


def _make_shelf(root: Path) -> None:
    """Give *root* one lesson so it matches the built-in preset."""
    lessons = root / "lessons"
    lessons.mkdir(parents=True, exist_ok=True)
    (lessons / "0001-intro.html").write_text("<html><body>intro</body></html>")


def test_version_command_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"lesvi {lesvi.__version__}"


def test_installed_distribution_matches_module_version() -> None:
    """The installed package metadata is built from ``lesvi.__version__`` (hatchling)."""
    assert distribution_version("lesvi") == lesvi.__version__


def test_console_script_entry_point_is_wired_to_cli_main() -> None:
    scripts = entry_points(group="console_scripts")
    assert any(
        entry.name == "lesvi" and entry.value == "lesvi.cli:main" for entry in scripts
    )


def test_console_script_prints_version() -> None:
    lesvi_script = shutil.which("lesvi")
    if lesvi_script is None:
        pytest.skip("the lesvi console script is not on PATH")
    result = subprocess.run(
        [lesvi_script, "version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"lesvi {lesvi.__version__}"


def test_module_invocation_prints_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lesvi", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"lesvi {lesvi.__version__}"


@pytest.mark.parametrize("argv", [[], ["bogus"], ["version", "--bogus"]])
def test_usage_errors_exit_two_with_usage(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_add_registers_path_that_matches_preset_as_one_shelf(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)

    result = _run_cli("add", str(shelf), config=config)

    assert result.returncode == 0
    assert "data-engg" in result.stdout
    raw = tomllib.loads(config.read_text())
    assert raw["shelves"] == {"data-engg": {"path": str(shelf)}}


def test_add_registers_each_matching_child_as_its_own_shelf(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    learning = tmp_path / "Learning"
    for child in ("data-engg", "GK", "oni"):
        _make_shelf(learning / child)
    (learning / "assets").mkdir(parents=True)
    (learning / "assets" / "logo.svg").write_text("<svg/>")
    (learning / "Notes").mkdir()

    result = _run_cli("add", str(learning), config=config)

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert sorted(raw["shelves"]) == ["data-engg", "gk", "oni"]
    assert raw["shelves"]["data-engg"]["path"] == str(learning / "data-engg")
    assert raw["shelves"]["gk"]["path"] == str(learning / "GK")


def test_add_single_flag_registers_the_parent_as_one_shelf(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    learning = tmp_path / "Learning"
    _make_shelf(learning / "data-engg")
    _make_shelf(learning / "oni")

    result = _run_cli("add", "--single", str(learning), config=config)

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert list(raw["shelves"]) == ["learning"]


def test_add_name_overrides_the_derived_slug(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "Data Engg"
    _make_shelf(shelf)

    result = _run_cli("add", "--name", "Data-Engineering", str(shelf), config=config)

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert list(raw["shelves"]) == ["data-engineering"]


def test_add_title_is_stored_for_display(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)

    result = _run_cli("add", "--title", "Data Engineering", str(shelf), config=config)

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert raw["shelves"]["data-engg"]["title"] == "Data Engineering"


def test_add_missing_path_fails_without_creating_the_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"

    result = _run_cli("add", str(tmp_path / "nope"), config=config)

    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert not config.exists()


def test_add_folder_without_preset_match_suggests_single_flag(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    plain = tmp_path / "plain"
    (plain / "assets").mkdir(parents=True)
    (plain / "assets" / "logo.svg").write_text("<svg/>")

    result = _run_cli("add", str(plain), config=config)

    assert result.returncode == 1
    assert "--single" in result.stderr


def test_add_twice_for_the_same_path_is_a_no_op(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    _run_cli("add", str(shelf), config=config)

    result = _run_cli("add", str(shelf), config=config)

    assert result.returncode == 0
    assert "already" in result.stdout.lower()
    raw = tomllib.loads(config.read_text())
    assert len(raw["shelves"]) == 1


def test_add_name_collision_from_another_path_is_an_error(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    first = tmp_path / "first"
    second = tmp_path / "second"
    _make_shelf(first)
    _make_shelf(second)
    _run_cli("add", "--name", "shared", str(first), config=config)

    result = _run_cli("add", "--name", "shared", str(second), config=config)

    assert result.returncode == 1
    assert "already registered" in result.stderr
    raw = tomllib.loads(config.read_text())
    assert raw["shelves"]["shared"]["path"] == str(first)


def test_add_name_with_multiple_matching_children_is_an_error(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    learning = tmp_path / "Learning"
    _make_shelf(learning / "data-engg")
    _make_shelf(learning / "oni")

    result = _run_cli("add", "--name", "everything", str(learning), config=config)

    assert result.returncode == 1
    assert "--single" in result.stderr
    assert not config.exists()


def test_list_shows_name_path_counts_and_missing_paths(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    reference = shelf / "reference"
    reference.mkdir()
    (reference / "cheatsheet.html").write_text("<html></html>")
    _run_cli("add", str(shelf), config=config)

    ghost = tmp_path / "ghost"
    _make_shelf(ghost)
    _run_cli("add", str(ghost), config=config)
    shutil.rmtree(ghost)

    result = _run_cli("list", config=config)

    assert result.returncode == 0
    assert "data-engg" in result.stdout
    assert str(shelf) in result.stdout
    assert "lessons 1" in result.stdout
    assert "reference 1" in result.stdout
    assert "missing" in result.stdout


def test_list_json_is_machine_readable(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    _run_cli("add", str(shelf), config=config)

    result = _run_cli("list", "--json", config=config)

    assert result.returncode == 0
    assert json.loads(result.stdout) == [
        {
            "name": "data-engg",
            "path": str(shelf),
            "title": None,
            "exists": True,
            "categories": {"lessons": 1, "reference": 0, "research": 0},
        }
    ]


def test_list_without_a_config_reports_no_shelves(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"

    result = _run_cli("list", config=config)

    assert result.returncode == 0
    assert "no shelves" in result.stdout.lower()


def test_remove_deletes_the_entry_but_not_the_folder(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    _run_cli("add", str(shelf), config=config)

    result = _run_cli("remove", "data-engg", config=config)

    assert result.returncode == 0
    assert "data-engg" in result.stdout
    raw = tomllib.loads(config.read_text())
    assert raw["shelves"] == {}
    assert (shelf / "lessons" / "0001-intro.html").exists()


def test_remove_accepts_a_path_and_keeps_unknown_keys(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    config.write_text(
        'port = 9999\ncustom_key = "keep me"\n\n[shelves.other]\npath = "~/elsewhere"\n'
    )
    _run_cli("add", str(shelf), config=config)

    result = _run_cli("remove", str(shelf), config=config)

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert raw["custom_key"] == "keep me"
    assert set(raw["shelves"]) == {"other"}


def test_remove_unknown_name_is_an_error(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"

    result = _run_cli("remove", "ghost", config=config)

    assert result.returncode == 1
    assert "ghost" in result.stderr


def _fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    """Size, mtime and content hash of every path under *root*."""
    fingerprint: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        fingerprint[str(path.relative_to(root))] = (
            stat.st_size,
            stat.st_mtime_ns,
            digest,
        )
    return fingerprint


def test_no_command_touches_any_file_under_a_shelf(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    (shelf / "reference").mkdir()
    (shelf / "reference" / "cheatsheet.html").write_text("<html></html>")
    (shelf / "assets").mkdir()
    (shelf / "assets" / "lesson.css").write_text("body {}")
    before = _fingerprint(shelf)

    assert _run_cli("add", str(shelf), config=config).returncode == 0
    assert _run_cli("list", config=config).returncode == 0
    assert _run_cli("list", "--json", config=config).returncode == 0
    assert _run_cli("remove", "data-engg", config=config).returncode == 0

    assert _fingerprint(shelf) == before


def test_no_command_touches_files_when_adding_matching_children(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    learning = tmp_path / "Learning"
    _make_shelf(learning / "data-engg")
    _make_shelf(learning / "oni")
    before = _fingerprint(learning)

    assert _run_cli("add", str(learning), config=config).returncode == 0
    assert _run_cli("list", config=config).returncode == 0
    assert _run_cli("remove", "data-engg", config=config).returncode == 0

    assert _fingerprint(learning) == before


def test_add_name_applies_when_exactly_one_child_matches(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    learning = tmp_path / "Learning"
    _make_shelf(learning / "oni")
    (learning / "plain").mkdir(parents=True)
    (learning / "plain" / "notes.md").write_text("x")

    result = _run_cli("add", "--name", "renamed", str(learning), config=config)

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert raw["shelves"] == {"renamed": {"path": str(learning / "oni")}}


def test_add_single_with_name_and_title(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "proj" / "lessons"
    _make_shelf(shelf)

    result = _run_cli(
        "add",
        "--single",
        "--name",
        "proj-notes",
        "--title",
        "Proj Notes",
        str(shelf),
        config=config,
    )

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert raw["shelves"]["proj-notes"] == {
        "path": str(shelf),
        "title": "Proj Notes",
    }


def test_remove_relative_path_resolves_against_cwd(tmp_path: Path) -> None:
    config = tmp_path / "cfg" / "config.toml"
    work = tmp_path / "work"
    shelf = work / "data-engg"
    _make_shelf(shelf)
    _run_cli("add", "--name", "custom", str(shelf), config=config)

    result = _run_cli("remove", "./data-engg", config=config, cwd=work)

    assert result.returncode == 0
    raw = tomllib.loads(config.read_text())
    assert raw["shelves"] == {}


def test_list_resolves_relative_config_paths_against_config_dir(
    tmp_path: Path,
) -> None:
    cfgdir = tmp_path / "cfg"
    _make_shelf(cfgdir / "data-engg")
    config = cfgdir / "config.toml"
    config.write_text('[shelves.data-engg]\npath = "data-engg"\n')

    result = _run_cli("list", config=config)

    assert result.returncode == 0
    assert "lessons 1" in result.stdout


def test_malformed_config_reports_an_error_not_a_traceback(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("shelves = 5\n")

    result = _run_cli("list", config=config)

    assert result.returncode == 1
    assert "error" in result.stderr
    assert "Traceback" not in result.stderr
