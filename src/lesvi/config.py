"""Configuration: the single source of truth for shelves and server settings.

The file is ``${LESVI_CONFIG:-~/.config/lesvi/config.toml}``, hand-editable at
all times. It is parsed and edited with :mod:`tomlkit`, so comments, key order,
formatting, and unknown keys all survive ``lesvi add`` / ``lesvi remove``.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

PRESET_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    "lessons": ("lessons/*.html",),
    "reference": ("reference/**/*.html",),
    "research": ("research/**/*.md", "reference/research/**/*.md"),
}

PRESET_IGNORES: tuple[str, ...] = (
    "learning-records/**",
    "assets/**",
    "index.html",
    "node_modules/**",
)


class ConfigError(Exception):
    """A config file or shelf operation is invalid."""


def config_path() -> Path:
    """Return the config file path: ``$LESVI_CONFIG`` or the XDG default."""
    override = os.environ.get("LESVI_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "lesvi" / "config.toml"


def resolve_path(raw: str | Path, config_dir: Path) -> Path:
    """Expand ``~``; resolve relative paths against the config file's dir."""
    expanded = Path(os.path.expanduser(os.fspath(raw)))
    if expanded.is_absolute():
        return expanded
    return (config_dir / expanded).resolve()


_SLUG_RUN = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Turn arbitrary text into a shelf slug: ``[a-z0-9-]+``, no edge dashes."""
    return _SLUG_RUN.sub("-", text.strip().lower()).strip("-")


def glob_match(pattern: str, rel: str) -> bool:
    """Match a POSIX relative path against a glob.

    ``*`` stays within one path segment; ``**`` spans zero or more segments;
    matching is case-sensitive.
    """
    return _match_parts(pattern.split("/"), rel.split("/"))


def _match_parts(pattern: Sequence[str], path: Sequence[str]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return _match_parts(rest, path) or (
            bool(path) and _match_parts(pattern, path[1:])
        )
    return (
        bool(path)
        and fnmatch.fnmatchcase(path[0], head)
        and _match_parts(rest, path[1:])
    )


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_ignored(rel: str, ignore: Sequence[str]) -> bool:
    if any(part.startswith(".") for part in rel.split("/")):
        return True
    return any(glob_match(pattern, rel) for pattern in ignore)


def category_counts(
    root: Path,
    categories: Mapping[str, Sequence[str]],
    ignore: Sequence[str] = PRESET_IGNORES,
) -> dict[str, int]:
    """Count files per category glob under *root*, skipping ignored paths."""
    counts = dict.fromkeys(categories, 0)
    if not root.is_dir():
        return counts
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if not _is_ignored(_relative(root, here / name), ignore)
        ]
        for filename in filenames:
            rel = _relative(root, here / filename)
            if _is_ignored(rel, ignore):
                continue
            for category, patterns in categories.items():
                if any(glob_match(pattern, rel) for pattern in patterns):
                    counts[category] += 1
    return counts


def matches_preset(root: Path) -> bool:
    """True when *root* holds at least one file the built-in preset counts."""
    return any(category_counts(root, PRESET_CATEGORIES).values())


def shelf_categories(table: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Effective category globs for a shelf table: override or built-in preset."""
    raw = table.get("categories")
    if not isinstance(raw, Mapping):
        return dict(PRESET_CATEGORIES)
    result: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            result[str(key)] = (value,)
        elif isinstance(value, Sequence):
            result[str(key)] = tuple(str(item) for item in value)
    return result


def shelf_ignores(table: Mapping[str, Any]) -> tuple[str, ...]:
    """Effective ignore globs for a shelf table: override or built-in preset."""
    raw = table.get("ignore")
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence):
        return tuple(str(item) for item in raw)
    return PRESET_IGNORES


def store_path(path: Path) -> str:
    """Render *path* for config storage, preferring ``~``-relative form."""
    resolved = path.resolve()
    home = Path.home()
    if resolved == home:
        return "~"
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    return f"~/{relative.as_posix()}"


def _new_document() -> tomlkit.TOMLDocument:
    document = tomlkit.document()
    document.add(tomlkit.comment("lesvi configuration - hand-editable at all times."))
    document.add(
        tomlkit.comment("Comments, key order and formatting are preserved on save.")
    )
    return document


# --- Config model -------------------------------------------------------------


class Config:
    """A loaded config file; ``add_shelf``/``remove_shelf`` then ``save``."""

    def __init__(self, path: Path, data: tomlkit.TOMLDocument) -> None:
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        target = path if path is not None else config_path()
        if not target.exists():
            return cls(target, _new_document())
        try:
            data = tomlkit.parse(target.read_text(encoding="utf-8"))
        except TOMLKitError as exc:
            raise ConfigError(f"invalid TOML in {target}: {exc}") from exc
        shelves = data.get("shelves")
        if shelves is not None:
            if not isinstance(shelves, Mapping):
                raise ConfigError(f"{target}: [shelves] must be a table")
            for name, table in shelves.items():
                if not isinstance(table, Mapping):
                    raise ConfigError(f"{target}: shelf {name!r} must be a table")
                stored = table.get("path")
                if not isinstance(stored, str) or not stored:
                    raise ConfigError(
                        f"{target}: shelf {name!r} needs a non-empty 'path'"
                    )
        return cls(target, data)

    def shelves(self) -> Mapping[str, Any]:
        """The ``[shelves]`` table as loaded (empty when absent)."""
        shelves = self.data.get("shelves")
        if isinstance(shelves, Mapping):
            return shelves
        return {}

    def add_shelf(self, name: str, path: Path, *, title: str | None = None) -> None:
        if "shelves" not in self.data:
            self.data["shelves"] = tomlkit.table()
        shelves = self.data["shelves"]
        if name in shelves:
            raise ConfigError(f"shelf {name!r} is already registered")
        table = tomlkit.table()
        table["path"] = store_path(path)
        if title:
            table["title"] = title
        shelves[name] = table

    def has_shelf(self, name: str) -> bool:
        return name in self.shelves()

    def find_shelf_by_path(self, path: Path) -> str | None:
        """Return the name of the shelf registered at *path*, if any."""
        wanted = path.resolve()
        for name, table in self.shelves().items():
            stored = table.get("path")
            if (
                isinstance(stored, str)
                and resolve_path(stored, self.path.parent).resolve() == wanted
            ):
                return str(name)
        return None

    def remove_shelf(self, target: str) -> str:
        """Remove the shelf named *target* or living at path *target*.

        Returns the removed shelf's name. Raises :class:`ConfigError` when no
        entry matches; nothing else in the config is touched.
        """
        shelves = self.shelves()
        if target in shelves:
            del self.data["shelves"][target]
            return target
        name = self.find_shelf_by_path(resolve_path(target, self.path.parent))
        if name is None:
            raise ConfigError(f"no shelf named or at {target!r}")
        del self.data["shelves"][name]
        return name

    def save(self) -> None:
        text = tomlkit.dumps(self.data)
        if not text.endswith("\n"):
            text += "\n"
        directory = self.path.parent
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o700)
        mode = self.path.stat().st_mode & 0o777 if self.path.exists() else 0o600
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=f".{self.path.name}."
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
