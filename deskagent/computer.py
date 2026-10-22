"""Runs the computer toolset actions Claude asks for on the local screen.

Screenshots are scaled down before they are sent, and Claude's coordinates are
in that scaled space, so every coordinate is divided by the scale factor before
the mouse moves.
"""

import base64
import io
import sys
import time

import mss
import pyautogui
from PIL import Image

if sys.platform == "win32":
    import ctypes

    # Without this, Windows display scaling (125%, 150%) makes clicks land in the wrong place.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

# Slamming the mouse into a screen corner raises FailSafeException: the emergency stop.
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

# Names Claude uses for keys, mapped to the names pyautogui understands.
KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "super": "win",
    "cmd": "win",
    "meta": "win",
    "control": "ctrl",
    "page_up": "pageup",
    "page_down": "pagedown",
    "prior": "pageup",
    "next": "pagedown",
    "back_space": "backspace",
    "delete": "delete",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def _key(name: str) -> str:
    name = name.strip().lower()
    return KEY_ALIASES.get(name, name)


def _combo(text: str) -> list[str]:
    return [_key(k) for k in text.split("+") if k.strip()]


class Computer:
    def __init__(self, max_width: int = 1280):
        self.max_width = max_width
        self.scale = 1.0

    def _png(self, img: Image.Image) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.standard_b64encode(buf.getvalue()).decode()

    def _grab(self) -> Image.Image:
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1])
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def screenshot(self) -> str:
        img = self._grab()
        self.scale = min(1.0, self.max_width / img.width)
        if self.scale < 1.0:
            img = img.resize((round(img.width * self.scale), round(img.height * self.scale)), Image.LANCZOS)
        return self._png(img)

    def zoom(self, region) -> str:
        x0, y0, x1, y1 = (round(v / self.scale) for v in region)
        img = self._grab().crop((x0, y0, x1, y1))
        if img.width > self.max_width:
            ratio = self.max_width / img.width
            img = img.resize((self.max_width, round(img.height * ratio)), Image.LANCZOS)
        return self._png(img)

    def _xy(self, coordinate):
        if coordinate is None:
            return None
        x, y = coordinate
        return round(x / self.scale), round(y / self.scale)

    def _with_modifiers(self, text, fn):
        mods = _combo(text) if text else []
        for m in mods:
            pyautogui.keyDown(m)
        try:
            fn()
        finally:
            for m in reversed(mods):
                pyautogui.keyUp(m)

    def run(self, action: str, inp: dict):
        """Returns ("image", base64_png) or ("text", message)."""
        xy = self._xy(inp.get("coordinate"))

        if action == "screenshot":
            return "image", self.screenshot()
        if action == "zoom":
            return "image", self.zoom(inp["region"])
        if action == "cursor_position":
            x, y = pyautogui.position()
            return "text", f"[{round(x * self.scale)}, {round(y * self.scale)}]"

        clicks = {
            "left_click": ("left", 1),
            "right_click": ("right", 1),
            "middle_click": ("middle", 1),
            "double_click": ("left", 2),
            "triple_click": ("left", 3),
        }
        if action in clicks:
            button, n = clicks[action]
            pos = xy or pyautogui.position()
            self._with_modifiers(inp.get("text"), lambda: pyautogui.click(*pos, clicks=n, interval=0.08, button=button))
        elif action == "left_click_drag":
            sx, sy = self._xy(inp["start_coordinate"])

            def drag():
                pyautogui.moveTo(sx, sy)
                pyautogui.dragTo(*xy, duration=0.4, button="left")

            self._with_modifiers(inp.get("text"), drag)
        elif action == "mouse_move":
            pyautogui.moveTo(*xy, duration=0.15)
        elif action == "left_mouse_down":
            pyautogui.mouseDown()
        elif action == "left_mouse_up":
            pyautogui.mouseUp()
        elif action == "scroll":
            amount = int(inp.get("scroll_amount", 3))
            direction = inp["scroll_direction"]
            if xy:
                pyautogui.moveTo(*xy)

            def do_scroll():
                if direction in ("up", "down"):
                    pyautogui.scroll(amount * 120 * (1 if direction == "up" else -1))
                else:
                    pyautogui.hscroll(amount * 120 * (1 if direction == "right" else -1))

            self._with_modifiers(inp.get("text"), do_scroll)
        elif action == "type":
            # write() only handles plain ASCII; paste anything else through the clipboard.
            text = inp["text"]
            if text.isascii():
                pyautogui.write(text, interval=0.01)
            else:
                _paste(text)
        elif action == "key":
            for _ in range(int(inp.get("repeat", 1))):
                pyautogui.hotkey(*_combo(inp["text"]))
        elif action == "hold_key":
            keys = _combo(inp["text"])
            for k in keys:
                pyautogui.keyDown(k)
            time.sleep(min(float(inp.get("duration", 1)), 300))
            for k in reversed(keys):
                pyautogui.keyUp(k)
        elif action == "wait":
            time.sleep(min(float(inp.get("duration", 1)), 300))
        else:
            raise ValueError(f"Unsupported action: {action}")

        time.sleep(0.3)  # let the screen settle before the next action
        return "text", "OK"


def _paste(text: str):
    import tkinter

    root = tkinter.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    root.destroy()
