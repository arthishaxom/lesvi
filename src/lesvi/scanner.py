"""Scanner: walk shelf roots and produce artifact records.

The scanner applies a shelf's effective category globs and ignore list
(preset or per-shelf override), derives heuristics metadata, and never writes
to a shelf. ``HTML_READ_LIMIT`` caps how much of an artifact the metadata
parser ever sees.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lesvi.config import (
    Config,
    category_label,
    glob_match,
    is_ignored,
    resolve_path,
    shelf_categories,
    shelf_ignores,
)
from lesvi.metadata import heuristic_metadata

log = logging.getLogger(__name__)

HTML_READ_LIMIT = 64 * 1024


@dataclass(frozen=True)
class Artifact:
    """One indexed artifact, shaped as documented in spec section 8."""

    shelf: str
    path: str
    url: str
    category: str
    number: int | None
    title: str
    description: str
    tags: tuple[str, ...]
    mtime: str
    size: int
    pinned: bool = False
    meta_source: str = "heuristic"
    category_key: str = ""

    def to_json(self) -> dict[str, Any]:
        record: dict[str, object] = {
            "shelf": self.shelf,
            "path": self.path,
            "url": self.url,
            "category": self.category,
            "number": self.number,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "mtime": self.mtime,
            "size": self.size,
            "pinned": self.pinned,
            "meta_source": self.meta_source,
        }
        return record


def scan_shelf(
    name: str,
    root: Path,
    categories: Mapping[str, Sequence[str]],
    ignore: Sequence[str],
) -> list[Artifact]:
    """Index every file under *root* matching *categories*, skipping *ignore*."""
    if not root.is_dir():
        return []
    real_root = root.resolve()
    records: list[Artifact] = []
    for relative, category_key in _iter_matches(root, categories, ignore):
        record = _artifact(name, root, real_root, relative, category_key)
        if record is not None:
            records.append(record)
    return records


def scan(config: Config) -> list[Artifact]:
    """Index every registered shelf; missing shelf roots index as empty."""
    records: list[Artifact] = []
    for name, table in config.shelves().items():
        root = resolve_path(str(table.get("path", "")), config.path.parent)
        records.extend(
            scan_shelf(
                str(name),
                root,
                shelf_categories(table),
                shelf_ignores(table),
            )
        )
    return records


def _iter_matches(
    root: Path,
    categories: Mapping[str, Sequence[str]],
    ignore: Sequence[str],
) -> Iterator[tuple[str, str]]:
    """Yield ``(relative path, category key)`` in deterministic walk order."""
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not is_ignored(_relative(root, here / dirname), ignore)
        ]
        for filename in sorted(filenames):
            relative = _relative(root, here / filename)
            if is_ignored(relative, ignore):
                continue
            matched = _match_category(relative, categories)
            if matched is not None:
                yield relative, matched


def _match_category(
    relative: str, categories: Mapping[str, Sequence[str]]
) -> str | None:
    for key, patterns in categories.items():
        if any(glob_match(pattern, relative) for pattern in patterns):
            return key
    return None


def _artifact(
    name: str,
    root: Path,
    real_root: Path,
    relative: str,
    category_key: str,
) -> Artifact | None:
    path = root / relative
    try:
        stat_result = path.stat()
    except OSError:
        log.debug("skipping unreadable artifact %s", path, exc_info=True)
        return None
    if not stat.S_ISREG(stat_result.st_mode):
        log.debug("skipping non-regular artifact %s", path)
        return None
    if path.is_symlink():
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            log.debug("skipping broken symlink %s", path, exc_info=True)
            return None
        if not resolved.is_relative_to(real_root):
            log.debug("skipping artifact outside the shelf root: %s", path)
            return None
    try:
        url = "/a/" + quote(f"{name}/{relative}", safe="/")
    except UnicodeEncodeError:
        log.debug("skipping artifact with a non-UTF-8 name: %r", relative)
        return None
    metadata = heuristic_metadata(
        relative, category_label(category_key), _read_prefix(path)
    )
    return Artifact(
        shelf=name,
        path=relative,
        url=url,
        category=category_label(category_key),
        category_key=category_key,
        number=metadata.number,
        title=metadata.title,
        description=metadata.description,
        tags=metadata.tags,
        mtime=_format_mtime(stat_result.st_mtime, path),
        size=stat_result.st_size,
    )


def _format_mtime(timestamp: float, path: Path) -> str:
    """ISO 8601 local mtime; unrepresentable values degrade to the epoch."""
    try:
        return (
            datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
        )
    except (OSError, OverflowError, ValueError):
        log.debug("unrepresentable mtime %r for %s", timestamp, path, exc_info=True)
        return datetime.fromtimestamp(0).astimezone().isoformat(timespec="seconds")


def _read_prefix(path: Path, limit: int = HTML_READ_LIMIT) -> str | None:
    """Read at most *limit* bytes of *path*; ``None`` when it cannot be read."""
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        log.debug("cannot read artifact %s", path, exc_info=True)
        return None


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
