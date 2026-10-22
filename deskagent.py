"""Desk Agent: your own AI that runs on your laptop.

No accounts, no API keys, no internet needed once a model is downloaded. The AI
runs on the laptop through llama.cpp (llama-server.exe, shipped with the app).

It can chat, write and run code in a projects folder, open apps, files and web
pages, type into windows, read web pages, and use any MCP server you plug in. You
can switch between models or leave it on Auto, which picks one for each job. It
keeps a memory of what it learns about you and gets more useful the more you use it.

Everything is in this one file. Build it with build.bat, or let the GitHub
workflow make the installer.
"""

import asyncio
import html
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import jsonschema
import pyautogui
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger("deskagent")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------- models

# Downloadable models. The exact file is looked up on Hugging Face at download time,
# picking the first single file whose name contains the quant, so renamed files still work.
CATALOG = {
    "quick": {
        "label": "Quick (Qwen 2.5, 1.5B)",
        "repo": "bartowski/Qwen2.5-1.5B-Instruct-GGUF",
        "quant": "Q4_K_M",
        "size_gb": 1.0,
        "about": "Fast replies, fine for chat and simple jobs. Runs on almost anything.",
    },
    "general": {
        "label": "General (Qwen 3, 4B)",
        "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF",
        "quant": "Q4_K_M",
        "size_gb": 2.5,
        "about": "The best all rounder for this laptop. Good at using tools. Start with this one.",
    },
    "coder": {
        "label": "Coder (Qwen 2.5 Coder, 7B)",
        "repo": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
        "quant": "Q4_K_M",
        "size_gb": 4.7,
        "about": "Best at writing programs. Slower, and wants 8 GB of RAM or more.",
    },
}
AUTO = "auto"
AUTO_LABEL = "Auto (picks a model for each job)"
CODE_WORDS = re.compile(
    r"\b(code|coding|program|script|python|javascript|html|css|function|bug|debug|compile|"
    r"app|website|game|sql|class|error|traceback|refactor|api)\b", re.I)
ACTION_WORDS = re.compile(r"\b(open|make|create|write|build|run|find|search|save|install|type|fix|file|folder)\b", re.I)


# ---------------------------------------------------------------- settings and saved data

DEFAULTS = {
    "model": AUTO,
    "context": 8192,
    "threads": 0,  # 0 means let llama.cpp decide
    "desktop_tools": True,
    "web_tools": True,
    "coding_tools": True,
    "learn": True,
    "confirm_actions": True,
    "max_steps": 25,
    "workspace": str(Path.home() / "Documents" / "DeskAgent Projects"),
    "custom_models": {},  # name -> path to a .gguf file you added yourself
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


def models_dir() -> Path:
    # Model files are gigabytes, so they go in Local, not the Roaming profile.
    base = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share"
    path = Path(base) / "DeskAgent" / "models"
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
        return {"first_used": date.today().isoformat(), "jobs": 0, "words_written": 0, "models": {}}


def save_stats(stats: dict) -> None:
    with stats_path().open("w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)


def open_file(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", str(path)])


def model_file(key: str, cfg: dict) -> Path | None:
    """Where a model's .gguf lives, or None if it isn't downloaded."""
    if key in cfg.get("custom_models", {}):
        path = Path(cfg["custom_models"][key])
        return path if path.exists() else None
    folder = models_dir() / key
    files = sorted(folder.glob("*.gguf")) if folder.exists() else []
    return files[0] if files else None


def installed_models(cfg: dict) -> dict:
    """key -> label for every model that is ready to use."""
    found = {k: m["label"] for k, m in CATALOG.items() if model_file(k, cfg)}
    found.update({k: f"{k} (your file)" for k in cfg.get("custom_models", {}) if model_file(k, cfg)})
    return found


# ---------------------------------------------------------------- downloading models

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "DeskAgent"})
    return urllib.request.urlopen(req, timeout=timeout)


def download_model(key: str, progress, cancel: threading.Event):
    """Downloads a catalog model. progress(done_bytes, total_bytes) is called as it goes."""
    entry = CATALOG[key]
    with http_get(f"https://huggingface.co/api/models/{entry['repo']}") as r:
        files = [s["rfilename"] for s in json.load(r).get("siblings", [])]
    quant = entry["quant"].lower()
    choices = [f for f in files if f.lower().endswith(".gguf") and quant in f.lower() and "-of-" not in f]
    if not choices:
        raise RuntimeError(f"No {entry['quant']} file found in {entry['repo']}")
    name = sorted(choices, key=len)[0]
    url = f"https://huggingface.co/{entry['repo']}/resolve/main/{urllib.parse.quote(name)}"

    folder = models_dir() / key
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (name + ".part")
    done = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "DeskAgent", "Range": f"bytes={done}-"})
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status != 206:  # server ignored the resume request, start again
            done = 0
        total = done + int(r.headers.get("Content-Length", 0))
        with part.open("ab" if done else "wb") as f:
            while chunk := r.read(1 << 20):
                if cancel.is_set():
                    return None
                f.write(chunk)
                done += len(chunk)
                progress(done, total)
    final = folder / name
    part.replace(final)
    return final


# ---------------------------------------------------------------- the local AI engine

def find_llama_server() -> Path | None:
    exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    for candidate in (here / "llama" / exe, here / exe):
        if candidate.exists():
            return candidate
    found = shutil.which("llama-server")
    return Path(found) if found else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Engine:
    """Runs llama-server in the background and talks to it over its local
    OpenAI style chat API. Nothing leaves the laptop."""

    def __init__(self, cfg: dict, errlog):
        self.cfg = cfg
        self.errlog = errlog
        self.proc: subprocess.Popen | None = None
        self.model: str | None = None
        self.port = 0
        self.lock = threading.Lock()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def load(self, key: str, status=lambda text: None):
        """Starts llama-server with the given model, unless it is already running it."""
        with self.lock:
            if self.model == key and self.proc and self.proc.poll() is None:
                return
            self.stop()
            server = find_llama_server()
            if not server:
                raise RuntimeError("llama-server was not found. Reinstall Desk Agent, or put llama-server.exe in a 'llama' folder next to the app.")
            path = model_file(key, self.cfg)
            if not path:
                raise RuntimeError(f"The {key} model is not downloaded. Open Models to get it.")
            status(f"Loading {path.name}...")
            self.port = free_port()
            args = [str(server), "-m", str(path), "--host", "127.0.0.1", "--port", str(self.port),
                    "-c", str(self.cfg["context"]), "--jinja"]
            if self.cfg["threads"]:
                args += ["-t", str(self.cfg["threads"])]
            log.info("starting %s", " ".join(args))
            self.proc = subprocess.Popen(args, stdout=self.errlog, stderr=self.errlog, creationflags=NO_WINDOW)
            deadline = time.time() + 300
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"The model failed to load. Details in {app_dir() / 'engine.log'}")
                try:
                    with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                        if r.status == 200:
                            self.model = key
                            return
                except OSError:
                    pass
                time.sleep(0.5)
            raise RuntimeError("The model took too long to load.")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.model = None

    def chat(self, messages, tools=None, on_text=lambda t: None, stop_event=None, max_tokens=4096):
        """Streams one reply. Returns the assistant message (content and any tool_calls)."""
        body = {"messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": 0.4}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        content, calls, finish = [], {}, None
        with urllib.request.urlopen(req, timeout=900) as r:
            for raw in r:
                if stop_event and stop_event.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                choice = json.loads(data)["choices"][0]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    on_text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
                finish = choice.get("finish_reason") or finish
        message = {"role": "assistant", "content": strip_thinking("".join(content))}
        if calls:
            message["tool_calls"] = [
                {"id": c["id"] or f"call_{i}", "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for i, c in sorted(calls.items())
            ]
        return message, finish


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


# ---------------------------------------------------------------- tools

def _tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


CODING_TOOLS = [
    _tool("list_files", "List files and folders in the projects folder. Path is relative to the projects folder; use '.' for the top.",
          {"path": {"type": "string"}}, ["path"]),
    _tool("read_file", "Read a text file from the projects folder.", {"path": {"type": "string"}}, ["path"]),
    _tool("write_file", "Create or overwrite a text file in the projects folder. Folders are made as needed. Always send the whole file.",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("run_command", "Run a command in a Windows command prompt inside the projects folder, for example 'python app.py'. Times out after 120 seconds.",
          {"command": {"type": "string"}, "folder": {"type": "string", "description": "Subfolder to run in. Defaults to the top."}}, ["command"]),
]
DESKTOP_TOOLS = [
    _tool("open", "Open an app, file, folder or web address on the laptop, for example 'notepad', 'calc', 'C:\\\\Users\\\\me\\\\Documents' or 'https://bbc.co.uk'.",
          {"target": {"type": "string"}}, ["target"]),
    _tool("list_windows", "List the titles of the windows that are open.", {}, []),
    _tool("focus_window", "Bring a window to the front, matching part of its title.", {"title": {"type": "string"}}, ["title"]),
    _tool("type_text", "Type text into whichever window is at the front. Call focus_window first.", {"text": {"type": "string"}}, ["text"]),
    _tool("press_keys", "Press a key or shortcut in the front window, for example 'enter', 'ctrl+s', 'alt+tab'.", {"keys": {"type": "string"}}, ["keys"]),
]
WEB_TOOLS = [
    _tool("web_search", "Search the web and get the top results with links.", {"query": {"type": "string"}}, ["query"]),
    _tool("read_webpage", "Fetch a web page and return its text.", {"url": {"type": "string"}}, ["url"]),
]
LOCAL_TOOLS = {t["function"]["name"]: t for t in CODING_TOOLS + DESKTOP_TOOLS + WEB_TOOLS}
HARMLESS = {"list_files", "read_file", "list_windows", "web_search", "read_webpage"}


class Workspace:
    """The one folder the coding tools may touch."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, rel: str) -> Path:
        path = (self.root / (rel or ".")).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"{rel} is outside the projects folder")
        return path


def _combo(text: str) -> list[str]:
    aliases = {"return": "enter", "escape": "esc", "control": "ctrl", "windows": "win", "super": "win"}
    return [aliases.get(k.strip().lower(), k.strip().lower()) for k in text.split("+") if k.strip()]


def _paste(text: str):
    root = tk.Tk()
    root.withdraw()
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    root.destroy()


def _page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|p|div|li|h\d|tr)[^>]*>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def run_local_tool(name: str, args: dict, workspace: Workspace | None) -> str:
    if name == "list_files":
        path = workspace.path(args["path"])
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        return "\n".join(f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)" for p in entries[:300]) or "(empty folder)"
    if name == "read_file":
        return workspace.path(args["path"]).read_text(encoding="utf-8", errors="replace")
    if name == "write_file":
        path = workspace.path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Saved {path}"
    if name == "run_command":
        proc = subprocess.run(args["command"], shell=True, cwd=workspace.path(args.get("folder", ".")), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW)
        return f"Exit code {proc.returncode}\n{(proc.stdout + chr(10) + proc.stderr).strip()}"
    if name == "open":
        target = args["target"]
        if sys.platform == "win32":
            os.startfile(target) if Path(target).exists() or "://" in target else subprocess.Popen(f'start "" {target}', shell=True)
        else:
            subprocess.Popen(["xdg-open", target])
        time.sleep(1.5)
        return f"Opened {target}"
    if name == "list_windows":
        titles = [t for t in pyautogui.getAllTitles() if t.strip()] if hasattr(pyautogui, "getAllTitles") else []
        return "\n".join(titles) or "No windows found."
    if name == "focus_window":
        wins = [w for w in pyautogui.getWindowsWithTitle(args["title"]) if w.title.strip()]
        if not wins:
            return f"No window with '{args['title']}' in its title."
        win = wins[0]
        if win.isMinimized:
            win.restore()
        win.activate()
        time.sleep(0.5)
        return f"'{win.title}' is now at the front."
    if name == "type_text":
        text = args["text"]
        pyautogui.write(text, interval=0.01) if text.isascii() else _paste(text)
        return "Typed."
    if name == "press_keys":
        pyautogui.hotkey(*_combo(args["keys"]))
        time.sleep(0.3)
        return "Pressed."
    if name == "web_search":
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(args["query"])
        with http_get(url) as r:
            page = r.read().decode("utf-8", "replace")
        results = []
        for href, title in re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page)[:8]:
            link = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("uddg", [href])[0]
            results.append(f"{_page_text(title)}\n{link}")
        return "\n\n".join(results) or "No results."
    if name == "read_webpage":
        with http_get(args["url"]) as r:
            return _page_text(r.read(3_000_000).decode("utf-8", "replace"))
    raise ValueError(f"Unknown tool {name}")


# ---------------------------------------------------------------- MCP servers

SEP = "__"


class MCPBridge:
    """Connects to MCP servers and exposes their tools to the model. The MCP client is
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
            self.tools.append(_tool(f"{name}{SEP}{tool.name}"[:64], (tool.description or tool.title or tool.name)[:1024],
                                    {}, []))
            self.tools[-1]["function"]["parameters"] = tool.input_schema or {"type": "object", "properties": {}}
        log.info("MCP server %s: %d tools", name, len(result.tools))

    def owns(self, tool_name: str) -> bool:
        return any(t["function"]["name"] == tool_name for t in self.tools)

    def schema(self, tool_name: str) -> dict:
        return next(t["function"]["parameters"] for t in self.tools if t["function"]["name"] == tool_name)

    def call(self, tool_name: str, arguments: dict) -> str:
        server, _, tool = tool_name.partition(SEP)
        future = asyncio.run_coroutine_threadsafe(self.sessions[server].call_tool(tool, arguments), self.loop)
        result = future.result(600)
        parts = []
        for item in getattr(result, "content", None) or []:
            if item.type == "text":
                parts.append(item.text)
            elif item.type == "image":
                parts.append("[an image was returned, which this model cannot see]")
            else:
                parts.append(item.model_dump_json(exclude_none=True))
        text = "\n".join(parts) or str(getattr(result, "structured_content", None) or "(no output)")
        return ("ERROR: " + text) if getattr(result, "is_error", False) else text

    def close(self):
        if self._shutdown and self._main_future:
            self.loop.call_soon_threadsafe(self._shutdown.set)
            try:
                self._main_future.result(timeout=15)
            except Exception:
                log.exception("Error closing MCP servers")
        self.loop.call_soon_threadsafe(self.loop.stop)


# ---------------------------------------------------------------- the agent

BASE_PROMPT = """You are Desk Agent, a helpful assistant running entirely on the user's own Windows laptop. Nothing they say leaves the laptop.
Today is {today}.

{tools}
Use a tool whenever it helps, one step at a time, and look at each result before the next step.
Ask before deleting files or anything else that can't be undone.
If a tool result says the user declined, stop and ask what they want instead.
Keep replies short and plain. When a job is done, say what you did in a sentence or two."""

TOOL_PROMPTS = {
    "coding_tools": "You can write and run code in the projects folder ({workspace}). For a new program, make a subfolder, write the files, run them to check they work, fix what breaks, then tell the user where it is and how to start it.",
    "desktop_tools": "You can open apps, files and web addresses, switch between windows, type, and press keys. You cannot see the screen, so use list_windows to check what is open, and focus_window before typing.",
    "web_tools": "You can search the web and read web pages. Say where information came from.",
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

Rewrite the memory to include anything new and lasting worth knowing next time: who they are, their work, preferences, apps and files they use, how they like things done, and jobs they repeat.
Leave out one off details, and anything secret such as passwords or card numbers.
Reply with only the new memory as short bullet points, under 300 words. If nothing new was learned, reply with the memory unchanged."""


class Agent:
    """One conversation. run() is called from one worker thread at a time."""

    def __init__(self, cfg: dict, ui, errlog):
        self.cfg = cfg
        self.ui = ui  # gui callbacks: stream(), say(), progress(), tool(), confirm(), stats_changed()
        self.engine = Engine(cfg, open(app_dir() / "engine.log", "a", encoding="utf-8", buffering=1))
        self.workspace = Workspace(cfg["workspace"]) if cfg["coding_tools"] else None
        self.mcp = MCPBridge(cfg.get("mcpServers", {}), errlog)
        self.stop_event = threading.Event()
        self.reset()

    def start(self):
        self.mcp.start()
        return self.mcp.tools, self.mcp.failed

    def close(self):
        self.engine.stop()
        self.mcp.close()

    def reset(self):
        self.messages: list = []
        self.started = datetime.now()

    def system_prompt(self) -> str:
        parts = [TOOL_PROMPTS[k].format(workspace=self.cfg["workspace"]) for k in TOOL_PROMPTS if self.cfg[k]]
        if self.mcp.tools:
            parts.append("You also have extra tools from MCP servers; their names start with the server name.")
        prompt = BASE_PROMPT.format(today=date.today().strftime("%A %d %B %Y"), tools="\n".join(parts) + "\n" if parts else "")
        memory = read_memory() if self.cfg["learn"] else ""
        if memory:
            prompt += f"\n\nWhat you have learned about this user from earlier conversations:\n{memory}"
        return prompt

    def tools(self) -> list:
        tools = []
        if self.cfg["coding_tools"]:
            tools += CODING_TOOLS
        if self.cfg["desktop_tools"]:
            tools += DESKTOP_TOOLS
        if self.cfg["web_tools"]:
            tools += WEB_TOOLS
        return tools + self.mcp.tools

    def choose(self, text: str):
        """Returns (model_key, reason or None)."""
        ready = installed_models(self.cfg)
        if not ready:
            raise RuntimeError("No AI model is downloaded yet. Press Models to get one.")
        if self.cfg["model"] != AUTO:
            if self.cfg["model"] in ready:
                return self.cfg["model"], None
            return next(iter(ready)), f"{self.cfg['model']} isn't downloaded, so using {ready[next(iter(ready))]}"
        if CODE_WORDS.search(text) and "coder" in ready:
            return "coder", "it looks like a coding job"
        if len(text.split()) < 12 and not ACTION_WORDS.search(text) and "quick" in ready and not self.messages:
            return "quick", "it's a short question"
        if "general" in ready:
            return "general", "it's a general job"
        key = next(iter(ready))
        return key, "it's the only model downloaded"

    def run(self, text: str):
        self.stop_event.clear()
        key = None
        try:
            key, reason = self.choose(text)
            if reason:
                self.ui.progress(f"Auto picked {installed_models(self.cfg).get(key, key)} because {reason}.")
            self.engine.load(key, status=self.ui.progress)
            self.messages.append({"role": "user", "content": text})
            self._loop()
        except pyautogui.FailSafeException:
            self.ui.say("Stopped: the mouse hit a screen corner (emergency stop).", kind="error")
        except (OSError, RuntimeError, ValueError) as e:
            log.exception("job failed")
            self.ui.say(str(e), kind="error")
        finally:
            if key:
                self._finish_job(key)

    def _loop(self):
        tools = self.tools()
        for _ in range(self.cfg["max_steps"]):
            if self.stop_event.is_set():
                self.ui.say("Stopped.", kind="error")
                return
            convo = [{"role": "system", "content": self.system_prompt()}] + self.messages
            self.ui.start_reply()
            message, finish = self.engine.chat(convo, tools=tools, on_text=self.ui.stream, stop_event=self.stop_event)
            self.ui.end_reply()
            self.messages.append(message)
            self._count(message)
            calls = message.get("tool_calls") or []
            if not calls:
                if finish == "length":
                    self.ui.say("The reply was cut off. Say 'continue' for the rest.", kind="error")
                return
            for call in calls:
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": self._run_tool(call)})
        self.ui.say(f"Stopped after {self.cfg['max_steps']} steps. Say 'continue' to keep going.", kind="error")

    def _run_tool(self, call) -> str:
        name = call["function"]["name"]
        raw = call["function"]["arguments"]
        if self.stop_event.is_set():
            return "Stopped by the user."
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return f"ERROR: the arguments were not valid JSON: {raw[:500]}"
        description = f"{name} {json.dumps(args)[:300]}"
        self.ui.tool(description)

        if name in LOCAL_TOOLS:
            schema = LOCAL_TOOLS[name]["function"]["parameters"]
        elif self.mcp.owns(name):
            schema = self.mcp.schema(name)
        else:
            return f"ERROR: there is no tool called {name}."
        try:
            jsonschema.validate(args, schema)
        except jsonschema.ValidationError as e:
            return f"ERROR: bad arguments: {e.message}"

        if self.cfg["confirm_actions"] and name not in HARMLESS and not self.ui.confirm(description):
            return "The user declined this action."
        try:
            result = self.mcp.call(name, args) if self.mcp.owns(name) else run_local_tool(name, args, self.workspace)
        except pyautogui.FailSafeException:
            raise
        except Exception as e:
            log.exception("tool %s failed", name)
            result = f"ERROR: {type(e).__name__}: {e}"
        # Small models have small memories, so keep tool output short.
        if len(result) > 6000:
            result = result[:4000] + "\n...(cut)...\n" + result[-1500:]
        return result

    # learning from use

    def _count(self, message):
        stats = load_stats()
        stats["words_written"] = stats.get("words_written", 0) + len((message.get("content") or "").split())
        save_stats(stats)

    def _finish_job(self, key):
        stats = load_stats()
        stats["jobs"] = stats.get("jobs", 0) + 1
        stats.setdefault("models", {})[key] = stats["models"].get(key, 0) + 1
        stats["last_used"] = datetime.now().isoformat(timespec="seconds")
        save_stats(stats)
        self.ui.stats_changed()

        history = app_dir() / "history"
        history.mkdir(exist_ok=True)
        with (history / f"{self.started:%Y-%m-%d_%H%M%S}.json").open("w", encoding="utf-8") as f:
            json.dump(self.messages, f, indent=1)

        if self.cfg["learn"] and self.messages and not self.stop_event.is_set():
            threading.Thread(target=self._learn, args=(list(self.messages),), daemon=True).start()

    def _learn(self, messages):
        lines = []
        for m in messages:
            if m["role"] in ("user", "assistant") and m.get("content"):
                lines.append(f"{m['role'].title()}: {m['content']}")
            for c in m.get("tool_calls") or []:
                lines.append(f"(used {c['function']['name']})")
        prompt = LEARN_PROMPT.format(memory=read_memory() or "(empty)", transcript="\n".join(lines)[-12000:])
        try:
            message, finish = self.engine.chat([{"role": "user", "content": prompt}], max_tokens=800)
            text = (message.get("content") or "").strip()
            if finish == "stop" and text:
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
        self._fill_model_box()
        self.root.after(50, self._drain)
        self._start_agent()
        if not installed_models(self.cfg):
            self.root.after(300, self.open_models)

    def _build(self):
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Model").pack(side="left")
        self.model_box = ttk.Combobox(top, state="readonly", width=36)
        self.model_box.bind("<<ComboboxSelected>>", self._model_changed)
        self.model_box.pack(side="left", padx=6)
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="Models", command=self.open_models).pack(side="right", padx=4)
        ttk.Button(top, text="Memory", command=lambda: open_file(self._ensure_memory())).pack(side="right")
        ttk.Button(top, text="New chat", command=self.new_chat).pack(side="right", padx=4)

        info = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        info.pack(fill="x")
        self.status = ttk.Label(info, text="Starting...")
        self.status.pack(side="left")
        self.stats_label = ttk.Label(info, foreground="#707070")
        self.stats_label.pack(side="right")

        self.log = scrolledtext.ScrolledText(self.root, wrap="word", state="disabled", font=("Segoe UI", 10), padx=8, pady=8)
        self.log.pack(fill="both", expand=True, padx=8)
        self.log.tag_config("you", foreground="#1a4fa0", font=("Segoe UI", 10, "bold"))
        self.log.tag_config("ai", foreground="#202020")
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

        self._write("Your own AI, running on this laptop. Nothing you type leaves it.\n"
                    "Try \"Make me a Python program that renames my holiday photos by date\" or \"Open Notepad and write a shopping list\".\n\n",
                    "progress")

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
        self.stats_label.config(text=f"{s.get('jobs', 0)} jobs since {s.get('first_used', date.today().isoformat())}"
                                     f"  |  {s.get('words_written', 0):,} words written")

    def _fill_model_box(self):
        self.model_names = {AUTO_LABEL: AUTO, **{label: k for k, label in installed_models(self.cfg).items()}}
        self.model_box.config(values=list(self.model_names))
        current = next((label for label, k in self.model_names.items() if k == self.cfg["model"]), AUTO_LABEL)
        self.model_box.set(current)

    def _model_changed(self, _event=None):
        self.cfg["model"] = self.model_names[self.model_box.get()]
        save_config(self.cfg)

    # agent lifecycle

    def _start_agent(self):
        if self.agent:
            old, self.agent = self.agent, None
            threading.Thread(target=old.close, daemon=True).start()
        self.status.config(text="Starting...")
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
            self.status.config(text="Stopping...")

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
        self.status.config(text="Thinking..." if busy else self.ready_text)

    # callbacks from the worker thread

    def start_reply(self):
        self.events.put(("say", ("", "ai")))

    def stream(self, text):
        self.events.put(("say", (text, "ai")))

    def end_reply(self):
        self.events.put(("say", ("\n\n", "ai")))

    def say(self, text, kind="ai"):
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
                    self.ready_text = "Ready" + (f"  |  {count} MCP tools" if count else "")
                    if failed:
                        self.ready_text += f"  |  failed: {', '.join(failed)}"
                        self._write("Some MCP servers did not start:\n"
                                    + "".join(f"  {k}: {v}\n" for k, v in failed.items())
                                    + f"Details in {app_dir() / 'mcp_servers.log'}\n\n", "error")
                    if not find_llama_server():
                        self._write("The AI engine (llama-server) is missing. Reinstall Desk Agent.\n\n", "error")
                    if not self.busy:
                        self.status.config(text=self.ready_text)
                elif kind == "confirm":
                    description, answer, done = data
                    answer["ok"] = self._ask(description)
                    done.set()
                elif kind == "done":
                    self._set_busy(False)
                elif kind == "call":
                    data()
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _ask(self, description) -> bool:
        win = tk.Toplevel(self.root)
        win.title("Allow this action?")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        result = {"ok": False}
        ttk.Label(win, text="The AI wants to:", padding=(12, 12, 12, 4)).pack(anchor="w")
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
        win.geometry(f"+{(win.winfo_screenwidth() - win.winfo_width()) // 2}+{(win.winfo_screenheight() - win.winfo_height()) // 3}")
        win.grab_set()
        win.focus_force()
        self.root.wait_window(win)
        return result["ok"]

    # models

    def open_models(self):
        win = tk.Toplevel(self.root)
        win.title("Models")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Models are downloaded once and then work offline. Start with General.",
                  wraplength=460).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        def refresh():
            win.destroy()
            self._fill_model_box()
            self.open_models()

        for i, (key, entry) in enumerate(CATALOG.items(), start=1):
            path = model_file(key, self.cfg)
            box = ttk.LabelFrame(frm, text=entry["label"], padding=8)
            box.grid(row=i, column=0, columnspan=3, sticky="we", pady=4)
            ttk.Label(box, text=f"{entry['about']}  ({entry['size_gb']} GB)", wraplength=440).pack(anchor="w")
            row = ttk.Frame(box)
            row.pack(fill="x", pady=(6, 0))
            if path:
                ttk.Label(row, text="Downloaded", foreground="#2e7d32").pack(side="left")
                ttk.Button(row, text="Delete", command=lambda p=path: (p.unlink(), refresh())).pack(side="right")
            else:
                bar = ttk.Progressbar(row, length=260, maximum=1000)
                bar.pack(side="left")
                label = ttk.Label(row, text="")
                label.pack(side="left", padx=6)
                button = ttk.Button(row, text="Download")
                button.pack(side="right")
                button.config(command=lambda k=key, b=bar, lab=label, btn=button: self._download(k, b, lab, btn, refresh))

        row = len(CATALOG) + 1
        ttk.Button(frm, text="Add my own .gguf file...", command=lambda: self._add_custom(refresh)).grid(row=row, column=0, sticky="w", pady=(10, 0))
        ttk.Button(frm, text="Open models folder", command=lambda: open_file(models_dir())).grid(row=row, column=1, sticky="w", pady=(10, 0), padx=6)

    def _download(self, key, bar, label, button, refresh):
        button.config(state="disabled")
        cancel = threading.Event()

        def progress(done, total):
            self.events.put(("call", lambda: (bar.config(value=1000 * done / total if total else 0),
                                              label.config(text=f"{done / 1e9:.2f} of {total / 1e9:.2f} GB"))))

        def work():
            try:
                download_model(key, progress, cancel)
                self.events.put(("call", refresh))
            except Exception as e:
                log.exception("download failed")
                error = str(e)
                self.events.put(("call", lambda: (label.config(text="Failed, try again"), button.config(state="normal"),
                                                  messagebox.showerror("Download failed", error))))

        threading.Thread(target=work, daemon=True).start()

    def _add_custom(self, refresh):
        path = filedialog.askopenfilename(title="Pick a .gguf model file", filetypes=[("GGUF models", "*.gguf")])
        if path:
            name = Path(path).stem[:40]
            self.cfg.setdefault("custom_models", {})[name] = path
            save_config(self.cfg)
            refresh()

    # settings

    def open_settings(self):
        if self.busy:
            return
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        workspace = tk.StringVar(value=self.cfg["workspace"])
        context = tk.StringVar(value=str(self.cfg["context"]))
        checks = {
            "coding_tools": ("Let the AI write and run code in the projects folder", tk.BooleanVar(value=self.cfg["coding_tools"])),
            "desktop_tools": ("Let the AI open apps and type into windows", tk.BooleanVar(value=self.cfg["desktop_tools"])),
            "web_tools": ("Let the AI search the web and read pages (needs internet)", tk.BooleanVar(value=self.cfg["web_tools"])),
            "confirm_actions": ("Ask me before it runs commands, types, opens things or changes files", tk.BooleanVar(value=self.cfg["confirm_actions"])),
            "learn": ("Learn from my conversations (keeps a memory file you can edit)", tk.BooleanVar(value=self.cfg["learn"])),
        }

        ttk.Label(frm, text="Projects folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=workspace, width=56).grid(row=1, column=0, columnspan=2, sticky="we")
        ttk.Label(frm, text="Memory size in tokens (bigger remembers more of a chat, uses more RAM)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(frm, textvariable=context, values=["4096", "8192", "16384", "32768"], width=10).grid(row=3, column=0, sticky="w")
        for i, (label, var) in enumerate(checks.values()):
            ttk.Checkbutton(frm, text=label, variable=var).grid(row=4 + i, column=0, columnspan=2, sticky="w", pady=(10 if i == 0 else 0, 0))
        row = 4 + len(checks)
        ttk.Label(frm, text="MCP servers are set in the config file (same format as Claude Desktop). Saving starts a new chat.",
                  wraplength=420).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
        ttk.Button(frm, text="Open config file", command=lambda: open_file(config_path())).grid(row=row + 1, column=0, sticky="w")

        def save():
            self.cfg = load_config()  # pick up any hand edits to mcpServers
            self.cfg.update(workspace=workspace.get().strip() or DEFAULTS["workspace"],
                            context=int(context.get()) if context.get().isdigit() else 8192,
                            model=self.model_names.get(self.model_box.get(), AUTO),
                            **{name: var.get() for name, (_, var) in checks.items()})
            save_config(self.cfg)
            win.destroy()
            self._start_agent()

        ttk.Button(frm, text="Save", command=save).grid(row=row + 2, column=1, sticky="e", pady=(14, 0))
        win.grab_set()

    def quit(self):
        if self.agent:
            self.agent.stop_event.set()
            self.agent.engine.stop()
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
    pyautogui.FAILSAFE = True  # slamming the mouse into a screen corner is the emergency stop
    # MCP servers write their own logs here; a windowed EXE has no console to show them.
    errlog = open(app_dir() / "mcp_servers.log", "a", encoding="utf-8", buffering=1)
    App(errlog).run()


if __name__ == "__main__":
    main()
