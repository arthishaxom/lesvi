"""Fallback auth behind Cloudflare Access: token login, session cookie, Bearer.

Cloudflare Access is the primary gate on the public hostname; this is the layer
that still holds when Access is off or the tunnel URL leaks (ADR-0004). One
shared secret — ``$LESVI_TOKEN`` > ``--token`` > ``auth_token`` — unlocks a
password form at ``/login`` that sets a signed session cookie, and the same
secret is accepted as ``Authorization: Bearer`` by scripts.

Raw artifacts are sandboxed (ADR-0010), so a document's opaque origin carries
no cookies on its own subresources. Dashboard links therefore embed a
**capability**: an HMAC-signed, shelf-scoped, 30-day ``~<expiry>-<hmac>`` path
segment that authorizes ``/a/`` without any other credential.

A request is **local** (auth-exempt) only when the TCP peer is loopback *and*
it carries no Cloudflare forwarding headers (ADR-0008). ``cloudflared`` reaches
the origin over loopback, so a peer-IP-only rule would exempt the whole tunnel.
"""

from __future__ import annotations

import hmac
import os
import re
import time
from collections.abc import Mapping
from hashlib import sha256

from lesvi.config import Config, is_loopback_host

#: The session cookie set by a successful ``/login``.
COOKIE_NAME = "lesvi_session"
#: Sessions last 30 days; the value is self-expiring and HMAC-signed.
SESSION_TTL = 30 * 24 * 60 * 60
#: Signed artifact links also last 30 days. Dashboard renders mint fresh stamps,
#: so only a bookmarked or shared URL ever reaches the expiry.
ARTIFACT_URL_TTL = 30 * 24 * 60 * 60
#: A capability segment: ``~``, the unix expiry without leading zeros, ``-``,
#: then the hex digest. The canonical spelling matters: a stamp is a URL
#: segment, and two spellings of one expiry should not both be accepted.
_STAMP_PATTERN = re.compile(r"~(0|[1-9]\d{0,9})-([0-9a-f]{32})\Z")
#: A session value's HMAC is a SHA-256 hex digest; anything else is malformed.
_SESSION_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
#: Paths that never require auth: the login form itself, health checks, and the
#: static UI the login page needs to render.
EXEMPT_PATHS = frozenset(
    {
        LOGIN_PATH,
        LOGOUT_PATH,
        "/healthz",
        "/manifest.webmanifest",
        "/sw.js",
        "/favicon.ico",
    }
)
EXEMPT_PREFIXES = ("/assets/",)
#: ``cloudflared`` adds these to every proxied request; their presence means the
#: request arrived through the tunnel, not directly from a local process.
FORWARDING_HEADERS = ("CF-Connecting-IP", "X-Forwarded-For")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """The named header, case-insensitively.

    HTTP header names are case-insensitive, but the mappings handed around here
    (a plain dict built from the request) are not: a proxy may send
    ``CF-Connecting-IP`` in any casing, and missing it would exempt a tunneled
    request from auth.
    """
    value = headers.get(name)
    if value is not None:
        return value
    wanted = name.lower()
    for key, candidate in headers.items():
        if key.lower() == wanted:
            return candidate
    return None


def resolve_token(
    flag: str | None = None,
    config: Config | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """The effective fallback token: ``$LESVI_TOKEN`` > ``--token`` > config.

    Blank values count as unset, so an empty ``LESVI_TOKEN`` cannot silently
    switch auth off or shadow a real credential behind it.
    """
    environment = os.environ if env is None else env
    candidates = (
        environment.get("LESVI_TOKEN"),
        flag,
        config.auth_token() if config is not None else None,
    )
    for candidate in candidates:
        if candidate:
            return candidate
    return None


class Auth:
    """The token policy: who is local, and which credentials are valid."""

    token: str
    allow_localhost: bool

    def __init__(self, token: str, *, allow_localhost: bool = True) -> None:
        self.token = token
        self.allow_localhost = allow_localhost

    def is_local(self, peer: str, headers: Mapping[str, str]) -> bool:
        """Whether *peer* is a direct loopback client (ADR-0008)."""
        if not self.allow_localhost or not is_loopback_host(peer):
            return False
        return not any(
            _header_value(headers, name) is not None for name in FORWARDING_HEADERS
        )

    def check_token(self, candidate: str | None) -> bool:
        """Constant-time token comparison; a blank candidate never matches."""
        if not candidate:
            return False
        return hmac.compare_digest(
            candidate.encode("utf-8"), self.token.encode("utf-8")
        )

    def bearer_token(self, header: str | None) -> str | None:
        """The credential in ``Authorization: Bearer …``, or ``None``."""
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return value.strip() or None

    def session_value(self, *, now: float | None = None) -> str:
        """A fresh signed session value: ``<expiry unix seconds>.<hmac>``.

        The token is the HMAC key, so changing the token invalidates every
        outstanding session without any server-side state.
        """
        expires = int((time.time() if now is None else now) + SESSION_TTL)
        return f"{expires}.{self._signature(expires)}"

    def session_valid(self, value: str | None, *, now: float | None = None) -> bool:
        """Whether *value* is an unexpired session this token signed."""
        if not value:
            return False
        raw_expiry, _, signature = value.partition(".")
        try:
            expires = int(raw_expiry)
        except ValueError:
            return False
        if expires < (time.time() if now is None else now):
            return False
        if _SESSION_SIGNATURE_PATTERN.fullmatch(signature) is None:
            # A cookie is attacker-controlled, and ``compare_digest`` raises
            # ``TypeError`` on non-ASCII strings: reject anything not shaped
            # like the digest we issue before comparing.
            return False
        return hmac.compare_digest(signature, self._signature(expires))

    def _signature(self, expires: int) -> str:
        message = f"lesvi-session.{expires}".encode()
        return hmac.new(self.token.encode("utf-8"), message, sha256).hexdigest()

    def artifact_stamp(self, shelf: str, *, now: float | None = None) -> str:
        """A fresh capability for *shelf*: ``~<expiry>-<hmac>``.

        The dashboard embeds this URL-safe segment in artifact links. It is
        shelf-scoped rather than per-file so an artifact's relative subresources
        (``../assets/quiz.js``) inherit it and validate for the same shelf.
        """
        expires = int((time.time() if now is None else now) + ARTIFACT_URL_TTL)
        return f"~{expires}-{self._artifact_signature(shelf, expires)}"

    def artifact_stamp_valid(
        self, stamp: str | None, shelf: str, *, now: float | None = None
    ) -> bool:
        """Whether *stamp* is an unexpired capability this token minted for *shelf*.

        Malformed, expired, other-token and wrong-shelf stamps are all rejected.
        """
        if not stamp:
            return False
        match = _STAMP_PATTERN.fullmatch(stamp)
        if match is None:
            return False
        expires = int(match.group(1))
        if expires < (time.time() if now is None else now):
            return False
        return hmac.compare_digest(
            match.group(2), self._artifact_signature(shelf, expires)
        )

    def _artifact_signature(self, shelf: str, expires: int) -> str:
        message = f"lesvi-artifact.{expires}.{shelf}".encode()
        return hmac.new(self.token.encode("utf-8"), message, sha256).hexdigest()[:32]


def cookie_value(header: str | None, name: str = COOKIE_NAME) -> str | None:
    """The named cookie's value from a ``Cookie`` header, or ``None``."""
    for part in (header or "").split(";"):
        key, _, value = part.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


def session_cookie(value: str, *, secure: bool) -> str:
    """The ``Set-Cookie`` value for a successful login."""
    attributes = [
        f"{COOKIE_NAME}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_TTL}",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def cleared_cookie(*, secure: bool) -> str:
    """The ``Set-Cookie`` value that forgets the session."""
    attributes = [
        f"{COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


def forwarded_https(headers: Mapping[str, str]) -> bool:
    """Whether TLS terminated in front of us (``X-Forwarded-Proto: https``).

    Cloudflare always sets this on proxied traffic; the cookie only earns the
    ``Secure`` attribute when the browser really is on HTTPS.
    """
    proto = _header_value(headers, "X-Forwarded-Proto") or ""
    return proto.split(",", 1)[0].strip().lower() == "https"


def is_exempt_path(path: str) -> bool:
    """Whether *path* is served without auth (login, health, static UI)."""
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)
