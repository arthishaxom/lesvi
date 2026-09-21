# lesvi — CONTEXT

`lesvi` (lessons view) is a single-port, always-on local server that turns agent-generated lessons and reference notes into a browsable, mobile-first library reachable through one Cloudflare tunnel hostname. Point it at folders — **shelves** — and it indexes what is already there, serves the originals untouched, and renders virtual dashboard pages. Nothing is ever written into your content folders.

Status: v1 in progress — spec is [issue #1](https://github.com/arthishaxom/lesvi/issues/1); scaffold, shelf registration, the scan/index/serve spine, the browsing pages (home feed + shelf pages), agent metadata overrides (sidecar JSON + `lesvi:*` meta tags), instant client-side search (`?q=`), persisted pins (`POST /api/pin`), live index updates (native events or mtime polling, debounced), the fallback auth layer (token login + signed session cookie, Bearer, direct-loopback exemption), the always-on systemd user service (`lesvi service`, `lesvi status`, verbosity controls), and the phone experience (installable PWA with an offline service worker, `lesvi url`) are in place.

## Glossary

| Term | Meaning |
|---|---|
| **Shelf** | A registered content root, named and stored in config. Examples: `data-engg`, `ricing`, a project's `lessons/` folder. |
| **Artifact** | One indexed file inside a shelf. v1 artifacts are `.html` lessons/references; media (images, PDF) is served but not carded. |
| **Category** | A named glob group inside a shelf. Built-in preset: **Lessons** (`lessons/*.html`), **Reference** (`reference/**/*.html`), **Research** (`research/**/*.md`, v2 rendering). |
| **Convention** | The preset category globs + ignore list, overridable per shelf in config. |
| **Virtual index** | Dashboard pages generated in memory per request. No `index.html` is written to disk, ever. |
| **Home** | `/` — the cross-shelf recency feed and pinned section. |
| **Shelf page** | `/s/<shelf>/` — one shelf's cards in curriculum order (`NNNN` ascending), with a recency toggle. |
| **Raw artifact** | `/a/<shelf>/<path>` — the original file served byte-for-byte; relative asset paths (`../assets/...`) keep working. |
| **Capability link** | `/a/~<expiry>-<hmac>/<shelf>/<path>` — an HMAC-signed, shelf-scoped URL that authorizes `/a/` without a cookie, so a sandboxed document's relative subresources load. 30-day TTL, minted per render; sharing one exposes that shelf. ([ADR-0010](docs/adr/0010-artifacts-sandboxed.md)) |
| **Preview script** | The old per-subject `preview` Python wrapper around `python -m http.server` (one port per subject, collisions between agents). lesvi replaces it. |
| **Sidecar** | `<file>.meta.json` next to an artifact; optional metadata override channel for agents. |
| **Meta tag** | `<meta name="lesvi:tags" content="...">` (also `lesvi:pin`, `lesvi:title`, `lesvi:description`) in an artifact's `<head>`. |
| **Pin** | Server-side favorite, stored in `~/.local/state/lesvi/state.json`, never in shelf folders. |
| **Public URL** | Optional config value (e.g. `https://lesvi.example.com`); what `lesvi url` prints for phone access. |

## Settled decisions

- **Build, don't adopt** — no existing tool covers the artifact-first gallery; adopt nothing wholesale. ([ADR-0001](docs/adr/0001-build-thin-coordinator.md))
- **Artifacts are watched in place** — registered shelves only, no copies, re-runs overwrite; no version history in v1. ([ADR-0002](docs/adr/0002-watch-shelves-in-place.md))
- **Indexes are virtual** — `/` and `/s/<shelf>/` are generated; hand-made `index.html` files are ignored. ([ADR-0003](docs/adr/0003-virtual-index.md))
- **One port, one hostname, auth at the edge** — server on configurable port (default `8787`); user adds one Cloudflare Tunnel public hostname + Access policy; app-level token fallback that also covers tunneled traffic. Raw artifacts run in a browser sandbox so their own JS cannot borrow that session, and are served through signed, shelf-scoped capability links so their subresources load without a cookie. ([ADR-0004](docs/adr/0004-single-port-single-hostname-auth.md), [ADR-0008](docs/adr/0008-localhost-exemption-not-tunneled.md), [ADR-0010](docs/adr/0010-artifacts-sandboxed.md), [docs/cloudflare-setup.md](docs/cloudflare-setup.md))
- **Metadata precedence** — sidecar > in-HTML `lesvi:*` meta tag > parsing heuristics. ([ADR-0005](docs/adr/0005-metadata-precedence.md))
- **Python + uv, stdlib-first** — config editing via `tomlkit` ([ADR-0009](docs/adr/0009-tomlkit-config-editing.md)), optional `watchfiles`, plain HTML/CSS/JS UI, `uvx lesvi`. ([ADR-0006](docs/adr/0006-python-uv-stdlib-first.md))
- **Preset + config overrides** — "bring your own convention" via `~/.config/lesvi/config.toml`; `lesvi add` writes entries with smart shelf detection. ([ADR-0007](docs/adr/0007-convention-preset-config.md))
- **Personal-first, OSS-friendly** — config-driven, no hardcoded personal paths; README + license when published.
- **Name** — `lesvi`; verified clean (PyPI 404, npm 404, no exact-name GitHub repo, 2026-09-15).

## Parked (v2+)

- Obsidian vault: Quartz, read-only, served under `/notes/`, vault path configurable (not on this machine today).
- Markdown rendering for `Research` category artifacts (currently indexed-but-hidden).
- `lesvi init-skill` — generates the tag/pin convention into a project's `.agents/skills/`.
- Version history on republish; `lesvi export` (static site); QR-to-phone; full-text search; live updates.

## Links

- [Spec: lesvi v1](https://github.com/arthishaxom/lesvi/issues/1) — v1 specification (GitHub issue).
- [docs/adr/](docs/adr/) — decision records.
- [docs/research/existing-tools.md](docs/research/existing-tools.md) — 2026-09-15 survey of prior art.
- [docs/cloudflare-setup.md](docs/cloudflare-setup.md) — tunnel + Access click-through.
