# ADR-0009: tomlkit for hand-editable config

- Status: Accepted
- Date: 2026-09-20
- Source: #3 implementation review; amends the "zero runtime dependencies" bullet in ADR-0006

## Context

ADR-0006 settled on stdlib-first with zero runtime dependencies, and spec §6 makes `~/.config/lesvi/config.toml` "hand-editable at all times". Shelf registration (#3) needs to persist changes there. `tomllib` parses TOML but the stdlib has no writer (the `tomli` project's writer is a separate library). The first implementation hand-rolled one: it preserved unknown keys, but every `lesvi add` / `lesvi remove` re-serialized the whole file, destroying user comments, key order, and formatting — and the writer itself needed escaping care (review found a comment-injection bug and an unescaped `DEL`).

## Decision

Use [tomlkit](https://github.com/python-poetry/tomlkit) to read **and** edit the config file. Writes go through its document model, so comments, key order, whitespace, and inline tables survive `lesvi add` / `lesvi remove`. tomlkit is pure Python with no transitive dependencies and becomes lesvi's first runtime dependency; `watchfiles` stays the optional watch extra.

## Consequences

- Hand edits survive every lesvi command; the config stays the single source of truth.
- ~90 lines of homegrown writer deleted; less escaping surface to get wrong.
- `uvx lesvi` installs one extra small pure-Python package.
- Spec §5's "Runtime deps: none" is superseded for the config module; the rest of the app stays stdlib.

## Alternatives considered

- **Keep the hand-rolled writer** — zero deps, but silently destroys comments and formatting, and needs ongoing escaping care.
- **tomli-w** — small and stable, but it serializes dicts just like our writer did and still drops comments; not worth a dependency.
- **JSON or INI config** — avoids the problem differently but TOML is already specified and is the friendlier hand-editing format.
