# ADR-0002: Watch shelves in place — no copies, no version history in v1

- Status: Accepted
- Date: 2026-09-15
- Source: interview round 2

## Context

Agents already write lessons directly into their project/subject folders (`~/Learning/data-engg/lessons/...`, `~/Dev/kiittime/lessons/...`). A central "publish store" would require agents to copy files or change their workflow, creating drift between where agents work and where the library lives. Version history was raised by research as a possible enhancement but was not requested.

## Decision

- Register shelf roots; the server watches, indexes, and serves files **at their original paths**.
- `lesvi add <path>` writes the shelf to config; no file is ever copied or moved.
- Re-running a lesson overwrites the file in place; the card's recency timestamp updates. No version history in v1.
- URLs are derived from the shelf name + relative path, so they stay stable as long as the file stays put.

## Consequences

- Zero workflow change for agents and for the user's existing folders.
- The server holds only derived state (in-memory index + pins), never content.
- Deleting or renaming a file is picked up by the watcher; a rename changes the URL (accepted).
- Version history can be added later as an optional snapshot layer keyed by the same shelf/path slugs, without a URL redesign.
- Files live only where the user keeps them; backups are the user's existing file workflow, not lesvi's job.

## Alternatives considered

- **Central publish store** — stable store URLs and natural history, but agents must copy, and the store duplicates content.
- **Git-based history** — requires each shelf to be a repo; not true today.
- **Copy-on-first-index** — worst of both: duplicates content and still tracks originals.
