/* lesvi — progressive enhancement only: every page reads without JavaScript.
   This adds the theme toggle and keeps relative timestamps fresh. */

(() => {
  "use strict";

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
})();
