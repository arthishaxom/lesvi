# ADR-0005: Metadata precedence — sidecar > meta tag > heuristics

- Status: Accepted
- Date: 2026-09-15
- Source: interview round 4

## Context

Existing lessons carry no structured metadata. What exists: a `<title>` ("Lesson 24 — Apache Kafka Fundamentals"), a leading `.subtitle`/`<p>`, a numbered `NNNN-slug.html` filename, an in-page tag span (`Lesson 24 · Track B · Processing Paradigms`), and file mtime. The user wants auto-extraction to work on day one, plus a channel agents can write to when they know better, and ideally a future skill generator that teaches agents the convention.

## Decision

Metadata resolution order (first hit wins, field by field):

1. **Sidecar** `<file>.meta.json` — `{ "title", "description", "tags": [], "pin": true }`; works for any artifact type, ignored by indexing itself.
2. **In-HTML meta tags** — `<meta name="lesvi:title|description|tags|pin" content="...">`; one file, trivial for agents to emit.
3. **Heuristics** — title: `<title>`, stripping a leading `Lesson N — ` into the number badge; number: filename prefix `^(\d+)[-_]`; description: leading `.subtitle`/first `<p>` (truncated); tags: category name + tokens from the first `.lesson-tag`/`.badge` element (split on `·`, drop "Lesson N" and duration tokens); date: file mtime; category: matching glob.

Config controls globs, category labels, and parse toggles per shelf — not per-artifact metadata.

## Consequences

- Zero-workflow-change adoption: current lessons index correctly without edits.
- Agents get a deterministic, documented way to enrich (meta tag or sidecar), and a future `lesvi init-skill` can generate that convention into a project.
- Precedence is simple enough to explain in one line and to test exhaustively.
- Heuristic parsing must stay stdlib-only (`html.parser`) and defensive; a malformed lesson must degrade to filename-derived metadata, never crash the indexer.

## Alternatives considered

- **YAML frontmatter in HTML comments** — exotic for HTML artifacts and easy to break.
- **A single index file per shelf** — a second source of truth agents would forget to update.
- **Skill-only convention** — breaks all existing lessons until re-authored.
- **Database** — unnecessary state for a few hundred files; in-memory index + JSON state is enough.
