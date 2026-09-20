# ADR-0008: The localhost auth exemption must not cover tunneled traffic

- Status: Accepted
- Date: 2026-09-20
- Source: pre-implementation review; extends [ADR-0004](0004-single-port-single-hostname-auth.md)

## Context

`allow_localhost = true` (spec #1 §12) skips auth for loopback clients so local browsing is frictionless. But `cloudflared` runs on the same machine and forwards tunneled requests to the origin over loopback, so a peer-IP-only rule would exempt every request — local browser and public internet alike. The token fallback would gate nothing, contradicting ADR-0004's consequence that "if Access is ever disabled or the tunnel URL leaks, the app token still gates access."

Cloudflare adds `CF-Connecting-IP` and `X-Forwarded-For` to all proxied traffic ([Cloudflare HTTP headers](https://developers.cloudflare.com/fundamentals/reference/http-request-headers/)). A request reaching the loopback-bound origin without those headers can only have come from a process already running on the machine — a trust boundary lesvi does not need to defend, since such a process can read shelf files directly.

## Decision

A request is treated as **local** (auth-exempt) only when both hold:

1. the TCP peer is loopback, and
2. it carries no Cloudflare forwarding headers (`CF-Connecting-IP`, `X-Forwarded-For`).

Tunneled requests must pass the token check even though their TCP peer is loopback. `/login` is the password form; the same secret is accepted as `Authorization: Bearer` for scripts.

## Consequences

- The fallback layer really does protect the public hostname: with Access off or the URL leaked, visitors hit the login page, not the library.
- Direct local browsing is unchanged — no login for loopback requests without forwarding headers.
- Test: with a token set, `GET /` from loopback **with** `CF-Connecting-IP` → login redirect; **without** → served.
- Works with the existing loopback bind; no second port or interface needed.

## Alternatives considered

- **Peer IP only** — simplest, but leaves the tunnel entirely to Cloudflare Access and makes ADR-0004's stated fallback false.
- **Validate the Access JWT in lesvi** — stronger, but duplicates cloudflared's optional `Protect with Access` setting and couples the app to one Access team; rejected for v1.
- **Bind a non-loopback interface for tunnel traffic** — reintroduces port/interface surface without adding security.
