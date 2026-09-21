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
    SIDECAR_SUFFIX,
    Config,
    category_label,
    glob_match,
    is_ignored,
    is_sidecar,
    resolve_path,
    shelf_categories,
    shelf_ignores,
)
from lesvi.metadata import resolve_metadata

log = logging.getLogger(__name__)

HTML_READ_LIMIT = 64 * 1024
#: Sidecars are tiny; anything larger is not a sidecar we should trust.
SIDECAR_READ_LIMIT = 64 * 1024


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
    #: Whether an enrichment channel declared this artifact pinned; the index
    #: resolves it against :class:`lesvi.state.PinState`. Never in the API JSON.
    pin_seed: bool = False

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


def artifact_url(shelf: str, relative: str, *, stamp: str | None = None) -> str:
    """The public URL for a shelf-relative artifact.

    Without *stamp* this is the stable ``/a/<shelf>/<path>`` address; with one
    (a capability segment from :meth:`lesvi.auth.Auth.artifact_stamp`) the
    segment goes right after ``/a/`` so the artifact's relative subresources
    inherit it. Both spellings percent-encode the path identically, so they
    address the same bytes.
    """
    base = f"/a/{stamp}/" if stamp else "/a/"
    return base + quote(f"{shelf}/{relative}", safe="/")


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


def scan_artifact(
    name: str,
    root: Path,
    relative: str,
    categories: Mapping[str, Sequence[str]],
    ignore: Sequence[str],
) -> Artifact | None:
    """Index one shelf-relative file, or ``None`` when it is not an artifact.

    The incremental seam the watcher uses: same ignore/sidecar/category rules as
    a full scan, reading only this file's prefix and sidecar.
    """
    if not relative or is_sidecar(relative) or is_ignored(relative, ignore):
        return None
    if not _plain_relative(relative):
        log.debug("rejecting non-relative artifact path: %r", relative)
        return None
    category_key = match_category(relative, categories)
    if category_key is None:
        return None
    return _artifact(name, root, root.resolve(), relative, category_key)


def scan_subtree(
    name: str,
    root: Path,
    relative: str,
    categories: Mapping[str, Sequence[str]],
    ignore: Sequence[str],
) -> list[Artifact]:
    """Index the matching artifacts under one shelf-relative path.

    *relative* is a directory (``""`` means the shelf root) or a file. The walk
    mirrors a full scan: ignored directories are pruned and symlinked
    directories are not followed. Anything that would escape the shelf root is
    rejected.
    """
    if relative and not _plain_relative(relative):
        log.debug("rejecting non-relative subtree path: %r", relative)
        return []
    target = root / relative if relative else root
    if target.is_symlink() and target.is_dir():
        return []  # a full scan never descends into a symlinked directory
    if target.is_file():
        record = scan_artifact(name, root, relative, categories, ignore)
        return [record] if record is not None else []
    if not target.is_dir():
        return []
    real_root = root.resolve()
    records: list[Artifact] = []
    for rel, category_key in _iter_matches(root, categories, ignore, start=target):
        record = _artifact(name, root, real_root, rel, category_key)
        if record is not None:
            records.append(record)
    return records


def _plain_relative(relative: str) -> bool:
    """A shelf-relative path that may be joined onto a shelf root safely.

    Absolute paths, traversal, empty segments and dot segments are rejected so
    the public incremental helpers cannot be pointed outside the shelf.
    """
    if not relative or relative.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in relative.split("/"))


def match_category(
    relative: str, categories: Mapping[str, Sequence[str]]
) -> str | None:
    """The first category key whose globs match *relative*; ``None`` otherwise."""
    for key, patterns in categories.items():
        if any(glob_match(pattern, relative) for pattern in patterns):
            return key
    return None


def _iter_matches(
    root: Path,
    categories: Mapping[str, Sequence[str]],
    ignore: Sequence[str],
    *,
    start: Path | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield ``(relative path, category key)`` in deterministic walk order."""
    for dirpath, dirnames, filenames in os.walk(start if start is not None else root):
        here = Path(dirpath)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not is_ignored(_relative(root, here / dirname), ignore)
        ]
        for filename in sorted(filenames):
            if is_sidecar(filename):
                continue  # metadata about an artifact, never an artifact itself
            relative = _relative(root, here / filename)
            if is_ignored(relative, ignore):
                continue
            matched = match_category(relative, categories)
            if matched is not None:
                yield relative, matched


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
        url = artifact_url(name, relative)
    except UnicodeEncodeError:
        log.debug("skipping artifact with a non-UTF-8 name: %r", relative)
        return None
    metadata = resolve_metadata(
        relative,
        category_label(category_key),
        _read_prefix(path),
        _read_sidecar(path, real_root),
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
        meta_source=metadata.meta_source,
        pin_seed=metadata.pin_seed,
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


def _read_sidecar(path: Path, real_root: Path) -> str | None:
    """Read the artifact's ``<file>.meta.json`` sidecar; ``None`` when absent.

    Sidecar symlinks get the same root check as artifact symlinks, so a link
    cannot smuggle metadata in from outside the shelf.
    """
    sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
    if not sidecar.is_file():  # the common case: no sidecar at all
        return None
    if sidecar.is_symlink():
        try:
            resolved = sidecar.resolve(strict=True)
        except OSError:
            log.debug("skipping broken sidecar symlink %s", sidecar, exc_info=True)
            return None
        if not resolved.is_relative_to(real_root):
            log.debug("skipping sidecar outside the shelf root: %s", sidecar)
            return None
    return _read_prefix(sidecar, SIDECAR_READ_LIMIT)


def _read_prefix(path: Path, limit: int = HTML_READ_LIMIT) -> str | None:
    """Read at most *limit* bytes of *path*; ``None`` when it cannot be read."""
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        log.debug("cannot read %s", path, exc_info=True)
        return None


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
