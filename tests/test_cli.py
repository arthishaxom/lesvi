"""Tests for the ``lesvi`` command-line shell."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from importlib.metadata import entry_points
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import IO, Any

import pytest

import lesvi
from lesvi.cli import log_level, main


@pytest.fixture(autouse=True)
def isolated_token_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ambient ``LESVI_TOKEN`` must not silently switch auth on in tests.

    Subprocess helpers spread ``os.environ``, so a token exported in the
    developer's shell would otherwise change every serve fixture. Tests that
    want a token pass it explicitly.
    """
    monkeypatch.delenv("LESVI_TOKEN", raising=False)


def _run_cli(
    *args: str,
    config: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m lesvi`` against an isolated ``LESVI_CONFIG``."""
    environment = {
        **os.environ,
        "LESVI_CONFIG": str(config),
        "LESVI_STATE": str(config.parent / "state.json"),
        **(env or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "lesvi", *args],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
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
    assert raw.get("shelves", {}) == {}
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
    assert raw.get("shelves", {}) == {}


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


def _read_serve_banner(stream: IO[str], timeout: float = 10.0) -> tuple[list[str], str]:
    """Read ``serve`` stdout until the ``local:`` line; return (lines, URL).

    Reads the raw file descriptor: ``TextIOWrapper.readline`` buffers ahead,
    which would hide the remaining banner lines from ``select``.
    """
    descriptor = stream.fileno()
    pending = b""
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([descriptor], [], [], 0.1)
        if not ready:
            continue
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        pending += chunk
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace")
            lines.append(line)
            match = re.fullmatch(r"local:\s+(\S+)", line)
            if match:
                return lines, match.group(1)
    raise AssertionError(f"no local URL in serve output: {lines!r}")


@pytest.mark.parametrize("port", ["70000", "-1", "abc"])
def test_serve_rejects_bad_ports_as_usage_errors(
    port: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["serve", "--port", port])

    assert excinfo.value.code == 2
    assert "port" in capsys.readouterr().err


def test_serve_rejects_an_empty_host_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["serve", "--host", "", "--config", str(tmp_path / "config.toml")])

    assert code == 1
    captured = capsys.readouterr()
    assert "--host" in captured.err
    assert "Traceback" not in captured.err


def test_serve_prints_the_banner_and_serves_the_index(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    _run_cli("add", str(shelf), config=config)
    ignored = tmp_path / "ignored.toml"

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lesvi",
            "serve",
            "--port",
            "0",
            "--config",
            str(config),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "LESVI_CONFIG": str(ignored),
            "LESVI_STATE": str(tmp_path / "state.json"),
        },
    )
    try:
        assert process.stdout is not None
        lines, local_url = _read_serve_banner(process.stdout)
        with urllib.request.urlopen(
            f"{local_url}/api/index.json", timeout=5
        ) as response:
            payload = json.load(response)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert any(line.startswith("config:") and str(config) in line for line in lines)
    assert any(re.fullmatch(r"shelves:\s+1", line) for line in lines)
    assert any(re.fullmatch(r"artifacts:\s+1", line) for line in lines)
    assert local_url.startswith("http://127.0.0.1:")
    assert payload["shelves"]["data-engg"]["total"] == 1


def _start_serve(
    tmp_path: Path,
    config: Path,
    state: Path | None = None,
    *extra: str,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], list[str], str]:
    """Start ``lesvi serve`` on an ephemeral port; return (process, banner, url)."""
    environment = {
        **os.environ,
        "LESVI_CONFIG": str(tmp_path / "unused-config.toml"),
        "LESVI_STATE": str(state if state is not None else tmp_path / "state.json"),
        **(env or {}),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lesvi",
            "serve",
            "--port",
            "0",
            "--config",
            str(config),
            *extra,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    lines, local_url = _read_serve_banner(process.stdout)
    return process, lines, local_url


def _fetch_index(local_url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{local_url}/api/index.json", timeout=5) as resp:
        return json.load(resp)


def _wait_for_index(
    local_url: str, predicate: Callable[[dict[str, Any]], bool], timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    payload = _fetch_index(local_url)
    while time.monotonic() < deadline:
        if predicate(payload):
            return payload
        time.sleep(0.05)
        payload = _fetch_index(local_url)
    return payload


def test_serve_reads_stored_pins_from_the_state_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    config.write_text(f'[shelves.data-engg]\npath = "{shelf}"\n')
    state = tmp_path / "state.json"
    state.write_text(
        '{"pins": ["data-engg/lessons/0001-intro.html"], '
        '"explicit": ["data-engg/lessons/0001-intro.html"]}\n'
    )

    process, _lines, local_url = _start_serve(tmp_path, config, state)
    try:
        with urllib.request.urlopen(f"{local_url}/api/index.json", timeout=5) as resp:
            payload = json.load(resp)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert payload["artifacts"][0]["pinned"] is True


def test_serve_shows_a_pin_seeded_by_a_sidecar(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    (shelf / "lessons" / "0001-intro.html.meta.json").write_text('{"pin": true}')
    config.write_text(f'[shelves.data-engg]\npath = "{shelf}"\n')

    process, _lines, local_url = _start_serve(tmp_path, config)
    try:
        with urllib.request.urlopen(f"{local_url}/api/index.json", timeout=5) as resp:
            payload = json.load(resp)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert payload["artifacts"][0]["pinned"] is True


def test_serve_honours_the_port_configured_in_the_file(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    _run_cli("add", str(shelf), config=config)
    config.write_text(f"port = 0\n{config.read_text()}")

    process = subprocess.Popen(
        [sys.executable, "-m", "lesvi", "serve", "--config", str(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "LESVI_CONFIG": str(config),
            "LESVI_STATE": str(tmp_path / "state.json"),
        },
    )
    try:
        assert process.stdout is not None
        _lines, local_url = _read_serve_banner(process.stdout)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert local_url.startswith("http://127.0.0.1:")


# --- live updates (issue #10) -------------------------------------------------


@pytest.mark.parametrize("interval", ["0", "-1", "abc", "nan", "inf"])
def test_serve_rejects_bad_poll_intervals_as_usage_errors(
    interval: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["serve", "--poll-interval", interval])

    assert excinfo.value.code == 2
    assert "poll-interval" in capsys.readouterr().err


def test_serve_picks_up_a_new_lesson_without_a_restart(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    config.write_text(f'[shelves.data-engg]\npath = "{shelf}"\n')

    process, lines, local_url = _start_serve(tmp_path, config, None, "--poll-interval", "0.05")
    try:
        assert any("watch:" in line and "polling" in line for line in lines)
        (shelf / "lessons" / "0002-live.html").write_text(
            "<title>Lesson 2 — Live Update</title><p>Published while serving.</p>"
        )
        payload = _wait_for_index(
            local_url,
            lambda data: data["shelves"]["data-engg"]["total"] == 2,
        )
        with urllib.request.urlopen(f"{local_url}/", timeout=5) as resp:
            home = resp.read().decode()
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert payload["shelves"]["data-engg"]["total"] == 2
    assert any(
        record["title"] == "Live Update" for record in payload["artifacts"]
    )
    assert "Live Update" in home
    assert "/a/data-engg/lessons/0002-live.html" in home


def test_serve_no_watch_keeps_serving_the_startup_index(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    config.write_text(f'[shelves.data-engg]\npath = "{shelf}"\n')

    process, lines, local_url = _start_serve(
        tmp_path, config, None, "--no-watch", "--poll-interval", "0.05"
    )
    try:
        assert any("watch:     off" in line for line in lines)
        (shelf / "lessons" / "0002-after-start.html").write_text(
            "<title>Lesson 2 — After Start</title>"
        )
        time.sleep(0.6)  # long enough for a fast poll to have landed
        payload = _fetch_index(local_url)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert payload["shelves"]["data-engg"]["total"] == 1
    assert payload["artifacts"][0]["path"] == "lessons/0001-intro.html"


# --- ops: port conflicts, verbosity, status, service (issue #12) --------------


def test_serve_port_in_use_names_the_port_and_hints_at_ss(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        code = main(
            [
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--config",
                str(tmp_path / "config.toml"),
            ]
        )
    finally:
        blocker.close()

    captured = capsys.readouterr()
    assert code == 1
    assert f"port {port}" in captured.err
    assert "ss -tlnp" in captured.err
    assert "Traceback" not in captured.err


def test_log_level_rises_with_verbosity() -> None:
    assert log_level(0) == logging.WARNING
    assert log_level(1) == logging.INFO
    assert log_level(2) == logging.DEBUG


def test_serve_verbose_logs_requests_to_stderr(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    config.write_text(f'[shelves.data-engg]\npath = "{shelf}"\n')

    process, _lines, local_url = _start_serve(tmp_path, config, None, "--verbose")
    try:
        urllib.request.urlopen(f"{local_url}/", timeout=5).read()
    finally:
        process.terminate()
        _stdout, stderr = process.communicate(timeout=10)

    assert "INFO" in stderr
    assert "GET /" in stderr


def test_status_reports_shelves_artifacts_and_the_service_state(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    reference = shelf / "reference"
    reference.mkdir()
    (reference / "cheatsheet.html").write_text("<html></html>")
    _run_cli("add", str(shelf), config=config)
    xdg = tmp_path / "xdg"
    unit = xdg / "systemd" / "user" / "lesvi.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\necho active\n")
    systemctl.chmod(0o755)

    result = _run_cli(
        "status",
        config=config,
        env={"XDG_CONFIG_HOME": str(xdg), "LESVI_SYSTEMCTL": str(systemctl)},
    )

    assert result.returncode == 0
    assert "shelves:   1" in result.stdout
    assert "artifacts: 2" in result.stdout
    assert "service:   active" in result.stdout
    assert "data-engg" in result.stdout
    assert "lessons 1" in result.stdout


def test_status_reports_an_uninstalled_service_without_shelves(
    tmp_path: Path,
) -> None:
    result = _run_cli(
        "status",
        config=tmp_path / "config.toml",
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            "LESVI_SYSTEMCTL": "",
        },
    )

    assert result.returncode == 0
    assert "shelves:   0" in result.stdout
    assert "artifacts: 0" in result.stdout
    assert "not installed" in result.stdout


def test_service_install_writes_the_unit_and_prints_next_steps(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")
    xdg = tmp_path / "xdg"

    result = _run_cli(
        "service",
        "install",
        "--config",
        str(config),
        config=config,
        env={"XDG_CONFIG_HOME": str(xdg), "LESVI_SYSTEMCTL": ""},
    )

    unit = xdg / "systemd" / "user" / "lesvi.service"
    assert result.returncode == 0
    assert unit.is_file()
    text = unit.read_text()
    assert "ExecStart=" in text
    assert str(config) in text
    assert "Restart=on-failure" in text
    assert "systemctl --user enable --now lesvi" in result.stdout
    assert "loginctl enable-linger $USER" in result.stdout


def test_service_uninstall_removes_the_unit(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    unit = xdg / "systemd" / "user" / "lesvi.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")

    result = _run_cli(
        "service",
        "uninstall",
        config=tmp_path / "config.toml",
        env={"XDG_CONFIG_HOME": str(xdg), "LESVI_SYSTEMCTL": ""},
    )

    assert result.returncode == 0
    assert not unit.exists()
    assert "Removed" in result.stdout


def test_service_uninstall_without_a_unit_is_an_error(tmp_path: Path) -> None:
    result = _run_cli(
        "service",
        "uninstall",
        config=tmp_path / "config.toml",
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "LESVI_SYSTEMCTL": ""},
    )

    assert result.returncode == 1
    assert "no service unit" in result.stderr


def test_service_status_reports_not_installed(tmp_path: Path) -> None:
    result = _run_cli(
        "service",
        "status",
        config=tmp_path / "config.toml",
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "LESVI_SYSTEMCTL": ""},
    )

    assert result.returncode == 0
    assert "not installed" in result.stdout


def test_service_status_reports_the_systemd_state(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    unit = xdg / "systemd" / "user" / "lesvi.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\necho inactive\n")
    systemctl.chmod(0o755)

    result = _run_cli(
        "service",
        "status",
        config=tmp_path / "config.toml",
        env={"XDG_CONFIG_HOME": str(xdg), "LESVI_SYSTEMCTL": str(systemctl)},
    )

    assert result.returncode == 0
    assert "state: inactive" in result.stdout


def test_serve_honours_watch_false_in_the_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    shelf = tmp_path / "data-engg"
    _make_shelf(shelf)
    config.write_text(
        f'watch = false\npoll_interval = 0.05\n[shelves.data-engg]\npath = "{shelf}"\n'
    )

    process, lines, local_url = _start_serve(tmp_path, config)
    try:
        assert any("watch:     off" in line for line in lines)
        (shelf / "lessons" / "0002-after-start.html").write_text(
            "<title>Lesson 2 — After Start</title>"
        )
        time.sleep(0.6)
        payload = _fetch_index(local_url)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert payload["shelves"]["data-engg"]["total"] == 1


# --- auth (issue #9) ----------------------------------------------------------


def test_serve_refuses_a_non_loopback_bind_without_a_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LESVI_TOKEN", raising=False)

    code = main(
        [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "0",
            "--config",
            str(tmp_path / "config.toml"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "--insecure" in captured.err
    assert "Traceback" not in captured.err


def test_serve_refuses_allow_localhost_false_without_a_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("allow_localhost = false\n")

    code = main(["serve", "--port", "0", "--config", str(config)])

    captured = capsys.readouterr()
    assert code == 1
    assert "allow_localhost" in captured.err
    assert "token" in captured.err
    assert "Traceback" not in captured.err


def test_serve_can_start_insecurely_with_a_loud_warning(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")

    process, lines, local_url = _start_serve(
        tmp_path, config, None, "--host", "0.0.0.0", "--insecure"
    )
    try:
        port = local_url.rsplit(":", 1)[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=5
        ) as response:
            payload = json.load(response)
    finally:
        process.terminate()
        _stdout, stderr = process.communicate(timeout=10)

    assert any(re.fullmatch(r"auth:\s+OFF \(--insecure\)", line) for line in lines)
    assert "no token" in stderr
    assert payload["ok"] is True


def test_serve_accepts_a_non_loopback_bind_with_a_configured_token(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('auth_token = "hunter2"\n')

    process, lines, local_url = _start_serve(tmp_path, config, None, "--host", "0.0.0.0")
    try:
        port = local_url.rsplit(":", 1)[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=5
        ) as response:
            payload = json.load(response)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert any(line.startswith("auth:") and "token" in line for line in lines)
    assert payload["ok"] is True


def test_an_environment_token_allows_a_non_loopback_bind(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")

    process, lines, local_url = _start_serve(
        tmp_path, config, None, "--host", "0.0.0.0", env={"LESVI_TOKEN": "hunter2"}
    )
    try:
        port = local_url.rsplit(":", 1)[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=5
        ) as response:
            payload = json.load(response)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert any(line.startswith("auth:") and "token" in line for line in lines)
    assert payload["ok"] is True


def test_serve_requires_a_login_for_tunneled_requests(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('auth_token = "hunter2"\n')

    process, _lines, local_url = _start_serve(tmp_path, config, None)
    try:
        port = local_url.rsplit(":", 1)[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/index.json",
            headers={"CF-Connecting-IP": "203.0.113.9"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        authenticated = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/index.json",
            headers={"CF-Connecting-IP": "203.0.113.9", "Authorization": "Bearer hunter2"},
        )
        with urllib.request.urlopen(authenticated, timeout=5) as response:
            payload = json.load(response)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert excinfo.value.code == 401
    assert payload["shelves"] == {}


def test_the_token_flag_enables_auth(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("")

    process, lines, local_url = _start_serve(
        tmp_path, config, None, "--token", "hunter2"
    )
    try:
        port = local_url.rsplit(":", 1)[1]
        refused = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/index.json",
            headers={"CF-Connecting-IP": "203.0.113.9"},
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(refused, timeout=5)
        allowed = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/index.json",
            headers={
                "CF-Connecting-IP": "203.0.113.9",
                "Authorization": "Bearer hunter2",
            },
        )
        with urllib.request.urlopen(allowed, timeout=5) as response:
            payload = json.load(response)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert any(line.startswith("auth:") and "token" in line for line in lines)
    assert excinfo.value.code == 401
    assert payload["shelves"] == {}


def test_allow_localhost_false_requires_a_login_on_loopback(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('auth_token = "hunter2"\nallow_localhost = false\n')

    process, _lines, local_url = _start_serve(tmp_path, config, None)
    try:
        port = local_url.rsplit(":", 1)[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/index.json")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
    finally:
        process.terminate()
        process.communicate(timeout=10)

    assert excinfo.value.code == 401


# --- phone-ready: `lesvi url` (issue #11) -------------------------------------


def _make_url_shelf(root: Path) -> None:
    """A shelf whose artifacts make number, slug and path lookups distinct."""
    lessons = root / "lessons"
    lessons.mkdir(parents=True, exist_ok=True)
    (lessons / "0024-apache-kafka-fundamentals.html").write_text(
        "<title>Lesson 24 — Apache Kafka Fundamentals</title>"
    )
    (lessons / "0025-schema-registry.html").write_text(
        "<title>Lesson 25 — Schema Registry</title>"
    )
    (lessons / "0026-café notes.html").write_text("<title>Café Notes</title>")
    reference = root / "reference"
    reference.mkdir()
    (reference / "kafka-cheatsheet.html").write_text("<html></html>")
    research = root / "research"
    research.mkdir()
    (research / "kafka-notes.md").write_text("# notes")


def _url_setup(tmp_path: Path, **settings: object) -> tuple[Path, Path]:
    """A config with one URL-test shelf and explicit top-level settings."""
    shelf = tmp_path / "data-engg"
    _make_url_shelf(shelf)
    config = tmp_path / "config.toml"
    lines = [
        f"{key} = {value}" if isinstance(value, int) else f'{key} = "{value}"'
        for key, value in settings.items()
    ]
    lines.append(f'[shelves.data-engg]\npath = "{shelf}"')
    config.write_text("\n".join(lines) + "\n")
    return config, shelf


def test_url_without_arguments_prints_the_browse_home_url(tmp_path: Path) -> None:
    # A wildcard bind is not browsable; the loopback address stands in.
    config, _shelf = _url_setup(tmp_path, port=9000, host="0.0.0.0")

    result = _run_cli("url", config=config)

    assert result.returncode == 0
    assert result.stdout.strip() == "http://127.0.0.1:9000/"


@pytest.mark.parametrize(
    "public",
    ["https://lesvi.example.com", "https://lesvi.example.com/"],
)
def test_url_uses_the_public_url_and_trims_a_trailing_slash(
    tmp_path: Path, public: str
) -> None:
    config, _shelf = _url_setup(tmp_path, public_url=public)

    home = _run_cli("url", config=config)
    shelf = _run_cli("url", "data-engg", config=config)

    assert home.returncode == 0
    assert home.stdout.strip() == "https://lesvi.example.com/"
    assert shelf.returncode == 0
    assert shelf.stdout.strip() == "https://lesvi.example.com/s/data-engg/"


def test_url_resolves_an_artifact_by_number(tmp_path: Path) -> None:
    config, _shelf = _url_setup(tmp_path, public_url="https://lesvi.example.com")

    result = _run_cli("url", "data-engg", "25", config=config)

    assert result.returncode == 0
    assert (
        result.stdout.strip()
        == "https://lesvi.example.com/a/data-engg/lessons/0025-schema-registry.html"
    )


def test_url_resolves_an_artifact_by_slug_substring_case_insensitively(
    tmp_path: Path,
) -> None:
    config, _shelf = _url_setup(tmp_path, public_url="https://lesvi.example.com")

    result = _run_cli("url", "data-engg", "SCHEMA", config=config)

    assert result.returncode == 0
    assert result.stdout.strip().endswith("/lessons/0025-schema-registry.html")


def test_an_exact_relative_path_beats_an_ambiguous_substring(tmp_path: Path) -> None:
    config, _shelf = _url_setup(tmp_path, public_url="https://lesvi.example.com")
    exact = "lessons/0024-apache-kafka-fundamentals.html"

    result = _run_cli("url", "data-engg", exact, config=config)

    assert result.returncode == 0
    assert result.stdout.strip().endswith(f"/a/data-engg/{exact}")


def test_url_lists_the_candidates_when_several_artifacts_match(
    tmp_path: Path,
) -> None:
    config, _shelf = _url_setup(tmp_path)

    result = _run_cli("url", "data-engg", "kafka", config=config)

    assert result.returncode == 1
    assert "kafka" in result.stderr
    assert "0024-apache-kafka-fundamentals.html" in result.stderr
    assert "kafka-cheatsheet.html" in result.stderr
    # Hidden research is indexed but never browsable, so it is not a candidate.
    assert "kafka-notes.md" not in result.stderr
    assert "Traceback" not in result.stderr


def test_url_does_not_resolve_a_hidden_research_artifact(tmp_path: Path) -> None:
    config, _shelf = _url_setup(tmp_path)

    result = _run_cli("url", "data-engg", "kafka-notes", config=config)

    assert result.returncode == 1
    assert "hidden" in result.stderr
    assert "kafka-notes.md" in result.stderr
    assert "Traceback" not in result.stderr


def test_url_accepts_a_percent_encoded_lookup(tmp_path: Path) -> None:
    config, _shelf = _url_setup(tmp_path, public_url="https://lesvi.example.com")

    result = _run_cli("url", "data-engg", "caf%C3%A9", config=config)

    assert result.returncode == 0
    assert (
        result.stdout.strip()
        == "https://lesvi.example.com/a/data-engg/lessons/0026-caf%C3%A9%20notes.html"
    )


def test_url_rejects_an_empty_artifact_lookup(tmp_path: Path) -> None:
    config, _shelf = _url_setup(tmp_path)

    result = _run_cli("url", "data-engg", "", config=config)

    assert result.returncode == 1
    assert "artifact" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "http://127.0.0.1:8787/"),
        ("[::]", "http://127.0.0.1:8787/"),
        ("::1", "http://[::1]:8787/"),
    ],
)
def test_url_browse_hosts_cover_wildcards_and_ipv6(
    tmp_path: Path, host: str, expected: str
) -> None:
    config, _shelf = _url_setup(tmp_path, host=host)

    result = _run_cli("url", config=config)

    assert result.returncode == 0
    assert result.stdout.strip() == expected


def test_url_reports_an_unknown_shelf_with_the_registered_names(
    tmp_path: Path,
) -> None:
    config, _shelf = _url_setup(tmp_path)

    result = _run_cli("url", "nope", config=config)

    assert result.returncode == 1
    assert "nope" in result.stderr
    assert "data-engg" in result.stderr
    assert "Traceback" not in result.stderr


def test_url_reports_an_unknown_artifact(tmp_path: Path) -> None:
    config, _shelf = _url_setup(tmp_path)

    result = _run_cli("url", "data-engg", "zzz", config=config)

    assert result.returncode == 1
    assert "zzz" in result.stderr
    assert "Traceback" not in result.stderr


def test_url_open_launches_the_browser_with_the_printed_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _shelf = _url_setup(tmp_path, public_url="https://lesvi.example.com")
    monkeypatch.setenv("LESVI_CONFIG", str(config))
    monkeypatch.setenv("LESVI_STATE", str(tmp_path / "state.json"))
    opened: list[str] = []

    def launch(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", launch)

    code = main(["url", "--open"])

    assert code == 0
    assert opened == ["https://lesvi.example.com/"]
    assert capsys.readouterr().out.strip() == "https://lesvi.example.com/"


def test_url_open_reports_a_browser_that_cannot_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _shelf = _url_setup(tmp_path)
    monkeypatch.setenv("LESVI_CONFIG", str(config))
    monkeypatch.setenv("LESVI_STATE", str(tmp_path / "state.json"))

    def refuse(_url: str) -> bool:
        return False

    monkeypatch.setattr(webbrowser, "open", refuse)

    code = main(["url", "--open"])

    captured = capsys.readouterr()
    assert code == 1
    assert "browser" in captured.err
    assert "http://127.0.0.1:8787/" in captured.out  # the URL is still printed
