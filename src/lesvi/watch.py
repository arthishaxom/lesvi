"""Watch registered shelves and keep the in-memory index current.

Two event sources, one consumer. Native filesystem events come from
``watchfiles`` when the optional extra is installed; otherwise (or when it
fails, e.g. the inotify watch limit is exhausted) mtime polling at the
configured interval behaves identically.

Every event is filtered through the same shelf convention a full scan uses, so:

* ignored paths, dotpaths and ``node_modules`` produce no work;
* a sidecar change is an update to its sibling artifact, never a card of its
  own — and sidecars beside ignored artifacts stay ignored;
* bursts settle for :data:`DEBOUNCE_SECONDS` before anything is parsed, so a
  file written in chunks is parsed once it stops changing;
* only the affected shelf is rescanned and rebuilt, never the whole index.

The watcher writes nothing: it is a reader of shelves, exactly like the
scanner. ``Watcher.handle`` is the synchronous core (also the test seam);
``Watcher.start`` runs an event source in one daemon thread.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lesvi.config import (
    SIDECAR_SUFFIX,
    Config,
    is_ignored,
    is_sidecar,
    resolve_path,
    shelf_categories,
    shelf_ignores,
)
from lesvi.index import Index
from lesvi.scanner import Artifact, match_category, scan_artifact, scan_subtree
from lesvi.state import PinState

log = logging.getLogger(__name__)

#: How long a burst of changes must go quiet before anything is parsed.
DEBOUNCE_SECONDS = 0.3
#: mtime polling cadence when native events are unavailable.
POLL_INTERVAL = 2.0

#: Event kinds; ``watchfiles``' ``Change`` members map onto these.
ADDED = "added"
MODIFIED = "modified"
DELETED = "deleted"

#: One stat's worth of identity: nanosecond mtime plus size.
_Stamp = tuple[int, int]


@dataclass(frozen=True)
class Event:
    """One filesystem change: absolute path plus what happened to it."""

    kind: str
    path: Path


@dataclass(frozen=True)
class _ShelfView:
    """A shelf as the watcher needs it: root plus its effective convention."""

    name: str
    root: Path
    categories: Mapping[str, tuple[str, ...]]
    ignore: tuple[str, ...]


@dataclass
class _Batch:
    """The pending index edits for one shelf within one event burst."""

    upsert: dict[str, Artifact] = field(default_factory=dict)
    remove: set[str] = field(default_factory=set)
    #: Subtrees being rescanned; existing records under them are reconciled.
    prefixes: set[str] = field(default_factory=set)


def watchfiles_installed() -> bool:
    """Whether the optional ``watchfiles`` extra can be imported."""
    return importlib.util.find_spec("watchfiles") is not None


class Watcher:
    """Keep an :class:`Index` current from filesystem events.

    *on_update* is called with each new snapshot so the server can swap the
    index it serves from; the watcher itself never mutates a shelf.
    """

    config: Config
    index: Index
    state: PinState | None
    debounce: float
    poll_interval: float
    prefer_watchfiles: bool
    on_update: Callable[[Index], None] | None
    _views: tuple[_ShelfView, ...]
    _stop: threading.Event
    _ready: threading.Event
    _native: bool

    def __init__(
        self,
        config: Config,
        index: Index,
        *,
        state: PinState | None = None,
        debounce: float = DEBOUNCE_SECONDS,
        poll_interval: float = POLL_INTERVAL,
        prefer_watchfiles: bool = True,
        on_update: Callable[[Index], None] | None = None,
    ) -> None:
        self.config = config
        self.index = index
        # Pins resolved by the index must also resolve for records the watcher
        # adds or replaces; defaulting to the index's own state avoids
        # silently unpinning records when the caller omits it.
        self.state = index.state if state is None else state
        self.debounce = debounce
        self.poll_interval = poll_interval
        self.prefer_watchfiles = prefer_watchfiles
        self.on_update = on_update
        self._views = _shelf_views(config)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._native = False
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the watcher thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def using_watchfiles(self) -> bool:
        """Whether the attached source is native events (as opposed to polling)."""
        return self._native

    def start(self) -> None:
        """Start watching in a daemon thread; a running watcher is left be.

        Returns once the event source is attached and reconciled: a change made
        before the call is in the index, and a write that follows it is
        guaranteed to be seen. Blocks at most five seconds.
        """
        if self.running:
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="lesvi-watch", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            log.warning("the watcher did not start within five seconds")

    def stop(self) -> None:
        """Ask the event source to stop and wait briefly for the thread."""
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            self._thread = None
            return
        thread.join(timeout=5)
        if thread.is_alive():
            # Keep the handle so a second start() cannot stack a watcher.
            log.debug("the watcher thread did not stop within five seconds")
            return
        self._thread = None

    # -- the synchronous core ----------------------------------------------

    def handle(self, events: Iterable[Event]) -> Index:
        """Apply *events* to the index; returns the current snapshot.

        Events are coalesced per shelf so a burst rebuilds each affected
        shelf's sorted views once. The event kind is advisory: each path is
        stat()ed, so a create/delete pair converges on whatever the disk now
        holds. When nothing changed, the previous index is returned untouched
        and ``on_update`` is not called.
        """
        batches: dict[str, _Batch] = {}
        for event in events:
            for view in self._views:
                batch = batches.setdefault(view.name, _Batch())
                self._collect(view, batch, event.path)
        index = self.index
        for view in self._views:
            batch = batches.get(view.name)
            if batch is None or not (batch.upsert or batch.remove or batch.prefixes):
                continue
            index = self._apply(index, view, batch)
        if index is not self.index:
            self.index = index
            if self.on_update is not None:
                self.on_update(index)
        return index

    def _collect(self, view: _ShelfView, batch: _Batch, path: Path) -> None:
        """Fold one event path into *batch*, mirroring full-scan filtering."""
        relative = _relative(path, view.root)
        if relative is None:
            return
        if is_sidecar(relative):
            primary = relative[: -len(SIDECAR_SUFFIX)]
            if not primary or is_ignored(primary, view.ignore):
                return  # metadata about an ignored artifact stays ignored
            record = scan_artifact(
                view.name, view.root, primary, view.categories, view.ignore
            )
            if record is None:
                batch.remove.add(primary)
            else:
                batch.upsert[primary] = record
            return
        if is_ignored(relative, view.ignore):
            return
        target = view.root / relative if relative else view.root
        if target.is_dir():
            # A file replaced by a directory leaves a stale record at this
            # exact path; everything below it is reconciled by the rescan.
            if relative:
                batch.remove.add(relative)
            self._collect_subtree(view, batch, relative)
        elif target.is_file():
            record = scan_artifact(
                view.name, view.root, relative, view.categories, view.ignore
            )
            if record is None:
                batch.remove.add(relative)
            else:
                batch.upsert[relative] = record
        else:
            # Gone: a deleted file, or a directory whose children went with it.
            if relative:
                batch.remove.add(relative)
            batch.prefixes.add(relative)

    def _collect_subtree(self, view: _ShelfView, batch: _Batch, relative: str) -> None:
        """Rescan a directory event; the subtree is reconciled, not appended."""
        for record in scan_subtree(
            view.name, view.root, relative, view.categories, view.ignore
        ):
            batch.upsert[record.path] = record
        batch.prefixes.add(relative)

    def _apply(self, index: Index, view: _ShelfView, batch: _Batch) -> Index:
        shelf = index.shelves.get(view.name)
        if shelf is None:
            return index
        remove = set(batch.remove)
        if batch.prefixes:
            # Match each record's ancestors against the prefix set once: a
            # mass delete yields one event per file, and scanning the whole
            # shelf per event would be quadratic.
            whole_shelf = "" in batch.prefixes
            for record in shelf.curriculum:
                parts = record.path.split("/")
                if whole_shelf or any(
                    "/".join(parts[:depth]) in batch.prefixes
                    for depth in range(1, len(parts))
                ):
                    remove.add(record.path)
        # Records the batch just re-scanned are re-added by the upsert.
        remove.difference_update(batch.upsert)
        return index.changed(view.name, upsert=batch.upsert.values(), remove=remove)

    # -- event sources ------------------------------------------------------

    def _run(self) -> None:
        native = self.prefer_watchfiles and watchfiles_installed() and self._watchable()
        self._native = native
        while not self._stop.is_set():
            source = self._watchfiles_events if native else self._poll_events
            try:
                batches = source()
                while not self._stop.is_set():
                    try:
                        batch = next(batches)
                    except StopIteration:
                        return
                    self.handle(batch)
            except Exception as exc:
                if native:
                    log.warning(
                        "native file watching failed (%s); falling back to "
                        "polling every %.2fs",
                        exc,
                        self.poll_interval,
                    )
                    native = False
                    self._native = False
                    self._recover()
                    continue
                log.exception("the file watcher stopped")
                return

    def _watchable(self) -> bool:
        return any(view.root.is_dir() for view in self._views)

    def _recover(self) -> None:
        """Rescan every shelf, so changes missed while attaching are applied."""
        try:
            self.handle(Event(ADDED, view.root) for view in self._views)
        except Exception:
            log.exception("the recovery rescan failed")

    def _watchfiles_events(self) -> Iterator[list[Event]]:
        """Yield debounced native batches; may raise when the backend fails."""
        from watchfiles import Change, watch

        kinds = {
            Change.added: ADDED,
            Change.modified: MODIFIED,
            Change.deleted: DELETED,
        }
        roots = [str(view.root) for view in self._views if view.root.is_dir()]
        if not roots:
            raise RuntimeError("no shelf roots exist to watch")
        stream = watch(
            *roots,
            # The watcher applies the shelf convention itself; taking every
            # raw change keeps hidden names and vendored dirs visible to it.
            watch_filter=None,
            debounce=int(self.debounce * 1000),
            # A short timeout with yield_on_timeout lets the first iteration
            # prove the watches are attached before start() returns.
            rust_timeout=250,
            yield_on_timeout=True,
            stop_event=self._stop,
        )
        primed = False
        for changes in stream:
            if not primed:
                primed = True
                self._recover()
                self._ready.set()
            if changes:
                yield [Event(kinds[change], Path(path)) for change, path in changes]

    def _poll_events(self) -> Iterator[list[Event]]:
        """Yield mtime-diff batches; two walks per detected change."""
        previous = {view.name: _snapshot(view) for view in self._views}
        self._recover()
        self._ready.set()
        while not self._stop.wait(self.poll_interval):
            current = {view.name: _snapshot(view) for view in self._views}
            if current == previous:
                continue
            if self._stop.wait(self.debounce):
                return  # stopping
            settled = {view.name: _snapshot(view) for view in self._views}
            batch: list[Event] = []
            for view in self._views:
                batch.extend(
                    _diff_events(view, previous[view.name], settled[view.name])
                )
            previous = settled
            if batch:
                yield batch


def _shelf_views(config: Config) -> tuple[_ShelfView, ...]:
    """A shelf's root and convention, resolved once at startup."""
    views: list[_ShelfView] = []
    for name, table in config.shelves().items():
        root = resolve_path(str(table.get("path", "")), config.path.parent).resolve()
        views.append(
            _ShelfView(
                str(name),
                root,
                shelf_categories(table),
                shelf_ignores(table),
            )
        )
    return tuple(views)


def _snapshot(view: _ShelfView) -> dict[str, tuple[_Stamp | None, _Stamp | None]]:
    """Every indexable artifact's identity: the file and its sidecar stamps.

    Folding the sidecar into its artifact's signature means one diff catches
    both channels, and a sidecar never looks like a card of its own.
    """
    root = view.root
    snapshot: dict[str, tuple[_Stamp | None, _Stamp | None]] = {}
    if not root.is_dir():
        return snapshot
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not is_ignored(_relative(here / dirname, root) or "", view.ignore)
        ]
        for filename in sorted(filenames):
            relative = _relative(here / filename, root)
            if (
                relative is None
                or relative == ""
                or is_sidecar(relative)
                or is_ignored(relative, view.ignore)
            ):
                continue
            if match_category(relative, view.categories) is None:
                continue
            artifact = root / relative
            snapshot[relative] = (
                _stamp(artifact),
                _stamp(artifact.with_name(artifact.name + SIDECAR_SUFFIX)),
            )
    return snapshot


def _diff_events(
    view: _ShelfView,
    previous: Mapping[str, tuple[_Stamp | None, _Stamp | None]],
    current: Mapping[str, tuple[_Stamp | None, _Stamp | None]],
) -> list[Event]:
    """The added/modified/deleted events between two poll snapshots."""
    events = [
        Event(ADDED, view.root / relative)
        for relative in sorted(current.keys() - previous.keys())
    ]
    events += [
        Event(DELETED, view.root / relative)
        for relative in sorted(previous.keys() - current.keys())
    ]
    events += [
        Event(MODIFIED, view.root / relative)
        for relative in sorted(previous.keys() & current.keys())
        if previous[relative] != current[relative]
    ]
    return events


def _stamp(path: Path) -> _Stamp | None:
    """Nanosecond mtime and size, or ``None`` when the path is not there."""
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _relative(path: Path, root: Path) -> str | None:
    """The shelf-relative POSIX path, or ``None`` when outside the shelf.

    A string prefix check keeps per-event cost low; the paths the event
    sources produce are absolute and normalised, so this is equivalent to
    :meth:`Path.relative_to` at a fraction of the cost.
    """
    root_text = str(root)
    text = str(path)
    if text == root_text:
        return ""
    prefix = root_text if root_text.endswith("/") else root_text + "/"
    if not text.startswith(prefix):
        return None
    return text[len(prefix) :]
