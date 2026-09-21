# ADR-0010: Raw artifacts are sandboxed and served through signed links

- Status: Accepted
- Date: 2026-09-21
- Source: [#9](https://github.com/arthishaxom/lesvi/issues/9), from a review comment on [#5](https://github.com/arthishaxom/lesvi/issues/5)

## Context

`/a/<shelf>/<path>` serves artifacts byte-for-byte (spec §10), so an artifact's
own JavaScript — quiz widgets, mermaid — runs in the lesvi origin. With the
fallback auth layer (ADR-0004), that origin also carries the session cookie and
the authenticated API, so artifact JS could read `/api/index.json`, call
`POST /api/pin`, and reach any future authenticated endpoint. Artifacts are
agent-generated and may embed third-party scripts, so "it is my own HTML" is not
a boundary.

The byte-for-byte mandate forbids rewriting artifacts, and ADR-0004's
single-port, single-hostname shape forbids moving them to a second origin.

Sandboxing alone is not enough. A sandboxed document gets an **opaque origin**,
and the browser sends no cookies on its subresource requests (verified with
headless Chromium: the document was `200`, while its CSS/JS/PNG got
`303 → /login`). Locally the loopback exemption hid this; on the tunnel every
lesson rendered unstyled and scriptless.

## Decision

`/a/` responses carry `Content-Security-Policy: sandbox allow-scripts
allow-popups allow-popups-to-escape-sandbox` and
`Access-Control-Allow-Origin: *`:

- the document runs in an opaque origin: no cookies, no `localStorage`, no
  same-origin `fetch`, so the authenticated API is out of reach;
- scripts still run (quizzes, mermaid), and `allow-popups` +
  `allow-popups-to-escape-sandbox` keep `target="_blank"` links working;
- CORS lets the opaque origin *fetch* artifact bytes (`fetch`, module
  scripts); classic subresources (CSS, images, plain `<script>`) do not need
  it.

Because that origin has no cookie to authenticate with, artifact URLs are
**capabilities**. A signed URL inserts one segment after `/a/`:

```
/a/~<expiry-unix>-<hmac>/<shelf>/<path>
hmac = HMAC-SHA256(token, "lesvi-artifact.<expiry>.<shelf>").hexdigest()[:32]
```

- The capability is **shelf-scoped**, not per-file: a document's relative
  subresources (`../assets/quiz.js`) inherit the `~…` segment and validate for
  the same shelf. TTL is 30 days.
- Dashboards and `/api/index.json` embed signed URLs whenever auth is on;
  without a token there is nothing to sign and URLs stay as they are.
- An unsigned `.html`/`.htm` document opened by a session-authorized browser
  gets `302` to its signed URL, so bookmarks and old caches keep working; local
  and Bearer requests are served as-is (they are not browser navigations).
- Unsigned `/a/` URLs stay behind the normal auth rules (ADR-0004/0008).

## Consequences

- Compromised or prompt-injected artifact JS can no longer read the index,
  toggle pins, or borrow the session; it can still run, render, open links, and
  fetch its own shelf's artifacts and assets.
- **Sharing semantics**: a signed URL is a bearer capability. Anyone who holds
  one — a shared dashboard link, a chat paste, a browser history sync — can
  read every artifact and asset in that shelf, without logging in, until it
  expires (30 days). Rotating the token revokes every outstanding link and
  session at once.
- A shelf-scoped stamp means a stamp leaked for one lesson exposes the shelf,
  not just that lesson. That is the price of subresource inheritance; per-file
  stamps cannot survive relative asset paths.
- The `~` prefix is reserved for capability segments: a shelf whose name
  starts with `~` cannot be addressed by its unsigned URL (the first segment
  is always read as a capability). A signed URL still reaches it, because only
  the leading segment is stripped. `lesvi add` slugifies names, so this only
  arises in hand-edited configs.
- Sandboxed documents lose `document.cookie`, `localStorage`, form submission
  and downloads inside artifacts; none of today's lessons use them.
- CSP is sent on `200` responses; `304` responses repeat the CORS header but
  not the CSP — the browser reuses the stored response's policy.
- `Access-Control-Allow-Origin: *` on `/a/` responses is safe: the API and
  dashboards deliberately send no CORS headers, and credentialed cross-origin
  reads cannot use `*`.
- Verified by tests: stamp validity (expiry, tamper, wrong shelf, wrong token),
  signed pages and API payloads, cookie-less subresource `200`s, cookie-less
  unsigned `303`s, the unsigned-document redirect, and path-safety with a stamp
  in the URL.

## Alternatives considered

- **Content rewriting** (strip/neutralize scripts) — forbidden by the
  byte-for-byte mandate and would break quizzes.
- **Separate origin/hostname for artifacts** — strongest isolation, but
  reintroduces the second port/hostname ADR-0004 removed.
- **Do nothing** — leaves artifact JS with the session; the whole point of the
  fallback layer is that a leaked tunnel URL is not enough.
- **Sandbox without signed links** — the bug this ADR now records: opaque
  origins send no cookie, so subresources redirect to `/login`.
- **Per-file stamps** — relative subresources cannot carry a file-specific
  credential, so assets would still be unreachable.
- **Capability in a query string** — relative URL resolution drops query
  strings, so subresources would not inherit it.
- **`sandbox allow-scripts allow-same-origin`** — `allow-same-origin` undoes
  the opaque origin and with it every protection.
