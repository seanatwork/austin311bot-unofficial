#!/usr/bin/env python3
"""Generate the default Open Graph card image (docs/og-default.png).

1200×630 PNG used as the og:image for every page that doesn't ship its
own. Run from the repo root:

    python scripts/generate_og_image.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "og-default.png"

W, H = 1200, 630

# Palette (matches docs/index.html light-mode tokens)
BG          = (248, 250, 252)   # --bg
ACCENT      = (217, 119, 6)     # amber 600 — Austin "Fun" highlight
TEXT_HEAD   = (15, 23, 42)      # --text-head
TEXT_BODY   = (30, 41, 59)      # --text
TEXT_MUTED  = (100, 116, 139)   # --text-desc
BORDER      = (226, 232, 240)   # --border


def _load_font(candidates: list[str], size: int) -> ImageFont.ImageFont:
    """Try a series of font paths, fall back to PIL's default."""
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Top accent bar
    draw.rectangle((0, 0, W, 12), fill=ACCENT)

    bold = _load_font([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ], 110)
    sub = _load_font([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ], 36)
    label = _load_font([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ], 24)
    pill = _load_font([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ], 24)

    # Title — "Austin 311" centered horizontally, weighted toward the top third.
    title = "Austin 311"
    tw = draw.textlength(title, font=bold)
    draw.text(((W - tw) / 2, 130), title, font=bold, fill=TEXT_HEAD)

    # Tagline
    tagline = "Real data on what your city is doing — and what it isn't."
    sw = draw.textlength(tagline, font=sub)
    draw.text(((W - sw) / 2, 270), tagline, font=sub, fill=TEXT_BODY)

    # Pill row — service categories. Plain text inside rounded boxes.
    pills = [
        "Homeless",
        "Budget",
        "Graffiti",
        "Traffic",
        "Crime",
        "Parking",
    ]
    pad_x = 22
    pad_y = 12
    gap = 14
    pill_y = 360

    pill_widths = [draw.textlength(p, font=pill) + pad_x * 2 for p in pills]
    total = sum(pill_widths) + gap * (len(pills) - 1)
    x = (W - total) / 2
    for text, pw in zip(pills, pill_widths):
        ph = 50
        draw.rounded_rectangle((x, pill_y, x + pw, pill_y + ph),
                               radius=ph / 2, fill=(255, 255, 255), outline=BORDER, width=2)
        tw_p = draw.textlength(text, font=pill)
        draw.text((x + (pw - tw_p) / 2, pill_y + (ph - 28) / 2 - 2), text,
                  font=pill, fill=TEXT_BODY)
        x += pw + gap

    # Footer — site URL centered
    footer_y = H - 70
    url_text = "austin311.com"
    uw = draw.textlength(url_text, font=label)
    draw.text(((W - uw) / 2, footer_y), url_text, font=label, fill=TEXT_MUTED)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT_PATH, "PNG", optimize=True)
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
