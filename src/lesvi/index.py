"""In-memory artifact store: sorted views per shelf and the JSON payload.

:class:`Index` is the object the server renders from and ``/api/index.json``
serialises. Curriculum order is ``NNNN`` ascending (unnumbered last, then
path); recency is mtime descending.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lesvi.config import (
    HIDDEN_CATEGORIES,
    Config,
    category_label,
    resolve_path,
    shelf_categories,
    shelf_ignores,
)
from lesvi.scanner import Artifact, scan_shelf


@dataclass(frozen=True)
class CategoryInfo:
    """One category of a shelf: its key, display label, count and visibility."""

    key: str
    label: str
    count: int
    hidden: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "count": self.count,
            "hidden": self.hidden,
        }


@dataclass(frozen=True)
class Shelf:
    """One indexed shelf with its precomputed sorted views."""

    name: str
    title: str
    categories: tuple[CategoryInfo, ...]
    curriculum: tuple[Artifact, ...]
    recency: tuple[Artifact, ...]

    @property
    def total(self) -> int:
        return len(self.curriculum)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "categories": [category.to_json() for category in self.categories],
            "curriculum": [artifact.path for artifact in self.curriculum],
            "recency": [artifact.path for artifact in self.recency],
            "total": self.total,
        }


class Index:
    """A snapshot of every registered shelf; scanning happens in :meth:`build`."""

    def __init__(self, shelves: Mapping[str, Shelf]) -> None:
        self.shelves: dict[str, Shelf] = dict(shelves)
        self.artifacts: tuple[Artifact, ...] = tuple(
            artifact for shelf in self.shelves.values() for artifact in shelf.curriculum
        )

    @classmethod
    def build(cls, config: Config) -> Index:
        """Scan every configured shelf into a fresh index."""
        return cls(
            {
                name: _build_shelf(str(name), table, config)
                for name, table in config.shelves().items()
            }
        )

    def by_number(self, shelf: str) -> tuple[Artifact, ...]:
        """Curriculum order for *shelf*: ``NNNN`` ascending, then path."""
        return self.shelves[shelf].curriculum

    def by_recency(self, shelf: str) -> tuple[Artifact, ...]:
        """Recency order for *shelf*: mtime descending."""
        return self.shelves[shelf].recency

    def to_json(self) -> dict[str, Any]:
        """The ``/api/index.json`` payload: shelves (with sorted views) + artifacts."""
        return {
            "shelves": {name: shelf.to_json() for name, shelf in self.shelves.items()},
            "artifacts": [artifact.to_json() for artifact in self.artifacts],
        }


def _build_shelf(name: str, table: Mapping[str, Any], config: Config) -> Shelf:
    root = resolve_path(str(table.get("path", "")), config.path.parent)
    categories = shelf_categories(table)
    records = scan_shelf(name, root, categories, shelf_ignores(table))

    counts = dict.fromkeys(categories, 0)
    for record in records:
        if record.category_key in counts:
            counts[record.category_key] += 1

    raw_title = table.get("title")
    title = raw_title if isinstance(raw_title, str) and raw_title else name

    return Shelf(
        name=name,
        title=title,
        categories=tuple(
            CategoryInfo(
                key=key,
                label=category_label(key),
                count=count,
                hidden=key in HIDDEN_CATEGORIES,
            )
            for key, count in counts.items()
        ),
        curriculum=tuple(sorted(records, key=_curriculum_key)),
        recency=tuple(sorted(records, key=_recency_key, reverse=True)),
    )


def _curriculum_key(artifact: Artifact) -> tuple[bool, int, str]:
    number = artifact.number
    return (number is None, number if number is not None else 0, artifact.path)


def _recency_key(artifact: Artifact) -> float:
    return datetime.fromisoformat(artifact.mtime).timestamp()
