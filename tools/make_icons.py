"""Generate lesvi's PWA icons and favicon — stdlib only, deterministic.

Run from the repo root and commit the output:

    uv run python tools/make_icons.py

The mark is three white "card" bars on the accent colour, echoing the feed.
Maskable icons fill the whole canvas and keep the glyph inside the 80% safe
zone; standard icons are a rounded square with transparent corners.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent / "src" / "lesvi" / "ui"

ACCENT = (11, 92, 173)  # --accent light, also the manifest theme_color
INK = (255, 255, 255)
#: Samples per axis per output pixel; smooths the rounded corners.
SUPERSAMPLE = 3
#: (centre x, centre y, half width, half height) of each card bar.
BARS = (
    (0.50, 0.355, 0.200, 0.045),
    (0.50, 0.500, 0.260, 0.045),
    (0.50, 0.645, 0.220, 0.045),
)
#: Icon corner radius as a fraction of the side.
RADIUS = 0.22
#: Maskable icons must keep their glyph inside the centre 80% circle.
SAFE_RADIUS = 0.40


def _rounded_rect(
    x: float, y: float, cx: float, cy: float, half_w: float, half_h: float, radius: float
) -> float:
    """Signed distance to a rounded rectangle; negative inside."""
    dx = abs(x - cx) - (half_w - radius)
    dy = abs(y - cy) - (half_h - radius)
    return (
        math.hypot(max(dx, 0.0), max(dy, 0.0))
        + min(max(dx, dy), 0.0)
        - radius
    )


def _color(x: float, y: float, *, rounded: bool) -> tuple[int, int, int, int]:
    """RGBA at normalised coordinates; transparent outside a rounded square."""
    if rounded and _rounded_rect(x, y, 0.5, 0.5, 0.5, 0.5, RADIUS) > 0:
        return (0, 0, 0, 0)
    for cx, cy, half_w, half_h in BARS:
        if _rounded_rect(x, y, cx, cy, half_w, half_h, half_h) < 0:
            return (*INK, 255)
    return (*ACCENT, 255)


def render(size: int, *, rounded: bool) -> bytes:
    """RGBA bytes for a *size* × *size* icon, supersampled for smooth edges."""
    step = 1.0 / (size * SUPERSAMPLE)
    samples = SUPERSAMPLE * SUPERSAMPLE
    rgba = bytearray()
    for py in range(size):
        for px in range(size):
            red = green = blue = alpha = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx + 0.5) * step
                    y = (py * SUPERSAMPLE + sy + 0.5) * step
                    r, g, b, a = _color(x, y, rounded=rounded)
                    red += r * a
                    green += g * a
                    blue += b * a
                    alpha += a
            if alpha:
                rgba += bytes(
                    (
                        round(red / alpha),
                        round(green / alpha),
                        round(blue / alpha),
                        round(alpha / samples),
                    )
                )
            else:
                rgba += b"\x00\x00\x00\x00"
    return bytes(rgba)


def png(size: int, *, rounded: bool) -> bytes:
    """A valid RGBA PNG of the rendered icon."""
    rgba = render(size, rounded=rounded)
    stride = size * 4
    raw = b"".join(
        b"\x00" + rgba[row * stride : (row + 1) * stride] for row in range(size)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def ico(png_bytes: bytes, size: int) -> bytes:
    """A single-image ICO whose payload is a PNG (supported everywhere current)."""
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack(
        "<BBBBHHII",
        size if size < 256 else 0,
        size if size < 256 else 0,
        0,
        0,
        1,
        32,
        len(png_bytes),
        len(header) + 16,
    )
    return header + entry + png_bytes


def main() -> None:
    _check_safe_zone()
    icons = {
        "icon-192.png": png(192, rounded=True),
        "icon-512.png": png(512, rounded=True),
        "icon-maskable-512.png": png(512, rounded=False),
        "favicon.ico": ico(png(48, rounded=True), 48),
    }
    for name, data in icons.items():
        target = UI_DIR / name
        target.write_bytes(data)
        print(f"wrote {target} ({len(data)} bytes)")


def _check_safe_zone() -> None:
    """The glyph must stay inside a maskable icon's centre-80% safe circle."""
    for cx, cy, half_w, half_h in BARS:
        for x in (cx - half_w, cx + half_w):
            for y in (cy - half_h, cy + half_h):
                inside = math.hypot(x - 0.5, y - 0.5) <= SAFE_RADIUS
                assert inside, f"bar corner ({x}, {y}) leaves the safe zone"


if __name__ == "__main__":
    main()
