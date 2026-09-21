# lesvi

`lesvi` (lessons view) is a single-port, always-on local server that turns
agent-generated lessons and reference notes into a browsable, mobile-first
library reachable through one Cloudflare tunnel hostname. Point it at folders —
**shelves** — and it indexes what is already there, serves the originals
untouched, and renders virtual dashboard pages. Nothing is ever written into
your content folders.

**Status: v1 in progress.** The spec is
[issue #1](https://github.com/arthishaxom/lesvi/issues/1) and the work is
tracked as [open tickets](https://github.com/arthishaxom/lesvi/issues). The
scaffold, shelf registration (`lesvi add` / `list` / `remove`), the scanning
index behind `lesvi serve`, byte-for-byte raw artifact serving, the home feed /
shelf pages, the agent metadata overrides (sidecar + `lesvi:*` meta tags), live
index updates (native events or mtime polling), the fallback auth layer (token
login, signed session cookie, Bearer, direct-loopback exemption), the always-on
systemd user service (`lesvi service`, `lesvi status`, `-v` logging), and the
phone experience (`lesvi url`, installable PWA with an offline service worker)
are in place.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/)

Runtime dependency: [tomlkit](https://github.com/python-poetry/tomlkit) (pure
Python), so `lesvi add` / `remove` preserve the comments and formatting in your
hand-edited config.

Optional: install the `watch` extra (`uvx lesvi[watch]`, or
`uv tool install "lesvi[watch]"`) for native filesystem events. Without it,
lesvi polls file mtimes every 2 seconds — same behavior, less efficient.

## Run

```sh
uv run lesvi version
```

Once published, no checkout is needed:

```sh
uvx lesvi version
```

## Shelves

Register folders and lesvi indexes them in place — no file is ever copied,
moved, or written.

```sh
uv run lesvi add ~/Learning        # one shelf per matching child folder
uv run lesvi add ~/Dev/proj/lessons --single --name proj
uv run lesvi list                  # name, path, category counts, missing marker
uv run lesvi list --json
uv run lesvi remove proj
```

Shelves live in `~/.config/lesvi/config.toml` (`$LESVI_CONFIG` overrides the
path), hand-editable at all times; unknown keys survive lesvi's edits.

## Serve

```sh
uv run lesvi serve              # binds the configured port (default 8787)
uv run lesvi serve --port 9000  # one-off override
uv run lesvi serve --no-watch   # serve the startup index, never rescan
uv run lesvi serve --poll-interval 5   # force mtime polling every 5s
```

`serve` scans every registered shelf into an in-memory index, renders the
virtual dashboards (`/` pinned section + recency feed, `/s/<shelf>/` curriculum
order with a `?sort=recent` toggle; Research stays hidden in v1), and serves the
index at `/api/index.json` (`Cache-Control: no-store`, gzipped when the client
accepts it). Every page carries an instant client-side search over
title/description/tags; the query persists in `?q=`, so a reload or a shared
URL reopens the same filtered view with the result count announced. Cards also
carry a pin toggle: it updates optimistically and stores the decision through
`POST /api/pin` (`{"shelf", "path", "pinned"}` → `204`). Each indexed artifact
and its relative assets are served byte-for-byte at `/a/<shelf>/<path>` with
`ETag`/`Last-Modified` revalidation. Artifact documents are sandboxed to an
opaque origin, so their quiz/mermaid JavaScript runs but cannot read the
authenticated API (ADR-0010). The UI assets (`/assets/app.css`,
`/assets/app.js`) add the persisted theme toggle; pages read fine with
JavaScript disabled. Auth (below) protects everything except the login form,
health check and static UI.

The index keeps itself current while `serve` runs: writing, overwriting,
renaming or deleting a lesson (or its `.meta.json` sidecar) updates the feed
within one debounce tick (~300 ms) — no restart. With the `watch` extra, native
filesystem events do the work; otherwise mtimes are polled every `poll_interval`
seconds (default 2; `watch = false` or `--no-watch` disables watching, and
`--poll-interval S` forces polling). Ignored paths stay ignored, bursts are
debounced, and a chunked write is parsed once it settles.

## Auth

Cloudflare Access is the primary gate; lesvi's token is the fallback that still
holds when Access is off or the tunnel URL leaks (ADR-0004). One shared secret
is read in this order — **`$LESVI_TOKEN` > `--token` > `auth_token`** in the
config:

```sh
LESVI_TOKEN=… uv run lesvi serve     # environment (wins over everything else)
uv run lesvi serve --token …         # flag
# or in ~/.config/lesvi/config.toml:
auth_token = "…"
```

With a token set, requests that did not come directly from a local process must
authenticate:

- **Browser**: visit `/login` and enter the token; lesvi sets the
  `lesvi_session` cookie (HttpOnly, SameSite=Lax, `Secure` behind
  `X-Forwarded-Proto: https`, 30-day HMAC-signed value) and sends you home.
  `/logout` forgets it. Changing the token invalidates every session.
- **Scripts**: `Authorization: Bearer <token>` works without logging in:
  `curl -H "Authorization: Bearer $LESVI_TOKEN" https://lesvi.example.com/api/index.json`.
- **Local**: direct loopback requests skip auth (`allow_localhost = true`,
  default). `cloudflared` also reaches the origin over loopback, so a request
  counts as local only when the peer is loopback **and** it carries no
  `CF-Connecting-IP`/`X-Forwarded-For` (ADR-0008) — tunneled traffic always
  logs in. Set `allow_localhost = false` to require a credential even locally
  (this needs a token; `serve` refuses to start without one).

Exempt paths never need auth: `/login`, `/logout`, `/healthz` (a small
`{"ok": true, "shelves": N, "artifacts": M}` JSON), `/manifest.webmanifest`,
`/sw.js`, `/assets/*` and `/favicon.ico`. Unauthenticated API requests get
`401`; pages redirect to `/login`.

With **no token and a non-loopback bind**, `serve` refuses to start. To accept
that risk anyway (the library would be public), pass `--insecure`; lesvi prints
a loud warning and says so in the banner. A loopback bind needs no token.

Raw artifacts keep their byte-for-byte guarantee and run in a browser sandbox
(opaque origin) so their JavaScript cannot read the authenticated API
(ADR-0010). Because a sandboxed document sends no cookie on its subresources,
dashboard cards link to **signed** artifact URLs —
`/a/~<expiry>-<hmac>/<shelf>/<path>` — and opening an unsigned document while
logged in redirects you to its signed form. The capability is **shelf-scoped**
and valid for 30 days: anyone you share such a link with can read that shelf's
artifacts and assets without logging in, so treat dashboard and
`/api/index.json` URLs as shareable. Changing the token revokes every
outstanding link and session at once.

## Phone

Open the public URL on the phone, complete the Cloudflare Access login, and
install the app — the manifest plus maskable icons make it a standalone PWA
("Add to Home Screen"). A service worker precaches the app shell and the index
and keeps visited lesson pages: it serves the cached copy first and refreshes
it in the background, so the dashboard and lesson text read on flaky mobile
networks and offline. Because sandboxed documents (ADR-0010) run in an opaque
origin, browsers do not route their CSS/JS/image requests through the service
worker — those still need the network, so an offline lesson can render
unstyled. Requests made before logging in are never cached, and logging out
(or reaching the login page) drops the cache.

`lesvi url` prints the URL to open (the `public_url` when configured, else the
local address), resolving a shelf or one artifact by number, slug substring, or
exact relative path:

```sh
uv run lesvi url                              # the home page
uv run lesvi url data-engg                    # one shelf
uv run lesvi url data-engg 25                 # artifact 0025-…
uv run lesvi url data-engg kafka              # slug substring
uv run lesvi url data-engg lessons/0024-apache-kafka-fundamentals.html
uv run lesvi url data-engg 25 --open          # also open the printed URL here
```

A lookup that matches several artifacts fails and lists the candidates — pass
the `NNNN` or the exact relative path. The Cloudflare click-through and a
phone verification checklist live in
[docs/cloudflare-setup.md](docs/cloudflare-setup.md).

## Enriching artifacts

lesvi fills every card from parsing heuristics first, but agents can override
any field without touching the heuristics. Resolution is per field, first hit
wins: **sidecar JSON > `lesvi:*` meta tags > heuristics** (ADR-0005).

A sidecar sits next to the artifact as `<file>.meta.json`:

```json
{
  "title": "Kafka, end to end",
  "description": "Topics, partitions, offsets and consumer groups",
  "tags": ["Track B", "Kafka"],
  "pin": true
}
```

Or in the artifact's `<head>`, one meta tag per field:

```html
<meta name="lesvi:title" content="Kafka, end to end">
<meta name="lesvi:description" content="Topics, partitions, offsets and consumer groups">
<meta name="lesvi:tags" content="Track B, Kafka">
<meta name="lesvi:pin" content="true">
```

`tags` is a JSON list in a sidecar and a comma-separated string in a meta tag;
either override replaces the whole tag list, and `"tags": []` clears it
entirely. `number` always comes from the `NNNN` filename. `pin` only *seeds* the
server-side pin: once you toggle that card, your choice wins over the
declaration. Pins live in `~/.local/state/lesvi/state.json` (`$LESVI_STATE`) —
never in a shelf folder — and survive restarts. The card's pin toggle writes
them through `POST /api/pin` (JSON only, same-origin; the content type is what
keeps other sites out without a CORS preflight), and Home's pinned section
follows without a restart. Broken sidecars or unusable meta tags are ignored field
by field (logged at debug level), so the artifact keeps indexing.

## Status

```sh
uv run lesvi status   # config, server address, service state, shelves, counts
```

`status` prints where the config lives, the bind address and public URL, the
systemd user service state, and each shelf with its category counts (missing
shelf folders are marked). It never touches a shelf.

## Always-on service

```sh
uv run lesvi service install     # writes ~/.config/systemd/user/lesvi.service
uv run lesvi service status      # active / inactive / failed / not installed
uv run lesvi service uninstall   # stop, disable, remove the unit
```

`install` writes a systemd **user** unit (no sudo) whose `ExecStart` is the
resolved `lesvi serve` invocation for your config, with `Restart=on-failure`.
It only writes the unit — run the two commands it prints to start the service
now and at every login:

```sh
systemctl --user enable --now lesvi
loginctl enable-linger $USER   # keep serving after you log out (needed for phone access)
```

The unit runs the lesvi you invoked. If that was `uvx lesvi`, run
`uv tool install "lesvi[watch]"` first so `ExecStart` names a durable install
instead of a `uv` cache entry that pruning can remove.

Logs land on stderr, which systemd routes to the journal
(`journalctl --user -u lesvi -f`). Run `serve` with `-v` for INFO (requests,
watcher decisions) or `-vv` for DEBUG; the default is warnings only.

The unit starts `lesvi serve` without your shell environment, so a token set
only as `LESVI_TOKEN` does not reach it — put `auth_token` in the config, which
lesvi keeps owner-only.

`serve` fails fast when its port is taken, naming the port and pointing at
`ss -tlnp | grep <port>` to find the culprit.

## Development

```sh
uv run ruff check   # lint
uv run basedpyright # typecheck
uv run pytest       # tests
```

The package version has one source of truth: `__version__` in
`src/lesvi/__init__.py` (hatchling reads it at build time). After bumping it,
run `uv sync --reinstall-package lesvi` so the editable install's metadata
matches before testing.

The domain glossary and settled decisions live in [CONTEXT.md](CONTEXT.md) and
[docs/adr/](docs/adr/).

## License

MIT — see [LICENSE](LICENSE).
