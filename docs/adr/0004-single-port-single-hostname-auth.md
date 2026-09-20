# ADR-0004: Single port, single tunnel hostname, auth at the edge with app fallback

- Status: Accepted
- Date: 2026-09-15
- Source: interview rounds 1–2; [research §4, §6](../research/existing-tools.md)

## Context

The machine already runs `cloudflared` 2026.8.2 as a system service with a **remotely-managed (token) tunnel**. Today the user adds a dashboard route per local port (`subdomain → http://localhost:PORT`) and starts a `python -m http.server` per subject — N ports, N tunnel routes, and collisions when agents overlap. Findings:

- One tunnel carries **many public hostnames**, each mapped to its own local port. More hostnames is supported but unnecessary: one server, one route suffices.
- Dashboard (remote-managed) path routing exists but is undocumented for regex/ordering, and it does **not** strip prefixes; locally-rewriting would be required. Avoid it.
- Cloudflare Access is free ≤ 50 users (email OTP / IdP), consumes one seat for one human, and attaches to a tunnel hostname with no app code change.

## Decision

- `lesvi` listens on **one configurable port** (default `8787`, host `127.0.0.1`). Shelves multiply, ports do not.
- The user adds **one** tunnel public hostname → `http://localhost:8787` and one Access self-hosted application with an email-allow policy. This is a documented dashboard click-through ([docs/cloudflare-setup.md](../cloudflare-setup.md)); lesvi does **not** automate Cloudflare.
- App-level fallback auth: a shared token (config or `LESVI_TOKEN`) checked via session cookie for the browser (`/login`) or `Authorization: Bearer` for scripts. Localhost requests are exempt by default.
- `public_url` config value lets `lesvi url` print the phone-usable link.

## Consequences

- The port-collision class disappears: every agent just writes files; no agent starts a server.
- One tunnel route and one Access policy cover every shelf, current and future.
- Serving under a subpath is avoided entirely, so lessons' relative asset paths (`../assets/lesson.css`) keep working.
- Cloudflare remains a manual (documented) setup step — acceptable, one-time.
- If Access is ever disabled or the tunnel URL leaks, the app token still gates access.

## Alternatives considered

- **Path-based routing on one hostname** — no prefix stripping, undocumented semantics for dashboard tunnels; rejected.
- **Separate hostname per shelf** — multiplies dashboard work and auth policies for no benefit.
- **Tailscale Serve/Funnel, ngrok, zrok** — Tailscale fits tailnet-only clients but adds a second identity plane; ngrok free has quotas/interstitials; the existing tunnel already wins.
- **App auth only** — possible, but edge auth (OTP on the phone) is strictly better for a public hostname.
