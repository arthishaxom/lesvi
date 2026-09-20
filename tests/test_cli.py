"""Tests for the ``lesvi`` command-line shell."""

from __future__ import annotations

import shutil
import subprocess
import sys
from importlib.metadata import entry_points
from importlib.metadata import version as distribution_version

import pytest

import lesvi
from lesvi.cli import main


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
