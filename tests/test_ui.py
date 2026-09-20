"""Tests for the UI assets: theme plumbing and accessibility floors.

There is no browser in the test environment, so the stylesheet is read as the
contract it is: token contrast is computed with the WCAG formula, and the
interactive selectors are checked for the 44 px minimum.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parent.parent / "src" / "lesvi" / "ui"
CSS = (UI_DIR / "app.css").read_text(encoding="utf-8")
JS = (UI_DIR / "app.js").read_text(encoding="utf-8")

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
    [".brand", ".theme-toggle", ".shelf-link", ".sort-link", ".show-more a"],
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
