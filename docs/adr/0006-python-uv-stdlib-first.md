# ADR-0006: Python + uv, stdlib-first

- Status: Accepted
- Date: 2026-09-15
- Source: interview round 1; machine recon (Python 3.14.7 + uv 0.12.3, Node 24 available)

## Context

The tool is a personal utility that must be trivially runnable, agent-hackable, and OSS-friendly. The user already lives in Python for this workflow (their `preview` scripts are stdlib Python). The machine has Python 3.14 + uv and Node 24; no Go/Docker.

## Decision

- Python ≥ 3.11 (developed on 3.14), packaged with `uv`/`pyproject.toml`; entry point `lesvi`; distributable as `uvx lesvi`.
- **Zero runtime dependencies.** `http.server` (ThreadingHTTPServer), `tomllib`, `html.parser`, `json`, `argparse` cover the whole v1 surface.
- `watchfiles` is an **optional extra** for native filesystem events; without it, the server polls mtimes at a configurable interval (default 2s). Correctness never depends on the extra.
- UI is plain HTML/CSS/JS rendered server-side from string templates; no build step, no framework.
- MIT license.

## Consequences

- One-command install/run anywhere with Python; no venv drama for agents that extend it.
- We hand-roll small things a framework would provide (templating, watching, routing), which is affordable at this scale and keeps the surface auditable.
- Node is only needed later for the v2 Quartz vault, not for lesvi itself.
- Polling fallback keeps behavior identical across environments, just less efficient on huge trees.

## Alternatives considered

- **Node + TypeScript** — richer ecosystem (chokidar) and npx distribution, but adds a toolchain for a file-watcher-and-renderer.
- **Go single binary** — best distribution story, but no Go on the machine and a slower iteration loop for the user's agent workflow.
- **Static generator + any server** — too limited: no pin/search/recency without server-side state anyway.
