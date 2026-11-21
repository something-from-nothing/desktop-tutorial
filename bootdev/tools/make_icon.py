"""Draws the Boot.dev Dashboard icon: app.ico for Windows and launcher PNGs for Android.

usage: make_icon.py <out_dir> <android_res_dir>
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024


def draw(pad_frac=0.0, rounded=True):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        r = int(0x1a + (0x10 - 0x1a) * t); g = int(0x1e + (0x12 - 0x1e) * t); b = int(0x2c + (0x1a - 0x2c) * t)
        gd.line([(0, y), (S, y)], fill=(r, g, b, 255))
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    if rounded:
        md.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    else:
        md.rectangle([0, 0, S - 1, S - 1], fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    p = int(S * pad_frac)
    inner = S - 2 * p
    def X(v): return p + int(inner * v)
    # gold bars rising, like a progress chart
    gold = (244, 192, 78, 255); gold2 = (217, 154, 30, 255)
    bars = [(0.22, 0.62), (0.40, 0.48), (0.58, 0.34)]
    for x0, top in bars:
        d.rounded_rectangle([X(x0), X(top), X(x0 + 0.13), X(0.78)], radius=int(inner * 0.03), fill=gold if x0 > 0.3 else gold2)
    # a spark above the tallest bar
    cx, cy, r = X(0.775), X(0.24), int(inner * 0.07)
    d.polygon([(cx, cy - r * 2), (cx + r * 0.55, cy - r * 0.55), (cx + r * 2, cy), (cx + r * 0.55, cy + r * 0.55),
               (cx, cy + r * 2), (cx - r * 0.55, cy + r * 0.55), (cx - r * 2, cy), (cx - r * 0.55, cy - r * 0.55)], fill=(255, 236, 180, 255))
    return img


def main(out, res):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    big = draw()
    big.save(out / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    res = Path(res)
    full = draw(0.12, rounded=False)
    for name, px in {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}.items():
        d = res / ("mipmap-" + name); d.mkdir(parents=True, exist_ok=True)
        draw(0.06).resize((px, px), Image.LANCZOS).save(d / "ic_launcher.png")
        full.resize((px, px), Image.LANCZOS).save(d / "ic_launcher_round.png")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
