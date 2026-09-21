"""HTTP surface: the single-port server, auth, dashboards and raw artifacts.

``/a/<shelf>/<path>`` serves the file on disk byte-for-byte — HTML keeps
working with its relative assets, quiz JS and mermaid, and media downloads with
its own content type. Artifacts are sandboxed (opaque origin) and readable
cross-origin, so lesson JS runs but cannot reach the authenticated API
(ADR-0010). Conditional requests get ``304`` from a strong ``ETag`` and
``Last-Modified``. Anything that would leave the shelf root, plus dotpaths and
``node_modules``, is a ``404``; directories are never listed.

Auth (when a token is configured): direct loopback requests are exempt, the
token unlocks ``/login`` and sets a signed session cookie, and scripts may send
``Authorization: Bearer`` instead (ADR-0004, ADR-0008). Dashboards link to
capability-stamped artifacts — ``/a/~<expiry>-<hmac>/<shelf>/<path>`` — because
a sandboxed document's opaque origin sends no cookie on its subresources; a
session-authorized browser that opens an unsigned document is redirected to its
signed URL (ADR-0010).

The PWA surface — ``/manifest.webmanifest``, ``/sw.js``, ``/favicon.ico`` — is
served verbatim and auth-exempt like ``/assets/*``: the phone installs the app
and its service worker keeps the shell and visited lessons readable offline.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import stat
import threading
from dataclasses import dataclass
from datetime import UTC
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, cast
from urllib.parse import parse_qs, unquote, urlsplit

from lesvi import __version__
from lesvi.auth import (
    LOGIN_PATH,
    LOGOUT_PATH,
    Auth,
    cleared_cookie,
    cookie_value,
    forwarded_https,
    is_exempt_path,
    session_cookie,
)
from lesvi.config import DEFAULT_HOST, DEFAULT_PORT
from lesvi.index import Index
from lesvi.render import (
    HOME_PAGE_SIZE,
    MAX_HOME_LIMIT,
    RECENT_SORT,
    render_home,
    render_login,
    render_shelf,
)
from lesvi.scanner import artifact_url

log = logging.getLogger(__name__)

ARTIFACT_PREFIX = "/a/"
SHELF_PREFIX = "/s/"
ASSET_PREFIX = "/assets/"
PIN_PATH = "/api/pin"
HEALTHZ_PATH = "/healthz"
CHUNK_SIZE = 64 * 1024
OCTET_STREAM = "application/octet-stream"
#: A pin payload is three short fields; anything larger is not one we should read.
PIN_BODY_LIMIT = 64 * 1024
#: A login form is one short field; anything larger is not one we should read.
LOGIN_BODY_LIMIT = 8 * 1024
#: Artifact documents get an opaque origin but may still run scripts and open
#: links in new tabs (ADR-0010).
ARTIFACT_SANDBOX = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
#: Artifact subresources must stay readable from that opaque origin, or ES
#: module quizzes would stop loading.
CORS_ANY = "*"

UI_ASSETS: dict[str, str] = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript; charset=utf-8",
    "favicon.ico": "image/x-icon",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
}

#: Static files served at the root, where the PWA needs them: a manifest and
#: a service worker have fixed addresses, and browsers ask for the favicon.
STATIC_PATHS: dict[str, str] = {
    "/manifest.webmanifest": "manifest.webmanifest",
    "/sw.js": "sw.js",
    "/favicon.ico": "favicon.ico",
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
    """A ``ThreadingHTTPServer`` that carries the in-memory index and the auth policy."""

    daemon_threads: bool = True
    index: Index
    #: Serialises index swaps: a pin write and a watcher publish must not
    #: interleave, or one snapshot would clobber the other's work.
    index_lock: threading.Lock
    #: The token policy; ``None`` serves without auth (a loopback bind or an
    #: explicit ``--insecure``).
    auth: Auth | None

    def __init__(
        self, address: tuple[str, int], index: Index, auth: Auth | None = None
    ) -> None:
        super().__init__(address, LesviRequestHandler)
        self.index = index
        self.index_lock = threading.Lock()
        self.auth = auth

    def set_index(self, index: Index) -> None:
        """Swap in a fresh snapshot, healing its pin decisions first.

        A watcher builds from a snapshot that can predate a pin recorded on
        the served index, so every swap re-resolves pins against the served
        state before taking effect. Returns only once the swap is visible.
        """
        with self.index_lock:
            state = self.index.state if self.index.state is not None else index.state
            self.index = index.with_pins(state)


class LesviRequestHandler(BaseHTTPRequestHandler):
    server_version: str = f"lesvi/{__version__}"
    protocol_version: str = "HTTP/1.1"
    #: A stalled client must not hold a handler thread forever (slow headers
    #: or a half-sent body); the stdlib closes the connection on a timeout.
    timeout: ClassVar[float | None] = 30
    # Declared upstream; annotated here so basedpyright accepts the assignments
    # that drop keep-alive when a response cannot be completed in full.
    close_connection: bool

    @property
    def index(self) -> Index:
        return cast(LesviServer, self.server).index

    @property
    def auth(self) -> Auth | None:
        return cast(LesviServer, self.server).auth

    def do_GET(self) -> None:
        self._handle(include_body=True)

    def do_HEAD(self) -> None:
        self._handle(include_body=False)

    def do_POST(self) -> None:
        self._handle(include_body=False, post=True)

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s - %s", self.address_string(), format % args)

    def _handle(self, *, include_body: bool, post: bool = False) -> None:
        try:
            self._respond(include_body=include_body, post=post)
        except OSError:
            # The client vanished mid-response (cancelled load, flaky mobile
            # network): nobody is left to send a 500 to.
            log.debug("client disconnected during %s", self.path, exc_info=True)
            self.close_connection = True

    def _respond(self, *, include_body: bool, post: bool = False) -> None:
        try:
            target = urlsplit(self.path)
        except ValueError:  # absolute-form target with a malformed IPv6 authority
            self.send_error(400, "bad request target")
            return
        path = target.path
        if not self._authorized(path):
            self._refuse(path)
            return
        if post:
            if path == PIN_PATH:
                self._set_pin()
            elif path == LOGIN_PATH:
                self._login()
            else:
                self.send_error(404, "not found")
        elif path == "/api/index.json":
            self._send_index(include_body=include_body)
        elif path == HEALTHZ_PATH:
            self._send_healthz(include_body=include_body)
        elif path == LOGIN_PATH:
            self._send_login(include_body=include_body)
        elif path == LOGOUT_PATH:
            self._logout()
        elif path.startswith(ARTIFACT_PREFIX):
            self._serve_artifact(path, include_body=include_body, query=target.query)
        elif path == "/":
            self._send_home(target.query, include_body=include_body)
        elif path.startswith(SHELF_PREFIX):
            self._send_shelf(path, target.query, include_body=include_body)
        elif path in STATIC_PATHS:
            self._send_static(STATIC_PATHS[path], include_body=include_body)
        elif path.startswith(ASSET_PREFIX):
            self._send_static(path[len(ASSET_PREFIX) :], include_body=include_body)
        else:
            self.send_error(404, "not found")

    # --- auth (ADR-0004, ADR-0008, ADR-0010) ----------------------------------

    def _authorized(self, path: str) -> bool:
        """Whether this request may proceed: exempt, local, or credentialled."""
        return self._credential(path) is not None

    def _credential(self, path: str) -> str | None:
        """How this request is authorized, or ``None`` when it is not.

        One of ``"open"`` (no token configured), ``"exempt"``, ``"local"``,
        ``"stamp"`` (a signed artifact capability), ``"bearer"`` or
        ``"session"``. :meth:`_serve_artifact` needs the distinction: only a
        session-authorized browser navigation is redirected to a signed URL.
        """
        auth = self.auth
        if auth is None:
            return "open"
        if is_exempt_path(path):
            return "exempt"
        if auth.is_local(self.client_address[0], self._header_map()):
            return "local"
        capability = _capability_segments(path)
        if capability is not None and auth.artifact_stamp_valid(*capability):
            return "stamp"
        bearer = auth.bearer_token(self.headers.get("Authorization"))
        if bearer is not None and auth.check_token(bearer):
            return "bearer"
        if auth.session_valid(cookie_value(self.headers.get("Cookie"))):
            return "session"
        return None

    def _header_map(self) -> dict[str, str]:
        """Request headers as a plain mapping for :mod:`lesvi.auth`'s checks."""
        return dict(self.headers.items())

    def _refuse(self, path: str) -> None:
        """Send an unauthenticated request to the login form, or a 401 for APIs.

        Pages redirect so a browser lands on ``/login``; the JSON API answers
        ``401`` because a fetch that follows a redirect would read the login
        page as a success.
        """
        # A body we will not read must close the connection: HTTP/1.1
        # keep-alive would otherwise parse the leftovers as the next request.
        closing = self.command not in ("GET", "HEAD") or self._request_has_body()
        if closing:
            self.close_connection = True
        if not path.startswith("/api/"):
            self._redirect(LOGIN_PATH, status=303, close=closing)
            return
        body = b'{"error":"unauthorized"}\n'
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if closing:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _request_has_body(self) -> bool:
        """Whether the request declares a body a refusal would leave unread."""
        length = self.headers.get("Content-Length")
        if length is not None and length.strip() != "0":
            return True
        return self.headers.get("Transfer-Encoding") is not None

    def _send_login(
        self, *, include_body: bool, error: bool = False, status: int = 200
    ) -> None:
        if self.auth is None:
            # No token configured: there is nothing to log into.
            self._redirect("/", status=303)
            return
        self._send_html(
            render_login(error=error), include_body=include_body, status=status
        )

    def _login(self) -> None:
        """``POST /login``: check the token, set the session cookie, go home."""
        auth = self.auth
        if auth is None:
            # The form body is unread; this connection cannot be reused.
            self.close_connection = True
            self._redirect("/", status=303, close=True)
            return
        body = self._read_login_body()
        if body is None:
            self.send_error(400, "invalid login payload")
            return
        candidate = parse_qs(body, keep_blank_values=True).get("token", [""])[0]
        if not auth.check_token(candidate):
            self._send_login(include_body=True, error=True, status=401)
            return
        cookie = session_cookie(
            auth.session_value(), secure=forwarded_https(self._header_map())
        )
        self._redirect("/", status=303, set_cookie=cookie, no_store=True)

    def _read_login_body(self) -> str | None:
        """The raw form body, or ``None`` when missing, oversized or broken."""
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            self.close_connection = True
            return None
        if not 0 < length <= LOGIN_BODY_LIMIT:
            # Refusing without draining the body would desync keep-alive.
            self.close_connection = True
            return None
        try:
            return self.rfile.read(length).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _logout(self) -> None:
        """``GET /logout``: forget the session cookie and show the form again."""
        cookie = cleared_cookie(secure=forwarded_https(self._header_map()))
        self._redirect(LOGIN_PATH, status=303, set_cookie=cookie, no_store=True)

    def _send_healthz(self, *, include_body: bool) -> None:
        body = json.dumps(
            {
                "ok": True,
                "shelves": len(self.index.shelves),
                "artifacts": len(self.index.artifacts),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _send_home(self, query: str, *, include_body: bool) -> None:
        limit = _parse_limit(query)
        self._send_html(
            render_home(self._signed_index(), limit=limit), include_body=include_body
        )

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
            render_shelf(self._signed_index(), name, sort=sort),
            include_body=include_body,
        )

    def _send_html(self, body: str, *, include_body: bool, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(data)

    def _redirect(
        self,
        location: str,
        *,
        status: int = 301,
        set_cookie: str | None = None,
        close: bool = False,
        no_store: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        if close:
            self.send_header("Connection", "close")
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_static(self, name: str, *, include_body: bool) -> None:
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
            self._signed_index().to_json(), separators=(",", ":"), ensure_ascii=False
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

    def _set_pin(self) -> None:
        """``POST /api/pin``: record a decision, persist it, swap the index.

        The response is ``204`` only after both the state file and the served
        index reflect the decision, so a refresh — or a restart — agrees.
        """
        if not self._pin_request_allowed():
            return
        payload = self._read_pin_payload()
        if payload is None:
            self.send_error(400, "invalid pin payload")
            return
        shelf, path, pinned = payload
        server = cast(LesviServer, self.server)
        try:
            with server.index_lock:
                server.index = server.index.set_pin(shelf, path, pinned)
        except KeyError:
            self.send_error(404, "unknown artifact")
            return
        except OSError:
            log.exception("cannot save the pin state file")
            self.send_error(500, "cannot save pin state")
            return
        self.send_response(204)
        self.end_headers()

    def _pin_request_allowed(self) -> bool:
        """Whether this looks like the UI's own JSON fetch, not a cross-site post.

        A page on another origin cannot send ``application/json`` without a
        CORS preflight, so requiring it keeps the endpoint out of CSRF reach;
        the ``Sec-Fetch-Site`` check (browsers only; absent for scripts)
        refuses what would slip through as a same-site request.
        """
        media_type = self.headers.get("Content-Type", "")
        if media_type.split(";", 1)[0].strip().lower() != "application/json":
            # The body is left unread, so this connection cannot be reused.
            self.close_connection = True
            self.send_error(415, "pin payload must be application/json")
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            self.close_connection = True
            self.send_error(403, "cross-site pin request refused")
            return False
        return True

    def _read_pin_payload(self) -> tuple[str, str, bool] | None:
        """The ``(shelf, path, pinned)`` triple in the request body, or None."""
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            self.close_connection = True
            return None
        if not 0 < length <= PIN_BODY_LIMIT:
            # Refusing without draining the body would desync keep-alive.
            self.close_connection = True
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        shelf = data.get("shelf")
        path = data.get("path")
        pinned = data.get("pinned")
        if not isinstance(shelf, str) or not shelf:
            return None
        if not isinstance(path, str) or not path:
            return None
        if not isinstance(pinned, bool):
            return None
        return shelf, path, pinned

    def _serve_artifact(
        self, path: str, *, include_body: bool, query: str = ""
    ) -> None:
        resolved = _resolve_artifact(self.index, path)
        if resolved is None:
            self.send_error(404, "not found")
            return
        auth = self.auth
        if (
            auth is not None
            and _is_document(resolved.target)
            and self._credential(path) == "session"
        ):
            # An unsigned document opened by a logged-in browser (a bookmark,
            # or a cached dashboard): send it to its signed URL so the
            # sandboxed document's cookie-less subresources inherit the
            # capability. Local and Bearer clients are not navigations and are
            # served as-is.
            self._redirect(
                _signed_artifact_url(
                    auth, resolved.shelf, resolved.relative, query=query
                ),
                status=302,
                no_store=True,
            )
            return
        # Sandboxed opaque origin + CORS-for-subresources: lesson JS runs, but
        # it carries no session and cannot read the API (ADR-0010).
        self._send_file(
            resolved.target,
            content_type_for(resolved.target),
            resolved.stat_result,
            include_body=include_body,
            sandbox=True,
            cors=True,
        )

    def _signed_index(self) -> Index:
        """The served index with capability-stamped ``/a/`` URLs.

        Without auth there is nothing to sign and the index passes through
        unchanged; with it, every render mints fresh stamps so dashboard links
        keep working once the page is cached or bookmarked (ADR-0010).
        """
        auth = self.auth
        if auth is None:
            return self.index
        return self.index.with_urls(
            lambda artifact: _signed_artifact_url(
                auth, artifact.shelf, artifact.path
            )
        )

    def _send_file(
        self,
        target: Path,
        content_type: str,
        stat_result: os.stat_result,
        *,
        include_body: bool,
        sandbox: bool = False,
        cors: bool = False,
    ) -> None:
        """Send one regular file with validators, streaming it when wanted."""
        etag = _etag(stat_result)
        last_modified = formatdate(stat_result.st_mtime, usegmt=True)
        if self._not_modified(etag, stat_result.st_mtime):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", "no-cache")
            if cors:
                self.send_header("Access-Control-Allow-Origin", CORS_ANY)
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
        if cors:
            self.send_header("Access-Control-Allow-Origin", CORS_ANY)
        if sandbox:
            self.send_header("Content-Security-Policy", ARTIFACT_SANDBOX)
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


@dataclass(frozen=True)
class _ResolvedArtifact:
    """An ``/a/`` request that maps to a regular file inside a shelf."""

    target: Path
    stat_result: os.stat_result
    #: The shelf the URL names, not the resolved target's own shelf: an alias
    #: must be signed under the path it was requested by.
    shelf: str
    #: The requested shelf-relative path (decoded), not the symlink target's.
    relative: str
    #: The leading capability segment, when the URL carried one.
    stamp: str | None


def _signed_artifact_url(
    auth: Auth, shelf: str, relative: str, *, query: str = ""
) -> str:
    """The capability-stamped URL for *shelf*'s *relative* path."""
    url = artifact_url(shelf, relative, stamp=auth.artifact_stamp(shelf))
    return f"{url}?{query}" if query else url


def _is_document(target: Path) -> bool:
    """Whether *target* is served as an HTML document with subresources."""
    return target.suffix.lower() in (".html", ".htm")


def _artifact_segments(path: str) -> list[str] | None:
    """The decoded ``/a/`` path segments, or ``None`` when undecodable.

    Decoding happens exactly once, here, and both the capability check and the
    resolution read the same segments — an encoded separator cannot make them
    disagree about which shelf a stamp was validated for.
    """
    if not path.startswith(ARTIFACT_PREFIX):
        return None
    try:
        decoded = unquote(path[len(ARTIFACT_PREFIX) :], errors="strict")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded:
        return None
    return decoded.split("/")


def _capability_segments(path: str) -> tuple[str, str] | None:
    """The ``(stamp, shelf)`` capability in an ``/a/`` path, if any.

    A capability is a first segment (``~<expiry>-<hmac>``) followed by the
    shelf it was minted for; everything after is a normal artifact path.
    """
    segments = _artifact_segments(path)
    if segments is None or len(segments) < 2 or not segments[0].startswith("~"):
        return None
    return segments[0], segments[1]


def _resolve_artifact(index: Index, path: str) -> _ResolvedArtifact | None:
    """Map an ``/a/`` URL to a regular file inside a shelf root, or ``None``.

    One leading ``~…`` capability segment is stripped before the shelf lookup;
    the shelf name and the *requested* relative path come back with the file so
    a redirect can mint a signed URL for the same address. ``~`` is reserved
    for capabilities: a shelf whose name starts with it is unreachable here.

    The raw suffix is decoded exactly once; every segment must be a plain name
    (no traversal, dotpaths or ``node_modules``), and the fully resolved path —
    symlinks included — must stay inside the shelf's real root. In-shelf
    symlinks are aliases, not loopholes: the resolved target's own segments must
    be plain too, so an alias cannot serve a hidden or vendored file.
    """
    segments = _artifact_segments(path)
    if segments is None:
        return None
    stamp: str | None = None
    if segments[0].startswith("~"):
        stamp = segments.pop(0)
    if len(segments) < 2:
        return None
    name = segments[0]
    relative = segments[1:]
    shelf = index.shelves.get(name)
    if shelf is None or any(not _plain_segment(part) for part in relative):
        return None
    try:
        resolved = shelf.root.joinpath(*relative).resolve(strict=True)
        stat_result = resolved.stat()
    except OSError:
        log.debug("artifact not served: %s", path, exc_info=True)
        return None
    if not resolved.is_relative_to(shelf.root) or not stat.S_ISREG(stat_result.st_mode):
        return None
    if any(not _plain_segment(part) for part in resolved.relative_to(shelf.root).parts):
        return None
    return _ResolvedArtifact(
        target=resolved,
        stat_result=stat_result,
        shelf=name,
        relative="/".join(relative),
        stamp=stamp,
    )


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
    index: Index,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    auth: Auth | None = None,
) -> LesviServer:
    """Bind a server for *index*; use port ``0`` for an ephemeral port.

    *auth* is the token policy; ``None`` serves without auth (a loopback bind
    or an explicit ``--insecure``).
    """
    return LesviServer((host, port), index, auth)
