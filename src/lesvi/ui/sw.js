/* lesvi service worker — the app shell and visited lesson documents, readable
   offline.

   Dashboard renders mint a fresh capability stamp into every /a/ URL
   (/a/~<expiry>-<hmac>/<shelf>/<path>), so artifact cache keys are normalised
   by stripping the stamp: one lesson is one cache entry no matter which render
   produced the link. Auth redirects and error responses are never cached, and
   reaching the login page (or logging out) drops the cache, so cached bytes
   do not outlive the session on this device.

   Sandboxed artifact documents run in an opaque origin (ADR-0010), and
   Chromium does not dispatch their subresource requests to a service worker,
   so this caches lesson HTML — not the lessons' own CSS/JS/images. Offline, a
   cached lesson renders unstyled; the app shell and index are fully cached. */

const CACHE = "lesvi-v1";
const STAMP = /^\/a\/~[0-9]{1,10}-[0-9a-f]{32}\//;

//: Precache the shell and the index; runtime fetches fill any gaps (logged
//: out, offline at install).
const SHELL = [
  "/",
  "/assets/app.css",
  "/assets/app.js",
  "/manifest.webmanifest",
  "/assets/icon-192.png",
  "/assets/icon-512.png",
  "/api/index.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      await Promise.all(
        SHELL.map(async (url) => {
          try {
            const response = await fetch(url, { credentials: "same-origin" });
            await putIfCacheable(cache, url, response);
          } catch (error) {
            /* offline or signed out: the runtime fetch fills this in later */
          }
        })
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name !== CACHE) await caches.delete(name);
      }
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname === "/logout") {
    // Give up the session, give up the cached bytes.
    event.waitUntil(caches.delete(CACHE));
    return;
  }
  if (url.pathname.startsWith("/a/")) {
    // Visited lessons: cached copy first, refreshed in the background so a
    // republished lesson lands on the next visit.
    event.respondWith(cacheFirst(event, request, cacheKey(request.url)));
    return;
  }
  if (request.mode === "navigate" || SHELL.includes(url.pathname)) {
    // Shell and index: serve the cached copy now, revalidate in the
    // background (stale-while-revalidate).
    event.respondWith(staleWhileRevalidate(event, request));
  }
});

/** The cache key for a URL: the capability stamp stripped, query included. */
function cacheKey(href) {
  const url = new URL(href);
  url.pathname = url.pathname.replace(STAMP, "/a/");
  return url.href;
}

/** Serve a cached artifact immediately, refreshing it in the background. */
async function cacheFirst(event, request, key) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(key);
  if (cached) {
    event.waitUntil(refresh(cache, key, request));
    return cached;
  }
  const response = await fetch(request);
  await store(cache, key, response); // best-effort: never fail the fetch for it
  return response;
}

/** Serve the cached shell immediately, revalidating it in the background. */
async function staleWhileRevalidate(event, request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) {
    event.waitUntil(refresh(cache, request, request));
    return cached;
  }
  try {
    const response = await fetch(request);
    await store(cache, request, response);
    return response;
  } catch (error) {
    if (request.mode === "navigate") {
      // An unvisited deep link offline: the shell is better than an error page.
      const home = await cache.match("/");
      if (home) return home;
    }
    throw error;
  }
}

/** Store best-effort: a full cache must not break a successful fetch. */
async function store(cache, key, response) {
  try {
    await putIfCacheable(cache, key, response);
  } catch (error) {
    /* quota, or a body that cannot be cloned: serve the response anyway */
  }
}

/** Store a response unless it is a login redirect or an error. */
async function putIfCacheable(cache, key, response) {
  if (new URL(response.url).pathname === "/login") {
    // The session expired or the token rotated: drop this device's copy.
    await caches.delete(CACHE);
    return;
  }
  // Only complete, direct 200s: never opaque (status 0) or partial (206).
  if (response.status !== 200 || response.redirected) return;
  const headers = new Headers(response.headers);
  // fetch() hands back a decoded body; keeping the header invites a second
  // decode when the cached copy is served.
  headers.delete("Content-Encoding");
  headers.delete("Content-Length");
  const body = response.clone().body;
  await cache.put(
    key,
    new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    })
  );
}

/** Refresh a cached entry in the background; failures are invisible. */
function refresh(cache, key, request) {
  return fetch(request)
    .then((response) => putIfCacheable(cache, key, response))
    .catch(() => {});
}
