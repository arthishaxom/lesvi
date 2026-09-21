"""Tests for the UI assets: theme plumbing, accessibility floors, the PWA.

There is no browser in the test environment, so the stylesheet is read as the
contract it is: token contrast is computed with the WCAG formula, and the
interactive selectors are checked for the 44 px minimum. The service worker is
smoke-tested by reading its source — it needs a browser to run; the offline
flows are verified by hand (docs/cloudflare-setup.md §5).
"""

from __future__ import annotations

import json
import math
import re
import struct
import zlib
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parent.parent / "src" / "lesvi" / "ui"
CSS = (UI_DIR / "app.css").read_text(encoding="utf-8")
JS = (UI_DIR / "app.js").read_text(encoding="utf-8")
MANIFEST = json.loads((UI_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
SW = (UI_DIR / "sw.js").read_text(encoding="utf-8")

LIGHT = re.search(r":root\s*\{([^}]*)\}", CSS)
DARK = re.search(r':root\[data-theme="dark"\]\s*\{([^}]*)\}', CSS)
assert LIGHT is not None and DARK is not None

TEXT_PAIRS = (
    ("ink", "bg"),
    ("ink", "surface"),
    ("muted", "surface"),
    ("muted", "chip"),
    ("accent-ink", "accent"),
)


def _token(block: str, name: str) -> str:
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", block)
    assert match is not None, f"--{name} is not defined as a hex colour"
    return match.group(1)


def _rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match is not None, f"no rule for {selector}"
    return match.group(1)


def _luminance(color: str) -> float:
    channels = []
    for start in (1, 3, 5):
        value = int(color[start : start + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize(
    ("theme", "block"),
    [("light", LIGHT.group(1)), ("dark", DARK.group(1))],
)
def test_theme_text_pairs_meet_aa_contrast(theme: str, block: str) -> None:
    for foreground, background in TEXT_PAIRS:
        ratio = _contrast(_token(block, foreground), _token(block, background))
        assert ratio >= 4.5, f"{theme}: {foreground} on {background} = {ratio:.2f}"


@pytest.mark.parametrize(
    "selector",
    [
        ".brand",
        ".theme-toggle",
        ".shelf-link",
        ".sort-link",
        ".show-more a",
        ".search-input",
        ".pin-toggle",
    ],
)
def test_interactive_controls_declare_a_44px_minimum(selector: str) -> None:
    assert "min-height: 44px" in _rule(CSS, selector)


def test_the_stylesheet_follows_the_system_and_both_explicit_themes() -> None:
    assert "@media (prefers-color-scheme: dark)" in CSS
    assert ':root:not([data-theme="light"])' in CSS
    assert ':root[data-theme="dark"]' in CSS
    assert ':root[data-theme="light"]' in CSS
    assert ":focus-visible" in CSS
    assert ".card:focus-within" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS


def test_the_script_persists_the_choice_and_follows_the_system() -> None:
    assert 'localStorage.setItem("lesvi-theme"' in JS
    assert 'localStorage.getItem("lesvi-theme"' in JS
    assert "dataset.theme" in JS
    assert "matchMedia" in JS
    assert 'addEventListener("change"' in JS


def test_the_script_searches_client_side_and_persists_the_query() -> None:
    assert "SEARCH_DEBOUNCE_MS = 100" in JS
    assert "dataset.search" in JS
    assert "URLSearchParams" in JS
    assert "history.replaceState" in JS
    assert "search-count" in JS
    assert '"1 result"' in JS or "1 result" in JS
    assert "preventDefault" in JS  # Enter keeps sort/limit instead of navigating
    assert "syncSamePageLinks" in JS  # sort and "Show more" keep ?q=


def test_the_script_toggles_pins_optimistically_and_posts_them() -> None:
    assert 'fetch("/api/pin"' in JS
    assert '"POST"' in JS
    assert "aria-pressed" in JS
    assert "pinned:" in JS
    assert "pin-status" in JS


def test_the_search_box_is_hidden_until_the_script_takes_over() -> None:
    assert ".search[hidden]" in CSS
    assert "display: none" in _rule(CSS, ".search[hidden]")


def test_the_script_registers_the_service_worker() -> None:
    assert 'serviceWorker.register("/sw.js")' in JS
    assert "isSecureContext" in JS


def test_the_manifest_installs_as_a_standalone_app() -> None:
    assert MANIFEST["name"] == "lesvi"
    assert MANIFEST["short_name"] == "lesvi"
    assert MANIFEST["display"] == "standalone"
    assert MANIFEST["start_url"] == "/"
    assert MANIFEST["scope"] == "/"
    assert MANIFEST["background_color"].startswith("#")
    assert MANIFEST["theme_color"].startswith("#")
    for icon in MANIFEST["icons"]:
        assert icon["type"] == "image/png"
        assert (UI_DIR / icon["src"].rsplit("/", 1)[-1]).is_file()
    sizes = {icon["sizes"] for icon in MANIFEST["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any("maskable" in icon.get("purpose", "") for icon in MANIFEST["icons"])


def test_the_service_worker_precaches_the_shell_and_index_json() -> None:
    for entry in (
        '"/"',
        '"/assets/app.css"',
        '"/assets/app.js"',
        '"/manifest.webmanifest"',
        '"/api/index.json"',
    ):
        assert entry in SW, entry
    assert 'addEventListener("install"' in SW
    assert "skipWaiting" in SW


def test_the_service_worker_serves_the_shell_stale_while_revalidating() -> None:
    assert "staleWhileRevalidate" in SW
    assert "cache.put" in SW
    assert "cache.match" in SW


def test_the_service_worker_caches_visited_artifacts_first() -> None:
    assert 'startsWith("/a/")' in SW
    assert "cacheFirst" in SW
    # A refresh that fails (offline, expired) must not disturb the cached copy.
    assert ".catch(" in SW


def test_the_service_worker_normalises_capability_stamps() -> None:
    # Dashboard renders mint a fresh ~<expiry>-<hmac> segment per visit; the
    # cache key must not, or every visit would miss cache and bloat it.
    assert "~[0-9]{1,10}-[0-9a-f]{32}" in SW


def test_the_service_worker_drops_stale_caches_on_activate() -> None:
    assert 'addEventListener("activate"' in SW
    assert "caches.delete" in SW
    assert "clients.claim" in SW


def test_the_service_worker_drops_cached_bytes_when_the_session_ends() -> None:
    assert '"/logout"' in SW
    assert '"/login"' in SW
    assert "caches.delete(CACHE)" in SW


def _decode_png(name: str) -> tuple[int, bytes]:
    """Width and RGBA rows for a non-interlaced 8-bit RGBA PNG we generated."""
    data = (UI_DIR / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    idat = b""
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        if kind == b"IDAT":
            idat += data[offset + 8 : offset + 8 + length]
        offset += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4
    rows = []
    for row in range(height):
        start = row * (stride + 1)
        assert raw[start] == 0, "the generator writes filter-0 rows"
        rows.append(raw[start + 1 : start + 1 + stride])
    return width, b"".join(rows)


def _pixel(rgba: bytes, size: int, x: int, y: int) -> tuple[int, int, int, int]:
    at = (y * size + x) * 4
    red, green, blue, alpha = rgba[at : at + 4]
    return red, green, blue, alpha


def test_the_maskable_icon_fills_the_canvas_and_keeps_its_glyph_safe() -> None:
    size, rgba = _decode_png("icon-maskable-512.png")
    assert size == 512
    # Full bleed: every corner stays opaque when the platform crops to a shape.
    for x, y in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
        assert _pixel(rgba, size, x, y)[3] == 255, (x, y)
    # The glyph stays inside the 80% safe circle, or a mask would clip it.
    for y in range(size):
        for x in range(size):
            if _pixel(rgba, size, x, y)[:3] != (255, 255, 255):
                continue
            dx = (x + 0.5) / size - 0.5
            dy = (y + 0.5) / size - 0.5
            assert math.hypot(dx, dy) <= 0.40, (x, y)


def test_the_standard_icon_has_rounded_transparent_corners() -> None:
    size, rgba = _decode_png("icon-192.png")

    assert _pixel(rgba, size, 0, 0)[3] == 0
    assert _pixel(rgba, size, size // 2, size // 2)[3] == 255
