#!/usr/bin/env python3
"""Regenerate public/'s icons and social image from Keyup's waveform mark (needs Pillow).

The social image (og-image.png, 1200x630) carries up to three lines of text beside the mark:
--text (large), --subtitle and --host (small, grey; omitted when blank). The defaults are the
generic image the public tree ships; a deployment draws its own name, e.g.

    tools/make-icons.py --text "Example County" --host archive.example.org --out /tmp/site
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PUBLIC = Path(__file__).resolve().parents[1] / "public"
BLUE = (42, 120, 214, 255)
WHITE = (255, 255, 255, 255)
BARS = ((16, 8, 24), (11, 11, 21), (21, 11, 21), (6, 14, 18), (26, 14, 18))  # from the SVG favicon
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
OVERSAMPLE = 8


def mark(size: int) -> Image.Image:
    canvas = size * OVERSAMPLE
    scale = canvas / 32
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, canvas - 1, canvas - 1), radius=7 * scale, fill=BLUE)
    half = 1.3 * scale
    for x, top, bottom in BARS:
        draw.rounded_rectangle(
            ((x * scale) - half, (top * scale) - half, (x * scale) + half, (bottom * scale) + half),
            radius=half,
            fill=WHITE,
        )
    return image.resize((size, size), Image.LANCZOS)


def social(text: str, subtitle: str, host: str) -> Image.Image:
    image = Image.new("RGBA", (1200, 630), (15, 23, 42, 255))
    image.alpha_composite(mark(180), (90, 225))
    draw = ImageDraw.Draw(image)
    draw.text((310, 225), text, font=ImageFont.truetype(FONT, 72), fill=WHITE)
    if subtitle:
        draw.text((310, 315), subtitle, font=ImageFont.truetype(FONT, 56), fill=WHITE)
    if host:
        draw.text((310, 395), host, font=ImageFont.truetype(FONT, 34), fill=(148, 163, 184, 255))
    return image.convert("RGB")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--text", default="Keyup", help="the social image's first, large line (default: Keyup)"
    )
    parser.add_argument("--subtitle", default="radio scanner archive", help="its second line; blank for none")
    parser.add_argument(
        "--host", default="", help="a hostname drawn small underneath; blank (default) for none"
    )
    parser.add_argument(
        "--out", type=Path, default=PUBLIC, help="output directory (default: frontend/public)"
    )
    parser.add_argument("--social-only", action="store_true", help="write og-image.png and none of the icons")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.social_only:
        mark(64).save(args.out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        mark(180).convert("RGB").save(args.out / "apple-touch-icon.png")
        mark(192).save(args.out / "icon-192.png")
        mark(512).save(args.out / "icon-512.png")
    social(args.text, args.subtitle, args.host).save(args.out / "og-image.png", optimize=True)


if __name__ == "__main__":
    main()
