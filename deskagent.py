"""Desk Agent: lets Claude do jobs on a Windows laptop.

It can see and control the screen, search the web, read and write code in a
projects folder, run commands, and use any MCP server you plug in. You can pick
the model yourself or leave it on Auto, which picks one for each job. It keeps a
memory of what it learns about you and gets more useful the more you use it.

Everything is in this one file. Build the EXE with build.bat, or let the GitHub
workflow do it.
"""

import asyncio
import base64
import io
import json
import logging
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from contextlib import AsyncExitStack
from datetime import date, datetime
from pathlib import Path
from tkinter import scrolledtext, ttk

if sys.platform == "win32":
    import ctypes

    # Without this, Windows display scaling (125%, 150%) makes clicks land in the wrong place.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

import anthropic
import jsonschema
import mss
import pyautogui
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

log = logging.getLogger("deskagent")


# ---------------------------------------------------------------- models

# Prices are US dollars per million tokens (input, output), used for the spend counter.
MODELS = {
    "claude-fable-5-1": {"label": "Fable 5.1 (smartest, priciest)", "price": (10.0, 50.0), "fallbacks": True, "updates": True},
    "claude-opus-5-5": {"label": "Opus 5.5 (strong all rounder)", "price": (4.0, 20.0), "fallbacks": True, "updates": True},
    "claude-sonnet-5-5": {"label": "Sonnet 5.5 (fast, cheaper)", "price": (2.0, 10.0), "fallbacks": True, "updates": True},
    "claude-haiku-5-5": {"label": "Haiku 5.5 (quickest, cheapest)", "price": (0.10, 0.50), "fallbacks": False, "updates": False},
}
AUTO = "auto"
AUTO_LABEL = "Auto (picks the best model for each job)"
ROUTER_MODEL = "claude-haiku-5-5"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


# ---------------------------------------------------------------- settings and saved data

DEFAULTS = {
    "api_key": "",
    "model": AUTO,
    "effort": "medium",
    "computer_use": True,
    "web_search": True,
    "coding_tools": True,
    "learn": True,
    "confirm_actions": True,
    "max_steps": 60,
    "screenshot_max_width": 1280,
    "workspace": str(Path.home() / "Documents" / "DeskAgent Projects"),
    # Same shape as Claude Desktop's "mcpServers" block, so configs can be copied across.
    # stdio server:  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me\\Documents"]}
    # remote server: {"url": "https://example.com/mcp"}
    "mcpServers": {},
}


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or Path.home() / ".config"
    path = Path(base) / "DeskAgent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_dir() / "config.json"


def memory_path() -> Path:
    return app_dir() / "memory.md"


def stats_path() -> Path:
    return app_dir() / "stats.json"


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        save_config(DEFAULTS)
        return dict(DEFAULTS)
    with path.open(encoding="utf-8") as f:
        return {**DEFAULTS, **json.load(f)}


def save_config(cfg: dict) -> None:
    with config_path().open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def read_memory() -> str:
    path = memory_path()
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def load_stats() -> dict:
    try:
        with stats_path().open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"first_used": date.today().isoformat(), "jobs": 0, "spend_usd": 0.0, "models": {}}


def open_file(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------- screen control

pyautogui.FAILSAFE = True  # slamming the mouse into a screen corner is the emergency stop
pyautogui.PAUSE = 0.05

# Names Claude uses for keys, mapped to the names pyautogui understands.
KEY_ALIASES = {
    "return": "enter", "escape": "esc", "super": "win", "cmd": "win", "meta": "win",
    "control": "ctrl", "page_up": "pageup", "page_down": "pagedown", "prior": "pageup",
    "next": "pagedown", "back_space": "backspace", "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right",
}


def _combo(text: str) -> list[str]:
    keys = [k.strip().lower() for k in text.split("+") if k.strip()]
    return [KEY_ALIASES.get(k, k) for k in keys]


class Computer:
    """Runs the computer toolset actions. Screenshots are scaled down before they are
    sent, and Claude's coordinates are in that scaled space, so every coordinate is
    divided by the scale factor before the mouse moves."""

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
            "left_click": ("left", 1), "right_click": ("right", 1), "middle_click": ("middle", 1),
            "double_click": ("left", 2), "triple_click": ("left", 3),
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
    root = tk.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    root.destroy()


# ---------------------------------------------------------------- coding tools

def _tool(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        "eager_input_streaming": True,
    }


CODING_TOOLS = [
    _tool("list_files", "List files and folders in the projects folder. Path is relative to the projects folder; use '.' for the top.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("read_file", "Read a text file from the projects folder.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("write_file", "Create or overwrite a text file in the projects folder. Folders are created as needed. Always send the whole file.",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("run_command", "Run a command in a Windows command prompt inside the projects folder, for example 'python app.py' or 'pip install requests'. Times out after 120 seconds. Use the computer tools instead for anything with a window.",
          {"command": {"type": "string"}, "folder": {"type": "string", "description": "Subfolder to run in, relative to the projects folder. Defaults to the top."}}, ["command"]),
]
CODING_TOOL_NAMES = {t["name"] for t in CODING_TOOLS}


class Workspace:
    """The one folder the coding tools may touch."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, rel: str) -> Path:
        path = (self.root / (rel or ".")).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"{rel} is outside the projects folder")
        return path

    def run(self, name: str, inp: dict) -> str:
        if name == "list_files":
            path = self._path(inp["path"])
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            lines = [f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)" for p in entries[:500]]
            return "\n".join(lines) or "(empty folder)"
        if name == "read_file":
            text = self._path(inp["path"]).read_text(encoding="utf-8", errors="replace")
            return text if len(text) < 200_000 else text[:200_000] + "\n...(cut off, file is too long)"
        if name == "write_file":
            path = self._path(inp["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inp["content"], encoding="utf-8")
            return f"Saved {path}"
        if name == "run_command":
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            proc = subprocess.run(
                inp["command"], shell=True, cwd=self._path(inp.get("folder", ".")), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=flags,
            )
            out = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
            if len(out) > 30_000:
                out = out[:15_000] + "\n...(middle cut)...\n" + out[-15_000:]
            return f"Exit code {proc.returncode}\n{out}"
        raise ValueError(f"Unknown tool {name}")


# ---------------------------------------------------------------- MCP servers

SEP = "__"


class MCPBridge:
    """Connects to MCP servers and exposes their tools to Claude. The MCP client is
    async and the rest of the app is not, so all servers share one event loop on a
    background thread. Tool names are prefixed with the server name (server__tool)."""

    def __init__(self, servers: dict, errlog):
        self.servers = servers
        self.errlog = errlog
        self.sessions: dict[str, ClientSession] = {}
        self.tools: list[dict] = []
        self.failed: dict[str, str] = {}
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._shutdown: asyncio.Event | None = None
        self._main_future = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def start(self):
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self.loop)
        self._ready.wait(timeout=60 * max(1, len(self.servers)) + 5)

    async def _main(self):
        # The transports use anyio cancel scopes, which must be entered and exited
        # in the same task, so this one task owns every connection for its lifetime.
        self._shutdown = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for name, spec in self.servers.items():
                    if spec.get("disabled"):
                        continue
                    try:
                        await asyncio.wait_for(self._connect(stack, name, spec), timeout=60)
                    except Exception as e:
                        log.exception("MCP server %s failed", name)
                        self.failed[name] = str(e) or type(e).__name__
                self._ready.set()
                await self._shutdown.wait()
        finally:
            self._ready.set()

    async def _connect(self, stack, name, spec):
        if "url" in spec:
            read, write = await stack.enter_async_context(streamable_http_client(spec["url"]))
        else:
            params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=spec.get("env") or None)
            # A windowed EXE has no stderr, so server logs go to a file instead.
            read, write = await stack.enter_async_context(stdio_client(params, errlog=self.errlog))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        self.sessions[name] = session
        for tool in result.tools:
            self.tools.append({
                "name": f"{name}{SEP}{tool.name}"[:64],
                "description": (tool.description or tool.title or tool.name)[:1024],
                "input_schema": tool.input_schema or {"type": "object", "properties": {}},
                "eager_input_streaming": True,
            })
        log.info("MCP server %s: %d tools", name, len(result.tools))

    def owns(self, tool_name: str) -> bool:
        return any(t["name"] == tool_name for t in self.tools)

    def schema(self, tool_name: str) -> dict:
        return next(t["input_schema"] for t in self.tools if t["name"] == tool_name)

    def call(self, tool_name: str, arguments: dict):
        """Returns (content_blocks, is_error) in Claude tool_result format."""
        server, _, tool = tool_name.partition(SEP)
        future = asyncio.run_coroutine_threadsafe(self.sessions[server].call_tool(tool, arguments), self.loop)
        result = future.result(600)
        blocks = []
        for item in getattr(result, "content", None) or []:
            if item.type == "text":
                blocks.append({"type": "text", "text": item.text})
            elif item.type == "image":
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": item.mime_type, "data": item.data}})
            else:
                blocks.append({"type": "text", "text": item.model_dump_json(exclude_none=True)})
        if not blocks:
            structured = getattr(result, "structured_content", None)
            blocks.append({"type": "text", "text": str(structured) if structured else "(no output)"})
        return blocks, bool(getattr(result, "is_error", False))

    def close(self):
        if self._shutdown and self._main_future:
            self.loop.call_soon_threadsafe(self._shutdown.set)
            try:
                self._main_future.result(timeout=15)
            except Exception:
                log.exception("Error closing MCP servers")
        self.loop.call_soon_threadsafe(self.loop.stop)


# ---------------------------------------------------------------- the agent

BASE_PROMPT = """You are Desk Agent, an assistant running on the user's own {os} laptop.
Today is {today}.

You can see and control the screen with the computer tools, search the web, and use any MCP tools listed.
{coding}Prefer an MCP or coding tool over clicking through an app when one can do the job; it is faster and more reliable.
Take a screenshot before acting on the screen, and again after anything that changes it, so you work from what is actually there.
Ask the user before anything that spends money, sends a message on their behalf, deletes files, or changes account or security settings.
If a tool result says the user declined an action, stop and ask what they want instead of trying another route.
Keep replies short and plain. When the job is done, say what you did in a sentence or two."""

CODING_PROMPT = """You can write and run code in the projects folder ({workspace}) with list_files, read_file, write_file and run_command.
When asked to make a program, put each one in its own subfolder, write the files, run them to check they work, fix what breaks, and tell the user where to find it and how to start it.
"""

ROUTER_PROMPT = """Pick the Claude model for the job below. Choose the cheapest model that will do it well.

claude-haiku-5-5: chat, quick questions, simple lookups, one or two step jobs.
claude-sonnet-5-5: everyday jobs, writing, research, small programs, screen tasks with a few steps.
claude-opus-5-5: long screen tasks across several apps, real programs with several files, debugging, careful reasoning.
claude-fable-5-1: only for the hardest jobs, such as large or tricky software projects or long research where mistakes are costly.

Effort: low for trivial jobs, medium for most, high for hard ones.
{memory}
The job:
{task}"""

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "enum": list(MODELS)},
        "effort": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["model", "effort", "reason"],
    "additionalProperties": False,
}

LEARN_PROMPT = """You keep a short memory file about one user of a desktop assistant, so it gets more useful over time.

Current memory:
<memory>
{memory}
</memory>

A conversation that just finished:
<conversation>
{transcript}
</conversation>

Rewrite the memory to include anything new and lasting worth knowing next time: who they are, their work, preferences, apps and files they use, how they like things done, jobs they repeat and what worked or failed when doing them.
Leave out one off details, anything secret such as passwords or card numbers, and anything that only came from a web page rather than the user.
Keep it as short bullet points under headings, under 600 words. If nothing new was learned, return the memory unchanged."""


def _jsonable(block):
    if hasattr(block, "model_dump"):
        block = block.model_dump(exclude_none=True)
    if isinstance(block, dict):
        if block.get("type") == "image":
            return {"type": "text", "text": "[image]"}
        return {k: _jsonable(v) if isinstance(v, (dict, list)) or hasattr(v, "model_dump") else v for k, v in block.items()}
    if isinstance(block, list):
        return [_jsonable(b) for b in block]
    return block


class Agent:
    """One conversation. run() is called from one worker thread at a time."""

    def __init__(self, cfg: dict, ui, errlog):
        self.cfg = cfg
        self.ui = ui  # gui callbacks: say(), progress(), tool(), confirm(), stats_changed()
        self.client = anthropic.Anthropic(api_key=cfg["api_key"] or None, max_retries=4)
        self.computer = Computer(cfg["screenshot_max_width"]) if cfg["computer_use"] else None
        self.workspace = Workspace(cfg["workspace"]) if cfg["coding_tools"] else None
        self.mcp = MCPBridge(cfg.get("mcpServers", {}), errlog)
        self.stop_event = threading.Event()
        self.stats_lock = threading.Lock()
        self.reset()

    def start(self):
        self.mcp.start()
        self.tools = self._tools()
        return self.mcp.tools, self.mcp.failed

    def close(self):
        self.mcp.close()

    def reset(self):
        # The system prompt and tools stay fixed for a whole conversation: changing them
        # part way through would throw away the model's earlier reasoning.
        self.messages: list = []
        self.started = datetime.now()
        coding = CODING_PROMPT.format(workspace=self.cfg["workspace"]) if self.cfg["coding_tools"] else ""
        system = BASE_PROMPT.format(os=f"{platform.system()} {platform.release()}", today=date.today().isoformat(), coding=coding)
        memory = read_memory() if self.cfg["learn"] else ""
        if memory:
            system += f"\n\nWhat you have learned about this user from earlier conversations:\n<memory>\n{memory}\n</memory>"
        self.system = system

    def _tools(self) -> list:
        tools = []
        if self.computer:
            tools.append({"type": "computer_toolset_20260801"})
        if self.cfg["web_search"]:
            # The basic web search version works on every model, so switching models
            # part way through a chat keeps the same tool list.
            tools.append({"type": "web_search_20250305", "name": "web_search", "max_uses": 5})
        if self.workspace:
            tools.extend(CODING_TOOLS)
        tools.extend(self.mcp.tools)
        return tools

    # model choice

    def choose(self, text: str):
        """Returns (model, effort, reason)."""
        if self.cfg["model"] != AUTO:
            return self.cfg["model"], self.cfg["effort"], None
        memory = read_memory() if self.cfg["learn"] else ""
        memory = f"\nWhat is known about this user:\n{memory}\n" if memory else ""
        recent = [m["content"] for m in self.messages[-6:] if m["role"] == "user" and isinstance(m["content"], str)]
        task = "\n".join(recent[-2:] + [text])
        try:
            resp = self.client.messages.create(
                model=ROUTER_MODEL,
                max_tokens=2000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": ROUTER_SCHEMA}},
                messages=[{"role": "user", "content": ROUTER_PROMPT.format(memory=memory, task=task[-6000:])}],
            )
            self._count(resp)
            pick = json.loads(next(b.text for b in resp.content if b.type == "text"))
            return pick["model"], pick["effort"], pick["reason"]
        except (anthropic.APIConnectionError, anthropic.APIStatusError, StopIteration, ValueError, KeyError):
            log.exception("router failed")
            return "claude-opus-5-5", "medium", "Auto could not decide, so using Opus"

    def _request(self, model, effort):
        info = MODELS.get(model, MODELS["claude-opus-5-5"])
        kwargs = {}
        betas = []
        if info["fallbacks"]:
            kwargs["fallbacks"] = "default"
            betas.append("server-side-fallback-2026-07-01")
        if info["updates"]:
            thinking = {"type": "adaptive", "display": "updates"}
            betas.append("thinking-display-updates-2026-08-18")
        else:
            thinking = {"type": "adaptive", "display": "summarized"}
        # Python raises ValueError when a streamed tool input is not valid JSON. Retry a
        # couple of times; API errors (auth, rate limit) are left to propagate.
        for attempt in range(3):
            try:
                with self.client.beta.messages.stream(
                    model=model,
                    max_tokens=64000,
                    system=self.system,
                    tools=self.tools,
                    messages=self.messages,
                    thinking=thinking,
                    output_config={"effort": effort},
                    cache_control={"type": "ephemeral"},
                    betas=betas,
                    **kwargs,
                ) as stream:
                    return stream.get_final_message()
            except ValueError:
                if attempt == 2:
                    raise
                log.warning("Unparseable tool input, retrying")

    # running a job

    def run(self, text: str):
        self.stop_event.clear()
        model, effort, reason = self.choose(text)
        if reason:
            self.ui.progress(f"Auto picked {MODELS[model]['label'].split(' (')[0]} at {effort} effort: {reason}")
        if self.messages and self.messages[-1]["role"] == "user":
            # The last turn ended without a reply (refusal or error), so add to that user message.
            last = self.messages[-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].append({"type": "text", "text": text})
        else:
            self.messages.append({"role": "user", "content": text})
        try:
            self._loop(model, effort)
        except pyautogui.FailSafeException:
            self._abandon_pending()
            self.ui.say("Stopped: the mouse hit a screen corner (emergency stop).", kind="error")
        except anthropic.AuthenticationError:
            self.ui.say("Your API key was rejected. Check it in Settings.", kind="error")
        except anthropic.APIError as e:
            self._abandon_pending()
            self.ui.say(f"API error: {e}", kind="error")
        finally:
            self._finish_job(model)

    def _abandon_pending(self):
        # A tool_use with no tool_result would make every later request fail, so answer it.
        if self.messages and self.messages[-1]["role"] == "assistant":
            pending = [b for b in self.messages[-1]["content"] if b.type == "tool_use"]
            if pending:
                self.messages.append({"role": "user", "content": [self._error(b, "Stopped by the user.") for b in pending]})

    def _loop(self, model, effort):
        for _ in range(self.cfg["max_steps"]):
            if self.stop_event.is_set():
                self.ui.say("Stopped.", kind="error")
                return

            resp = self._request(model, effort)
            self._count(resp)
            self.messages.append({"role": "assistant", "content": resp.content})

            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    self.ui.say(block.text)
                elif block.type == "thinking" and block.thinking.strip():
                    self.ui.progress(block.thinking)
                elif block.type == "server_tool_use":
                    self.ui.tool(f"web search: {block.input.get('query', '')}")

            if resp.stop_reason == "refusal":
                self.messages.pop()
                self.ui.say("Claude declined this request.", kind="error")
                return
            if resp.stop_reason == "pause_turn":
                continue

            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                return

            if resp.stop_reason == "max_tokens":
                results = [self._error(b, "Output was cut off before this call finished. Try again in smaller pieces.") for b in calls]
            else:
                results = self._run_tools(calls)
            self.messages.append({"role": "user", "content": results})

        self.ui.say(f"Stopped after {self.cfg['max_steps']} steps. Say 'continue' to keep going.", kind="error")

    def _run_tools(self, calls):
        results = []
        computer_failed = False
        for call in calls:
            if self.stop_event.is_set():
                results.append(self._error(call, "Stopped by the user."))
                continue
            if call.toolset_name == "computer":
                if computer_failed:
                    results.append(self._error(call, "Not executed: an earlier computer action in this turn failed."))
                    continue
                result = self._computer(call)
                computer_failed = result.get("is_error", False)
                results.append(result)
            elif call.name in CODING_TOOL_NAMES and self.workspace:
                results.append(self._coding(call))
            elif self.mcp.owns(call.name):
                results.append(self._mcp(call))
            else:
                results.append(self._error(call, f"Unknown tool {call.name}"))
        return results

    def _describe(self, call) -> str:
        return f"{call.name} {json.dumps(call.input)[:300]}"

    def _needs_confirm(self, call) -> bool:
        if not self.cfg["confirm_actions"]:
            return False
        harmless = {"screenshot", "zoom", "cursor_position", "wait", "mouse_move", "scroll", "list_files", "read_file"}
        return call.name not in harmless

    def _computer(self, call):
        self.ui.tool(self._describe(call))
        if self._needs_confirm(call) and not self.ui.confirm(self._describe(call)):
            return self._error(call, "The user declined this action.")
        try:
            kind, data = self.computer.run(call.name, call.input)
        except pyautogui.FailSafeException:
            raise
        except Exception as e:
            log.exception("computer action failed")
            return self._error(call, f"{type(e).__name__}: {e}")
        if kind == "image":
            content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]
        else:
            content = [{"type": "text", "text": data}]
        return {"type": "tool_result", "tool_use_id": call.id, "toolset_name": "computer", "content": content}

    def _validated(self, call, schema):
        try:
            jsonschema.validate(call.input, schema)
        except jsonschema.ValidationError as e:
            return self._error(call, json.dumps({"INVALID_JSON": json.dumps(call.input), "problem": e.message}))
        return None

    def _coding(self, call):
        self.ui.tool(self._describe(call))
        schema = next(t["input_schema"] for t in CODING_TOOLS if t["name"] == call.name)
        if bad := self._validated(call, schema):
            return bad
        if self._needs_confirm(call) and not self.ui.confirm(self._describe(call)):
            return self._error(call, "The user declined this action.")
        try:
            return {"type": "tool_result", "tool_use_id": call.id, "content": self.workspace.run(call.name, call.input)}
        except Exception as e:
            return self._error(call, f"{type(e).__name__}: {e}")

    def _mcp(self, call):
        self.ui.tool(self._describe(call))
        if bad := self._validated(call, self.mcp.schema(call.name)):
            return bad
        if self._needs_confirm(call) and not self.ui.confirm(self._describe(call)):
            return self._error(call, "The user declined this action.")
        try:
            content, is_error = self.mcp.call(call.name, call.input)
        except Exception as e:
            log.exception("MCP call failed")
            return self._error(call, f"{type(e).__name__}: {e}")
        return {"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error}

    @staticmethod
    def _error(call, message):
        result = {"type": "tool_result", "tool_use_id": call.id, "is_error": True, "content": message}
        if getattr(call, "toolset_name", None):
            result["toolset_name"] = call.toolset_name
        return result

    # learning from use

    def _count(self, resp):
        u = resp.usage
        price_in, price_out = MODELS.get(resp.model, MODELS["claude-opus-5-5"])["price"]
        cost = (
            (u.input_tokens or 0) * price_in
            + (getattr(u, "cache_read_input_tokens", 0) or 0) * price_in * 0.1
            + (getattr(u, "cache_creation_input_tokens", 0) or 0) * price_in * 1.25
            + (u.output_tokens or 0) * price_out
        ) / 1_000_000
        with self.stats_lock:
            stats = load_stats()
            stats["spend_usd"] = round(stats.get("spend_usd", 0) + cost, 4)
            with stats_path().open("w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
        self.ui.stats_changed()

    def _finish_job(self, model):
        with self.stats_lock:
            stats = load_stats()
            stats["jobs"] = stats.get("jobs", 0) + 1
            stats.setdefault("models", {})[model] = stats["models"].get(model, 0) + 1
            stats["last_used"] = datetime.now().isoformat(timespec="seconds")
            with stats_path().open("w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
        self.ui.stats_changed()

        history = app_dir() / "history"
        history.mkdir(exist_ok=True)
        with (history / f"{self.started:%Y-%m-%d_%H%M%S}.json").open("w", encoding="utf-8") as f:
            json.dump(_jsonable(self.messages), f, indent=1)

        if self.cfg["learn"] and self.messages:
            threading.Thread(target=self._learn, args=(list(self.messages),), daemon=True).start()

    def _learn(self, messages):
        lines = []
        for m in messages:
            content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            for b in content:
                b = _jsonable(b)
                if b.get("type") == "text" and b.get("text") and b["text"] != "[image]":
                    lines.append(f"{'User' if m['role'] == 'user' else 'Assistant'}: {b['text']}")
                elif b.get("type") == "tool_use":
                    lines.append(f"(used {b.get('name')})")
        transcript = "\n".join(lines)[-40_000:]
        try:
            resp = self.client.messages.create(
                model=ROUTER_MODEL,
                max_tokens=8000,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": LEARN_PROMPT.format(memory=read_memory() or "(empty)", transcript=transcript)}],
            )
            self._count(resp)
            if resp.stop_reason != "end_turn":
                return
            text = next((b.text for b in resp.content if b.type == "text"), "").strip()
            if text:
                memory_path().write_text(text + "\n", encoding="utf-8")
        except Exception:
            log.exception("learning failed")


# ---------------------------------------------------------------- the window

class App:
    """The agent runs on a worker thread; all widget updates go through a queue that
    the Tk main loop drains, because Tk is not thread safe."""

    def __init__(self, errlog):
        self.errlog = errlog
        self.cfg = load_config()
        self.agent: Agent | None = None
        self.busy = False
        self.events: queue.Queue = queue.Queue()
        self.ready_text = "Ready"

        self.root = tk.Tk()
        self.root.title("Desk Agent")
        self.root.geometry("820x640")
        self.root.minsize(520, 380)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._build()
        self._show_stats()
        self.root.after(50, self._drain)

        if not self.cfg["api_key"] and not os.environ.get("ANTHROPIC_API_KEY"):
            self.root.after(200, self.open_settings)
        else:
            self._start_agent()

    def _build(self):
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Model").pack(side="left")
        self.model_names = {AUTO_LABEL: AUTO, **{m["label"]: k for k, m in MODELS.items()}}
        self.model_box = ttk.Combobox(top, values=list(self.model_names), state="readonly", width=38)
        current = self.cfg["model"]
        self.model_box.set(AUTO_LABEL if current == AUTO else MODELS.get(current, MODELS["claude-opus-5-5"])["label"])
        self.model_box.bind("<<ComboboxSelected>>", self._model_changed)
        self.model_box.pack(side="left", padx=6)
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="Memory", command=lambda: open_file(self._ensure_memory())).pack(side="right", padx=4)
        ttk.Button(top, text="New chat", command=self.new_chat).pack(side="right")

        info = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        info.pack(fill="x")
        self.status = ttk.Label(info, text="Starting...")
        self.status.pack(side="left")
        self.stats_label = ttk.Label(info, foreground="#707070")
        self.stats_label.pack(side="right")

        self.log = scrolledtext.ScrolledText(self.root, wrap="word", state="disabled", font=("Segoe UI", 10), padx=8, pady=8)
        self.log.pack(fill="both", expand=True, padx=8)
        self.log.tag_config("you", foreground="#1a4fa0", font=("Segoe UI", 10, "bold"))
        self.log.tag_config("claude", foreground="#202020")
        self.log.tag_config("progress", foreground="#707070", font=("Segoe UI", 9, "italic"))
        self.log.tag_config("tool", foreground="#8a6d00", font=("Consolas", 9))
        self.log.tag_config("error", foreground="#b00020")

        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill="x")
        self.entry = tk.Text(bottom, height=3, wrap="word", font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_enter)
        buttons = ttk.Frame(bottom)
        buttons.pack(side="right", padx=(6, 0))
        self.send_btn = ttk.Button(buttons, text="Send", command=self.send)
        self.send_btn.pack(fill="x")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(fill="x", pady=(4, 0))

        self._write("Type a job below, for example \"Open Notepad and write a shopping list\" "
                    "or \"Make me a Python program that renames my holiday photos by date\".\n"
                    "Emergency stop: shove the mouse into any corner of the screen.\n\n", "progress")

    def _write(self, text, tag):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _ensure_memory(self):
        path = memory_path()
        if not path.exists():
            path.write_text("", encoding="utf-8")
        return path

    def _show_stats(self):
        s = load_stats()
        since = s.get("first_used", date.today().isoformat())
        self.stats_label.config(text=f"{s.get('jobs', 0)} jobs since {since}  |  about ${s.get('spend_usd', 0):.2f} spent")

    def _model_changed(self, _event=None):
        self.cfg["model"] = self.model_names[self.model_box.get()]
        save_config(self.cfg)
        if self.agent:
            self.agent.cfg["model"] = self.cfg["model"]

    # agent lifecycle

    def _start_agent(self):
        if self.agent:
            old, self.agent = self.agent, None
            threading.Thread(target=old.close, daemon=True).start()
        self.status.config(text="Connecting to MCP servers...")
        agent = Agent(self.cfg, self, self.errlog)

        def work():
            tools, failed = agent.start()
            self.agent = agent
            self.events.put(("ready", (len(tools), failed)))

        threading.Thread(target=work, daemon=True).start()

    def _on_enter(self, event):
        if not event.state & 0x1:  # Shift+Enter makes a new line
            self.send()
            return "break"

    def send(self):
        text = self.entry.get("1.0", "end").strip()
        if not text or self.busy or not self.agent:
            return
        self.entry.delete("1.0", "end")
        self._write(f"You: {text}\n\n", "you")
        self._set_busy(True)
        if self.cfg["computer_use"]:
            # Get out of the way so Claude sees the desktop, not this window.
            self.root.iconify()

        def work():
            try:
                self.agent.run(text)
            except Exception as e:
                log.exception("agent crashed")
                self.events.put(("say", (f"Something went wrong: {e}\n\n", "error")))
            self.events.put(("done", None))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        if self.agent:
            self.agent.stop_event.set()
            self.status.config(text="Stopping after the current step...")

    def new_chat(self):
        if self.busy or not self.agent:
            return
        self.agent.reset()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy):
        self.busy = busy
        self.send_btn.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        self.status.config(text="Working..." if busy else self.ready_text)

    # callbacks from the worker thread

    def say(self, text, kind="claude"):
        self.events.put(("say", (text.strip() + "\n\n", kind)))

    def progress(self, text):
        self.events.put(("say", (text.strip() + "\n", "progress")))

    def tool(self, text):
        self.events.put(("say", (f"> {text}\n", "tool")))

    def stats_changed(self):
        self.events.put(("stats", None))

    def confirm(self, description) -> bool:
        answer = {}
        done = threading.Event()
        self.events.put(("confirm", (description, answer, done)))
        done.wait()
        return answer.get("ok", False)

    def _drain(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "say":
                    self._write(*data)
                elif kind == "stats":
                    self._show_stats()
                elif kind == "ready":
                    count, failed = data
                    self.ready_text = f"Ready  |  {count} MCP tools"
                    if failed:
                        self.ready_text += f"  |  failed: {', '.join(failed)}"
                        self._write("Some MCP servers did not start:\n"
                                    + "".join(f"  {k}: {v}\n" for k, v in failed.items())
                                    + f"Details in {app_dir() / 'mcp_servers.log'}\n\n", "error")
                    if not self.busy:
                        self.status.config(text=self.ready_text)
                elif kind == "confirm":
                    description, answer, done = data
                    answer["ok"] = self._ask(description)
                    done.set()
                elif kind == "done":
                    self._set_busy(False)
                    self.root.deiconify()
                    self.root.lift()
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _ask(self, description) -> bool:
        win = tk.Toplevel(self.root)
        win.title("Allow this action?")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        result = {"ok": False}
        ttk.Label(win, text="Claude wants to:", padding=(12, 12, 12, 4)).pack(anchor="w")
        ttk.Label(win, text=description, wraplength=420, padding=(12, 0, 12, 12), font=("Consolas", 9)).pack(anchor="w")
        row = ttk.Frame(win, padding=12)
        row.pack(fill="x")

        def close(ok):
            result["ok"] = ok
            win.destroy()

        def allow_all():
            self.cfg["confirm_actions"] = False
            close(True)

        ttk.Button(row, text="Allow", command=lambda: close(True)).pack(side="left")
        ttk.Button(row, text="Allow all for this session", command=allow_all).pack(side="left", padx=6)
        ttk.Button(row, text="Deny", command=lambda: close(False)).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_width()) // 2
        y = (win.winfo_screenheight() - win.winfo_height()) // 3
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.focus_force()
        self.root.wait_window(win)
        return result["ok"]

    # settings

    def open_settings(self):
        if self.busy:
            return
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        key = tk.StringVar(value=self.cfg["api_key"])
        effort = tk.StringVar(value=self.cfg["effort"])
        workspace = tk.StringVar(value=self.cfg["workspace"])
        checks = {
            "computer_use": ("Let Claude control the mouse and keyboard", tk.BooleanVar(value=self.cfg["computer_use"])),
            "confirm_actions": ("Ask me before each click, keypress, command or MCP call", tk.BooleanVar(value=self.cfg["confirm_actions"])),
            "coding_tools": ("Let Claude write and run code in the projects folder", tk.BooleanVar(value=self.cfg["coding_tools"])),
            "web_search": ("Allow web search", tk.BooleanVar(value=self.cfg["web_search"])),
            "learn": ("Learn from my conversations (keeps a memory file you can edit)", tk.BooleanVar(value=self.cfg["learn"])),
        }

        ttk.Label(frm, text="Anthropic API key (console.anthropic.com)").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(frm, textvariable=key, show="*", width=56).grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 10))
        ttk.Label(frm, text="Effort when you pick a model yourself").grid(row=2, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=effort, values=EFFORTS, state="readonly", width=12).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="Projects folder").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frm, textvariable=workspace, width=56).grid(row=4, column=0, columnspan=2, sticky="we")
        for i, (label, var) in enumerate(checks.values()):
            ttk.Checkbutton(frm, text=label, variable=var).grid(row=5 + i, column=0, columnspan=2, sticky="w", pady=(10 if i == 0 else 0, 0))
        row = 5 + len(checks)
        ttk.Label(frm, text="MCP servers are set in the config file (same format as Claude Desktop). "
                            "Saving starts a new chat.", wraplength=400).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
        ttk.Button(frm, text="Open config file", command=lambda: open_file(config_path())).grid(row=row + 1, column=0, sticky="w")

        def save():
            self.cfg = load_config()  # pick up any hand edits to mcpServers
            self.cfg.update(api_key=key.get().strip(), effort=effort.get(), workspace=workspace.get().strip() or DEFAULTS["workspace"],
                            model=self.model_names[self.model_box.get()],
                            **{name: var.get() for name, (_, var) in checks.items()})
            save_config(self.cfg)
            win.destroy()
            self._start_agent()

        ttk.Button(frm, text="Save", command=save).grid(row=row + 2, column=1, sticky="e", pady=(14, 0))
        win.grab_set()

    def quit(self):
        if self.agent:
            self.agent.stop_event.set()
            threading.Thread(target=self.agent.close, daemon=True).start()
        self.root.after(300, self.root.destroy)

    def run(self):
        self.root.mainloop()


def main():
    logging.basicConfig(
        filename=app_dir() / "deskagent.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
    # MCP servers write their own logs here; a windowed EXE has no console to show them.
    errlog = open(app_dir() / "mcp_servers.log", "a", encoding="utf-8", buffering=1)
    App(errlog).run()


if __name__ == "__main__":
    main()
