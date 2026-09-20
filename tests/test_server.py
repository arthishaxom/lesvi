"""Tests for ``lesvi.server``: the JSON index route over a real HTTP socket."""

from __future__ import annotations

import gzip
import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from lesvi.config import Config
from lesvi.index import Index
from lesvi.server import make_server


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def index(tmp_path: Path) -> Index:
    shelf = tmp_path / "data-engg"
    _write(
        shelf,
        "lessons/0024-apache-kafka-fundamentals.html",
        "<title>Lesson 24 — Apache Kafka Fundamentals</title><p>Topics and offsets.</p>",
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[shelves.data-engg]\npath = "{shelf}"\n')
    return Index.build(Config.load(config_file))


@pytest.fixture
def server_address(index: Index) -> Iterator[tuple[str, int]]:
    server = make_server(index, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = str(server.server_address[0]), int(server.server_address[1])
    yield host, port
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(
    server_address: tuple[str, int], path: str, **headers: str
) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection(*server_address, timeout=5)
    connection.request("GET", path, headers=headers)
    return connection.getresponse()


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
    assert shelf["curriculum"] == ["lessons/0024-apache-kafka-fundamentals.html"]
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


def test_unknown_paths_are_404_for_now(server_address: tuple[str, int]) -> None:
    # Home and shelf pages arrive with their own ticket; only the API exists yet.
    for path in ("/", "/api/nope.json"):
        response = _get(server_address, path)
        assert response.status == 404
        response.read()
