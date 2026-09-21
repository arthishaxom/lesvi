"""Tests for ``lesvi.server``: raw artifacts and the JSON index over a socket."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import http.client
import json
import os
import re
import socket
import stat
import struct
import threading
import time
from collections.abc import Generator, Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin

import pytest

from lesvi.auth import ARTIFACT_URL_TTL, SESSION_TTL, Auth
from lesvi.config import Config, resolve_path
from lesvi.index import Index
from lesvi.server import ARTIFACT_SANDBOX, LOGIN_BODY_LIMIT, LesviServer, make_server
from lesvi.state import PinState

LESSON_PATH = "lessons/0024-apache-kafka-fundamentals.html"
LESSON_BYTES = (
    "<!doctype html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
    "<title>Lesson 24 — Apache Kafka Fundamentals</title>\n"
    "<link rel='stylesheet' href='../assets/lesson.css'>\n</head>\n"
    "<body>\n<p class='subtitle'>Topics and offsets.</p>\n"
    "<script src='../assets/quiz.js'></script>\n"
    "<script src='../assets/mermaid.min.js'></script>\n"
    "</body>\n</html>\n"
).encode()


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


@pytest.fixture
def shelf(tmp_path: Path) -> Path:
    """A miniature shelf in the real layout: assets, media, traps, symlinks."""
    root = tmp_path / "data-engg"
    _write(root, LESSON_PATH, LESSON_BYTES)
    _write(root, "assets/lesson.css", "body { margin: 0 }\n")
    _write(root, "assets/quiz.js", "void 'quiz';\n")
    _write(root, "assets/mermaid.min.js", "void 'mermaid';\n")
    _write(root, "assets/diagram.png", b"\x89PNG\r\n\x1a\nfake pixels")
    _write(root, "reference/kafka-cheatsheet.pdf", b"%PDF-1.4 fake\n")
    _write(root, "reference/kafka/deep.html", "<title>Deep Dive</title>")
    _write(root, "research/notes.md", "# notes")
    _write(root, "learning-records/export.html", "<title>Export</title>")
    _write(root, ".hidden/secret.html", "<title>Dot Secret</title>")
    _write(root, "lessons/.hidden.html", "<title>Dotfile Secret</title>")
    _write(root, "node_modules/pkg/readme.html", "<title>Vendor</title>")
    _write(root, "lessons/0027-café notes.html", "<title>Café Notes</title>")
    _write(root, f"{LESSON_PATH}.meta.json", '{"title": "Sidecar Override"}')
    os.mkfifo(root / "lessons" / "0008-pipe.html")  # opening it would block forever
    os.symlink(
        root / "reference" / "kafka" / "deep.html",
        root / "lessons" / "0025-deep-alias.html",
    )
    outside = _write(tmp_path, "outside.html", "<title>Outside Secret</title>")
    os.symlink(outside, root / "lessons" / "0026-escape.html")
    outside_dir = tmp_path / "outside-dir"
    _write(outside_dir, "secret.txt", "Outside Directory Secret")
    os.symlink(outside_dir, root / "linked-out")
    os.symlink("/etc/passwd", root / "lessons" / "0028-passwd.html")
    # Deterministic recency for the feed tests: café newest, then the deep dive
    # (also reachable through its alias), then 0024 as the oldest.
    for offset, relative in enumerate(
        (
            LESSON_PATH,
            "reference/kafka/deep.html",
            "lessons/0027-café notes.html",
        )
    ):
        stamp = 1_700_000_000 + offset * 100
        os.utime(root / relative, (stamp, stamp))
    return root


@pytest.fixture
def config(tmp_path: Path, shelf: Path) -> Config:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[shelves.data-engg]\npath = "{shelf}"\n'
        f'\n[shelves.ghost]\npath = "{tmp_path / "does-not-exist"}"\n'
    )
    return Config.load(config_file)


@pytest.fixture
def index(config: Config) -> Index:
    return Index.build(config)


@pytest.fixture(autouse=True)
def pin_state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test's pin store in tmp_path, never the real one."""
    path = tmp_path / "state.json"
    monkeypatch.setenv("LESVI_STATE", str(path))
    return path


@contextlib.contextmanager
def _running(server: LesviServer) -> Generator[tuple[str, int]]:
    """Serve *server* on a background thread for the duration of the block."""
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield str(server.server_address[0]), int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def server(index: Index) -> Iterator[LesviServer]:
    server = make_server(index, "127.0.0.1", 0)
    with _running(server):
        yield server


@pytest.fixture
def server_address(server: LesviServer) -> tuple[str, int]:
    return str(server.server_address[0]), int(server.server_address[1])


@pytest.fixture
def authed_server(index: Index) -> Iterator[LesviServer]:
    """A server with a token: auth is on, loopback stays exempt."""
    server = make_server(index, "127.0.0.1", 0, auth=Auth(TOKEN))
    with _running(server):
        yield server


@pytest.fixture
def authed_address(authed_server: LesviServer) -> tuple[str, int]:
    return str(authed_server.server_address[0]), int(authed_server.server_address[1])


def _get(
    server_address: tuple[str, int], path: str, **headers: str
) -> http.client.HTTPResponse:
    return _request(server_address, "GET", path, **headers)


def _request(
    server_address: tuple[str, int],
    method: str,
    path: str,
    *,
    body: bytes | str | None = None,
    **headers: str,
) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection(*server_address, timeout=5)
    connection.request(method, path, body=body, headers=headers)
    return connection.getresponse()


def _post(
    server_address: tuple[str, int], path: str, payload: object
) -> http.client.HTTPResponse:
    return _request(
        server_address,
        "POST",
        path,
        body=json.dumps(payload).encode(),
        **{"Content-Type": "application/json"},
    )


def _etag_and_modified(server_address: tuple[str, int]) -> tuple[str, str]:
    response = _get(server_address, f"/a/data-engg/{LESSON_PATH}")
    etag = response.getheader("ETag") or ""
    last_modified = response.getheader("Last-Modified") or ""
    response.read()
    return etag, last_modified


# --- the JSON index -----------------------------------------------------------


def test_api_index_serves_the_documented_shape_with_no_store(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/api/index.json")

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    payload = json.loads(response.read())
    assert set(payload) == {"shelves", "artifacts"}
    shelf = payload["shelves"]["data-engg"]
    assert shelf["title"] == "data-engg"
    assert shelf["categories"][0]["label"] == "Lessons"
    assert shelf["curriculum"] == [
        LESSON_PATH,
        "lessons/0025-deep-alias.html",
        "lessons/0027-café notes.html",
        "reference/kafka/deep.html",
        "research/notes.md",
    ]
    record = payload["artifacts"][0]
    assert record["url"] == "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html"
    assert record["number"] == 24
    assert record["title"] == "Sidecar Override"  # the sidecar wins
    assert record["description"] == "Topics and offsets."  # heuristics fill the rest
    assert record["meta_source"] == "sidecar"


def test_index_json_is_gzipped_when_the_client_accepts_it(
    server_address: tuple[str, int],
) -> None:
    plain = json.loads(_get(server_address, "/api/index.json").read())
    response = _get(server_address, "/api/index.json", **{"Accept-Encoding": "gzip"})

    assert response.status == 200
    assert response.getheader("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in (response.getheader("Vary") or "")
    assert json.loads(gzip.decompress(response.read())) == plain


def test_index_json_stays_plain_when_gzip_is_not_accepted(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/api/index.json")

    assert response.getheader("Content-Encoding") is None
    assert response.getheader("Vary") == "Accept-Encoding"
    assert json.loads(response.read())["artifacts"]


@pytest.mark.parametrize("header", ["gzip;q=0", "br, gzip;q=0.0", "*;q=0"])
def test_gzip_is_not_used_when_the_client_refuses_it(
    server_address: tuple[str, int], header: str
) -> None:
    response = _get(server_address, "/api/index.json", **{"Accept-Encoding": header})

    assert response.getheader("Content-Encoding") is None
    response.read()


def test_a_wildcard_accept_encoding_gets_gzip(server_address: tuple[str, int]) -> None:
    response = _get(server_address, "/api/index.json", **{"Accept-Encoding": "*"})

    assert response.getheader("Content-Encoding") == "gzip"
    response.read()


def test_unknown_api_paths_are_404(server_address: tuple[str, int]) -> None:
    response = _get(server_address, "/api/nope.json")

    assert response.status == 404
    response.read()


# --- the home feed and shelf pages --------------------------------------------


def _artifact_hrefs(body: str) -> list[str]:
    """Card links in document order; the page's own navigation is not /a/."""
    return re.findall(r'href="(/a/[^"]+)"', body)


def test_home_serves_the_cross_shelf_recency_feed(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/")
    body = response.read().decode()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    assert _artifact_hrefs(body) == [
        "/a/data-engg/lessons/0027-caf%C3%A9%20notes.html",
        "/a/data-engg/lessons/0025-deep-alias.html",
        "/a/data-engg/reference/kafka/deep.html",
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
    ]
    assert 'href="/s/data-engg/"' in body  # shelf navigation
    assert "research" not in body


def test_home_shows_fifty_cards_then_a_show_more_link(
    server_address: tuple[str, int], server: LesviServer, config: Config, shelf: Path
) -> None:
    for number in range(100, 160):
        _write(shelf, f"lessons/{number:04d}-bulk.html", "<p>bulk</p>")
    server.index = Index.build(config)

    first = _get(server_address, "/")
    first_body = first.read().decode()
    more = _get(server_address, "/?limit=100")
    more_body = more.read().decode()

    assert first.status == 200
    assert len(_artifact_hrefs(first_body)) == 50
    assert 'href="/?limit=64"' in first_body  # 4 carded + 60 bulk artifacts
    assert len(_artifact_hrefs(more_body)) == 64
    assert "Show more" not in more_body


def test_home_limit_is_clamped_and_garbage_falls_back_to_the_default(
    server_address: tuple[str, int],
) -> None:
    for query in ("limit=0", "limit=-5", "limit=nope"):
        response = _get(server_address, f"/?{query}")
        assert response.status == 200
        assert len(_artifact_hrefs(response.read().decode())) == 4, query


def test_home_show_more_stops_at_the_maximum_limit(
    server_address: tuple[str, int], server: LesviServer, config: Config, shelf: Path
) -> None:
    # 514 visible artifacts: past the 500 cap the link must stop offering a
    # page the server would clamp back to the same one.
    for number in range(100, 610):
        _write(shelf, f"lessons/{number:04d}-bulk.html", "<p>bulk</p>")
    server.index = Index.build(config)

    halfway = _get(server_address, "/?limit=400")
    halfway_body = halfway.read().decode()
    capped = _get(server_address, "/?limit=600")
    capped_body = capped.read().decode()

    assert len(_artifact_hrefs(halfway_body)) == 400
    assert 'href="/?limit=500"' in halfway_body
    assert len(_artifact_hrefs(capped_body)) == 500  # clamped request
    assert "Show more" not in capped_body


def test_shelf_page_serves_curriculum_order(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/s/data-engg/")
    body = response.read().decode()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    assert _artifact_hrefs(body) == [
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
        "/a/data-engg/lessons/0025-deep-alias.html",
        "/a/data-engg/lessons/0027-caf%C3%A9%20notes.html",
        "/a/data-engg/reference/kafka/deep.html",
    ]
    assert re.findall(r"<h2[^>]*>([^<]+)</h2>", body) == ["Lessons", "Reference"]
    assert 'href="/s/data-engg/?sort=recent"' in body
    assert "research" not in body


def test_shelf_page_recent_toggle_reorders_by_mtime(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/s/data-engg/?sort=recent")

    assert response.status == 200
    assert _artifact_hrefs(response.read().decode()) == [
        "/a/data-engg/lessons/0027-caf%C3%A9%20notes.html",
        "/a/data-engg/lessons/0025-deep-alias.html",
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
        "/a/data-engg/reference/kafka/deep.html",
    ]


def test_an_unknown_sort_falls_back_to_curriculum_order(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/s/data-engg/?sort=bogus")

    assert response.status == 200
    assert _artifact_hrefs(response.read().decode())[0] == (
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html"
    )


def test_shelf_url_without_a_trailing_slash_redirects(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/s/data-engg?sort=recent")

    assert response.status == 301
    assert response.getheader("Location") == "/s/data-engg/?sort=recent"
    assert response.read() == b""


def test_unknown_shelves_are_404(server_address: tuple[str, int]) -> None:
    for path in ("/s/nope/", "/s/", "/s", "/s/data-engg/extra/"):
        response = _get(server_address, path)
        assert response.status == 404, path
        response.read()


def test_head_mirrors_get_for_the_pages(server_address: tuple[str, int]) -> None:
    home = _request(server_address, "HEAD", "/")
    shelf = _request(server_address, "HEAD", "/s/data-engg/")
    asset = _request(server_address, "HEAD", "/assets/app.css")

    for response in (home, shelf):
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/html; charset=utf-8"
        assert int(response.getheader("Content-Length") or 0) > 0
        assert response.read() == b""
    assert asset.status == 200
    assert asset.getheader("Content-Type") == "text/css; charset=utf-8"
    assert asset.read() == b""


# --- UI assets and the theme contract -----------------------------------------


def test_ui_assets_serve_with_types_and_revalidate(
    server_address: tuple[str, int],
) -> None:
    css = _get(server_address, "/assets/app.css")
    js = _get(server_address, "/assets/app.js")

    assert css.status == 200
    assert css.getheader("Content-Type") == "text/css; charset=utf-8"
    assert js.status == 200
    assert js.getheader("Content-Type") == "text/javascript; charset=utf-8"
    assert css.read()
    assert js.read()

    etag = css.getheader("ETag") or ""
    conditional = _get(server_address, "/assets/app.css", **{"If-None-Match": etag})
    assert etag
    assert conditional.status == 304
    assert conditional.read() == b""


@pytest.mark.parametrize(
    "path",
    [
        "/assets/nope.css",
        "/assets/",
        "/assets/../config.toml",
        "/assets/app.css/extra",
        "/assets/App.css",
    ],
)
def test_only_the_known_ui_assets_are_served(
    server_address: tuple[str, int], path: str
) -> None:
    response = _get(server_address, path)

    assert response.status == 404, path
    response.read()


def test_pages_carry_the_ui_assets_and_the_theme_boot(
    server_address: tuple[str, int],
) -> None:
    body = _get(server_address, "/").read().decode()

    assert 'href="/assets/app.css"' in body
    assert 'src="/assets/app.js" defer' in body
    assert 'localStorage.getItem("lesvi-theme")' in body
    assert (
        '<meta name="viewport" content="width=device-width, initial-scale=1">' in body
    )
    assert '<main id="main"' in body
    assert 'class="skip-link"' in body


# --- the PWA surface (issue #11) ----------------------------------------------


def test_the_manifest_declares_an_installable_standalone_app(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/manifest.webmanifest")

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/manifest+json"
    payload = json.loads(response.read())
    assert payload["name"] == "lesvi"
    assert payload["short_name"] == "lesvi"
    assert payload["display"] == "standalone"
    assert payload["start_url"] == "/"
    assert payload["scope"] == "/"
    assert payload["background_color"].startswith("#")
    assert payload["theme_color"].startswith("#")
    sizes = {icon["sizes"] for icon in payload["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any("maskable" in icon.get("purpose", "") for icon in payload["icons"])


def test_the_service_worker_serves_from_the_root_and_revalidates(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, "/sw.js")

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/javascript; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-cache"
    assert response.read()


@pytest.mark.parametrize(
    ("path", "size"),
    [
        ("/assets/icon-192.png", 192),
        ("/assets/icon-512.png", 512),
        ("/assets/icon-maskable-512.png", 512),
    ],
)
def test_pwa_icons_are_pngs_of_the_declared_size(
    server_address: tuple[str, int], path: str, size: int
) -> None:
    response = _get(server_address, path)

    assert response.status == 200
    assert response.getheader("Content-Type") == "image/png"
    data = response.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (size, size)


def test_the_favicon_is_a_real_ico(server_address: tuple[str, int]) -> None:
    response = _get(server_address, "/favicon.ico")

    assert response.status == 200
    assert response.getheader("Content-Type") == "image/x-icon"
    reserved, kind, count = struct.unpack("<HHH", response.read()[:6])
    assert (reserved, kind, count) == (0, 1, 1)


def test_pages_link_the_manifest_icons_and_theme_colors(
    server_address: tuple[str, int],
) -> None:
    body = _get(server_address, "/").read().decode()

    assert '<link rel="manifest" href="/manifest.webmanifest">' in body
    assert '<link rel="icon" href="/assets/icon-192.png" type="image/png">' in body
    assert '<link rel="apple-touch-icon" href="/assets/icon-192.png">' in body
    assert 'name="theme-color"' in body


# --- the pin API (issue #7) ---------------------------------------------------


def test_post_pin_records_the_decision_and_returns_204(
    server_address: tuple[str, int], pin_state_file: Path
) -> None:
    response = _post(
        server_address,
        "/api/pin",
        {"shelf": "data-engg", "path": LESSON_PATH, "pinned": True},
    )

    assert response.status == 204
    assert response.read() == b""
    record = next(
        item
        for item in json.loads(_get(server_address, "/api/index.json").read())[
            "artifacts"
        ]
        if item["path"] == LESSON_PATH
    )
    assert record["pinned"] is True
    assert json.loads(pin_state_file.read_text())["pins"] == [
        f"data-engg/{LESSON_PATH}"
    ]


def test_pinning_puts_the_card_in_homes_pinned_section(
    server_address: tuple[str, int],
) -> None:
    _post(
        server_address,
        "/api/pin",
        {"shelf": "data-engg", "path": LESSON_PATH, "pinned": True},
    ).read()

    body = _get(server_address, "/").read().decode()

    assert "Pinned" in body
    # The pinned card leads the page and stays out of the recent feed.
    assert _artifact_hrefs(body)[0] == f"/a/data-engg/{LESSON_PATH}"
    assert _artifact_hrefs(body).count(f"/a/data-engg/{LESSON_PATH}") == 1


def test_a_pin_survives_a_server_restart(
    config: Config,
    server: LesviServer,
    server_address: tuple[str, int],
    pin_state_file: Path,
) -> None:
    _post(
        server_address,
        "/api/pin",
        {"shelf": "data-engg", "path": LESSON_PATH, "pinned": True},
    ).read()

    # A restart is a fresh index built from the same on-disk state file.
    server.index = Index.build(config, PinState.load(pin_state_file))
    body = _get(server_address, "/").read().decode()

    assert "Pinned" in body
    assert _artifact_hrefs(body)[0] == f"/a/data-engg/{LESSON_PATH}"


def test_an_explicit_unpin_beats_a_declared_seed_across_a_restart(
    shelf: Path, config: Config, server: LesviServer, server_address: tuple[str, int]
) -> None:
    _write(shelf, f"{LESSON_PATH}.meta.json", '{"pin": true}')
    server.index = Index.build(config)  # the sidecar seeds the pin
    assert (
        _artifact_hrefs(_get(server_address, "/").read().decode())[0]
        == f"/a/data-engg/{LESSON_PATH}"
    )

    response = _post(
        server_address,
        "/api/pin",
        {"shelf": "data-engg", "path": LESSON_PATH, "pinned": False},
    )
    restarted = Index.build(config, PinState.load())

    assert response.status == 204
    assert response.read() == b""
    assert restarted.by_number("data-engg")[0].pinned is False
    assert "Pinned" not in _get(server_address, "/").read().decode()


def test_a_watcher_publish_cannot_lose_a_pin(
    config: Config, server: LesviServer, server_address: tuple[str, int]
) -> None:
    _post(
        server_address,
        "/api/pin",
        {"shelf": "data-engg", "path": LESSON_PATH, "pinned": True},
    ).read()

    # The watcher publishes a snapshot it built before the pin landed.
    server.set_index(Index.build(config))

    payload = json.loads(_get(server_address, "/api/index.json").read())
    record = next(item for item in payload["artifacts"] if item["path"] == LESSON_PATH)
    assert record["pinned"] is True
    assert "Pinned" in _get(server_address, "/").read().decode()


def test_a_pin_request_must_be_json(
    server_address: tuple[str, int], pin_state_file: Path
) -> None:
    response = _request(
        server_address,
        "POST",
        "/api/pin",
        body=b'{"shelf":"data-engg","path":"x","pinned":true}',
        **{"Content-Type": "text/plain"},
    )

    assert response.status == 415
    response.read()
    assert not pin_state_file.exists()


def test_a_cross_site_pin_request_is_refused(
    server_address: tuple[str, int], pin_state_file: Path
) -> None:
    response = _request(
        server_address,
        "POST",
        "/api/pin",
        body=json.dumps(
            {"shelf": "data-engg", "path": LESSON_PATH, "pinned": True}
        ).encode(),
        **{
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status == 403
    response.read()
    assert not pin_state_file.exists()


def test_valid_pin_requests_keep_the_connection_alive(
    server_address: tuple[str, int],
) -> None:
    connection = http.client.HTTPConnection(*server_address, timeout=5)
    try:
        for pinned in (True, False):
            connection.request(
                "POST",
                "/api/pin",
                body=json.dumps(
                    {"shelf": "data-engg", "path": LESSON_PATH, "pinned": pinned}
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            assert response.status == 204
            assert response.read() == b""
    finally:
        connection.close()


def test_a_pin_for_an_unknown_artifact_is_a_404(
    server_address: tuple[str, int], pin_state_file: Path
) -> None:
    for shelf, path in (
        ("data-engg", "lessons/missing.html"),
        ("ghost", LESSON_PATH),
    ):
        response = _post(
            server_address, "/api/pin", {"shelf": shelf, "path": path, "pinned": True}
        )
        assert response.status == 404, (shelf, path)
        assert response.read()

    assert not pin_state_file.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"shelf": "data-engg", "path": LESSON_PATH},
        {"shelf": "data-engg", "path": LESSON_PATH, "pinned": "yes"},
        {"shelf": 5, "path": LESSON_PATH, "pinned": True},
        {"shelf": "", "path": "", "pinned": True},
        [1, 2, 3],
    ],
)
def test_a_malformed_pin_payload_is_a_400(
    server_address: tuple[str, int], pin_state_file: Path, payload: object
) -> None:
    response = _post(server_address, "/api/pin", payload)

    assert response.status == 400
    assert response.read()
    assert not pin_state_file.exists()


def test_an_oversized_or_empty_pin_body_is_a_400(
    server_address: tuple[str, int],
) -> None:
    oversized = _request(
        server_address,
        "POST",
        "/api/pin",
        body=b"x" * (256 * 1024),
        **{"Content-Type": "application/json; charset=utf-8"},
    )
    empty = _request(
        server_address,
        "POST",
        "/api/pin",
        body=b"",
        **{"Content-Type": "application/json"},
    )

    assert oversized.status == 400
    assert oversized.read()
    assert empty.status == 400
    assert empty.read()


def test_post_to_an_unknown_path_is_a_404(server_address: tuple[str, int]) -> None:
    response = _post(server_address, "/api/nope", {})

    assert response.status == 404
    assert response.read()


def test_toggling_a_pin_never_writes_into_the_shelf(
    shelf: Path, server_address: tuple[str, int]
) -> None:
    before = _sweep(shelf)

    for pinned in (True, False, True):
        response = _post(
            server_address,
            "/api/pin",
            {"shelf": "data-engg", "path": LESSON_PATH, "pinned": pinned},
        )
        assert response.status == 204
        response.read()

    assert _sweep(shelf) == before


# --- raw artifacts: bytes, assets and content types ---------------------------


def test_raw_artifact_is_served_byte_for_byte_as_html(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, f"/a/data-engg/{LESSON_PATH}")

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert response.getheader("Content-Length") == str(len(LESSON_BYTES))
    assert response.read() == LESSON_BYTES


def test_raw_artifacts_are_never_compressed(server_address: tuple[str, int]) -> None:
    response = _get(
        server_address, f"/a/data-engg/{LESSON_PATH}", **{"Accept-Encoding": "gzip"}
    )

    assert response.status == 200
    assert response.getheader("Content-Encoding") is None
    assert response.read() == LESSON_BYTES


def test_an_aborted_download_does_not_dump_a_traceback(
    server_address: tuple[str, int],
    shelf: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Large enough that the server is still writing when the client resets;
    # cancelled loads are routine behind a tunnel and must not spam stderr.
    _write(shelf, "assets/big.bin", b"x" * (16 * 1024 * 1024))

    with socket.create_connection(server_address, timeout=5) as connection:
        connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        connection.sendall(
            b"GET /a/data-engg/assets/big.bin HTTP/1.1\r\nHost: lesvi\r\n\r\n"
        )
    time.sleep(0.3)  # let the reset reach the handler thread

    captured = capfd.readouterr()
    assert "Traceback" not in captured.err
    assert "Exception occurred" not in captured.err


def test_relative_assets_resolve_above_the_lesson(
    server_address: tuple[str, int], shelf: Path
) -> None:
    # The lesson references ../assets/...; the URL has to serve those without
    # the asset being part of the index.
    for relative, expected_type in (
        ("assets/lesson.css", "text/css; charset=utf-8"),
        ("assets/quiz.js", "text/javascript; charset=utf-8"),
        ("assets/mermaid.min.js", "text/javascript; charset=utf-8"),
        ("assets/diagram.png", "image/png"),
        ("reference/kafka-cheatsheet.pdf", "application/pdf"),
    ):
        response = _get(server_address, f"/a/data-engg/{relative}")
        assert response.status == 200, relative
        assert response.getheader("Content-Type") == expected_type, relative
        assert response.read() == (shelf / relative).read_bytes(), relative


def test_the_lessons_own_hrefs_and_srcs_resolve_to_served_assets(
    server_address: tuple[str, int],
) -> None:
    # Resolve like a browser would, rather than restating the URL layout.
    lesson_url = f"/a/data-engg/{LESSON_PATH}"
    expected = {
        b"../assets/lesson.css": "text/css; charset=utf-8",
        b"../assets/quiz.js": "text/javascript; charset=utf-8",
        b"../assets/mermaid.min.js": "text/javascript; charset=utf-8",
    }
    references = re.findall(rb"(?:href|src)='([^']+)'", LESSON_BYTES)
    assert set(references) == set(expected)

    for reference in references:
        url = urljoin(lesson_url, reference.decode())
        response = _get(server_address, url)
        assert response.status == 200, url
        assert response.getheader("Content-Type") == expected[reference], url
        response.read()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sample.html", "text/html; charset=utf-8"),
        ("sample.css", "text/css; charset=utf-8"),
        ("sample.js", "text/javascript; charset=utf-8"),
        ("sample.mjs", "text/javascript; charset=utf-8"),
        ("sample.json", "application/json; charset=utf-8"),
        ("sample.txt", "text/plain; charset=utf-8"),
        ("sample.svg", "image/svg+xml"),
        ("sample.png", "image/png"),
        ("sample.jpg", "image/jpeg"),
        ("sample.jpeg", "image/jpeg"),
        ("sample.webp", "image/webp"),
        ("sample.avif", "image/avif"),
        ("sample.gif", "image/gif"),
        ("sample.pdf", "application/pdf"),
        ("sample.mp4", "video/mp4"),
        ("sample.webm", "video/webm"),
        ("sample.mp3", "audio/mpeg"),
        ("sample.wav", "audio/wav"),
        ("sample.bin", "application/octet-stream"),
        ("sample.md", "application/octet-stream"),
        ("sample.PNG", "image/png"),
    ],
)
def test_content_types_by_extension(
    server_address: tuple[str, int], shelf: Path, name: str, expected: str
) -> None:
    content = f"payload for {name}".encode()
    _write(shelf, f"assets/{name}", content)

    response = _get(server_address, f"/a/data-engg/assets/{name}")

    assert response.status == 200
    assert response.getheader("Content-Type") == expected
    assert response.read() == content


def test_percent_encoded_names_round_trip(server_address: tuple[str, int]) -> None:
    response = _get(server_address, "/a/data-engg/lessons/0027-caf%C3%A9%20notes.html")

    assert response.status == 200
    assert b"Caf\xc3\xa9 Notes" in response.read()


def test_mermaid_module_scripts_get_a_javascript_content_type(
    server_address: tuple[str, int], shelf: Path
) -> None:
    # mermaid ships .mjs; browsers refuse module scripts without a JS MIME type.
    _write(shelf, "assets/mermaid.esm.min.mjs", "export default {};\n")

    response = _get(server_address, "/a/data-engg/assets/mermaid.esm.min.mjs")

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/javascript; charset=utf-8"


def test_files_the_index_ignores_are_still_served_raw(
    server_address: tuple[str, int],
) -> None:
    # The ignore list decides what gets carded, not what may be fetched: assets
    # under ignored globs have to keep working, so visibility is not access control.
    for relative in ("learning-records/export.html", f"{LESSON_PATH}.meta.json"):
        response = _get(server_address, f"/a/data-engg/{relative}")
        assert response.status == 200, relative
        response.read()


def test_a_malformed_request_target_is_a_400_not_a_traceback(
    server_address: tuple[str, int],
) -> None:
    # The stdlib normalises origin-form targets like //[, but an absolute-form
    # target with a malformed authority reaches urlsplit() as-is.
    with socket.create_connection(server_address, timeout=5) as connection:
        connection.sendall(
            b"GET http://[foo/ HTTP/1.1\r\nHost: lesvi\r\nConnection: close\r\n\r\n"
        )
        response = connection.makefile("rb").read()

    assert response.startswith(b"HTTP/1.1 400")


def test_a_query_string_does_not_affect_serving(
    server_address: tuple[str, int],
) -> None:
    response = _get(server_address, f"/a/data-engg/{LESSON_PATH}?v=2")

    assert response.status == 200
    assert response.read() == LESSON_BYTES


# --- conditional requests -----------------------------------------------------


def test_etag_and_last_modified_are_sent(
    server_address: tuple[str, int], shelf: Path
) -> None:
    response = _get(server_address, f"/a/data-engg/{LESSON_PATH}")

    etag = response.getheader("ETag") or ""
    last_modified = response.getheader("Last-Modified") or ""
    assert etag.startswith('"') and etag.endswith('"')
    stamped = parsedate_to_datetime(last_modified)
    assert int(stamped.timestamp()) == int((shelf / LESSON_PATH).stat().st_mtime)
    response.read()


@pytest.mark.parametrize(
    "condition", ["etag", "weak-etag", "etag-list", "star", "date"]
)
def test_conditional_requests_return_304(
    server_address: tuple[str, int], condition: str
) -> None:
    etag, last_modified = _etag_and_modified(server_address)
    headers = {
        "etag": {"If-None-Match": etag},
        "weak-etag": {"If-None-Match": f"W/{etag}"},
        "etag-list": {"If-None-Match": f'"stale", {etag}'},
        "star": {"If-None-Match": "*"},
        "date": {"If-Modified-Since": last_modified},
    }[condition]

    response = _get(server_address, f"/a/data-engg/{LESSON_PATH}", **headers)

    assert response.status == 304
    assert response.getheader("ETag") == etag
    assert response.getheader("Last-Modified") == last_modified
    assert response.getheader("Content-Type") is None
    assert response.getheader("Content-Length") is None
    assert response.read() == b""


@pytest.mark.parametrize(
    "headers",
    [
        {"If-None-Match": '"stale"'},
        {"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"},
    ],
)
def test_stale_conditions_serve_the_file_again(
    server_address: tuple[str, int], headers: dict[str, str]
) -> None:
    response = _get(server_address, f"/a/data-engg/{LESSON_PATH}", **headers)

    assert response.status == 200
    assert response.read() == LESSON_BYTES


def test_if_none_match_takes_precedence_over_if_modified_since(
    server_address: tuple[str, int],
) -> None:
    _etag, last_modified = _etag_and_modified(server_address)

    response = _get(
        server_address,
        f"/a/data-engg/{LESSON_PATH}",
        **{"If-None-Match": '"stale"', "If-Modified-Since": last_modified},
    )

    assert response.status == 200
    response.read()


def test_republishing_invalidates_the_etag(
    server_address: tuple[str, int], shelf: Path
) -> None:
    url = f"/a/data-engg/{LESSON_PATH}"
    response = _get(server_address, url)
    old_etag = response.getheader("ETag") or ""
    response.read()
    path = shelf / LESSON_PATH
    before = path.stat()
    replacement = LESSON_BYTES.replace(
        b"Topics and offsets.", b"Republished with new offsets."
    )
    path.write_bytes(replacement)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

    response = _get(server_address, url, **{"If-None-Match": old_etag})

    assert response.status == 200
    assert response.getheader("ETag") != old_etag
    assert response.read() == replacement


def test_head_sends_the_headers_without_a_body(
    server_address: tuple[str, int],
) -> None:
    response = _request(server_address, "HEAD", f"/a/data-engg/{LESSON_PATH}")

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert response.getheader("Content-Length") == str(len(LESSON_BYTES))
    assert response.getheader("ETag")
    assert response.read() == b""


def test_head_mirrors_get_for_conditional_and_missing_paths(
    server_address: tuple[str, int],
) -> None:
    etag, _last_modified = _etag_and_modified(server_address)

    conditional = _request(
        server_address,
        "HEAD",
        f"/a/data-engg/{LESSON_PATH}",
        **{"If-None-Match": etag},
    )
    missing = _request(server_address, "HEAD", "/a/data-engg/missing.html")

    assert conditional.status == 304
    assert conditional.read() == b""
    assert missing.status == 404


# --- path safety --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/a/data-engg/../outside.html",
        "/a/data-engg/lessons/../../outside.html",
        "/a/data-engg/lessons/../0024-apache-kafka-fundamentals.html",
        "/a/data-engg/%2e%2e/outside.html",
        "/a/data-engg/lessons/%2E%2E/%2E%2E/outside.html",
        "/a/data-engg/lessons/%2e%2e%2F%2e%2e%2Foutside.html",
        "/a/data-engg/lessons%2F..%2F..%2Foutside.html",
        "/a/%2e%2e/outside.html",
        "/a/data-engg//etc/passwd",
        "/a/data-engg/%2Fetc%2Fpasswd",
        "/a/data-engg/lessons/%00.html",
        "/a/data-engg/lessons/%FF.html",
        "/a/data-engg/%252e%252e/outside.html",
        "/a/data-engg/.hidden/secret.html",
        "/a/data-engg/lessons/.hidden.html",
        "/a/data-engg/.hidden/../lessons/0024-apache-kafka-fundamentals.html",
        "/a/data-engg/node_modules/pkg/readme.html",
        "/a/data-engg/node_modules/../../outside.html",
        "/a/data-engg/lessons/0026-escape.html",
        "/a/data-engg/lessons/0028-passwd.html",
        "/a/data-engg/linked-out/secret.txt",
        "/a/data-engg/lessons/0008-pipe.html",
        "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html/",
        "/a/data-engg",
        "/a/data-engg/",
        "/a/data-engg/lessons",
        "/a/data-engg/lessons/",
        "/a/ghost/anything.html",
        "/a/nope/lessons/0024-apache-kafka-fundamentals.html",
        "/a/",
        "/a",
        "/a/data-engg/missing.html",
    ],
)
def test_unsafe_and_missing_paths_are_404(
    server_address: tuple[str, int], path: str
) -> None:
    response = _get(server_address, path)
    body = response.read()

    assert response.status == 404, path
    assert b"Outside Secret" not in body, path
    assert b"Dot Secret" not in body, path
    assert b"Outside Directory Secret" not in body, path
    assert b"root:" not in body, path  # a symlink to /etc/passwd must never leak


def test_directories_are_never_listed(server_address: tuple[str, int]) -> None:
    for path in ("/a/data-engg/lessons", "/a/data-engg/lessons/"):
        response = _get(server_address, path)
        assert response.status == 404
        assert b"0024" not in response.read()


def test_symlinks_that_stay_inside_the_shelf_are_served(
    server_address: tuple[str, int], shelf: Path
) -> None:
    response = _get(server_address, "/a/data-engg/lessons/0025-deep-alias.html")

    assert response.status == 200
    assert response.read() == (shelf / "reference/kafka/deep.html").read_bytes()


def test_symlinks_cannot_launder_hidden_vendored_or_looping_paths(
    server_address: tuple[str, int], shelf: Path
) -> None:
    aliases = (
        (
            shelf / ".hidden" / "secret.html",
            shelf / "lessons" / "0031-hidden-alias.html",
        ),
        (
            shelf / "node_modules" / "pkg" / "readme.html",
            shelf / "lessons" / "0032-vendor-alias.html",
        ),
        (shelf / "lessons" / "0033-loop.html", shelf / "lessons" / "0033-loop.html"),
    )
    for target, alias in aliases:
        os.symlink(target, alias)

    for alias in (alias for _target, alias in aliases):
        response = _get(server_address, f"/a/data-engg/{alias.relative_to(shelf)}")
        body = response.read()
        assert response.status == 404, alias.name
        assert b"Dot Secret" not in body, alias.name
        assert b"Vendor" not in body, alias.name


# --- shelves stay untouched ---------------------------------------------------


def _sweep(root: Path) -> dict[str, tuple[int, int, str]]:
    """mtime + size + digest for every file under *root*, symlinks included."""
    state: dict[str, tuple[int, int, str]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            stat_result = path.lstat()
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                state[relative] = (stat_result.st_mtime_ns, 0, os.readlink(path))
            elif not stat.S_ISREG(stat_result.st_mode):
                state[relative] = (stat_result.st_mtime_ns, 0, "special")
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state[relative] = (stat_result.st_mtime_ns, stat_result.st_size, digest)
    return state


def test_building_the_index_and_crawling_every_artifact_leaves_the_shelf_untouched(
    config: Config, server_address: tuple[str, int]
) -> None:
    roots = [
        resolve_path(str(table.get("path", "")), config.path.parent)
        for table in config.shelves().values()
    ]
    before = {root: _sweep(root) for root in roots}  # baseline: raw fixture files

    index = Index.build(config)  # scanning must not write either
    urls = [artifact.url for artifact in index.artifacts]
    assert urls
    for url in urls:
        response = _get(server_address, url)
        assert response.status == 200, url
        response.read()
    for extra in (
        "/a/data-engg/assets/lesson.css",
        "/a/data-engg/assets/diagram.png",
        "/a/data-engg/reference/kafka-cheatsheet.pdf",
        "/a/data-engg/missing.html",
        "/a/data-engg/../outside.html",
    ):
        _get(server_address, extra).read()

    assert {root: _sweep(root) for root in roots} == before


# --- auth (issue #9) ----------------------------------------------------------

TOKEN = "s3cret-token"
#: What cloudflared adds to every proxied request; it must force a login.
TUNNELED = {"CF-Connecting-IP": "203.0.113.9"}
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def _login(
    server_address: tuple[str, int], token: str, **headers: str
) -> http.client.HTTPResponse:
    return _request(
        server_address,
        "POST",
        "/login",
        body=urlencode({"token": token}),
        **{"Content-Type": "application/x-www-form-urlencoded", **headers},
    )


def _session_cookie(server_address: tuple[str, int], **headers: str) -> str:
    """Log in and return the ``name=value`` pair from ``Set-Cookie``."""
    response = _login(server_address, TOKEN, **headers)
    assert response.status == 303
    return (response.getheader("Set-Cookie") or "").split(";", 1)[0]


def test_a_direct_loopback_request_needs_no_login(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, "/")

    assert response.status == 200
    response.read()


@pytest.mark.parametrize(
    "forwarding",
    [
        {"CF-Connecting-IP": "203.0.113.9"},
        {"X-Forwarded-For": "203.0.113.9"},
        {"cf-connecting-ip": "203.0.113.9"},  # header names are case-insensitive
        {"x-forwarded-for": "203.0.113.9"},
    ],
)
def test_cloudflare_forwarding_headers_force_the_login_page(
    authed_address: tuple[str, int], forwarding: dict[str, str]
) -> None:
    for path in ("/", "/s/data-engg/", f"/a/data-engg/{LESSON_PATH}"):
        response = _get(authed_address, path, **forwarding)

        assert response.status == 303, path
        assert response.getheader("Location") == "/login", path
        assert response.read() == b""


def test_an_unauthenticated_api_request_is_401_not_a_redirect(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, "/api/index.json", **TUNNELED)

    assert response.status == 401
    assert response.getheader("Location") is None
    assert response.getheader("Cache-Control") == "no-store"
    assert json.loads(response.read()) == {"error": "unauthorized"}


def test_a_bearer_token_authorizes_scripts(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, "/api/index.json", **TUNNELED, **BEARER)

    assert response.status == 200
    assert json.loads(response.read())["shelves"]


def test_a_wrong_bearer_token_is_refused(
    authed_address: tuple[str, int],
) -> None:
    response = _get(
        authed_address,
        "/api/index.json",
        **TUNNELED,
        Authorization="Bearer not-the-token",
    )

    assert response.status == 401
    assert response.getheader("Location") is None
    assert json.loads(response.read()) == {"error": "unauthorized"}


def test_login_sets_a_signed_session_cookie_that_authorizes(
    authed_address: tuple[str, int],
) -> None:
    response = _login(authed_address, TOKEN, **TUNNELED)

    assert response.status == 303
    assert response.getheader("Location") == "/"
    cookie = response.getheader("Set-Cookie") or ""
    assert cookie.startswith("lesvi_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert f"Max-Age={SESSION_TTL}" in cookie
    assert "Secure" not in cookie
    assert response.getheader("Cache-Control") == "no-store"
    assert response.read() == b""

    pair = cookie.split(";", 1)[0]
    home = _get(authed_address, "/", **TUNNELED, Cookie=pair)
    api = _get(authed_address, "/api/index.json", **TUNNELED, Cookie=pair)

    assert home.status == 200
    home.read()
    assert api.status == 200
    assert json.loads(api.read())["shelves"]


def test_a_tampered_session_cookie_is_refused(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)
    name, _, value = pair.partition("=")
    tampered = f"{name}={value[:-1]}{'0' if value[-1] != '0' else '1'}"

    response = _get(authed_address, "/", **TUNNELED, Cookie=tampered)

    assert response.status == 303
    assert response.getheader("Location") == "/login"
    response.read()


def test_a_non_ascii_session_cookie_is_refused_without_a_crash(
    authed_address: tuple[str, int],
) -> None:
    """Malformed cookies must not raise inside ``hmac.compare_digest``."""
    response = _get(
        authed_address,
        "/",
        **TUNNELED,
        Cookie="lesvi_session=9999999999.é",
    )

    assert response.status == 303
    assert response.getheader("Location") == "/login"
    response.read()


def test_login_with_a_wrong_token_rerenders_the_form_without_a_cookie(
    authed_address: tuple[str, int],
) -> None:
    response = _login(authed_address, "wrong-token", **TUNNELED)

    assert response.status == 401
    assert response.getheader("Set-Cookie") is None
    body = response.read().decode()
    assert 'action="/login"' in body
    assert 'name="token"' in body
    assert "not correct" in body


def test_login_behind_https_marks_the_cookie_secure(
    authed_address: tuple[str, int],
) -> None:
    response = _login(
        authed_address, TOKEN, **TUNNELED, **{"x-forwarded-proto": "https"}
    )

    assert f"Max-Age={SESSION_TTL}" in (response.getheader("Set-Cookie") or "")
    assert "Secure" in (response.getheader("Set-Cookie") or "")
    response.read()


@pytest.mark.parametrize("content_length", ["abc", "0", str(LOGIN_BODY_LIMIT + 1)])
def test_a_malformed_login_body_is_rejected_and_closes(
    authed_address: tuple[str, int], content_length: str
) -> None:
    response = _request(
        authed_address,
        "POST",
        "/login",
        body=b"",
        **{
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": content_length,
            **TUNNELED,
        },
    )

    assert response.status == 400
    assert response.getheader("Connection") == "close"
    response.read()


def test_logout_clears_the_session_cookie(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, "/logout", **TUNNELED)

    assert response.status == 303
    assert response.getheader("Location") == "/login"
    cookie = response.getheader("Set-Cookie") or ""
    assert cookie.startswith("lesvi_session=;")
    assert "Max-Age=0" in cookie
    assert response.getheader("Cache-Control") == "no-store"
    assert response.read() == b""


def test_the_login_form_reads_without_scripts(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, "/login", **TUNNELED)
    body = response.read().decode()

    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    assert 'method="post" action="/login"' in body
    assert 'id="token"' in body
    assert "/assets/app.js" not in body


def test_login_without_a_configured_token_goes_straight_home(
    server_address: tuple[str, int],
) -> None:
    page = _get(server_address, "/login")
    submission = _login(server_address, "anything")

    assert page.status == 303
    assert page.getheader("Location") == "/"
    assert submission.status == 303
    assert submission.getheader("Location") == "/"
    assert submission.getheader("Set-Cookie") is None
    assert submission.getheader("Connection") == "close"


def test_exempt_paths_skip_the_login_redirect(
    authed_address: tuple[str, int],
) -> None:
    for path in (
        "/login",
        "/healthz",
        "/assets/app.css",
        "/manifest.webmanifest",
        "/sw.js",
        "/favicon.ico",
    ):
        response = _get(authed_address, path, **TUNNELED)
        assert response.status == 200, path
        assert response.getheader("Location") is None, path
        response.read()


def test_healthz_reports_counts_without_a_login(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, "/healthz", **TUNNELED)

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    payload = json.loads(response.read())
    assert payload == {"ok": True, "shelves": 2, "artifacts": 5}


def test_the_pin_api_refuses_an_unauthenticated_request(
    authed_address: tuple[str, int], pin_state_file: Path
) -> None:
    response = _request(
        authed_address,
        "POST",
        "/api/pin",
        body=json.dumps(
            {"shelf": "data-engg", "path": LESSON_PATH, "pinned": True}
        ).encode(),
        **{"Content-Type": "application/json", **TUNNELED},
    )

    assert response.status == 401
    assert response.read()
    assert not pin_state_file.exists()


def test_a_session_cookie_authorizes_the_pin_api(
    authed_address: tuple[str, int], pin_state_file: Path
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)

    response = _request(
        authed_address,
        "POST",
        "/api/pin",
        body=json.dumps(
            {"shelf": "data-engg", "path": LESSON_PATH, "pinned": True}
        ).encode(),
        **{
            "Content-Type": "application/json",
            "Cookie": pair,
            **TUNNELED,
        },
    )

    assert response.status == 204
    assert response.read() == b""
    assert json.loads(pin_state_file.read_text())["pins"] == [
        f"data-engg/{LESSON_PATH}"
    ]


def test_allow_localhost_false_requires_a_login_even_on_loopback(
    index: Index,
) -> None:
    server = make_server(index, "127.0.0.1", 0, auth=Auth(TOKEN, allow_localhost=False))
    with _running(server) as address:
        response = _get(address, "/")

    assert response.status == 303
    assert response.getheader("Location") == "/login"
    response.read()


# --- raw artifacts: sandbox and signed links (ADR-0010) -----------------------


def _signed_url(
    relative: str, shelf: str = "data-engg", *, now: float | None = None
) -> str:
    """An ``/a/`` URL carrying a fresh capability for *shelf*."""
    stamp = Auth(TOKEN).artifact_stamp(shelf, now=now)
    return f"/a/{stamp}/{shelf}/{relative}"


def _signed_hrefs(body: str) -> list[str]:
    return [href for href in _artifact_hrefs(body) if href.startswith("/a/~")]


def test_artifacts_are_sandboxed_and_readable_cross_origin(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, _signed_url(LESSON_PATH), **TUNNELED)

    assert response.status == 200
    assert response.getheader("Content-Security-Policy") == ARTIFACT_SANDBOX
    assert response.getheader("Access-Control-Allow-Origin") == "*"
    assert response.read() == LESSON_BYTES


def test_artifact_subresources_and_conditional_responses_keep_cors(
    authed_address: tuple[str, int],
) -> None:
    url = _signed_url("assets/quiz.js")
    asset = _get(authed_address, url, **TUNNELED)
    assert asset.getheader("Access-Control-Allow-Origin") == "*"
    assert asset.getheader("Content-Security-Policy") == ARTIFACT_SANDBOX
    asset.read()

    etag = asset.getheader("ETag") or ""
    conditional = _get(
        authed_address, url, **TUNNELED, **{"If-None-Match": etag}
    )
    assert conditional.status == 304
    assert conditional.getheader("Access-Control-Allow-Origin") == "*"
    conditional.read()


def test_the_authenticated_api_has_no_cors_for_sandboxed_artifacts(
    authed_address: tuple[str, int],
) -> None:
    for path in ("/", "/s/data-engg/", "/api/index.json"):
        response = _get(authed_address, path, **TUNNELED, **BEARER)
        assert response.getheader("Access-Control-Allow-Origin") is None, path
        response.read()


def test_dashboard_pages_link_to_signed_artifacts(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)

    home = _get(authed_address, "/", **TUNNELED, Cookie=pair)
    home_body = home.read().decode()
    shelf = _get(authed_address, "/s/data-engg/", **TUNNELED, Cookie=pair)
    shelf_body = shelf.read().decode()
    recent = _get(
        authed_address, "/s/data-engg/?sort=recent", **TUNNELED, Cookie=pair
    )
    recent_body = recent.read().decode()

    assert home.status == 200
    assert shelf.status == 200
    assert recent.status == 200
    # Every card link is signed; none of them are unsigned.
    assert len(_signed_hrefs(home_body)) == 4
    assert len(_signed_hrefs(shelf_body)) == 4
    assert len(_signed_hrefs(recent_body)) == 4
    assert len(_artifact_hrefs(home_body)) == len(_signed_hrefs(home_body))
    assert len(_artifact_hrefs(shelf_body)) == len(_signed_hrefs(shelf_body))
    assert len(_artifact_hrefs(recent_body)) == len(_signed_hrefs(recent_body))
    # A sandboxed document fetches these with no cookie; the stamp must carry.
    for href in _signed_hrefs(home_body):
        document = _get(authed_address, href, **TUNNELED)
        assert document.status == 200, href
        document.read()


def test_api_index_links_to_signed_artifacts(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)

    response = _get(authed_address, "/api/index.json", **TUNNELED, Cookie=pair)
    payload = json.loads(response.read())

    urls = [record["url"] for record in payload["artifacts"]]
    assert urls
    assert all(url.startswith("/a/~") for url in urls)
    for url in urls:
        artifact = _get(authed_address, url, **TUNNELED)
        assert artifact.status == 200, url
        artifact.read()


def test_a_signed_subresource_is_served_without_any_cookie(
    authed_address: tuple[str, int],
) -> None:
    """The regression this design exists for: sandboxed fetches send no cookie."""
    response = _get(authed_address, _signed_url("assets/quiz.js"), **TUNNELED)

    assert response.status == 200
    assert response.getheader("Access-Control-Allow-Origin") == "*"
    assert response.read() == b"void 'quiz';\n"


def test_an_unsigned_subresource_without_a_cookie_is_refused(
    authed_address: tuple[str, int],
) -> None:
    response = _get(authed_address, "/a/data-engg/assets/quiz.js", **TUNNELED)

    assert response.status == 303
    assert response.getheader("Location") == "/login"
    assert response.read() == b""


def test_an_unsigned_document_with_a_session_redirects_to_its_signed_url(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)

    response = _get(
        authed_address, f"/a/data-engg/{LESSON_PATH}", **TUNNELED, Cookie=pair
    )

    assert response.status == 302
    location = response.getheader("Location") or ""
    assert response.getheader("Cache-Control") == "no-store"
    stamp, _, remainder = location.removeprefix("/a/").partition("/")
    assert Auth(TOKEN).artifact_stamp_valid(stamp, "data-engg")
    assert remainder == f"data-engg/{LESSON_PATH}"
    response.read()

    document = _get(authed_address, location, **TUNNELED)  # no cookie
    assert document.status == 200
    assert document.getheader("Content-Security-Policy") == ARTIFACT_SANDBOX
    assert document.getheader("Access-Control-Allow-Origin") == "*"
    assert document.read() == LESSON_BYTES


def test_the_signed_document_keeps_relative_subresources_reachable(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)
    redirect = _get(
        authed_address, f"/a/data-engg/{LESSON_PATH}", **TUNNELED, Cookie=pair
    )
    location = redirect.getheader("Location") or ""
    redirect.read()

    # ``urljoin`` is how the browser resolves the lesson's ``../assets/...``.
    quiz = _get(authed_address, urljoin(location, "../assets/quiz.js"), **TUNNELED)
    css = _get(authed_address, urljoin(location, "../assets/lesson.css"), **TUNNELED)
    png = _get(authed_address, urljoin(location, "../assets/diagram.png"), **TUNNELED)

    assert quiz.status == 200
    assert quiz.read() == b"void 'quiz';\n"
    assert css.status == 200
    css.read()
    assert png.status == 200
    png.read()


def test_local_and_bearer_requests_keep_the_unsigned_document(
    authed_address: tuple[str, int],
) -> None:
    local = _get(authed_address, f"/a/data-engg/{LESSON_PATH}")
    bearer = _get(authed_address, f"/a/data-engg/{LESSON_PATH}", **TUNNELED, **BEARER)

    assert local.status == 200
    assert local.read() == LESSON_BYTES
    assert bearer.status == 200
    assert bearer.read() == LESSON_BYTES


def test_a_non_document_with_a_session_is_served_unsigned(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)

    response = _get(
        authed_address, "/a/data-engg/assets/diagram.png", **TUNNELED, Cookie=pair
    )

    assert response.status == 200
    assert response.getheader("Location") is None
    response.read()


def test_an_unsigned_document_redirect_keeps_its_query(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)

    response = _get(
        authed_address,
        f"/a/data-engg/{LESSON_PATH}?mode=quiz",
        **TUNNELED,
        Cookie=pair,
    )

    assert response.status == 302
    assert (response.getheader("Location") or "").endswith("?mode=quiz")
    response.read()


def test_an_expired_stamp_is_not_a_credential(
    authed_address: tuple[str, int],
) -> None:
    stale = _signed_url("assets/quiz.js", now=time.time() - ARTIFACT_URL_TTL - 10)

    response = _get(authed_address, stale, **TUNNELED)

    assert response.status == 303
    assert response.getheader("Location") == "/login"
    response.read()


def test_a_tampered_stamp_is_not_a_credential(
    authed_address: tuple[str, int],
) -> None:
    stamp = Auth(TOKEN).artifact_stamp("data-engg")
    tampered = f"{stamp[:-1]}{'0' if stamp[-1] != '0' else '1'}"

    response = _get(
        authed_address, f"/a/{tampered}/data-engg/assets/quiz.js", **TUNNELED
    )

    assert response.status == 303
    response.read()


def test_a_stamp_for_another_shelf_does_not_authorize(
    authed_address: tuple[str, int],
) -> None:
    stamp = Auth(TOKEN).artifact_stamp("ricing")

    response = _get(
        authed_address, f"/a/{stamp}/data-engg/assets/quiz.js", **TUNNELED
    )

    assert response.status == 303
    response.read()


def test_an_expired_stamp_with_a_session_redirects_to_a_fresh_one(
    authed_address: tuple[str, int],
) -> None:
    pair = _session_cookie(authed_address, **TUNNELED)
    stale = _signed_url(LESSON_PATH, now=time.time() - ARTIFACT_URL_TTL - 10)

    response = _get(authed_address, stale, **TUNNELED, Cookie=pair)

    assert response.status == 302
    location = response.getheader("Location") or ""
    assert location != stale
    stamp, _, _ = location.removeprefix("/a/").partition("/")
    assert Auth(TOKEN).artifact_stamp_valid(stamp, "data-engg")
    response.read()


def test_a_signed_request_cannot_smuggle_paths_out_of_the_shelf(
    authed_address: tuple[str, int],
) -> None:
    stamp = Auth(TOKEN).artifact_stamp("data-engg")

    for relative in (
        "../outside.html",
        "%2e%2e%2foutside.html",
        ".hidden/secret.html",
        "node_modules/pkg/readme.html",
        "lessons/0026-escape.html",  # a symlink out of the shelf
        "lessons/0028-passwd.html",  # a symlink to /etc/passwd
    ):
        response = _get(authed_address, f"/a/{stamp}/data-engg/{relative}", **TUNNELED)
        assert response.status == 404, relative
        assert b"Outside Secret" not in response.read(), relative


# --- auth wire behaviour (issue #9 review) ------------------------------------


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/index.json", 401),
        ("/api/index.json?q=1", 401),
        ("/API/index.json", 303),
        # http.server collapses a leading ``//`` to ``/``, so this reaches the
        # API branch and gets the JSON refusal.
        ("//api/index.json", 401),
        ("/api%2Findex.json", 303),
        ("/Assets/app.css", 303),
        ("/a/data-engg/assets/quiz.js", 303),
    ],
)
def test_unauthenticated_wire_forms_are_refused(
    authed_address: tuple[str, int], path: str, status: int
) -> None:
    response = _get(authed_address, path, **TUNNELED)

    assert response.status == status
    assert response.getheader("Access-Control-Allow-Origin") is None
    response.read()


def test_an_absolute_form_target_is_authorized_like_its_path(
    authed_address: tuple[str, int],
) -> None:
    refused = _get(
        authed_address, "http://lesvi.example.com/api/index.json", **TUNNELED
    )
    allowed = _get(
        authed_address,
        "http://lesvi.example.com/api/index.json",
        **TUNNELED,
        **BEARER,
    )

    assert refused.status == 401
    refused.read()
    assert allowed.status == 200
    assert json.loads(allowed.read())["shelves"]


def test_a_head_refusal_has_headers_but_no_body(
    authed_address: tuple[str, int],
) -> None:
    response = _request(authed_address, "HEAD", "/api/index.json", **TUNNELED)

    assert response.status == 401
    assert response.getheader("Content-Length") == str(
        len(b'{"error":"unauthorized"}\n')
    )
    assert response.read() == b""


def test_a_refused_post_announces_that_the_connection_closes(
    authed_address: tuple[str, int],
) -> None:
    response = _request(
        authed_address,
        "POST",
        "/api/pin",
        body=b"{}",
        **{"Content-Type": "application/json", **TUNNELED},
    )

    assert response.status == 401
    assert response.getheader("Connection") == "close"
    response.read()


def test_a_refused_get_with_a_declared_body_also_closes(
    authed_address: tuple[str, int],
) -> None:
    response = _request(authed_address, "GET", "/", body=b"hello", **TUNNELED)

    assert response.status == 303
    assert response.getheader("Connection") == "close"
    response.read()
