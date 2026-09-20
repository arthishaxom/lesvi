"""Tests for the systemd user unit helper (issue #12)."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from lesvi import service
from lesvi.config import Config


def _unit_file(xdg: Path) -> Path:
    return xdg / "systemd" / "user" / "lesvi.service"


def _fake_systemctl(tmp_path: Path, *, state: str = "active") -> Path:
    """A stand-in ``systemctl`` that reports *state* and records its calls."""
    calls = tmp_path / "systemctl.calls"
    binary = tmp_path / "systemctl"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        f"echo {state}\n"
    )
    binary.chmod(0o755)
    return binary


def _which_lesvi(name: str) -> str | None:
    return "/usr/local/bin/lesvi" if name == "lesvi" else None


def _which_nothing(_name: str) -> str | None:
    return None


# --- unit path and launcher ---------------------------------------------------


def test_unit_path_follows_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert service.unit_path() == _unit_file(tmp_path / "cfg")


def test_unit_path_defaults_to_dot_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert service.unit_path() == _unit_file(tmp_path / ".config")


def test_launcher_uses_the_running_console_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "bin" / "lesvi"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(script)])

    assert service.launcher() == [str(script.resolve())]


def test_launcher_falls_back_to_the_console_script_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["/gone/lesvi"])
    monkeypatch.setattr("shutil.which", _which_lesvi)

    assert service.launcher() == ["/usr/local/bin/lesvi"]


def test_launcher_falls_back_to_python_m_lesvi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["/src/lesvi/__main__.py"])
    monkeypatch.setattr("shutil.which", _which_nothing)

    assert service.launcher() == [sys.executable, "-m", "lesvi"]


def test_serve_command_uses_the_launcher_and_resolves_the_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"

    command = service.serve_command(config, launcher_command=["/usr/bin/lesvi"])

    assert command == [
        "/usr/bin/lesvi",
        "serve",
        "--config",
        str(config.resolve()),
    ]


# --- unit rendering -----------------------------------------------------------


def test_render_unit_has_execstart_restart_and_install_target() -> None:
    text = service.render_unit(
        ["/usr/bin/lesvi", "serve", "--config", "/home/u/.config/lesvi/config.toml"]
    )

    assert "[Service]" in text
    assert (
        "ExecStart=/usr/bin/lesvi serve --config /home/u/.config/lesvi/config.toml"
        in text
    )
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text


def test_render_unit_quotes_arguments_with_spaces() -> None:
    text = service.render_unit(
        ["/opt/les vi/lesvi", "serve", "--config", "/home/u/My Config.toml"]
    )

    assert (
        'ExecStart="/opt/les vi/lesvi" serve --config "/home/u/My Config.toml"' in text
    )


def test_render_unit_escapes_quotes_and_backslashes() -> None:
    text = service.render_unit(["/opt/we\\ird/le'svi"])

    assert 'ExecStart="/opt/we\\\\ird/le\'svi"' in text


def test_render_unit_escapes_percent_specifiers() -> None:
    text = service.render_unit(["/usr/bin/les%vi", "serve"])

    assert "ExecStart=/usr/bin/les%%vi serve" in text


# --- install and uninstall ----------------------------------------------------


def test_install_writes_the_unit_for_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    config = Config.load(tmp_path / "config.toml")

    unit = service.install(config, launcher_command=["/usr/bin/lesvi"])

    assert unit == _unit_file(tmp_path / "cfg")
    text = unit.read_text()
    expected = f"ExecStart=/usr/bin/lesvi serve --config {config.path.resolve()}"
    assert expected in text
    assert "Restart=on-failure" in text


def test_install_is_atomic_and_overwrites_an_existing_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    unit = _unit_file(tmp_path / "cfg")
    unit.parent.mkdir(parents=True)
    unit.write_text("junk\n")
    config = Config.load(tmp_path / "config.toml")

    service.install(config, launcher_command=["/usr/bin/lesvi"])

    assert "junk" not in unit.read_text()
    assert not list(unit.parent.glob(".*lesvi.service*")), "no temporary left behind"


def test_uninstall_stops_disables_and_removes_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    unit = _unit_file(xdg)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    binary = _fake_systemctl(tmp_path)
    monkeypatch.setenv("LESVI_SYSTEMCTL", str(binary))

    removed = service.uninstall()

    assert removed == unit
    assert not unit.exists()
    calls = (tmp_path / "systemctl.calls").read_text().splitlines()
    assert any(call.startswith("--user disable --now lesvi.service") for call in calls)
    assert any(call == "--user daemon-reload" for call in calls)


def test_uninstall_without_a_unit_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("LESVI_SYSTEMCTL", "")

    with pytest.raises(service.ServiceError, match="no service unit"):
        service.uninstall()


def test_uninstall_removes_the_file_without_systemctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    unit = _unit_file(xdg)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    monkeypatch.setenv("LESVI_SYSTEMCTL", "")

    assert service.uninstall() == unit
    assert not unit.exists()


# --- state --------------------------------------------------------------------


def test_unit_state_is_not_installed_without_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    assert service.unit_state() == "not installed"


def test_unit_state_reads_systemctl_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "cfg"
    unit = _unit_file(xdg)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("LESVI_SYSTEMCTL", str(_fake_systemctl(tmp_path, state="failed")))

    assert service.unit_state() == "failed"
    calls = (tmp_path / "systemctl.calls").read_text().splitlines()
    assert calls == ["--user is-active lesvi.service"]


def test_unit_state_is_unknown_without_systemctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "cfg"
    unit = _unit_file(xdg)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("LESVI_SYSTEMCTL", "")

    assert service.unit_state() == "unknown"


def test_serve_command_accepts_an_explicit_launcher_sequence(tmp_path: Path) -> None:
    launcher: Sequence[str] = ["python", "-m", "lesvi"]

    assert service.serve_command(tmp_path / "c.toml", launcher_command=launcher)[:3] == [
        "python",
        "-m",
        "lesvi",
    ]
