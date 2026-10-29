"""Draws the GitHub social preview (docs/social-preview.png, 1280x640): the icon and the tagline.

Run after changing the icon:  python tools/make_banner.py
Then upload it in the repository's Settings → General → Social preview.
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "social-preview.png"
W, H = 1280, 640


def font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for name in names:
        try:
            return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    top, bottom = np.array([14, 32, 64]), np.array([4, 11, 24])
    t = np.linspace(0, 1, H)[:, None, None]
    bg = (top * (1 - t) + bottom * t).repeat(W, axis=1).astype(np.uint8)
    img = Image.fromarray(bg, "RGB").convert("RGBA")

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([150, 120, 550, 520], fill=(92, 242, 189, 70))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90)))

    icon = Image.open(ROOT / "nova" / "web" / "nova.png").convert("RGBA").resize((300, 300), Image.LANCZOS)
    img.alpha_composite(icon, (190, 170))

    d = ImageDraw.Draw(img)
    d.text((560, 205), "Nova", font=font(["segoeuisb.ttf", "segoeuib.ttf", "arial.ttf"], 118), fill=(236, 246, 255))
    d.text((566, 345), "Voice assistant for Windows", font=font(["segoeui.ttf", "arial.ttf"], 44),
           fill=(142, 164, 194))
    d.text((566, 410), "Free · Open source · Speech stays on your PC", font=font(["segoeui.ttf", "arial.ttf"], 30),
           fill=(92, 242, 189))
    OUT.parent.mkdir(exist_ok=True)
    img.convert("RGB").save(OUT, optimize=True)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
