"""In-memory artifact store: sorted views per shelf and the JSON payload.

:class:`Index` is the object the server renders from and ``/api/index.json``
serialises. Curriculum order is ``NNNN`` ascending (unnumbered last, then
path); recency is mtime descending.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
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
from lesvi.state import PinState, artifact_key


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
    root: Path
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

    def __init__(
        self, shelves: Mapping[str, Shelf], state: PinState | None = None
    ) -> None:
        self.shelves: dict[str, Shelf] = dict(shelves)
        #: The reader's pin decisions, when the caller has them; ``None`` means
        #: "no decisions", so declared pin seeds apply as-is.
        self.state: PinState | None = state
        self.artifacts: tuple[Artifact, ...] = tuple(
            artifact for shelf in self.shelves.values() for artifact in shelf.curriculum
        )
        # v1 renders the visible categories only; research stays indexed in
        # ``/api/index.json`` but never reaches a card.
        self.visible_artifacts: tuple[Artifact, ...] = tuple(
            artifact for artifact in self.artifacts if not self._hidden(artifact)
        )

    @classmethod
    def build(cls, config: Config, state: PinState | None = None) -> Index:
        """Scan every configured shelf into a fresh index.

        When *state* is given, the reader's stored pin decisions are applied and
        declared pin seeds are honoured; without it, seeds still pin artifacts.
        """
        return cls(
            {
                name: _build_shelf(str(name), table, config, state)
                for name, table in config.shelves().items()
            },
            state=state,
        )

    def by_number(self, shelf: str) -> tuple[Artifact, ...]:
        """Curriculum order for *shelf*: ``NNNN`` ascending, then path."""
        return self.shelves[shelf].curriculum

    def by_recency(self, shelf: str) -> tuple[Artifact, ...]:
        """Recency order for *shelf*: mtime descending."""
        return self.shelves[shelf].recency

    def recent(self, limit: int | None = None) -> tuple[Artifact, ...]:
        """Visible artifacts across shelves, newest first; capped at *limit*."""
        ordered = sorted(
            self.visible_artifacts,
            key=lambda artifact: (
                -_recency_key(artifact),
                artifact.shelf,
                artifact.path,
            ),
        )
        return tuple(ordered if limit is None else ordered[:limit])

    def pinned(self) -> tuple[Artifact, ...]:
        """Pinned visible artifacts across shelves, newest first."""
        return tuple(artifact for artifact in self.recent() if artifact.pinned)

    def changed(
        self,
        shelf: str,
        *,
        upsert: Iterable[Artifact] = (),
        remove: Iterable[str] = (),
    ) -> Index:
        """A new index with one shelf's artifacts upserted or removed.

        Only the named shelf is touched; its category counts and sorted views
        are recomputed and the cross-shelf views follow. Pin seeds and stored
        decisions are applied as in :meth:`build`. Returns ``self`` when
        nothing would differ, so callers can use an identity check to skip
        notifying readers.
        """
        existing = self.shelves.get(shelf)
        if existing is None:
            return self
        before = {record.path: record for record in existing.curriculum}
        after = dict(before)
        for path in remove:
            after.pop(path, None)
        for record in upsert:
            after[record.path] = apply_pin(shelf, record, self.state)
        if after == before:
            return self

        records = list(after.values())
        counts = dict.fromkeys((category.key for category in existing.categories), 0)
        for record in records:
            if record.category_key in counts:
                counts[record.category_key] += 1
        updated = replace(
            existing,
            categories=tuple(
                replace(category, count=counts[category.key])
                for category in existing.categories
            ),
            curriculum=tuple(sorted(records, key=_curriculum_key)),
            recency=tuple(sorted(records, key=_recency_order)),
        )
        shelves = dict(self.shelves)
        shelves[shelf] = updated
        return Index(shelves, state=self.state)

    def _hidden(self, artifact: Artifact) -> bool:
        shelf = self.shelves.get(artifact.shelf)
        if shelf is None:  # pragma: no cover - every artifact names its shelf
            return False
        return any(
            category.hidden and category.key == artifact.category_key
            for category in shelf.categories
        )

    def to_json(self) -> dict[str, Any]:
        """The ``/api/index.json`` payload: shelves (with sorted views) + artifacts."""
        return {
            "shelves": {name: shelf.to_json() for name, shelf in self.shelves.items()},
            "artifacts": [artifact.to_json() for artifact in self.artifacts],
        }


def _build_shelf(
    name: str,
    table: Mapping[str, Any],
    config: Config,
    state: PinState | None,
) -> Shelf:
    root = resolve_path(str(table.get("path", "")), config.path.parent)
    categories = shelf_categories(table)
    records = scan_shelf(name, root, categories, shelf_ignores(table))
    records = [apply_pin(name, record, state) for record in records]

    counts = dict.fromkeys(categories, 0)
    for record in records:
        if record.category_key in counts:
            counts[record.category_key] += 1

    raw_title = table.get("title")
    title = raw_title if isinstance(raw_title, str) and raw_title else name

    return Shelf(
        name=name,
        title=title,
        root=root.resolve(),
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
        recency=tuple(sorted(records, key=_recency_order)),
    )


def apply_pin(name: str, record: Artifact, state: PinState | None) -> Artifact:
    """Resolve the artifact's effective pin: stored decision > seed > off."""
    pinned = (
        state.is_pinned(artifact_key(name, record.path), record.pin_seed)
        if state is not None
        else record.pin_seed
    )
    return replace(record, pinned=pinned)


def _curriculum_key(artifact: Artifact) -> tuple[bool, int, str]:
    number = artifact.number
    return (number is None, number if number is not None else 0, artifact.path)


def _recency_key(artifact: Artifact) -> float:
    return datetime.fromisoformat(artifact.mtime).timestamp()


def _recency_order(artifact: Artifact) -> tuple[float, str]:
    """Newest first with a path tie-break, so restart and incremental order agree."""
    return (-_recency_key(artifact), artifact.path)
