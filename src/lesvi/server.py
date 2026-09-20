"""HTTP surface: the single-port server and the ``/api/index.json`` route.

Only the JSON index route exists so far; raw artifacts, dashboards and auth land
with their own tickets.
"""

from __future__ import annotations

import gzip
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import urlsplit

from lesvi import __version__
from lesvi.config import DEFAULT_HOST, DEFAULT_PORT
from lesvi.index import Index

log = logging.getLogger(__name__)


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

    @property
    def index(self) -> Index:
        return cast(LesviServer, self.server).index

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/index.json":
            self._send_index()
        else:
            self.send_error(404, "not found")

    def log_message(self, format: str, *args: object) -> None:
        log.info("%s - %s", self.address_string(), format % args)

    def _send_index(self) -> None:
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
        self.wfile.write(body)


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
