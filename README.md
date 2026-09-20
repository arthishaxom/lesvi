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
scaffold and shelf registration (`lesvi add` / `list` / `remove`) are in
place.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/)

Zero runtime dependencies.

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

## Development

```sh
uv run ruff check   # lint
uv run mypy         # typecheck (strict)
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
