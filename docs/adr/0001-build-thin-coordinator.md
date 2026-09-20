# ADR-0001: Build a purpose-built coordinator instead of adopting an existing server

- Status: Accepted
- Date: 2026-09-15
- Source: interview rounds 1–2; [research survey](../research/existing-tools.md)

## Context

The workflow: AI agents generate HTML lessons into subject/project folders (`~/Learning/*`, project `lessons/` dirs). Today each subject has a `preview` script that runs `python -m http.server` on port 8080, and the user adds a separate tunnel route per port. Multiple agents collide on the port, the tunnel routes multiply, listings are bare, and nothing aggregates the folders.

A survey of 19+ tools (Caddy `file_server`, FileBrowser Quantum, SFTPGo, miniserve, dufs, static-web-server, nginx, launchpad, homepage/dashy, and others) found:

- No tool is artifact-first. Every candidate is folder-first (directory trees), has no project-grouped card index, tags, pinning, or recency feed.
- FileBrowser Quantum is closest (multi-source, indexed search) but is a general file manager; adopting it still requires a custom index layer.
- No agent-artifact gallery exists; the nearest is a 2-star MVP (`occ-artifact-gallery`).
- Quartz is the right tool for the v2 Obsidian vault, not for artifacts.

## Decision

Build `lesvi`, a thin Python coordinator that owns one port, watches registered shelves, and renders an artifact-first mobile index. Borrow *ideas* only:

- miniserve — QR-to-phone affordance (later);
- FileBrowser Quantum — multi-source config, indexed search;
- claude-code-viewer — reverse-proxy base path, PWA installability;
- occ-artifact-gallery — a publish/register primitive with stable URLs.

Reuse Quartz as a build target for the v2 vault.

## Consequences

- Full control of the artifact-first UX the user actually asked for.
- We own the watcher, index, path safety, and auth fallback — a small, testable surface.
- No upstream forks to maintain, no license surprises (MIT for ours).
- The serving substrate (static file serving) is the easy part; being "thin" is only possible because auth and TLS live at Cloudflare.

## Alternatives considered

- **Adopt FileBrowser Quantum** — heaviest feature overlap, wrong UX axis; still needs custom index code.
- **Caddy multi-root + index generator** — Caddy handles auth + multi-root, but dynamic features (pin, search, recency, metadata) still need the custom layer.
- **SFTPGo** — transfer/admin oriented, AGPL + proprietary theme terms.
- **Static site generator (Quartz for everything)** — wrong shape for ad-hoc agent artifacts; rebuild-on-write churn.
