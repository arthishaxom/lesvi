"""HTTP surface: the single-port server, ``/api/index.json`` and raw artifacts.

``/a/<shelf>/<path>`` serves the file on disk byte-for-byte — HTML keeps
working with its relative assets, quiz JS and mermaid, and media downloads with
its own content type. Conditional requests get ``304`` from a strong ``ETag``
and ``Last-Modified``. Anything that would leave the shelf root, plus dotpaths
and ``node_modules``, is a ``404``; directories are never listed.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import stat
from datetime import UTC
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from lesvi import __version__
from lesvi.config import DEFAULT_HOST, DEFAULT_PORT
from lesvi.index import Index
from lesvi.render import (
    HOME_PAGE_SIZE,
    MAX_HOME_LIMIT,
    RECENT_SORT,
    render_home,
    render_shelf,
)

log = logging.getLogger(__name__)

ARTIFACT_PREFIX = "/a/"
SHELF_PREFIX = "/s/"
ASSET_PREFIX = "/assets/"
CHUNK_SIZE = 64 * 1024
OCTET_STREAM = "application/octet-stream"

UI_ASSETS: dict[str, str] = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}

_UI_DIR = Path(__file__).resolve().parent / "ui"

CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


class LesviServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` that carries the in-memory index."""

    daemon_threads: bool = True
    index: Index

    def __init__(self, address: tuple[str, int], index: Index) -> None:
        super().__init__(address, LesviRequestHandler)
        self.index = index


class LesviRequestHandler(BaseHTTPRequestHandler):
    server_version: str = f"lesvi/{__version__}"
    protocol_version: str = "HTTP/1.1"
    # Declared upstream; annotated here so basedpyright accepts the assignments
    # that drop keep-alive when a response cannot be completed in full.
    close_connection: bool

    @property
    def index(self) -> Index:
        return cast(LesviServer, self.server).index

    def do_GET(self) -> None:
        self._handle(include_body=True)

    def do_HEAD(self) -> None:
        self._handle(include_body=False)

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s - %s", self.address_string(), format % args)

    def _handle(self, *, include_body: bool) -> None:
        try:
            self._respond(include_body=include_body)
        except OSError:
            # The client vanished mid-response (cancelled load, flaky mobile
            # network): nobody is left to send a 500 to.
            log.debug("client disconnected during %s", self.path, exc_info=True)
            self.close_connection = True

    def _respond(self, *, include_body: bool) -> None:
        try:
            target = urlsplit(self.path)
        except ValueError:  # absolute-form target with a malformed IPv6 authority
            self.send_error(400, "bad request target")
            return
        path = target.path
        if path == "/api/index.json":
            self._send_index(include_body=include_body)
        elif path.startswith(ARTIFACT_PREFIX):
            self._serve_artifact(path, include_body=include_body)
        elif path == "/":
            self._send_home(target.query, include_body=include_body)
        elif path.startswith(SHELF_PREFIX):
            self._send_shelf(path, target.query, include_body=include_body)
        elif path.startswith(ASSET_PREFIX):
            self._send_asset(path, include_body=include_body)
        else:
            self.send_error(404, "not found")

    def _send_home(self, query: str, *, include_body: bool) -> None:
        limit = _parse_limit(query)
        self._send_html(render_home(self.index, limit=limit), include_body=include_body)

    def _send_shelf(self, path: str, query: str, *, include_body: bool) -> None:
        if not path.endswith("/"):
            location = path + "/"
            if query:
                location += f"?{query}"
            self._redirect(location)
            return
        name = unquote(path[len(SHELF_PREFIX) : -1])
        shelf = self.index.shelves.get(name) if name and "/" not in name else None
        if shelf is None:
            self.send_error(404, "not found")
            return
        sort = RECENT_SORT if parse_qs(query).get("sort") == [RECENT_SORT] else ""
        self._send_html(
            render_shelf(self.index, name, sort=sort), include_body=include_body
        )

    def _send_html(self, body: str, *, include_body: bool) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_asset(self, path: str, *, include_body: bool) -> None:
        name = path[len(ASSET_PREFIX) :]
        content_type = UI_ASSETS.get(name)
        if content_type is None:
            self.send_error(404, "not found")
            return
        target = _UI_DIR / name
        try:
            stat_result = target.stat()
        except OSError:
            log.exception("UI asset missing from the package: %s", target)
            self.send_error(404, "not found")
            return
        self._send_file(target, content_type, stat_result, include_body=include_body)

    def _send_index(self, *, include_body: bool) -> None:
        body = json.dumps(
            self.index.to_json(), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "Vary": "Accept-Encoding",
        }
        if _accepts_gzip(self.headers.get("Accept-Encoding", "")):
            body = gzip.compress(body, compresslevel=6, mtime=0)
            headers["Content-Encoding"] = "gzip"
        self.send_response(200)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _serve_artifact(self, path: str, *, include_body: bool) -> None:
        resolved = _resolve_artifact(self.index, path)
        if resolved is None:
            self.send_error(404, "not found")
            return
        target, stat_result = resolved
        self._send_file(
            target, content_type_for(target), stat_result, include_body=include_body
        )

    def _send_file(
        self,
        target: Path,
        content_type: str,
        stat_result: os.stat_result,
        *,
        include_body: bool,
    ) -> None:
        """Send one regular file with validators, streaming it when wanted."""
        etag = _etag(stat_result)
        last_modified = formatdate(stat_result.st_mtime, usegmt=True)
        if self._not_modified(etag, stat_result.st_mtime):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat_result.st_size))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        # Artifacts are overwritten in place on republish; the validators above
        # make revalidation cheap, so never serve a stale copy blindly.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if include_body:
            self._send_body(target, stat_result.st_size)

    def _not_modified(self, etag: str, mtime: float) -> bool:
        """Whether the request's validators match the current representation."""
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match is not None:
            return _etag_matches(if_none_match, etag)
        if_modified_since = self.headers.get("If-Modified-Since")
        if not if_modified_since:
            return False
        since = _parse_http_date(if_modified_since)
        return since is not None and int(mtime) <= since

    def _send_body(self, target: Path, size: int) -> None:
        """Stream exactly *size* bytes; a truncated read closes the connection.

        I/O errors propagate to :meth:`_handle`; what is handled here is the
        file shrinking between the ``stat`` and the open — the advertised
        ``Content-Length`` can no longer be met, so the connection is dropped
        rather than ending the body early on a keep-alive socket.
        """
        remaining = size
        with target.open("rb") as handle:
            while remaining > 0:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        if remaining:
            log.debug("artifact shrank mid-response: %s", target)
            self.close_connection = True


def content_type_for(path: Path) -> str:
    """MIME type by extension; unknown extensions download as bytes."""
    return CONTENT_TYPES.get(path.suffix.lower(), OCTET_STREAM)


def _parse_limit(query: str) -> int:
    """The home feed's card budget: ``limit`` clamped to sane bounds."""
    values = parse_qs(query).get("limit")
    if not values:
        return HOME_PAGE_SIZE
    try:
        requested = int(values[0])
    except ValueError:
        return HOME_PAGE_SIZE
    if requested < 1:
        return HOME_PAGE_SIZE
    return min(requested, MAX_HOME_LIMIT)


def _resolve_artifact(index: Index, path: str) -> tuple[Path, os.stat_result] | None:
    """Map an ``/a/`` URL to a regular file inside a shelf root, or ``None``.

    The raw suffix is decoded exactly once; every segment must be a plain name
    (no traversal, dotpaths or ``node_modules``), and the fully resolved path —
    symlinks included — must stay inside the shelf's real root. In-shelf
    symlinks are aliases, not loopholes: the resolved target's own segments must
    be plain too, so an alias cannot serve a hidden or vendored file.
    """
    try:
        decoded = unquote(path[len(ARTIFACT_PREFIX) :], errors="strict")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded:
        return None
    parts = decoded.split("/")
    if len(parts) < 2:
        return None
    shelf = index.shelves.get(parts[0])
    segments = parts[1:]
    if shelf is None or any(not _plain_segment(part) for part in segments):
        return None
    try:
        resolved = shelf.root.joinpath(*segments).resolve(strict=True)
        stat_result = resolved.stat()
    except OSError:
        log.debug("artifact not served: %s", path, exc_info=True)
        return None
    if not resolved.is_relative_to(shelf.root) or not stat.S_ISREG(stat_result.st_mode):
        return None
    if any(not _plain_segment(part) for part in resolved.relative_to(shelf.root).parts):
        return None
    return resolved, stat_result


def _plain_segment(segment: str) -> bool:
    """A URL segment that names an ordinary file: no ``..``, dotfiles, vendored dirs."""
    return bool(segment) and not segment.startswith(".") and segment != "node_modules"


def _etag(stat_result: os.stat_result) -> str:
    """A strong validator: nanosecond mtime + size changes on every republish."""
    return f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'


def _etag_matches(header: str, etag: str) -> bool:
    """Whether an ``If-None-Match`` list matches *etag* (weak comparison)."""
    for token in header.split(","):
        candidate = token.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


def _parse_http_date(value: str) -> int | None:
    """Seconds since the epoch for an HTTP date, or ``None`` when unparsable."""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:  # no zone means UTC per RFC 9110
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return int(parsed.timestamp())
    except (OSError, OverflowError, ValueError):
        return None


def _accepts_gzip(header: str) -> bool:
    """Whether an ``Accept-Encoding`` header allows gzip, honouring q-values."""
    wildcard: bool | None = None
    for part in header.split(","):
        fields = part.split(";")
        token = fields[0].strip().lower()
        quality = 1.0
        for field in fields[1:]:
            name, _, value = field.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 1.0
        if token == "gzip":
            return quality > 0
        if token == "*":
            wildcard = quality > 0
    return wildcard is True


def make_server(
    index: Index, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> LesviServer:
    """Bind a server for *index*; use port ``0`` for an ephemeral port."""
    return LesviServer((host, port), index)
