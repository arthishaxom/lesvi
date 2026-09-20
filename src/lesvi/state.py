"""Pin state: the only state lesvi keeps outside shelf folders.

``~/.local/state/lesvi/state.json`` (``$LESVI_STATE`` or ``$XDG_STATE_HOME``
respected) holds the reader's pin decisions:

.. code-block:: json

    {"pins": ["data-engg/lessons/0024-kafka.html"], "explicit": ["…"]}

``pins`` are the artifacts currently pinned. ``explicit`` records every artifact
whose pin the reader has toggled: a pin declared by a sidecar or a ``lesvi:pin``
meta tag is only a *seed*, so it applies until a decision exists for that
artifact, and the stored decision wins from then on. The file is safe to delete
— it loses the pins, nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: A state file is two short lists; anything larger is not one we should parse.
STATE_READ_LIMIT = 256 * 1024


def state_path() -> Path:
    """The state file path: ``$LESVI_STATE`` or the XDG state default."""
    override = os.environ.get("LESVI_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "lesvi" / "state.json"


def artifact_key(shelf: str, path: str) -> str:
    """The state key for an artifact: ``<shelf>/<relative path>``."""
    return f"{shelf}/{path}"


@dataclass
class PinState:
    """The reader's pin decisions; :meth:`load` tolerates a missing file."""

    path: Path
    pins: set[str] = field(default_factory=set)
    explicit: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path | None = None) -> PinState:
        """Read the state file; a missing or broken file means "no decisions"."""
        target = path if path is not None else state_path()
        state = cls(path=target)
        if not target.is_file():
            return state
        try:
            with target.open("r", encoding="utf-8-sig") as handle:
                text = handle.read(STATE_READ_LIMIT)
            raw = json.loads(text)
        except Exception:
            # The state file must never break startup: deeply nested JSON can
            # raise RecursionError, unreadable files OSError, junk ValueError.
            log.debug("unreadable state file %s", target, exc_info=True)
            return state
        if not isinstance(raw, dict):
            log.debug("state file %s is not a JSON object", target)
            return state
        state.pins = _string_set(raw.get("pins"), target, "pins")
        state.explicit = _string_set(raw.get("explicit"), target, "explicit")
        return state

    def is_pinned(self, key: str, seed: bool = False) -> bool:
        """The effective pin for *key*: a stored decision beats a seed."""
        if key in self.pins:
            return True
        if key in self.explicit:
            return False
        return seed

    def set_pin(self, key: str, pinned: bool) -> None:
        """Record an explicit decision for *key* (the UI's pin toggle)."""
        self.explicit.add(key)
        if pinned:
            self.pins.add(key)
        else:
            self.pins.discard(key)

    def to_json(self) -> dict[str, list[str]]:
        """The state file payload, with deterministic key order."""
        return {"pins": sorted(self.pins), "explicit": sorted(self.explicit)}

    def save(self) -> None:
        """Write the state file atomically, owner-only."""
        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o700)
        text = json.dumps(self.to_json(), indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=f".{self.path.name}."
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _string_set(value: object, path: Path, key: str) -> set[str]:
    """The non-empty strings in a JSON list; anything else is dropped."""
    if value is None:
        return set()
    if not isinstance(value, list):
        log.debug("state file %s: %r is not a list", path, key)
        return set()
    result: set[str] = set()
    for item in value:
        if isinstance(item, str) and item:
            result.add(item)
        else:
            log.debug("state file %s: dropping %r entry %r", path, key, item)
    return result
