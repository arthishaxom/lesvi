/* lesvi — progressive enhancement only: every page reads without JavaScript.
   This adds the theme toggle, client-side search, pin toggles and keeps
   relative timestamps fresh. */

(() => {
  "use strict";

  //: How long typing must pause before the query is applied and persisted.
  const SEARCH_DEBOUNCE_MS = 100;

  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const storedTheme = () => {
    let value = null;
    try {
      value = localStorage.getItem("lesvi-theme");
    } catch (error) {
      value = null;
    }
    return value === "dark" || value === "light" ? value : null;
  };
  const systemTheme = () => (media.matches ? "dark" : "light");
  const activeTheme = () => storedTheme() || systemTheme();

  const toggle = document.querySelector(".theme-toggle");
  if (toggle) {
    const paint = (theme) => {
      const explicit = storedTheme();
      if (explicit) {
        root.dataset.theme = explicit;
      } else {
        delete root.dataset.theme; // no choice saved: keep following the system
      }
      toggle.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    };
    paint(activeTheme());
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      const next = activeTheme() === "dark" ? "light" : "dark";
      try {
        localStorage.setItem("lesvi-theme", next);
      } catch (error) {
        /* private mode: the choice just does not outlive the tab */
      }
      paint(next);
    });
    media.addEventListener("change", () => paint(activeTheme()));
  }

  const relative = (moment) => {
    const seconds = Math.max(0, (Date.now() - moment.getTime()) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    if (seconds < 604800) return Math.floor(seconds / 86400) + "d ago";
    if (seconds < 2592000) return Math.floor(seconds / 604800) + "w ago";
    if (seconds < 31536000) return Math.floor(seconds / 2592000) + "mo ago";
    return Math.floor(seconds / 31536000) + "y ago";
  };

  const refreshTimes = () => {
    for (const time of document.querySelectorAll("time.card-time[datetime]")) {
      const moment = new Date(time.getAttribute("datetime"));
      if (!Number.isNaN(moment.getTime())) {
        time.textContent = relative(moment);
      }
    }
  };
  refreshTimes();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshTimes();
  });

  /* Search: instant, client-side over title/description/tags. The box is
     rendered hidden so pages still read without JavaScript. */

  const normalize = (value) =>
    value
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "") // "café" matches "cafe"
      .replace(/\s+/g, " ")
      .trim();

  const searchForm = document.querySelector(".search");
  const searchInput = document.querySelector(".search-input");
  const searchCount = document.querySelector(".search-count");
  const noResults = document.querySelector(".no-results");
  const sections = Array.from(document.querySelectorAll("main section"));
  const cards = Array.from(document.querySelectorAll(".card-item")).map(
    (item) => ({
      item,
      text: normalize(item.querySelector(".card")?.dataset.search || ""),
    }),
  );

  const applyQuery = (query) => {
    const tokens = normalize(query).split(" ").filter(Boolean);
    let visible = 0;
    for (const card of cards) {
      const matches = tokens.every((token) => card.text.includes(token));
      card.item.hidden = !matches;
      if (matches) visible += 1;
    }
    for (const section of sections) {
      if (!section.querySelector(".card-item")) continue; // guidance, not cards
      section.hidden = !section.querySelector(".card-item:not([hidden])");
    }
    // "Show more" stays on offer while searching: the feed holds at most one
    // page, and the next page may hold the match that is missing here.
    if (noResults) noResults.hidden = tokens.length === 0 || visible > 0;
    if (searchCount) {
      searchCount.textContent = tokens.length
        ? visible === 1
          ? "1 result"
          : `${visible} results`
        : "";
    }
  };

  // Keep the query on same-page controls (sort links, "Show more"), so a
  // click there does not discard the filtered view.
  const syncSamePageLinks = (query) => {
    for (const link of document.querySelectorAll(".sort-link, .show-more a")) {
      const href = link.getAttribute("href");
      if (!href) continue;
      const url = new URL(href, window.location.href);
      if (query) {
        url.searchParams.set("q", query);
      } else {
        url.searchParams.delete("q");
      }
      link.setAttribute("href", url.pathname + url.search);
    }
  };

  const syncQuery = (query) => {
    const url = new URL(window.location.href);
    if (query) {
      url.searchParams.set("q", query);
    } else {
      url.searchParams.delete("q");
    }
    history.replaceState(null, "", url);
  };

  if (searchForm && searchInput) {
    searchForm.hidden = false;
    const initial = new URLSearchParams(window.location.search).get("q") || "";
    searchInput.value = initial;
    applyQuery(initial);
    syncSamePageLinks(initial);
    searchForm.addEventListener("submit", (event) => {
      event.preventDefault(); // the query is already applied; keep sort/limit
      applyQuery(searchInput.value);
      syncQuery(searchInput.value);
      syncSamePageLinks(searchInput.value);
    });
    let timer = 0;
    searchInput.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        applyQuery(searchInput.value);
        syncQuery(searchInput.value);
        syncSamePageLinks(searchInput.value);
      }, SEARCH_DEBOUNCE_MS);
    });
  }

  /* Pins: optimistic toggle, persisted by POST /api/pin, reverted on error. */

  const pinStatus = document.getElementById("pin-status");
  const announce = (message) => {
    if (pinStatus) pinStatus.textContent = message;
  };

  for (const button of document.querySelectorAll(".pin-toggle")) {
    button.hidden = false;
    button.addEventListener("click", async () => {
      const wasPressed = button.getAttribute("aria-pressed") === "true";
      const next = !wasPressed;
      const label = button.getAttribute("aria-label") || "";
      const title = label.replace(/^Unpin |^Pin /, "");
      const glyph = button.querySelector(".pin-glyph");
      const paint = (pressed) => {
        button.setAttribute("aria-pressed", String(pressed));
        button.setAttribute("aria-label", `${pressed ? "Unpin" : "Pin"} ${title}`);
        if (glyph) glyph.textContent = pressed ? "★" : "☆";
      };
      paint(next); // optimistic: the card reacts before the request lands
      button.disabled = true;
      try {
        const response = await fetch("/api/pin", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            shelf: button.dataset.shelf,
            path: button.dataset.path,
            pinned: next,
          }),
        });
        if (response.status === 401) {
          window.location.assign("/login"); // the session expired mid-page
          return;
        }
        if (!response.ok) throw new Error(`pin failed: ${response.status}`);
        announce(`${next ? "Pinned" : "Unpinned"} ${title}`);
      } catch (error) {
        paint(wasPressed);
        announce("Could not update the pin. Try again.");
      } finally {
        button.disabled = false;
      }
    });
  }
})();
