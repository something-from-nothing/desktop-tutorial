"""Draws sample "FC 26 like" screens for the tests (testdata/screens).

These are made up mock ups with invented clubs and players, not real game screenshots.
They exist to test the capture, change detection and explain pipeline without the game.

usage: make_samples.py <out_dir>
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
BG, PANEL, LINE, TEXT, MUTED, ACC, PITCH = (10, 14, 30), (22, 28, 52), (60, 70, 110), (240, 242, 250), (150, 160, 190), (0, 230, 160), (20, 90, 60)


def font(size):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def frame(title, tabs, active):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((60, 36), title, fill=TEXT, font=font(40))
    x = 60
    for t in tabs:
        d.text((x, 110), t, fill=ACC if t == active else MUTED, font=font(24))
        x += 60 + len(t) * 15
    d.line([60, 150, W - 60, 150], fill=LINE, width=2)
    return img, d


def card(d, x, y, rating, pos, name):
    d.rounded_rectangle([x, y, x + 150, y + 200], radius=14, fill=(196, 160, 60))
    d.text((x + 14, y + 12), str(rating), fill=(30, 20, 0), font=font(36))
    d.text((x + 14, y + 56), pos, fill=(30, 20, 0), font=font(22))
    d.text((x + 14, y + 160), name, fill=(30, 20, 0), font=font(18))


def ultimate_team(coins):
    img, d = frame("ULTIMATE TEAM", ["Home", "Squads", "Play", "Transfers", "Store", "Objectives"], "Squads")
    d.rectangle([60, 180, 1300, 1040], fill=PITCH)
    spots = [(610, 860, 84, "GK", "Hartley"), (260, 640, 82, "LB", "Iqbal"), (480, 680, 85, "CB", "Okafor"), (740, 680, 83, "CB", "Brennan"),
             (960, 640, 81, "RB", "Varga"), (480, 440, 86, "CM", "Mensah"), (740, 440, 84, "CM", "Calloway"), (610, 300, 87, "CAM", "Lindqvist"),
             (300, 220, 85, "LW", "Duarte"), (920, 220, 86, "RW", "Novak"), (610, 190, 88, "ST", "Whitmore")]
    for x, y, r, p, n in spots:
        card(d, x, y - 10, r, p, n)
    d.rounded_rectangle([1360, 180, 1860, 520], radius=16, fill=PANEL)
    for i, (k, v) in enumerate([("Squad Rating", "85"), ("Chemistry", "29/33"), ("Coins", coins), ("FC Points", "250"), ("Division Rivals", "Division 6")]):
        d.text((1390, 205 + i * 62), k, fill=MUTED, font=font(22))
        d.text((1660, 205 + i * 62), v, fill=TEXT, font=font(26))
    return img


def career():
    img, d = frame("CAREER MODE", ["Central", "Squad", "Transfers", "Club", "Season"], "Central")
    d.text((60, 180), "Northbridge Athletic  ·  Sat 16 Aug 2025", fill=TEXT, font=font(30))
    d.rounded_rectangle([60, 240, 900, 700], radius=16, fill=PANEL)
    teams = ["Kingsmere Rovers", "Port Allerton", "Northbridge Athletic", "Ashcombe Town", "Redfield City", "Hollins United"]
    d.text((90, 260), "League One", fill=MUTED, font=font(24))
    for i, t in enumerate(teams):
        d.text((90, 310 + i * 60), f"{i + 1}   {t}", fill=ACC if "Northbridge" in t else TEXT, font=font(26))
        d.text((760, 310 + i * 60), str(9 - i), fill=TEXT, font=font(26))
    d.rounded_rectangle([960, 240, 1860, 460], radius=16, fill=PANEL)
    d.text((990, 260), "NEXT MATCH", fill=MUTED, font=font(22))
    d.text((990, 310), "vs Kingsmere Rovers (H)", fill=TEXT, font=font(34))
    d.text((990, 370), "Sat 23 Aug 2025 · League One", fill=MUTED, font=font(24))
    d.rounded_rectangle([960, 500, 1860, 700], radius=16, fill=PANEL)
    d.text((990, 520), "TRANSFER BUDGET", fill=MUTED, font=font(22))
    d.text((990, 570), "€12.5M", fill=TEXT, font=font(44))
    d.text((1400, 520), "WAGE BUDGET", fill=MUTED, font=font(22))
    d.text((1400, 570), "€180K / wk", fill=TEXT, font=font(36))
    return img


def match_stats():
    img, d = frame("HALF TIME", ["Stats", "Player Performance", "Tactics", "Substitutions"], "Stats")
    rows = [("Possession", "38%", "62%"), ("Shots", "3", "11"), ("Shots on target", "1", "6"), ("Expected goals", "0.3", "1.9"), ("Pass accuracy", "71%", "88%"), ("Tackles won", "4", "9")]
    d.text((520, 190), "Northbridge Athletic  0 : 1  Kingsmere Rovers", fill=TEXT, font=font(36))
    for i, (k, a, b) in enumerate(rows):
        y = 300 + i * 90
        d.text((560, y), a, fill=TEXT, font=font(34))
        d.text((860, y), k, fill=MUTED, font=font(28))
        d.text((1300, y), b, fill=TEXT, font=font(34))
    return img


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    ultimate_team("48,250").save(out / "ultimate_team.png")
    ultimate_team("48,300").save(out / "ultimate_team_coins_changed.png")
    career().save(out / "career_mode.png")
    match_stats().save(out / "half_time_stats.png")
    Image.new("RGB", (W, H), (0, 0, 0)).save(out / "blank_fullscreen.png")


if __name__ == "__main__":
    main(sys.argv[1])
