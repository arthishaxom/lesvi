# lesvi — v1 Specification

Status: approved (2026-09-20) · Drafted: 2026-09-15 · Glossary and decision links: [CONTEXT.md](CONTEXT.md)

## 1. Overview

`lesvi` (lessons view) is a single-port, always-on local server that indexes agent-generated HTML lessons and reference pages across registered folders (**shelves**) and serves a mobile-first dashboard, reachable through one Cloudflare Tunnel hostname plus Cloudflare Access. It replaces the per-subject `preview` scripts and per-port tunnel routes.

One sentence: **agents write files, lesvi serves them — one port, one hostname, one library.**

## 2. Goals and non-goals

### Goals (v1)

1. Index existing folders with zero workflow change for agents.
2. One server, one configurable port (default `8787`), one tunnel hostname.
3. Mobile-first card index: home recency feed, per-shelf curriculum order, tags, search, pins, dark/light, PWA.
4. Serve artifacts byte-for-byte so their own assets (`../assets/...`) and scripts work.
5. Auth: Cloudflare Access at the edge, app token fallback; nothing public by accident.
6. Virtual dashboards — no writes into shelf folders, ever.
7. Convention preset + per-shelf config overrides.
8. Stdlib-only Python package, runnable with `uvx lesvi`.

### Non-goals (v1)

- No markdown rendering (research notes and the Obsidian vault are v2).
- No file uploads, editing, or management UI — lesvi is read-only.
- No version history, no full-text search, no live push updates.
- No Cloudflare automation (documented click-through only).
- No multi-user accounts, sharing links, or public pages.

## 3. Users and flows

### 3.1 Phone reading (primary)

User opens `https://<host>` → Cloudflare Access email OTP → home feed shows what agents published lately across shelves → taps a card → lesson opens full-screen → back returns to the index. PWA install available; visited lessons readable from cache on flaky mobile networks.

### 3.2 Agent publishing

Nothing new to run. The agent writes/overwrites `~/Learning/data-engg/lessons/0025-foo.html`. Within one watch tick the card appears (newest-first at home, number-ordered on the shelf page).

Optional enrichment by the agent (any of):

- `<meta name="lesvi:tags" content="kafka, streaming">` / `lesvi:pin` / `lesvi:title` / `lesvi:description` in `<head>`;
- a sidecar `0025-foo.html.meta.json` next to the file.

### 3.3 Local terminal

`lesvi add ~/Learning`, `lesvi list`, `lesvi url data-engg 25`, `lesvi service install`, `lesvi status`.

## 4. Architecture

Single Python process, three loops:

```
filesystem ──(watch: watchfiles | mtime poll)──▶ scanner ──▶ in-memory index ──▶ renderer ──▶ HTTP
      ▲                                                             ▲
      └── shelves from config                          pins from ~/.local/state/lesvi/state.json
```

| Component | Module | Responsibility |
|---|---|---|
| Config | `config.py` | Load/save TOML, shelf model, preset conventions, env overrides |
| Scanner | `scanner.py` | Walk shelf roots, match globs, apply ignores, produce artifact records |
| Metadata | `metadata.py` | Sidecar/meta-tag/heuristic resolution; stdlib HTML parsing |
| Index | `index.py` | In-memory artifact store, sorted views, pins state |
| Watch | `watch.py` | `watchfiles` when installed, else mtime polling; debounce + incremental updates |
| Server | `server.py` | `ThreadingHTTPServer`, routing, path safety, static serving, JSON API |
| Render | `render.py` | Server-side HTML templates for home/shelf/login |
| Auth | `auth.py` | Token, session cookie, localhost exemption, Bearer support |
| Service | `service.py` | systemd user unit install/uninstall/status |
| CLI | `cli.py` | `argparse` command surface |
| UI | `ui/` | `app.css`, `app.js`, `sw.js`, manifest, icons (served verbatim) |

No threads beyond the HTTP server's and the watcher's; the index is guarded by a lock.

## 5. Repository layout

```
pyproject.toml            # uv project, [project.scripts] lesvi = "lesvi.cli:main"
LICENSE                   # MIT
README.md
CONTEXT.md                # domain glossary + decisions
SPEC.md                   # this file
docs/
  adr/0001..0007-*.md
  cloudflare-setup.md
  research/existing-tools.md
src/lesvi/
  __init__.py __main__.py cli.py config.py scanner.py metadata.py
  index.py watch.py server.py render.py auth.py service.py
  ui/ (app.css app.js sw.js manifest.webmanifest icon-192.png icon-512.png)
tests/
  test_config.py test_scanner.py test_metadata.py test_index.py
  test_server.py test_auth.py test_cli.py
```

Python ≥ 3.11. Runtime deps: none. Optional extra: `lesvi[watch]` → `watchfiles`.

## 6. Configuration

File: `${LESVI_CONFIG:-~/.config/lesvi/config.toml}` (created on first `lesvi add`; hand-editable).

```toml
port = 8787                 # int
host = "127.0.0.1"          # bind address
public_url = ""             # e.g. "https://lesvi.example.com" (used by `lesvi url`)
watch = true                # use watchfiles if installed, else poll
poll_interval = 2.0         # seconds, poll fallback
allow_localhost = true      # loopback requests skip auth
# auth_token = "…"          # or env LESVI_TOKEN (env wins over config)

[shelves.data-engg]
path = "~/Learning/data-engg"
title = "Data Engineering"        # optional display override
ignore = ["learning-records/**", "assets/**", "index.html"]  # optional, replaces preset ignores

[shelves.data-engg.categories]    # optional, replaces preset categories
lessons   = ["lessons/*.html"]
reference = ["reference/**/*.html"]
```

Rules:

- `~` expands; relative paths resolve against the config file's directory.
- Shelf names are slugs (`[a-z0-9-]+`), unique; they are URL segments.
- Absent `categories` → preset. A category value may be a string or a list of globs.
- Unknown keys are preserved on save (config round-trips losslessly).

### 6.1 Built-in preset

| Category | Globs |
|---|---|
| Lessons | `lessons/*.html` |
| Reference | `reference/**/*.html` |
| Research | `research/**/*.md`, `reference/research/**/*.md` (indexed, hidden until v2 rendering) |

Preset ignore: `learning-records/**`, `assets/**`, `index.html`, `node_modules/**`, dotfiles/dotdirs, anything outside the shelf root.

Glob semantics: POSIX relative paths; `*` within a segment, `**` across segments; matching is case-sensitive.

## 7. Shelves and registration

`lesvi add <path>`:

1. Resolve the path; slugify its basename as the default name.
2. **Smart detect**: if the path itself matches the preset (contains `lessons/` or `reference/` with matching files) → one shelf.
3. Otherwise, scan immediate children; every child that matches the preset becomes its own shelf (`--single` forces one shelf; `--name` sets the name explicitly).
4. Print what was added; append to config with section comments.

`lesvi remove <name|path>` deletes the entry. No content is touched. `lesvi list` shows name, path, category counts, and whether the path exists.

## 8. Scanning and index

Artifact record (JSON in `/api/index.json`):

```json
{
  "shelf": "data-engg",
  "path": "lessons/0024-apache-kafka-fundamentals.html",
  "url": "/a/data-engg/lessons/0024-apache-kafka-fundamentals.html",
  "category": "Lessons",
  "number": 24,
  "title": "Apache Kafka Fundamentals",
  "description": "Topics, partitions, offsets, consumer groups, …",
  "tags": ["Track B", "Processing Paradigms"],
  "mtime": "2026-09-15T12:04:11+05:30",
  "size": 58214,
  "pinned": false,
  "meta_source": "heuristic"
}
```

Scan behavior:

- Full scan on startup; incremental update on watch events (create/modify/delete/rename).
- Debounce bursts 300 ms; ignore changes in ignored paths; tolerate files being written in chunks (reparse on settling).
- HTML parsing reads at most the first 64 KB of each file.
- Sorted views precomputed per shelf: `by_number` (`number` asc, then path), `by_recency` (`mtime` desc).
- Scale target: 5,000 artifacts full-scan < 2 s; incremental event < 50 ms; `/api/index.json` < 1 MB at 5,000 artifacts.

## 9. Metadata resolution

Field-by-field, first hit wins (see [ADR-0005](docs/adr/0005-metadata-precedence.md)):

1. Sidecar `<filename>.meta.json`: `{"title", "description", "tags"[], "pin" bool}`.
2. `<meta name="lesvi:title|description|tags|pin">`; `tags` comma-separated.
3. Heuristics:
   - `number`: `^(\d+)[-_]` filename prefix;
   - `title`: `<title>` text; `Lesson 24 — Apache Kafka Fundamentals` → title `Apache Kafka Fundamentals`, number `24`; otherwise filename-derived title (slug → words, title-cased);
   - `description`: first `.subtitle` element, else first `<p>`, collapsed whitespace, capped at 160 chars + `…`;
   - `tags`: category label + tokens from the first element with class `lesson-tag` or `badge`, split on `·`, dropping `Lesson N` and duration tokens (`10 minutes`, `15 min`);
   - `mtime`: file modification time.

`pin` is always server state (sidecar/meta can seed it once; toggling in the UI wins thereafter).

Parser must never raise: any failure degrades to filename-derived metadata and logs at debug level.

## 10. HTTP surface

| Method | Path | Notes |
|---|---|---|
| GET | `/` | Home: pinned + cross-shelf recency feed |
| GET | `/s/<shelf>/` | Shelf page; `?sort=recent`; default curriculum order |
| GET | `/a/<shelf>/<path>` | Raw artifact/assets, byte-for-byte, `ETag`/`Last-Modified`, 304 support |
| GET | `/api/index.json` | Full index (shelves + artifacts), `Cache-Control: no-store` |
| POST | `/api/pin` | `{"shelf", "path", "pinned"}` → `204`; auth required unless localhost |
| GET | `/login`, POST `/login`, GET `/logout` | Token → session cookie |
| GET | `/manifest.webmanifest`, `/sw.js`, `/assets/<ui>`, `/favicon.ico` | Static UI assets, auth-exempt |
| GET | `/healthz` | `{"ok": true, "shelves": 7, "artifacts": 142}` |
| GET | `/notes/…` | Reserved for v2; 404 in v1 |

Path safety for `/a/`: resolve the requested path, require it to stay inside the shelf root (reject `..`, encoded traversal, absolute paths, symlinks escaping the root), reject dotfiles/dotdirs and `node_modules`. Miss → 404. Directory requests → 404 (no listings; the virtual index is the navigation).

Content types: by extension for html/css/js/svg/png/jpg/jpeg/webp/gif/pdf/mp4/webm/mp3/wav + `application/octet-stream` fallback. HTML served as `text/html; charset=utf-8`.

## 11. UI

- **Home**: header (`lesvi`, shelf count, search box), shelf filter chips, `Pinned` section (hidden when empty), `Recently updated` cards (max 50, then "show more"). Card: shelf chip, number badge, title, description, up to 3 tags, relative time ("2h ago").
- **Shelf page**: shelf title + counts, sort toggle (`Curriculum` / `Recent`), sections per category (Lessons, Reference; Research hidden in v1), cards with number badge and pin toggle.
- **Card tap**: opens the raw artifact in the same tab. Phone back returns to the index.
- **Search**: client-side over title/description/tags, instant, debounced 100 ms; result count announced; works on both pages; query persists in `?q=`.
- **Pin**: `POST /api/pin`, optimistic UI, persisted in `~/.local/state/lesvi/state.json`.
- **Theme**: `prefers-color-scheme` default + manual toggle persisted in `localStorage`.
- **PWA**: manifest (`name: lesvi`, `display: standalone`), maskable icons, service worker: precache shell + index JSON (stale-while-revalidate), runtime-cache visited `/a/` pages (cache-first with background refresh).
- **Accessibility**: semantic landmarks, visible focus, 44 px minimum tap targets, color contrast AA, works without JS for reading/serving.
- Refresh: index re-fetched on page load, `visibilitychange`, and pull-to-refresh; no websockets/SSE in v1.

## 12. Auth

- Effective token: `LESVI_TOKEN` env > `--token` flag > `auth_token` config. If none is set and `host` is not loopback, `lesvi serve` refuses to start unless `--insecure` is passed (loud warning).
- With a token: `/login` accepts the token (constant-time compare) and sets `lesvi_session` (HttpOnly, SameSite=Lax, `Secure` when `X-Forwarded-Proto: https`, 30-day TTL, HMAC-signed value). `Authorization: Bearer <token>` also accepted for scripts and CLI checks.
- Exempt paths: `/login`, `/logout`, `/healthz`, `/manifest.webmanifest`, `/sw.js`, `/assets/*`, `/favicon.ico`.
- `allow_localhost = true` (default) skips auth for **direct** loopback clients so local browsing stays frictionless. Because `cloudflared` also reaches the origin over loopback, a request is local only when the peer is loopback **and** it carries no Cloudflare forwarding headers (`CF-Connecting-IP` / `X-Forwarded-For`); tunneled requests must pass the token login. ([ADR-0008](docs/adr/0008-localhost-exemption-not-tunneled.md))
- Cloudflare Access remains the primary gate; this layer is the fallback. Setup: [docs/cloudflare-setup.md](docs/cloudflare-setup.md).

## 13. CLI

```
lesvi serve   [--port N] [--host H] [--config PATH] [--token T] [--no-watch] [--poll-interval S] [--insecure]
lesvi add     PATH [--name NAME] [--title TITLE] [--single]
lesvi remove  NAME|PATH
lesvi list    [--json]
lesvi url     [SHELF [ARTIFACT]] [--open]
lesvi status
lesvi service install | uninstall | status
lesvi version
```

- `serve` prints: config path, shelf/artifact counts, local URL, and `public_url` if set.
- `url` resolves an artifact by `NNNN` number, slug substring, or exact relative path; prints the public URL when `public_url` is configured (this is the "open on my phone" command), `--open` launches the local browser.
- `service install` writes `~/.config/systemd/user/lesvi.service` (`ExecStart` = the resolved `lesvi serve` invocation, `Restart=on-failure`), then prints the `systemctl --user enable --now lesvi` and `loginctl enable-linger $USER` commands; `service status` reports unit state.
- Exit codes: 0 ok, 1 error, 2 usage.

## 14. Operations

- Always-on via systemd user service; no sudo required.
- Logs to stdout/stderr (journald when run as a user unit). `--verbose` raises log level.
- Port conflicts: startup fails with a clear message naming the port and the likely culprit command (`ss -tlnp` hint).
- The watcher prints a warning once if `fs.inotify.max_user_watches` is exhausted and falls back to polling.
- State file: `~/.local/state/lesvi/state.json` (`{"pins": ["data-engg/lessons/0024-…"]}`); safe to delete (loses pins only).
- Cloudflare side (manual, documented): one public hostname → `http://localhost:8787`, one Access self-hosted app with an email-allow policy.

## 15. Testing

- `uv run pytest` (pytest as dev dependency only).
- Fixture shelves under `tmp_path` reproducing the real layout: numbered lessons, `.subtitle`, tag spans, live-tag variants, a sidecar, a meta-tagged file, a lesson with `../assets/`, a traversal attempt, non-HTML media.
- Unit: config round-trip, scanner globs/ignores, metadata precedence, index sorting/pins.
- Server (`http.client` on an ephemeral port): routes, 304s, content types, auth redirect/exemption (loopback with and without `CF-Connecting-IP`), pin API, path-safety rejections (including `%2e%2e` and symlink escape).
- CLI: `add/remove/list/url` via `LESVI_CONFIG`-scoped `subprocess` runs.
- Acceptance smoke test: `lesvi add ~/Learning` equivalent on the fixture tree, then crawl every card URL and assert `200` + expected content type.

## 16. Acceptance criteria (v1)

1. `uv run lesvi add ~/Learning` registers `data-engg`, `GK`, `oni`, `ricing`, `skyrim` (whichever match on the day) — no manual config edit needed.
2. Server on `:8787` serves `/` and each `/s/<shelf>/`; cards ordered by `NNNN` ascending with a working recency toggle.
3. `/a/...` serves a real lesson with its `../assets/lesson.css`, quiz JS, and mermaid intact.
4. Tags/descriptions parse from the in-page span/leading paragraph; a sidecar and a `lesvi:tags` meta tag each override correctly.
5. Search filters instantly; pins survive a server restart.
6. No file under any shelf is created, modified, or deleted by any lesvi command (verified by mtime/hash sweep in tests).
7. With a token configured, a non-loopback request is rejected — and so is a loopback request carrying `CF-Connecting-IP` — while a direct loopback request stays exempt; `/login` + cookie then succeeds; Bearer works.
8. `lesvi service install` produces a user unit that starts on boot (linger enabled).
9. `uv run pytest` passes.
10. `docs/cloudflare-setup.md` click-through is accurate on the user's account (tunnel route → Access app → phone OTP).

## 17. Deferred (v2+)

- Markdown rendering for `Research` artifacts (and any `.md` escape hatch).
- Quartz vault at `/notes/` from a configurable vault path (vault is not on this machine today).
- `lesvi init-skill` — generate the tagging/pin convention into a project's `.agents/skills/` (needs the existing teach/show-me skills as input).
- Version history on republish; `lesvi export` static site; QR-to-phone; full-text search; SSE live updates; multiple users.
