"""Draws Nova's app icon (nova/web/nova.ico): a navy squircle with a glowing mint orb.

Run once after changing the design:  python tools/make_icon.py
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 1024  # drawn large, scaled down for each icon size (smooth edges)
OUT = Path(__file__).resolve().parent.parent / "nova" / "web" / "nova.ico"


def squircle_mask(size: int, n: float = 5.0) -> Image.Image:
    """Superellipse |x|^n + |y|^n <= 1: the soft "app icon" square."""
    y, x = np.mgrid[-1:1:size * 1j, -1:1:size * 1j]
    inside = (np.abs(x) ** n + np.abs(y) ** n) <= 1
    return Image.fromarray((inside * 255).astype(np.uint8))


def radial(size: int, cx: float, cy: float, r: float, rgb, alpha: float = 1.0) -> Image.Image:
    y, x = np.mgrid[0:size, 0:size]
    d = np.sqrt((x - cx * size) ** 2 + (y - cy * size) ** 2) / (r * size)
    a = np.clip(1 - d, 0, 1) ** 1.6 * alpha
    img = np.zeros((size, size, 4), np.uint8)
    img[..., :3] = rgb
    img[..., 3] = (a * 255).astype(np.uint8)
    return Image.fromarray(img, "RGBA")


def draw() -> Image.Image:
    # Background: deep-sea navy, lighter at the top.
    top, bottom = np.array([20, 46, 88]), np.array([4, 11, 26])
    t = np.linspace(0, 1, S)[:, None, None]
    bg = (top * (1 - t) + bottom * t).repeat(S, axis=1).astype(np.uint8)
    icon = Image.fromarray(bg, "RGB").convert("RGBA")
    icon.alpha_composite(radial(S, 0.5, 0.52, 0.50, (92, 242, 189), 0.42))  # mint glow

    # The orb: three coloured clouds inside a sphere, then a glossy highlight.
    orb = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    orb.alpha_composite(radial(S, 0.5, 0.52, 0.30, (8, 40, 60), 1.0))
    orb.alpha_composite(radial(S, 0.44, 0.47, 0.30, (111, 247, 200), 1.0))
    orb.alpha_composite(radial(S, 0.60, 0.52, 0.25, (79, 220, 236), 1.0))
    orb.alpha_composite(radial(S, 0.52, 0.66, 0.22, (58, 124, 240), 0.95))
    orb.alpha_composite(radial(S, 0.47, 0.50, 0.16, (190, 255, 232), 0.7))  # bright core
    orb = orb.filter(ImageFilter.GaussianBlur(S * 0.012))
    sphere = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sphere).ellipse([S * 0.24, S * 0.26, S * 0.76, S * 0.78], fill=255)
    orb.putalpha(Image.fromarray(np.minimum(np.asarray(orb.getchannel("A")), np.asarray(sphere))))
    icon.alpha_composite(orb)
    icon.alpha_composite(radial(S, 0.41, 0.38, 0.09, (255, 255, 255), 0.85))  # highlight

    # Glass rim on the squircle, then cut it out.
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = squircle_mask(S)
    edge = mask.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(S * 0.004))
    rim.putalpha(edge.point(lambda v: min(255, v * 2) // 3))
    icon.alpha_composite(rim)
    icon.putalpha(mask.filter(ImageFilter.GaussianBlur(1.2)))
    return icon


if __name__ == "__main__":
    big = draw()
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
    big.resize((256, 256), Image.LANCZOS).save(OUT, sizes=sizes)
    big.resize((256, 256), Image.LANCZOS).save(OUT.with_suffix(".png"))
    print("wrote", OUT, OUT.stat().st_size, "bytes")
