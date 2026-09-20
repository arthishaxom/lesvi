# ADR-0007: Convention preset + per-shelf config overrides

- Status: Accepted
- Date: 2026-09-15
- Source: interview rounds 3–4

## Context

The user asked directly: "should we give the ability to put our own convention? Like a default else provide a file or something config?" Shelf layouts already differ (`data-engg/reference/research/` vs `ricing/research/`), and as an OSS-friendly project lesvi can't assume one person's folder names. At the same time, the user's existing layout must work with zero setup.

## Decision

- Ship a **built-in preset**:
  - `Lessons` → `lessons/*.html`
  - `Reference` → `reference/**/*.html`
  - `Research` → `research/**/*.md`, `reference/research/**/*.md` (indexed but hidden until v2 markdown rendering)
  - ignore: `learning-records/**`, `assets/**`, `index.html`, dotfiles, `node_modules/**`
- Config is `~/.config/lesvi/config.toml` (XDG), the single source of truth, hand-editable at all times:
  - global keys: `port`, `host`, `public_url`, `watch`, `poll_interval`, `auth_token`;
  - `[shelves.<name>]` map entries: `path`, `title`, optional `[shelves.<name>.categories]` glob overrides (string or list) and `ignore` extensions.
- `lesvi add <path>` performs **smart detection**: if the path itself matches the preset it becomes one shelf; otherwise each child that matches becomes its own shelf. `--single` / `--name` override. `lesvi remove` deletes the entry. The CLI always re-reads/writes the same TOML file — no hidden database.

## Consequences

- Works out of the box on `~/Learning/*` and on project `lessons/` folders; arbitrary layouts are supported by editing config.
- Users can see and version their setup; agents can edit it too (it's just TOML).
- Config and CLI can't drift (single writer path, same format).
- Categories are labels, not URLs — adding one later doesn't break links.

## Alternatives considered

- **Convention only (no config)** — fails the OSS/genericity goal.
- **Config only (no preset)** — every user starts from a blank file; bad first-run experience.
- **Marker files per project** — clever, but scatters config across machines/folders and complicates "what is registered?".
- **Auto-discovery of a fixed parent** — too magical; explicit `add` keeps intent visible.
