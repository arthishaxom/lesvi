"""Tests for ``lesvi.auth``: token resolution, sessions, and the localhost rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from lesvi.auth import (
    ARTIFACT_URL_TTL,
    COOKIE_NAME,
    SESSION_TTL,
    Auth,
    cleared_cookie,
    cookie_value,
    forwarded_https,
    is_exempt_path,
    resolve_token,
    session_cookie,
)
from lesvi.config import Config, ConfigError, is_loopback_host

TOKEN = "correct horse battery staple"


def _config(tmp_path: Path, text: str = "") -> Config:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return Config.load(path)


# --- token resolution ---------------------------------------------------------


def test_the_environment_token_wins_over_the_flag_and_the_config(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, 'auth_token = "from-config"\n')

    token = resolve_token("from-flag", config, env={"LESVI_TOKEN": "from-env"})

    assert token == "from-env"


def test_the_flag_wins_over_the_config(tmp_path: Path) -> None:
    config = _config(tmp_path, 'auth_token = "from-config"\n')

    assert resolve_token("from-flag", config, env={}) == "from-flag"


def test_the_config_token_is_the_last_resort(tmp_path: Path) -> None:
    config = _config(tmp_path, 'auth_token = "from-config"\n')

    assert resolve_token(None, config, env={}) == "from-config"


def test_no_configured_token_resolves_to_none(tmp_path: Path) -> None:
    assert resolve_token("", _config(tmp_path), env={"LESVI_TOKEN": ""}) is None


def test_a_non_string_auth_token_is_a_config_error(tmp_path: Path) -> None:
    config = _config(tmp_path, "auth_token = 5\n")

    with pytest.raises(ConfigError, match="auth_token"):
        resolve_token(None, config, env={})


# --- loopback hosts -----------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.53", "::1", "localhost", "LOCALHOST", "[::1]"]
)
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host", ["", "0.0.0.0", "::", "192.168.1.10", "10.0.0.1", "lesvi.example.com"]
)
def test_non_loopback_hosts(host: str) -> None:
    assert not is_loopback_host(host)


# --- the localhost exemption (ADR-0008) ---------------------------------------


def test_a_direct_loopback_peer_is_local() -> None:
    assert Auth(TOKEN).is_local("127.0.0.1", {})
    assert Auth(TOKEN).is_local("::1", {"Host": "localhost"})


@pytest.mark.parametrize(
    "headers",
    [
        {"CF-Connecting-IP": "203.0.113.9"},
        {"X-Forwarded-For": "203.0.113.9"},
        {"X-Forwarded-For": ""},  # present at all is enough
        {"cf-connecting-ip": "203.0.113.9"},  # names are case-insensitive
        {"x-forwarded-for": "203.0.113.9"},
    ],
)
def test_forwarding_headers_make_a_loopback_peer_non_local(
    headers: dict[str, str],
) -> None:
    assert not Auth(TOKEN).is_local("127.0.0.1", headers)


def test_a_non_loopback_peer_is_never_local() -> None:
    assert not Auth(TOKEN).is_local("203.0.113.9", {})


def test_allow_localhost_false_removes_the_exemption() -> None:
    auth = Auth(TOKEN, allow_localhost=False)

    assert not auth.is_local("127.0.0.1", {})


# --- token comparison and Bearer ----------------------------------------------


def test_the_token_must_match_exactly() -> None:
    auth = Auth(TOKEN)

    assert auth.check_token(TOKEN)
    assert not auth.check_token(TOKEN + "x")
    assert not auth.check_token(TOKEN[:-1])
    assert not auth.check_token("")
    assert not auth.check_token(None)


@pytest.mark.parametrize("header", ["Bearer abc", "bearer abc", "BEARER  abc "])
def test_bearer_credentials_are_parsed(header: str) -> None:
    assert Auth(TOKEN).bearer_token(header) == "abc"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   "])
def test_non_bearer_headers_yield_no_credential(header: str | None) -> None:
    assert Auth(TOKEN).bearer_token(header) is None


# --- sessions -----------------------------------------------------------------


def test_a_fresh_session_round_trips() -> None:
    auth = Auth(TOKEN)

    value = auth.session_value()

    assert auth.session_valid(value)


def test_a_session_expires() -> None:
    auth = Auth(TOKEN)
    value = auth.session_value(now=1_000.0)

    assert auth.session_valid(value, now=1_000.0 + SESSION_TTL - 1)
    assert not auth.session_valid(value, now=1_000.0 + SESSION_TTL + 1)


def test_a_tampered_session_is_rejected() -> None:
    auth = Auth(TOKEN)
    value = auth.session_value(now=1_000.0)

    expiry, _, signature = value.partition(".")
    assert not auth.session_valid(f"{expiry}.{signature[:-1]}0")
    assert not auth.session_valid(f"{int(expiry) + 999}.{signature}")
    assert not auth.session_valid("garbage")
    assert not auth.session_valid("")
    assert not auth.session_valid(None)


def test_a_session_signed_with_another_token_is_rejected() -> None:
    value = Auth(TOKEN).session_value()

    assert not Auth("another token").session_valid(value)


def test_a_non_ascii_session_signature_is_rejected() -> None:
    """A cookie is attacker-controlled; ``compare_digest`` raises on non-ASCII."""
    auth = Auth(TOKEN)
    expiry, _, _ = auth.session_value().partition(".")

    assert not auth.session_valid(f"{expiry}.é")
    assert not auth.session_valid(f"{expiry}.{'0' * 63}é")
    assert not auth.session_valid(f"{expiry}.{'0' * 63}")  # too short


def test_the_session_cookie_is_hardened() -> None:
    header = session_cookie("1700000000.deadbeef", secure=False)

    assert header.startswith(f"{COOKIE_NAME}=1700000000.deadbeef")
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header
    assert "Path=/" in header
    assert f"Max-Age={SESSION_TTL}" in header
    assert "Secure" not in header


def test_the_session_cookie_is_secure_behind_https() -> None:
    assert "Secure" in session_cookie("value", secure=True)
    assert "Secure" in cleared_cookie(secure=True)


def test_the_cleared_cookie_forgets_the_session() -> None:
    header = cleared_cookie(secure=False)

    assert header.startswith(f"{COOKIE_NAME}=;")
    assert "Max-Age=0" in header


def test_the_cookie_value_is_read_by_name() -> None:
    header = f"a=b; {COOKIE_NAME}=xyz; other=c"

    assert cookie_value(header) == "xyz"
    assert cookie_value("a=b") is None
    assert cookie_value(None) is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"X-Forwarded-Proto": "https"}, True),
        ({"X-Forwarded-Proto": "HTTPS"}, True),
        ({"X-Forwarded-Proto": "https, http"}, True),
        ({"x-forwarded-proto": "https"}, True),  # names are case-insensitive
        ({"X-Forwarded-Proto": "http"}, False),
        ({"X-Forwarded-Proto": ""}, False),
        ({}, False),
    ],
)
def test_forwarded_https_detection(header: dict[str, str], expected: bool) -> None:
    assert forwarded_https(header) is expected


# --- signed artifact links (ADR-0010) -----------------------------------------

SHELF = "data-engg"


def test_an_artifact_stamp_round_trips() -> None:
    auth = Auth(TOKEN)

    stamp = auth.artifact_stamp(SHELF)

    assert stamp.startswith("~")
    assert auth.artifact_stamp_valid(stamp, SHELF)


def test_an_artifact_stamp_expires() -> None:
    auth = Auth(TOKEN)
    stamp = auth.artifact_stamp(SHELF, now=1_000.0)

    assert auth.artifact_stamp_valid(stamp, SHELF, now=1_000.0 + ARTIFACT_URL_TTL - 1)
    assert not auth.artifact_stamp_valid(
        stamp, SHELF, now=1_000.0 + ARTIFACT_URL_TTL + 1
    )


def test_an_artifact_stamp_is_shelf_scoped() -> None:
    auth = Auth(TOKEN)
    stamp = auth.artifact_stamp(SHELF, now=1_000.0)

    assert auth.artifact_stamp_valid(stamp, SHELF, now=1_000.0)
    assert not auth.artifact_stamp_valid(stamp, "ricing", now=1_000.0)


def test_an_artifact_stamp_signed_with_another_token_is_rejected() -> None:
    stamp = Auth(TOKEN).artifact_stamp(SHELF, now=1_000.0)

    assert not Auth("another token").artifact_stamp_valid(stamp, SHELF, now=1_000.0)


def test_a_tampered_artifact_stamp_is_rejected() -> None:
    auth = Auth(TOKEN)
    stamp = auth.artifact_stamp(SHELF, now=1_000.0)
    prefix, _, digest = stamp.rpartition("-")
    expires = int(prefix[1:])

    for tampered in (
        f"{prefix}-{digest[:-1]}{'0' if digest[-1] != '0' else '1'}",
        f"~{expires + 999}-{digest}",  # the expiry is signed too
    ):
        assert not auth.artifact_stamp_valid(tampered, SHELF, now=1_000.0)


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        "",
        "~",
        "~123",
        "~123-",
        "~123-abc",
        "123-deadbeef",
        "~abc-deadbeef",
        "~~123-deadbeef",
        " ~123-deadbeef",
        "~123-deadbeef ",
        "~-deadbeef",
    ],
)
def test_malformed_artifact_stamps_are_rejected(stamp: str | None) -> None:
    assert not Auth(TOKEN).artifact_stamp_valid(stamp, SHELF)


def test_artifact_stamp_format_is_strict() -> None:
    auth = Auth(TOKEN)
    stamp = auth.artifact_stamp(SHELF, now=1_000.0)

    assert not auth.artifact_stamp_valid(stamp.upper(), SHELF, now=1_000.0)
    assert not auth.artifact_stamp_valid(stamp + "x", SHELF, now=1_000.0)
    assert not auth.artifact_stamp_valid(stamp.replace("~", "~0"), SHELF, now=1_000.0)


# --- exempt paths -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/logout",
        "/healthz",
        "/manifest.webmanifest",
        "/sw.js",
        "/favicon.ico",
        "/assets/app.css",
        "/assets/app.js",
    ],
)
def test_exempt_paths(path: str) -> None:
    assert is_exempt_path(path)


@pytest.mark.parametrize(
    "path",
    ["/", "/s/data-engg/", "/a/data-engg/lessons/x.html", "/api/index.json", "/login/extra"],
)
def test_protected_paths(path: str) -> None:
    assert not is_exempt_path(path)
