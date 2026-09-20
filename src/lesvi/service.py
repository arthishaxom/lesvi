"""systemd user unit: install, uninstall, and state for the always-on server.

``lesvi service install`` writes ``~/.config/systemd/user/lesvi.service``
(``$XDG_CONFIG_HOME`` respected) with ``ExecStart`` pointing at the resolved
lesvi invocation for the active config and ``Restart=on-failure``. Installing
only writes the unit — starting it is a two-command click-through that the CLI
prints: ``systemctl --user enable --now lesvi`` plus ``loginctl
enable-linger $USER``.

``uninstall`` stops and disables the unit best-effort, removes the file, and
asks systemd to forget it. ``unit_state`` reports what
``systemctl --user is-active`` says.

systemd interaction is best-effort and never required: with no ``systemctl`` on
``PATH``, status degrades to ``unknown`` and uninstall just removes the file.
``$LESVI_SYSTEMCTL`` points the helper at an alternative binary (an empty
value disables it), which keeps tests and non-systemd hosts off the real
user manager.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from lesvi.config import Config

log = logging.getLogger(__name__)

UNIT_NAME = "lesvi.service"
DESCRIPTION = "lesvi - agent-generated lessons and reference notes"
DOCUMENTATION = "https://github.com/arthishaxom/lesvi"
#: A stop/disable waits on the unit; never let a hung process hang uninstall.
SYSTEMCTL_TIMEOUT = 15.0

NOT_INSTALLED = "not installed"
UNKNOWN = "unknown"


class ServiceError(Exception):
    """A service operation cannot be completed."""


def unit_path() -> Path:
    """The user unit path: ``$XDG_CONFIG_HOME/systemd/user`` or the default."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user" / UNIT_NAME


def systemctl_path() -> Path | None:
    """The ``systemctl`` to drive, or ``None``.

    ``$LESVI_SYSTEMCTL`` wins when set; an empty value disables systemd
    interaction entirely.
    """
    override = os.environ.get("LESVI_SYSTEMCTL")
    if override is not None:
        override = override.strip()
        return Path(override) if override else None
    found = shutil.which("systemctl")
    return Path(found) if found else None


def launcher() -> list[str]:
    """The command prefix that runs lesvi.

    The console script that invoked this process wins; ``python -m lesvi`` and
    a missing script fall back to the interpreter running us, so the unit
    always names an absolute, existing program.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        candidate = Path(argv0).expanduser()
        if candidate.name != "__main__.py":
            try:
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover - resolve rarely fails
                resolved = None
            if (
                resolved is not None
                and resolved.is_file()
                and os.access(resolved, os.X_OK)
            ):
                return [str(resolved)]
    found = shutil.which("lesvi")
    if found:
        return [found]
    return [sys.executable, "-m", "lesvi"]


def serve_command(
    config_path: Path, *, launcher_command: Sequence[str] | None = None
) -> list[str]:
    """The full ``serve`` argument list for *config_path* (an ``ExecStart``)."""
    prefix = list(launcher_command) if launcher_command is not None else launcher()
    return [*prefix, "serve", "--config", str(config_path.expanduser().resolve())]


def render_unit(command: Sequence[str]) -> str:
    """The unit file text for *command*."""
    exec_start = " ".join(_quote_argument(argument) for argument in command)
    return (
        "[Unit]\n"
        f"Description={DESCRIPTION}\n"
        f"Documentation={DOCUMENTATION}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install(config: Config, *, launcher_command: Sequence[str] | None = None) -> Path:
    """Write the unit for *config* atomically; returns the unit path."""
    command = serve_command(config.path, launcher_command=launcher_command)
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_unit(command))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def uninstall() -> Path:
    """Stop/disable best-effort, remove the unit; returns the removed path.

    Raises :class:`ServiceError` when there is no unit to remove.
    """
    target = unit_path()
    if not target.is_file():
        raise ServiceError(f"no service unit at {target}")
    _systemctl("disable", "--now", UNIT_NAME)
    target.unlink()
    _systemctl("daemon-reload")
    return target


def unit_state() -> str:
    """The unit's systemd state: ``active``/``inactive``/... or a sentinel."""
    if not unit_path().is_file():
        return NOT_INSTALLED
    result = _systemctl("is-active", UNIT_NAME)
    if result is None:
        return UNKNOWN
    return result.stdout.strip() or UNKNOWN


def _systemctl(*args: str) -> subprocess.CompletedProcess[str] | None:
    """Run ``systemctl --user *args`` best-effort; ``None`` when unavailable."""
    binary = systemctl_path()
    if binary is None:
        return None
    try:
        return subprocess.run(
            [str(binary), "--user", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=SYSTEMCTL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("systemctl %s failed: %s", " ".join(args), exc)
        return None


#: Characters systemd's ExecStart parser reads literally; anything else is
#: wrapped in double quotes with backslashes and quotes escaped.
_SAFE_ARGUMENT = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def _quote_argument(argument: str) -> str:
    # ``%%`` survives systemd's specifier expansion as a literal percent.
    escaped = argument.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if escaped and _SAFE_ARGUMENT.fullmatch(escaped):
        return escaped
    return f'"{escaped}"'
