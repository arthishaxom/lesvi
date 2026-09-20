# ADR-0003: Virtual index — no generated files in shelf folders

- Status: Accepted
- Date: 2026-09-15
- Source: interview rounds 1, 3, 4

## Context

The user's subjects look inconsistent today: `ricing` and `oni` have hand-made `index.html` dashboards; `data-engg` has none. Those dashboards were a self-described "tacky, hacky" workaround for serving a single project. The user wants one home page across all shelves and per-shelf dashboards that always reflect the current state, without file churn.

## Decision

All dashboards are **virtual**:

- `/` — cross-shelf home: pinned section + recency feed.
- `/s/<shelf>/` — shelf page: cards in curriculum order (`NNNN` ascending), toggle to recency.
- Generated per request from the in-memory index in `src/lesvi`, served as HTML.
- Nothing is written into shelf folders; existing hand-made `index.html` files are ignored (the user may delete them at leisure).

## Consequences

- No generated files polluting note/lesson folders; no merge conflicts with agents writing `index.html`.
- Every shelf gets the same dashboard, including ones that never had one.
- The index can never go stale relative to the dashboard.
- Viewing without the server (offline static hosting) is not possible until a future `lesvi export` command materializes the same pages; accepted for v1.
- Server-side rendering means the UI works without JS for reading; JS only powers search/sort/pin interactions.

## Alternatives considered

- **Write `index.html` into each shelf** — offline-friendly, but file churn, overwrites hand-made dashboards, agents could clobber them.
- **Generate a static site** — adds a build step and a second deployment target; overkill for a local tool.
