"""Draws sample "Football Manager like" screens for the tests (testdata/screens).

These are made up mock ups with invented clubs and players, not real game screenshots.
They exist to test the capture, change detection and explain pipeline without the game.

usage: make_samples.py <out_dir>
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
BG, SIDE, PANEL, LINE, TEXT, MUTED, ACC = (24, 18, 40), (34, 26, 58), (40, 32, 66), (64, 54, 98), (235, 232, 245), (160, 150, 190), (120, 210, 140)


def font(size):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/segoeui.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def frame(title, date):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 240, H], fill=SIDE)
    for i, item in enumerate(["Portal", "Inbox", "Squad", "Tactics", "Data Hub", "Schedule", "Competitions", "Scouting", "Transfers", "Club Info", "Finances", "Dev. Centre"]):
        d.text((28, 110 + i * 52), item, fill=TEXT if item.lower().startswith(title.lower()[:4]) else MUTED, font=font(22))
    d.rectangle([240, 0, W, 72], fill=PANEL)
    d.text((270, 20), title, fill=TEXT, font=font(30))
    d.text((W - 360, 22), date, fill=TEXT, font=font(24))
    d.rounded_rectangle([W - 140, 14, W - 20, 58], radius=10, fill=ACC)
    d.text((W - 118, 22), "Continue", fill=(20, 30, 20), font=font(20))
    return img, d


def table(d, x, y, headers, rows, widths):
    d.rectangle([x, y, x + sum(widths) + 20, y + 44], fill=PANEL)
    cx = x + 10
    for h, w in zip(headers, widths):
        d.text((cx, y + 10), h, fill=MUTED, font=font(20))
        cx += w
    for r, row in enumerate(rows):
        yy = y + 50 + r * 42
        if r % 2:
            d.rectangle([x, yy - 4, x + sum(widths) + 20, yy + 36], fill=(30, 23, 50))
        cx = x + 10
        for v, w in zip(row, widths):
            d.text((cx, yy + 4), str(v), fill=TEXT, font=font(20))
            cx += w


def squad(date):
    img, d = frame("Squad", date)
    rows = [("J. Hartley", "GK", 29, "4.0", ""), ("M. Okafor", "D (C)", 26, "3.5", ""), ("L. Brennan", "D (C)", 31, "3.5", "Injured"),
            ("T. Varga", "D (R)", 23, "3.0", ""), ("S. Iqbal", "D (L)", 27, "3.0", ""), ("R. Calloway", "DM", 28, "3.5", "Unhappy"),
            ("D. Mensah", "M (C)", 24, "4.0", ""), ("A. Lindqvist", "AM (R)", 22, "3.5", ""), ("P. Duarte", "AM (L)", 25, "3.0", ""),
            ("K. Whitmore", "ST (C)", 30, "4.0", ""), ("E. Novak", "ST (C)", 19, "2.5", "")]
    table(d, 280, 110, ["Name", "Position", "Age", "Ability", "Status"], rows, [360, 220, 120, 160, 200])
    d.text((1400, 120), "Northbridge Athletic", fill=TEXT, font=font(28))
    d.text((1400, 170), "Next: Kingsmere Rovers (H)", fill=MUTED, font=font(22))
    d.text((1400, 205), "Sat 23 Aug 2025, League One", fill=MUTED, font=font(22))
    return img


def league():
    img, d = frame("Competitions", "Sat 16 Aug 2025")
    teams = ["Kingsmere Rovers", "Port Allerton", "Northbridge Athletic", "Ashcombe Town", "Redfield City", "Hollins United", "Castlebay", "Westmoor", "Fenwick", "Oxley Park"]
    rows = [(i + 1, t, 3, 5 - i, 9 - i if i < 9 else 0) for i, t in enumerate(teams)]
    table(d, 280, 110, ["Pos", "Team", "Pld", "GD", "Pts"], rows, [100, 420, 100, 100, 100])
    return img


def money():
    img, d = frame("Finances", "Sat 16 Aug 2025")
    for i, (k, v) in enumerate([("Balance", "£4.2M"), ("Transfer Budget", "£650K"), ("Wage Budget", "£92K p/w"), ("Current Wages", "£97K p/w")]):
        d.rounded_rectangle([280 + i * 400, 120, 640 + i * 400, 260], radius=16, fill=PANEL)
        d.text((305 + i * 400, 140), k, fill=MUTED, font=font(22))
        d.text((305 + i * 400, 185), v, fill=TEXT if i < 3 else (240, 120, 120), font=font(40))
    return img


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    squad("Sat 16 Aug 2025").save(out / "squad.png")
    squad("Sun 17 Aug 2025").save(out / "squad_next_day.png")
    league().save(out / "league_table.png")
    money().save(out / "finances.png")
    Image.new("RGB", (W, H), (0, 0, 0)).save(out / "blank_fullscreen.png")


if __name__ == "__main__":
    main(sys.argv[1])
