"""Tests for ``lesvi.server``: raw artifacts and the JSON index over a socket."""

from __future__ import annotations

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
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import pytest

from lesvi.config import Config, resolve_path
from lesvi.index import Index
from lesvi.server import LesviServer, make_server

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


@pytest.fixture
def server(index: Index) -> Iterator[LesviServer]:
    server = make_server(index, "127.0.0.1", 0)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def server_address(server: LesviServer) -> tuple[str, int]:
    return str(server.server_address[0]), int(server.server_address[1])


def _get(
    server_address: tuple[str, int], path: str, **headers: str
) -> http.client.HTTPResponse:
    return _request(server_address, "GET", path, **headers)


def _request(
    server_address: tuple[str, int], method: str, path: str, **headers: str
) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection(*server_address, timeout=5)
    connection.request(method, path, headers=headers)
    return connection.getresponse()


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
    assert record["title"] == "Apache Kafka Fundamentals"


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
