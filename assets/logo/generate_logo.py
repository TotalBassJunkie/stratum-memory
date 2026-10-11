"""Generate the Stratum-Memory logo: SVG (vector) + PNG rasters + favicon.

Design: an isometric stack of four "strata" plates (the four storage layers:
filesystem, temporal graph, vector/KV, reflect). Dark slate base, a cool-to-warm
accent ramp, and a top plate that glows with the accent. Reads clearly at 32 px
and scales to 1024 px.

Dependency-light: SVG is written by hand; PNGs/ICO are drawn with Pillow (no SVG
rasterizer required).

Run:  python assets/logo/generate_logo.py
Outputs: stratum-icon.svg, stratum-logo.svg, stratum-icon-<n>.png,
         stratum-logo.png, favicon.ico
"""

from __future__ import annotations

import math
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Palette.
BG = "#0B1220"
BG2 = "#111c33"
EDGE = "#1e293b"
PLATES = ["#3B4A63", "#0EA5E9", "#22D3EE", "#F59E0B"]  # deep slate -> amber
TEXT = "#E8EEF7"
TEXT_DIM = "#8AA0BE"

ICON = 256


def _iso_plate(cx: float, cy: float, half_w: float, half_h: float, thickness: float):
    """Return a hexagonal isometric plate as an SVG path (top + front + side)."""
    top = [
        (cx, cy - half_h),
        (cx + half_w, cy),
        (cx, cy + half_h),
        (cx - half_w, cy),
    ]
    # front face (down by thickness)
    front = [
        (cx - half_w, cy),
        (cx, cy + half_h),
        (cx, cy + half_h + thickness),
        (cx - half_w, cy + thickness),
    ]
    side = [
        (cx, cy + half_h),
        (cx + half_w, cy),
        (cx + half_w, cy + thickness),
        (cx, cy + half_h + thickness),
    ]
    return top, front, side


def _pts(points) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def _shade(hex_color: str, factor: float) -> str:
    """Lighten (factor>1) or darken (factor<1) a hex colour."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _icon_group() -> str:
    """The four isometric plates, bottom-first so upper ones overlay correctly."""
    cx = ICON / 2
    base_y = 158.0
    step = 30.0
    half_w = 76.0
    half_h = 36.0
    thickness = 13.0
    parts = []
    # Draw bottom -> top so the top plate sits on the stack.
    for i in range(len(PLATES) - 1, -1, -1):
        color = PLATES[i]
        cy = base_y - i * step
        top, front, side = _iso_plate(cx, cy, half_w, half_h, thickness)
        parts.append(f'<polygon points="{_pts(front)}" fill="{_shade(color, 0.62)}"/>')
        parts.append(f'<polygon points="{_pts(side)}" fill="{_shade(color, 0.78)}"/>')
        parts.append(
            f'<polygon points="{_pts(top)}" fill="{color}" '
            f'stroke="{_shade(color, 1.25)}" stroke-width="1.5"/>'
        )
    return "\n  ".join(parts)


def icon_svg() -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ICON} {ICON}" width="{ICON}" height="{ICON}" role="img" aria-label="Stratum Memory">
  <title>Stratum Memory</title>
  <defs>
    <linearGradient id="bgg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{BG2}"/><stop offset="1" stop-color="{BG}"/>
    </linearGradient>
  </defs>
  <rect width="{ICON}" height="{ICON}" rx="56" fill="url(#bgg)"/>
  <g>
  {_icon_group()}
  </g>
</svg>
'''


def logo_svg() -> str:
    # Scale the 256px icon down and centre it vertically in the 200px banner.
    icon_size = 150
    scale = icon_size / ICON
    icon_x = 34
    icon_y = (200 - icon_size) / 2
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 780 200" width="780" height="200" role="img" aria-label="Stratum Memory">
  <title>Stratum Memory</title>
  <defs>
    <linearGradient id="bgg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{BG2}"/><stop offset="1" stop-color="{BG}"/>
    </linearGradient>
  </defs>
  <rect width="780" height="200" rx="30" fill="url(#bgg)"/>
  <g transform="translate({icon_x},{icon_y}) scale({scale:.4f})">
  {_icon_group()}
  </g>
  <text x="216" y="98" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="62" font-weight="700" fill="{TEXT}">Stratum</text>
  <text x="219" y="142" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="24" font-weight="500" letter-spacing="9" fill="{TEXT_DIM}">MEMORY</text>
</svg>
'''


# ---------------------------------------------------------------- raster (PIL)


def _polys_for(size: int):
    """Scaled polygons for raster rendering: [(top_pts, front_pts, side_pts, colour)]."""
    s = size / ICON
    cx = ICON / 2
    base_y = 158.0
    step = 30.0
    half_w = 76.0
    half_h = 36.0
    thickness = 13.0
    out = []
    for i in range(len(PLATES) - 1, -1, -1):
        cy = base_y - i * step
        top, front, side = _iso_plate(cx, cy, half_w, half_h, thickness)
        out.append(
            (
                [(x * s, y * s) for x, y in top],
                [(x * s, y * s) for x, y in front],
                [(x * s, y * s) for x, y in side],
                PLATES[i],
            )
        )
    return out


def render_icon_png(size: int):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(56 * size / ICON), fill=BG)
    for top, front, side, color in _polys_for(size):
        # bottom (front) then side then top so the top face wins.
        draw.polygon(front, fill=_shade(color, 0.62))
        draw.polygon(side, fill=_shade(color, 0.78))
        draw.polygon(top, fill=color, outline=_shade(color, 1.25))
    return img


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for c in (
        r"C:\Windows\Fonts\seguisb.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ):
        if Path(c).exists():
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()


def render_logo_png():
    from PIL import Image, ImageDraw

    W, H = 780, 200
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, W - 1, H - 1), radius=30, fill=BG)
    # Render the icon to its own layer at 2x for smooth downscaling, then place
    # it centred-left. Prevents the bottom slab from being clipped.
    icon_px = 150
    tmp = Image.new("RGBA", (ICON, ICON), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tmp)
    for top, front, side, color in _polys_for(ICON):
        tdraw.polygon(front, fill=_shade(color, 0.62))
        tdraw.polygon(side, fill=_shade(color, 0.78))
        tdraw.polygon(top, fill=color, outline=_shade(color, 1.25))
    tmp = tmp.resize((icon_px, icon_px), Image.LANCZOS)
    img.alpha_composite(tmp, (34, (H - icon_px) // 2))
    draw.text((216, 44), "Stratum", font=_font(64, bold=True), fill=TEXT)
    draw.text((219, 118), "M E M O R Y", font=_font(22), fill=TEXT_DIM)
    return img


def main() -> None:
    (HERE / "stratum-icon.svg").write_text(icon_svg(), encoding="utf-8")
    (HERE / "stratum-logo.svg").write_text(logo_svg(), encoding="utf-8")
    render_icon_png(1024).save(HERE / "stratum-icon.png")
    for n in (512, 256, 128, 64, 32, 16):
        render_icon_png(n).save(HERE / f"stratum-icon-{n}.png")
    render_icon_png(256).save(
        HERE / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
    )
    render_logo_png().save(HERE / "stratum-logo.png")
    print("logo assets written to", HERE)


if __name__ == "__main__":
    main()
