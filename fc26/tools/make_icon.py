"""Draws the FC26 Dashboard icon: app.ico for Windows and launcher PNGs for Android.

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
    # a pitch seen from above, with a gold bar chart rising out of the centre circle
    line = (110, 200, 140, 255); w = max(2, int(inner * 0.025))
    d.rounded_rectangle([X(0.14), X(0.20), X(0.86), X(0.80)], radius=int(inner * 0.04), outline=line, width=w)
    d.line([X(0.5), X(0.20), X(0.5), X(0.80)], fill=line, width=w)
    d.ellipse([X(0.40), X(0.40), X(0.60), X(0.60)], outline=line, width=w)
    d.rectangle([X(0.14), X(0.36), X(0.24), X(0.64)], outline=line, width=w)
    d.rectangle([X(0.76), X(0.36), X(0.86), X(0.64)], outline=line, width=w)
    gold = (244, 192, 78, 255); gold2 = (217, 154, 30, 255)
    for x0, top in [(0.30, 0.56), (0.44, 0.42), (0.58, 0.30)]:
        d.rounded_rectangle([X(x0), X(top), X(x0 + 0.11), X(0.70)], radius=int(inner * 0.02), fill=gold if x0 > 0.4 else gold2)
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
