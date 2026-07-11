#!/usr/bin/env python3
"""Generate apple-touch-icon.png matching HTMLDICE Cthulhu theme."""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 180
OUT = Path(__file__).resolve().parent / "assets" / "apple-touch-icon.png"

ABYSS = (3, 8, 6)
DEEP = (16, 31, 24)
GOLD = (201, 162, 39)
GOLD_DIM = (140, 112, 28)
GLOW = (111, 207, 151)
GLOW_SOFT = (61, 143, 98)
BLOOD = (122, 32, 48)
PARCHMENT = (212, 196, 168)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def radial_gradient(size, inner, outer):
    img = Image.new("RGB", (size, size))
    px = img.load()
    c = (size - 1) / 2
    max_r = c * math.sqrt(2)
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c) / max_r
            px[x, y] = lerp(inner, outer, min(1.0, d ** 0.85))
    return img


def draw_icon():
    img = radial_gradient(SIZE, DEEP, ABYSS).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # Nebula glow
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((18, 52, 162, 168), fill=(*GLOW_SOFT, 55))
    gdraw.ellipse((52, 18, 148, 112), fill=(*BLOOD, 28))
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    cx, cy = SIZE // 2, SIZE // 2 + 2

    # Outer gold ring
    draw.ellipse((14, 14, 166, 166), outline=GOLD_DIM, width=2)
    draw.ellipse((20, 20, 160, 160), outline=GOLD, width=3)

    # Inner circle
    draw.ellipse((28, 28, 152, 152), fill=(10, 20, 16, 220), outline=(*GOLD_DIM, 180), width=1)

    # D100 die (diamond / rotated square)
    die_size = 46
    points = []
    for angle_deg in (45, 135, 225, 315):
        rad = math.radians(angle_deg)
        points.append((cx + die_size * math.cos(rad), cy - 6 + die_size * math.sin(rad)))
    draw.polygon(points, fill=(22, 42, 32), outline=GOLD, width=2)

    # Die face lines
    draw.line([points[0], points[2]], fill=(*GOLD_DIM, 200), width=1)
    draw.line([points[1], points[3]], fill=(*GOLD_DIM, 200), width=1)

    # Eldritch eye on die
    eye_cy = cy - 8
    draw.ellipse((cx - 16, eye_cy - 11, cx + 16, eye_cy + 9), fill=GLOW)
    draw.ellipse((cx - 11, eye_cy - 8, cx + 11, eye_cy + 6), fill=(18, 36, 28))
    draw.ellipse((cx - 5, eye_cy - 5, cx + 5, eye_cy + 5), fill=ABYSS)
    draw.ellipse((cx - 2, eye_cy - 3, cx, eye_cy - 1), fill=PARCHMENT)

    # "100" hint — minimal arc text substitute: three gold ticks
    for i, ox in enumerate((-22, 0, 22)):
        draw.rectangle((cx + ox - 2, cy + 24, cx + ox + 2, cy + 30), fill=GOLD if i == 1 else GOLD_DIM)

    # Corner runes (small dots)
    for ox, oy in ((-58, -58), (58, -58), (-58, 58), (58, 58)):
        draw.ellipse((cx + ox - 3, cy + oy - 3, cx + ox + 3, cy + oy + 3), fill=(*GLOW_SOFT, 160))

    # Subtle vignette
    vignette = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse((-8, -8, SIZE + 8, SIZE + 8), outline=(0, 0, 0, 0), fill=(0, 0, 0, 0))
    for i in range(12):
        alpha = int(18 + i * 6)
        pad = i * 3
        vdraw.ellipse((pad, pad, SIZE - pad, SIZE - pad), outline=(0, 0, 0, alpha), width=2)
    vignette = vignette.filter(ImageFilter.GaussianBlur(2))
    img = Image.alpha_composite(img, vignette)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    draw_icon()
