#!/usr/bin/env python3
"""Generate the Sac Valley Cams brand assets (deterministic, no external deps
beyond Pillow). Geometry lives on a 64-unit grid; small renderings drop
details that would smear at 16 px.

Outputs into web/:
  logo.svg            - scalable master (used in page header + modern favicons)
  favicon.ico         - 16/32/48 px raster (classic browsers)
  apple-touch-icon.png- 180 px (iOS, with the "big" details enabled)
  logo.png            - 512 px (README / og / marketing)

Design: a camera aperture (lens) looking down a perspective road; the red car
sits at the vanishing point. Palette follows the site (dark slate + steel-blue
accent + the site's soft red).
"""
from __future__ import annotations
from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parents[1] / "web"

BG = "#141b24"          # rounded badge background
RING = "#8fa2bd"        # steel-blue accent (matches site)
LENS = "#1b2431"        # inside-the-lens field
ROAD = "#49576e"        # asphalt seen through the lens (big renders)
ROAD_SMALL = "#596880"  # brighter asphalt so the road survives at 16 px
CAR = "#e28c8c"         # site accent red
LINE = "#dfe6ef"        # dash marks
GOLD = "#d9a05f"        # valley horizon glow (only for big renders)

# 64-grid coordinates (same numbers as the SVG master below)
GRID = 64
C = (32, 32)
RING_R = 21
RING_W = 6
# perspective road: wide at the lens bottom edge, vanishing point at center
ROAD_POLY = [(26, 52), (38, 52), (32.4, 33), (31.6, 33)]
CAR_RECT = (29.9, 43.4, 34.1, 49.6)          # top-down car on the road (big renders)
CAR_DOT = (32, 44)                            # small renders: bold red dot
DASHES = [(31.9, 35.6, 32.1, 37.2), (31.9, 39.2, 32.1, 40.8),
          (31.9, 42.8, 32.1, 44.4), (31.9, 46.4, 32.1, 48.0)]
HORIZON = (15.0, 20.2, 49.0, 22.4)           # sunset arc inside the lens (big)

SVG_MASTER = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="1" y="1" width="62" height="62" rx="14" fill="{bg}"/>
  <circle cx="32" cy="32" r="{ring}" fill="{lens}" stroke="{ring_c}" stroke-width="{w}"/>
  <ellipse cx="32" cy="21.3" rx="17" ry="1.1" fill="{gold}"/>
  <polygon points="26,52 38,52 32.4,33 31.6,33" fill="{road}"/>
  <g fill="{line}" opacity="0.8">
    <rect x="31.95" y="35.6" width="0.22" height="1.6"/>
    <rect x="31.95" y="39.2" width="0.22" height="1.6"/>
    <rect x="31.95" y="42.8" width="0.22" height="1.6"/>
  </g>
  <rect x="29.9" y="43.4" width="4.2" height="6.2" rx="1.4" fill="{car}"/>
</svg>
""".format(bg=BG, ring=RING_R, lens=LENS, ring_c=RING, w=RING_W,
           road=ROAD, gold=GOLD, line=LINE, car=CAR)


def render(size: int, big: bool) -> Image.Image:
    k = size / GRID
    def S(pt):
        return (pt[0] * k, pt[1] * k)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(14 * k)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG)

    # lens interior
    d.ellipse([C[0] * k - RING_R * k, C[1] * k - RING_R * k,
               C[0] * k + RING_R * k, C[1] * k + RING_R * k], fill=LENS)
    if big:
        d.ellipse([HORIZON[0] * k, HORIZON[1] * k, HORIZON[2] * k, HORIZON[3] * k], fill=GOLD)
    d.polygon([S(p) for p in ROAD_POLY], fill=ROAD if big else ROAD_SMALL)
    if big:
        for dsh in DASHES:
            d.rectangle([dsh[0] * k, dsh[1] * k, dsh[2] * k, dsh[3] * k], fill=LINE)

    # car on the road — bold dot at small sizes, top-down car at big
    if k < 0.5:
        rad = max(2, round(1.8 * k))
        cx, cy = S(CAR_DOT)
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=CAR)
    else:
        d.rounded_rectangle([CAR_RECT[0] * k, CAR_RECT[1] * k,
                             CAR_RECT[2] * k, CAR_RECT[3] * k],
                            radius=max(1, int(1.4 * k)), fill=CAR)

    # ring on top of everything (crisp outline)
    d.ellipse([C[0] * k - RING_R * k, C[1] * k - RING_R * k,
               C[0] * k + RING_R * k, C[1] * k + RING_R * k],
              fill=None, outline=RING, width=max(1, int(RING_W * k)))
    return img


def main():
    WEB.mkdir(exist_ok=True)
    (WEB / "logo.svg").write_text(SVG_MASTER)

    master = render(512, big=True)
    master.save(WEB / "logo.png")

    apple = render(180, big=True)
    apple.save(WEB / "apple-touch-icon.png")

    favicon = render(48, big=False)
    favicon.save(WEB / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    print("wrote web/logo.svg, logo.png, apple-touch-icon.png, favicon.ico")


if __name__ == "__main__":
    main()
