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
shelf pages, the agent metadata overrides (sidecar + `lesvi:*` meta tags), and
live index updates (native events or mtime polling) are in place.

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
virtual dashboards (`/` recency feed, `/s/<shelf>/` curriculum order with a
`?sort=recent` toggle; Research stays hidden in v1), and serves the index at
`/api/index.json` (`Cache-Control: no-store`, gzipped when the client accepts
it). Each indexed artifact and its relative assets are served byte-for-byte at
`/a/<shelf>/<path>` with `ETag`/`Last-Modified` revalidation. The UI assets
(`/assets/app.css`, `/assets/app.js`) add the persisted theme toggle; pages
read fine with JavaScript disabled. Search, the pin toggle and auth land with
their feature tickets.

The index keeps itself current while `serve` runs: writing, overwriting,
renaming or deleting a lesson (or its `.meta.json` sidecar) updates the feed
within one debounce tick (~300 ms) — no restart. With the `watch` extra, native
filesystem events do the work; otherwise mtimes are polled every `poll_interval`
seconds (default 2; `watch = false` or `--no-watch` disables watching, and
`--poll-interval S` forces polling). Ignored paths stay ignored, bursts are
debounced, and a chunked write is parsed once it settles.

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
never in a shelf folder. Broken sidecars or unusable meta tags are ignored field
by field (logged at debug level), so the artifact keeps indexing.

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
