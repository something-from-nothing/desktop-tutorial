"""Desk Agent: your own AI that runs on your laptop.

No accounts, no API keys, no internet needed once a model is downloaded. The AI
runs on the laptop through llama.cpp (llama-server.exe, shipped with the app), and
the interface is a web page served by this program and shown in its own window.

Features: chats saved in a sidebar, projects with their own instructions and
files, file attachments, agents (built in and your own) that can hand work to
each other, a preview panel for pages and code the AI makes, response styles, a
think first mode, editing and regenerating replies, switchable models with an
Auto mode, tools for code, the desktop and the web, MCP servers, and a memory of
what it learns about you.

Everything is in this one file. Run it with Python, build it with build.bat, or
let the GitHub workflow make the installer.
"""

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import AsyncExitStack
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema
import pyautogui
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger("deskagent")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ---------------------------------------------------------------- models

# Downloadable models. The exact file is looked up on Hugging Face at download time,
# picking the shortest single file whose name contains the quant, so renamed files still work.
CATALOG = {
    "quick": {
        "label": "Quick", "detail": "Qwen 2.5, 1.5B",
        "repo": "bartowski/Qwen2.5-1.5B-Instruct-GGUF", "quant": "Q4_K_M", "size_gb": 1.0,
        "about": "Fast replies, fine for chat and simple jobs. Runs on almost anything.",
    },
    "general": {
        "label": "General", "detail": "Qwen 3, 4B",
        "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF", "quant": "Q4_K_M", "size_gb": 2.5,
        "about": "The best all rounder for a laptop, and good at using tools. Start with this one.",
    },
    "coder": {
        "label": "Coder", "detail": "Qwen 2.5 Coder, 7B",
        "repo": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", "quant": "Q4_K_M", "size_gb": 4.7,
        "about": "Best at writing programs. Slower, and wants 8 GB of RAM or more.",
    },
}
AUTO = "auto"
CODE_WORDS = re.compile(
    r"\b(code|coding|program|script|python|javascript|html|css|function|bug|debug|compile|"
    r"app|website|game|sql|class|error|traceback|refactor|api)\b", re.I)
ACTION_WORDS = re.compile(r"\b(open|make|create|write|build|run|find|search|save|install|type|fix|file|folder)\b", re.I)

STYLES = {
    "normal": "",
    "concise": "Keep answers as short as possible. Skip preamble and summaries.",
    "explanatory": "Explain your reasoning and teach the ideas behind the answer, with examples.",
    "formal": "Write in a clear, professional, formal tone.",
    "friendly": "Write in a warm, relaxed, conversational tone.",
}

# Built in agents. Each one is a role with its own instructions, tools and favourite model.
# Tool groups: code, desktop, web, artifacts, delegate.
BUILTIN_AGENTS = {
    "assistant": {
        "name": "Assistant", "model": AUTO, "tools": ["code", "desktop", "web", "artifacts", "delegate"],
        "instructions": "You are a capable general assistant. Handle the job yourself, or hand a self contained part to a specialist agent with ask_agent when that will do it better.",
    },
    "coder": {
        "name": "Coder", "model": "coder", "tools": ["code", "artifacts"],
        "instructions": "You are a careful programmer. Plan briefly, write clean working code with short comments, run it to check it works, and fix what breaks before you report back. For a web page or small visual app, also show it with create_artifact.",
    },
    "researcher": {
        "name": "Researcher", "model": "general", "tools": ["web"],
        "instructions": "You research questions. Search the web, read the most relevant pages, compare sources, and give a clear answer with the links you used. Say when sources disagree or you are unsure.",
    },
    "writer": {
        "name": "Writer", "model": "general", "tools": ["artifacts"],
        "instructions": "You write and edit: emails, posts, letters, reports, stories. Match the reader and purpose, keep it plain and human, and put long finished pieces in a markdown artifact.",
    },
    "planner": {
        "name": "Planner", "model": "general", "tools": ["delegate"],
        "instructions": "You break big jobs into clear steps, then hand each step to the right agent with ask_agent and pull their results together into one answer.",
    },
}


# ---------------------------------------------------------------- settings and saved data

DEFAULTS = {
    "model": AUTO,
    "context": 8192,
    "threads": 0,  # 0 means let llama.cpp decide
    "learn": True,
    "confirm_actions": True,
    "max_steps": 25,
    "workspace": str(Path.home() / "Documents" / "DeskAgent Projects"),
    "custom_models": {},  # name -> path to a .gguf file you added yourself
    "agents": {},  # your own agents, same shape as BUILTIN_AGENTS
    # Same shape as Claude Desktop's "mcpServers" block, so configs can be copied across.
    # stdio server:  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me\\Documents"]}
    # remote server: {"url": "https://example.com/mcp"}
    "mcpServers": {},
    # Google sign in. Make a "Desktop app" OAuth client at console.cloud.google.com and paste its details here.
    "google_client_id": "",
    "google_client_secret": "",
    "require_sign_in": False,
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


def current_user() -> dict | None:
    """The Google account signed in on this laptop, or None for the guest profile."""
    return read_json(app_dir() / "session.json", {}).get("user")


def profile_dir() -> Path:
    """Each Google account gets its own chats, projects, memory and stats. The guest
    profile uses the top folder, so nothing made before sign in existed is lost."""
    user = current_user()
    if not user:
        return app_dir()
    path = app_dir() / "profiles" / hashlib.sha256(user["sub"].encode()).hexdigest()[:16]
    path.mkdir(parents=True, exist_ok=True)
    return path


def sub_dir(name: str) -> Path:
    path = profile_dir() / name
    path.mkdir(exist_ok=True)
    return path


def config_path() -> Path:
    return app_dir() / "config.json"


def memory_path() -> Path:
    return profile_dir() / "memory.md"


def stats_path() -> Path:
    return profile_dir() / "stats.json"


def read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    tmp.replace(path)


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        write_json(path, DEFAULTS)
    return {**DEFAULTS, **read_json(path, {})}


def read_memory() -> str:
    path = memory_path()
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def load_stats() -> dict:
    return read_json(stats_path(), {"first_used": date.today().isoformat(), "jobs": 0, "words_written": 0, "models": {}})


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value or ""):
        raise ValueError("bad id")
    return value


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
    found.update({k: k for k in cfg.get("custom_models", {}) if model_file(k, cfg)})
    return found


def all_agents(cfg: dict) -> dict:
    return {**BUILTIN_AGENTS, **cfg.get("agents", {})}


# ---------------------------------------------------------------- reading attached files

def extract_text(name: str, data: bytes) -> str:
    """Turns an attached file into text the model can read."""
    lower = name.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if lower.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        raise ValueError("Pictures can't be read by the models on this laptop yet. Attach text, code, PDF or Word files.")
    text = data.decode("utf-8", "replace")
    if "\x00" in text[:2000]:
        raise ValueError(f"{name} doesn't look like a text file.")
    return text


# ---------------------------------------------------------------- downloading models

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 DeskAgent"})
    return urllib.request.urlopen(req, timeout=timeout)


DOWNLOADS: dict[str, dict] = {}  # key -> {"done", "total", "error"}


def download_model(key: str):
    """Downloads a catalog model, recording progress in DOWNLOADS. Resumes partial downloads."""
    entry = CATALOG[key]
    state = DOWNLOADS[key] = {"done": 0, "total": 0, "error": None}
    try:
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
            state["total"] = done + int(r.headers.get("Content-Length", 0))
            with part.open("ab" if done else "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    state["done"] = done
        part.replace(folder / name)
        DOWNLOADS.pop(key, None)
    except Exception as e:
        log.exception("download failed")
        state["error"] = str(e)


# ---------------------------------------------------------------- the local AI engine

def here() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def find_llama_server() -> Path | None:
    exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    for candidate in (here() / "llama" / exe, here() / exe):
        if candidate.exists():
            return candidate
    found = shutil.which("llama-server")
    return Path(found) if found else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def strip_thinking(text: str) -> tuple[str, str]:
    """Splits '<think>...</think> answer' into (answer, thinking)."""
    thoughts = "\n".join(m.strip() for m in re.findall(r"<think>(.*?)(?:</think>|$)", text, flags=re.S))
    answer = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S).strip()
    return answer, thoughts


class Engine:
    """Runs llama-server in the background and talks to its local OpenAI style chat
    API. Nothing leaves the laptop."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.errlog = open(app_dir() / "engine.log", "a", encoding="utf-8", buffering=1)
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
                raise RuntimeError("The AI engine (llama-server) is missing. Reinstall Desk Agent.")
            path = model_file(key, self.cfg)
            if not path:
                raise RuntimeError(f"The {key} model isn't downloaded. Open Models to get it.")
            status(f"Loading {CATALOG.get(key, {}).get('label', key)} model...")
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
        """Streams one reply. Returns (assistant message, finish reason)."""
        body = {"messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": 0.4}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        content, calls, finish = [], {}, None
        with urllib.request.urlopen(req, timeout=900) as r:
            for raw in r:
                if stop_event and stop_event.is_set():
                    finish = "stopped"
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
        answer, thinking = strip_thinking("".join(content))
        message = {"role": "assistant", "content": answer}
        if thinking:
            message["thinking"] = thinking
        if calls:
            message["tool_calls"] = [
                {"id": c["id"] or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for _, c in sorted(calls.items())
            ]
        return message, finish


# ---------------------------------------------------------------- tools

def _tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TOOL_GROUPS = {
    "code": [
        _tool("list_files", "List files and folders in the projects folder. Path is relative to the projects folder; use '.' for the top.",
              {"path": {"type": "string"}}, ["path"]),
        _tool("read_file", "Read a text file from the projects folder.", {"path": {"type": "string"}}, ["path"]),
        _tool("write_file", "Create or overwrite a text file in the projects folder. Folders are made as needed. Always send the whole file.",
              {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        _tool("run_command", "Run a command in a Windows command prompt inside the projects folder, for example 'python app.py'. Times out after 120 seconds.",
              {"command": {"type": "string"}, "folder": {"type": "string", "description": "Subfolder to run in. Defaults to the top."}}, ["command"]),
    ],
    "desktop": [
        _tool("open", "Open an app, file, folder or web address on the laptop, for example 'notepad', 'calc' or 'https://bbc.co.uk'.",
              {"target": {"type": "string"}}, ["target"]),
        _tool("list_windows", "List the titles of the windows that are open.", {}, []),
        _tool("focus_window", "Bring a window to the front, matching part of its title.", {"title": {"type": "string"}}, ["title"]),
        _tool("type_text", "Type text into whichever window is at the front. Call focus_window first.", {"text": {"type": "string"}}, ["text"]),
        _tool("press_keys", "Press a key or shortcut in the front window, for example 'enter', 'ctrl+s', 'alt+tab'.", {"keys": {"type": "string"}}, ["keys"]),
    ],
    "web": [
        _tool("web_search", "Search the web and get the top results with links.", {"query": {"type": "string"}}, ["query"]),
        _tool("read_webpage", "Fetch a web page and return its text.", {"url": {"type": "string"}}, ["url"]),
    ],
    "artifacts": [
        _tool("create_artifact", "Show the user a finished piece of work in the side panel: a web page or small app (html), a drawing (svg), a document (markdown) or a code file (code). Send the complete content. Use the same title again to update it.",
              {"title": {"type": "string"}, "kind": {"type": "string", "enum": ["html", "svg", "markdown", "code"]},
               "language": {"type": "string", "description": "For code: python, javascript and so on."},
               "content": {"type": "string"}}, ["title", "kind", "content"]),
    ],
    "delegate": [
        _tool("ask_agent", "Hand a self contained task to a specialist agent and get its answer back. Describe the task fully; the agent can't see this chat.",
              {"agent": {"type": "string", "description": "Agent id"}, "task": {"type": "string"}}, ["agent", "task"]),
    ],
}
GROUP_PROMPTS = {
    "code": "You can write and run code in the projects folder ({workspace}). For a new program, make a subfolder, write the files, run them to check they work, fix what breaks, then say where it is and how to start it.",
    "desktop": "You can open apps, files and web addresses, switch windows, type and press keys. You can't see the screen, so use list_windows to check what is open and focus_window before typing.",
    "web": "You can search the web and read pages. Say where information came from.",
    "artifacts": "Use create_artifact to show finished web pages, drawings, documents or code in the side panel.",
    "delegate": "Agents you can hand work to with ask_agent: {agents}.",
}
TOOL_GROUP_OF = {t["function"]["name"]: g for g, tools in TOOL_GROUPS.items() for t in tools}
HARMLESS = {"list_files", "read_file", "list_windows", "web_search", "read_webpage", "create_artifact", "ask_agent"}


def workspace_path(cfg: dict, rel: str) -> Path:
    root = Path(cfg["workspace"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / (rel or ".")).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"{rel} is outside the projects folder")
    return path


def _combo(text: str) -> list[str]:
    aliases = {"return": "enter", "escape": "esc", "control": "ctrl", "windows": "win", "super": "win"}
    return [aliases.get(k.strip().lower(), k.strip().lower()) for k in text.split("+") if k.strip()]


def _paste(text: str):
    if sys.platform == "win32":
        subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"], input=text,
                       text=True, encoding="utf-8", creationflags=NO_WINDOW)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)


def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<(br|p|div|li|h\d|tr)[^>]*>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def run_local_tool(name: str, args: dict, cfg: dict) -> str:
    if name == "list_files":
        path = workspace_path(cfg, args["path"])
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        return "\n".join(f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)" for p in entries[:300]) or "(empty folder)"
    if name == "read_file":
        return workspace_path(cfg, args["path"]).read_text(encoding="utf-8", errors="replace")
    if name == "write_file":
        path = workspace_path(cfg, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Saved {path}"
    if name == "run_command":
        proc = subprocess.run(args["command"], shell=True, cwd=workspace_path(cfg, args.get("folder", ".")), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW)
        return f"Exit code {proc.returncode}\n{(proc.stdout + chr(10) + proc.stderr).strip()}"
    if name == "open":
        target = args["target"]
        if sys.platform == "win32":
            if Path(target).exists() or "://" in target:
                os.startfile(target)
            else:
                subprocess.Popen(f'start "" {target}', shell=True)
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
        if text.isascii():
            pyautogui.write(text, interval=0.01)
        else:
            _paste(text)
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
            results.append(f"{page_text(title)}\n{link}")
        return "\n\n".join(results) or "No results."
    if name == "read_webpage":
        with http_get(args["url"]) as r:
            return page_text(r.read(3_000_000).decode("utf-8", "replace"))
    raise ValueError(f"Unknown tool {name}")


# ---------------------------------------------------------------- MCP servers

SEP = "__"


class MCPBridge:
    """Connects to MCP servers and exposes their tools to the model. The MCP client is
    async and the rest of the app is not, so all servers share one event loop on a
    background thread. Tool names are prefixed with the server name (server__tool)."""

    def __init__(self, servers: dict):
        self.servers = servers
        self.errlog = open(app_dir() / "mcp_servers.log", "a", encoding="utf-8", buffering=1)
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
            entry = _tool(f"{name}{SEP}{tool.name}"[:64], (tool.description or tool.title or tool.name)[:1024], {}, [])
            entry["function"]["parameters"] = tool.input_schema or {"type": "object", "properties": {}}
            self.tools.append(entry)
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
                parts.append("[an image was returned, which this model can't see]")
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


# ---------------------------------------------------------------- chats and projects

class Store:
    """Chats and projects, one JSON file each."""

    def chat_path(self, chat_id):
        return sub_dir("chats") / f"{safe_id(chat_id)}.json"

    def project_path(self, project_id):
        return sub_dir("projects") / f"{safe_id(project_id)}.json"

    def list_chats(self):
        chats = []
        for p in sub_dir("chats").glob("*.json"):
            c = read_json(p, None)
            if c:
                chats.append({k: c.get(k) for k in ("id", "title", "project", "agent", "updated")})
        return sorted(chats, key=lambda c: c["updated"] or "", reverse=True)

    def get_chat(self, chat_id):
        chat = read_json(self.chat_path(chat_id), None)
        if not chat:
            raise KeyError(chat_id)
        return chat

    def new_chat(self, project=None, agent="assistant"):
        now = datetime.now().isoformat(timespec="seconds")
        chat = {"id": uuid.uuid4().hex[:12], "title": "New chat", "project": project, "agent": agent,
                "created": now, "updated": now, "messages": [], "artifacts": []}
        self.save_chat(chat)
        return chat

    def save_chat(self, chat):
        chat["updated"] = datetime.now().isoformat(timespec="seconds")
        write_json(self.chat_path(chat["id"]), chat)

    def delete_chat(self, chat_id):
        self.chat_path(chat_id).unlink(missing_ok=True)

    def list_projects(self):
        projects = [read_json(p, None) for p in sub_dir("projects").glob("*.json")]
        return sorted((p for p in projects if p), key=lambda p: p["name"].lower())

    def get_project(self, project_id):
        return read_json(self.project_path(project_id), None) if project_id else None

    def save_project(self, project):
        write_json(self.project_path(project["id"]), project)

    def delete_project(self, project_id):
        self.project_path(project_id).unlink(missing_ok=True)
        for c in self.list_chats():
            if c["project"] == project_id:
                chat = self.get_chat(c["id"])
                chat["project"] = None
                self.save_chat(chat)


# ---------------------------------------------------------------- the agent

BASE_PROMPT = """You are {agent_name}, part of Desk Agent, an assistant running entirely on the user's own Windows laptop. Nothing they say leaves the laptop.
Today is {today}.

{instructions}

{tools}Use a tool whenever it helps, one step at a time, and look at each result before the next step.
Ask before deleting files or anything else that can't be undone. If a tool result says the user declined, stop and ask what they want instead.
Write in plain language. Use markdown for structure when it helps: headings, lists, tables and fenced code blocks with the language named."""

THINK_PROMPT = "Before answering, think the problem through step by step inside <think></think> tags, then give your answer after the closing tag."

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


class Brain:
    """Runs jobs: one chat turn at a time, with tools and agents. Sends progress to
    the browser through emit()."""

    def __init__(self, emit):
        self.emit = emit
        self.cfg = load_config()
        self.store = Store()
        self.engine = Engine(self.cfg)
        self.mcp = MCPBridge(self.cfg.get("mcpServers", {}))
        self.stop_event = threading.Event()
        self.busy_chat: str | None = None
        self.pending: dict[str, dict] = {}  # confirmation id -> {"event", "ok"}
        self.allow_all = False
        threading.Thread(target=self.mcp.start, daemon=True).start()

    def warm_up(self):
        """Starts the model the next chat will most likely use."""
        try:
            ready = installed_models(self.cfg)
            wanted = self.cfg["model"] if self.cfg["model"] in ready else "general" if "general" in ready else next(iter(ready), None)
            if wanted and not self.busy_chat:
                self.engine.load(wanted)
                self.emit({"type": "state"})
        except Exception:
            log.exception("warm up failed")

    def reload_config(self, changes: dict):
        cfg = load_config()
        cfg.update(changes)
        write_json(config_path(), cfg)
        old_servers = self.cfg.get("mcpServers")
        self.cfg.clear()
        self.cfg.update(cfg)
        if cfg.get("mcpServers") != old_servers:
            old = self.mcp
            threading.Thread(target=old.close, daemon=True).start()
            self.mcp = MCPBridge(cfg.get("mcpServers", {}))
            threading.Thread(target=self.mcp.start, daemon=True).start()

    # building the prompt

    def tools_for(self, agent: dict, allow_delegate=True) -> list:
        tools = []
        for group in agent.get("tools", []):
            if group == "delegate" and not allow_delegate:
                continue
            tools += TOOL_GROUPS.get(group, [])
        if "mcp" in agent.get("tools", []) or agent.get("tools") == ["code", "desktop", "web", "artifacts", "delegate"]:
            tools += self.mcp.tools
        return tools

    def system_prompt(self, agent_id: str, agent: dict, chat: dict | None, style="normal", think=False, allow_delegate=True) -> str:
        others = ", ".join(f"{k} ({a['name']})" for k, a in all_agents(self.cfg).items() if k != agent_id)
        parts = [GROUP_PROMPTS[g].format(workspace=self.cfg["workspace"], agents=others)
                 for g in agent.get("tools", []) if g in GROUP_PROMPTS and (g != "delegate" or allow_delegate)]
        prompt = BASE_PROMPT.format(agent_name=agent["name"], today=date.today().strftime("%A %d %B %Y"),
                                    instructions=agent.get("instructions", ""), tools="\n".join(parts) + "\n" if parts else "")
        if STYLES.get(style):
            prompt += "\n\nStyle: " + STYLES[style]
        if think:
            prompt += "\n\n" + THINK_PROMPT
        project = self.store.get_project(chat.get("project")) if chat else None
        if project:
            prompt += f"\n\nThis chat is part of the project \"{project['name']}\"."
            if project.get("instructions"):
                prompt += f"\nProject instructions:\n{project['instructions']}"
            budget = self.cfg["context"] * 3 // 2  # roughly half the context, in characters
            for f in project.get("files", []):
                if budget <= 0:
                    break
                text = f["text"][:budget]
                budget -= len(text)
                prompt += f"\n\n<project_file name=\"{f['name']}\">\n{text}\n</project_file>"
        memory = read_memory() if self.cfg["learn"] else ""
        if memory:
            prompt += f"\n\nWhat you have learned about this user from earlier conversations:\n{memory}"
        return prompt

    def model_messages(self, messages: list, system: str) -> list:
        """The conversation as the model sees it, trimmed from the oldest end to fit."""
        keep = ("role", "content", "tool_calls", "tool_call_id")
        out = []
        for m in messages:
            m2 = {k: v for k, v in m.items() if k in keep}
            if m["role"] == "user" and m.get("attachments"):
                files = "".join(f"\n\n<attached_file name=\"{a['name']}\">\n{a['text']}\n</attached_file>" for a in m["attachments"])
                m2["content"] = (m.get("content") or "") + files
            out.append(m2)
        budget = int(self.cfg["context"] * 2.6) - len(system)  # characters, about 3 per token with room for the reply
        total = 0
        start = len(out)
        while start > 0 and total + len(json.dumps(out[start - 1])) < budget:
            start -= 1
            total += len(json.dumps(out[start]))
        start = min(start, len(out) - 1)
        while start > 0 and out[start]["role"] == "tool":  # never start on a tool result
            start -= 1
        return [{"role": "system", "content": system}] + out[start:]

    def choose_model(self, agent: dict, text: str, first: bool):
        """Returns (model_key, reason or None)."""
        ready = installed_models(self.cfg)
        if not ready:
            raise RuntimeError("No AI model is downloaded yet. Open Models to get one.")
        wanted = self.cfg["model"] if self.cfg["model"] != AUTO else agent.get("model", AUTO)
        if wanted != AUTO:
            if wanted in ready:
                return wanted, None
            key = "general" if "general" in ready else next(iter(ready))
            return key, f"{wanted} isn't downloaded"
        if CODE_WORDS.search(text) and "coder" in ready:
            return "coder", "it looks like a coding job"
        if first and len(text.split()) < 12 and not ACTION_WORDS.search(text) and "quick" in ready:
            return "quick", "it's a short question"
        if "general" in ready:
            return "general", None
        return next(iter(ready)), None

    # running a job

    def start_job(self, chat_id: str, body: dict):
        if self.busy_chat:
            raise RuntimeError("Already working on something. Wait for it to finish or press Stop.")
        chat = self.store.get_chat(chat_id)
        if body.get("regenerate"):
            while chat["messages"] and chat["messages"][-1]["role"] != "user":
                chat["messages"].pop()
            if not chat["messages"]:
                raise RuntimeError("Nothing to regenerate.")
        else:
            if body.get("edit_index") is not None:
                del chat["messages"][int(body["edit_index"]):]
            message = {"role": "user", "content": body.get("text", "").strip(), "attachments": body.get("attachments", [])}
            if not message["content"] and not message["attachments"]:
                raise RuntimeError("Type a message first.")
            chat["messages"].append(message)
            if chat["title"] == "New chat":
                words = message["content"].split() or [message["attachments"][0]["name"]]
                chat["title"] = " ".join(words[:7])[:60]
        self.store.save_chat(chat)
        self.busy_chat = chat_id
        self.stop_event.clear()
        threading.Thread(target=self._job, args=(chat, body), daemon=True).start()

    def _job(self, chat, body):
        cid = chat["id"]
        model = None
        try:
            agent_id = chat.get("agent") or "assistant"
            agent = all_agents(self.cfg).get(agent_id, BUILTIN_AGENTS["assistant"])
            last_user = next(m for m in reversed(chat["messages"]) if m["role"] == "user")
            first = sum(1 for m in chat["messages"] if m["role"] == "user") == 1
            model, reason = self.choose_model(agent, last_user.get("content", ""), first)
            label = installed_models(self.cfg).get(model, model)
            self.emit({"type": "status", "chat": cid, "text": f"Using {label}" + (f" because {reason}" if reason else "")})
            self.engine.load(model, status=lambda t: self.emit({"type": "status", "chat": cid, "text": t}))
            system = self.system_prompt(agent_id, agent, chat, body.get("style", "normal"), bool(body.get("think")))
            self._loop(chat, chat["messages"], system, self.tools_for(agent), sub=None)
        except pyautogui.FailSafeException:
            self.emit({"type": "error", "chat": cid, "text": "Stopped: the mouse hit a screen corner (emergency stop)."})
        except Exception as e:
            log.exception("job failed")
            self.emit({"type": "error", "chat": cid, "text": str(e) or type(e).__name__})
        finally:
            self.store.save_chat(chat)
            self.busy_chat = None
            self.emit({"type": "done", "chat": cid})
            if model:
                self._after_job(chat, model)

    def _loop(self, chat, messages, system, tools, sub):
        """One agent working until it answers. sub is the ask_agent call id for helper agents."""
        steps = self.cfg["max_steps"] if sub is None else 12
        for _ in range(steps):
            if self.stop_event.is_set():
                return "Stopped by the user."
            convo = self.model_messages(messages, system)
            self.emit({"type": "assistant_start", "chat": chat["id"], "sub": sub})
            message, finish = self.engine.chat(
                convo, tools=tools, stop_event=self.stop_event,
                on_text=lambda t: self.emit({"type": "token", "chat": chat["id"], "sub": sub, "text": t}))
            messages.append(message)
            self._count(message)
            if sub is None:
                self.store.save_chat(chat)
            self.emit({"type": "assistant_end", "chat": chat["id"], "sub": sub, "message": message})
            calls = message.get("tool_calls") or []
            if not calls:
                if finish == "length":
                    self.emit({"type": "error", "chat": chat["id"], "text": "The reply was cut off. Say 'continue' for the rest."})
                return message.get("content", "")
            for call in calls:
                result, extra = self._run_tool(chat, call, sub)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result, **extra})
                if sub is None:
                    self.store.save_chat(chat)
        return "Stopped: too many steps."

    def _run_tool(self, chat, call, sub):
        """Returns (result text, extra fields to store on the tool message)."""
        name = call["function"]["name"]
        raw = call["function"]["arguments"]
        cid = chat["id"]
        try:
            args = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return f"ERROR: the arguments were not valid JSON: {raw[:500]}", {}
        self.emit({"type": "tool_call", "chat": cid, "sub": sub, "id": call["id"], "name": name, "args": args})

        if name in TOOL_GROUP_OF:
            schema = next(t["function"]["parameters"] for t in TOOL_GROUPS[TOOL_GROUP_OF[name]] if t["function"]["name"] == name)
        elif self.mcp.owns(name):
            schema = self.mcp.schema(name)
        else:
            return f"ERROR: there is no tool called {name}.", {}
        try:
            jsonschema.validate(args, schema)
        except jsonschema.ValidationError as e:
            return f"ERROR: bad arguments: {e.message}", {}

        if self.cfg["confirm_actions"] and not self.allow_all and name not in HARMLESS:
            if not self.confirm(cid, f"{name} {json.dumps(args)[:400]}"):
                return "The user declined this action.", {}

        extra = {}
        try:
            if name == "create_artifact":
                result, extra = self._artifact(chat, args)
            elif name == "ask_agent":
                result, extra = self._ask_agent(chat, call["id"], args)
            elif self.mcp.owns(name):
                result = self.mcp.call(name, args)
            else:
                result = run_local_tool(name, args, self.cfg)
        except pyautogui.FailSafeException:
            raise
        except Exception as e:
            log.exception("tool %s failed", name)
            result = f"ERROR: {type(e).__name__}: {e}"
        # Small models have small memories, so keep tool output short.
        if len(result) > 6000:
            result = result[:4000] + "\n...(cut)...\n" + result[-1500:]
        self.emit({"type": "tool_result", "chat": cid, "sub": sub, "id": call["id"], "name": name, "result": result[:3000]})
        return result, extra

    def _artifact(self, chat, args):
        artifacts = chat.setdefault("artifacts", [])
        existing = next((a for a in artifacts if a["title"] == args["title"]), None)
        artifact = {"id": existing["id"] if existing else uuid.uuid4().hex[:8], "title": args["title"], "kind": args["kind"],
                    "language": args.get("language", ""), "content": args["content"],
                    "version": (existing["version"] + 1) if existing else 1}
        if existing:
            artifacts[artifacts.index(existing)] = artifact
        else:
            artifacts.append(artifact)
        self.emit({"type": "artifact", "chat": chat["id"], "artifact": artifact})
        return f"Shown to the user in the side panel as '{args['title']}' (version {artifact['version']}).", {"artifact": artifact["id"]}

    def _ask_agent(self, chat, call_id, args):
        agents = all_agents(self.cfg)
        agent_id = args["agent"].strip().lower()
        if agent_id not in agents:
            agent_id = next((k for k, a in agents.items() if a["name"].lower() == agent_id), None)
        if not agent_id:
            return f"ERROR: no agent called {args['agent']}. Choose from: {', '.join(agents)}.", {}
        agent = agents[agent_id]
        self.emit({"type": "agent_start", "chat": chat["id"], "id": call_id, "agent": agent["name"]})
        system = self.system_prompt(agent_id, agent, chat, allow_delegate=False)
        messages = [{"role": "user", "content": args["task"]}]
        answer = self._loop(chat, messages, system, self.tools_for(agent, allow_delegate=False), sub=call_id)
        log_entries = [{"role": m["role"], "content": (m.get("content") or "")[:3000],
                        "tools": [c["function"]["name"] for c in m.get("tool_calls") or []]} for m in messages[1:]]
        self.emit({"type": "agent_end", "chat": chat["id"], "id": call_id})
        return f"{agent['name']} replied:\n{answer}", {"agent": agent["name"], "agent_log": log_entries}

    def confirm(self, chat_id, description) -> bool:
        cid = uuid.uuid4().hex[:8]
        waiter = {"event": threading.Event(), "ok": False}
        self.pending[cid] = waiter
        self.emit({"type": "confirm", "chat": chat_id, "id": cid, "description": description})
        while not waiter["event"].wait(0.5):
            if self.stop_event.is_set():
                break
        self.pending.pop(cid, None)
        return waiter["ok"]

    def answer_confirm(self, cid, ok, allow_all=False):
        if allow_all:
            self.allow_all = True
        waiter = self.pending.get(cid)
        if waiter:
            waiter["ok"] = ok
            waiter["event"].set()

    # learning from use

    def _count(self, message):
        stats = load_stats()
        stats["words_written"] = stats.get("words_written", 0) + len((message.get("content") or "").split())
        write_json(stats_path(), stats)

    def _after_job(self, chat, model):
        stats = load_stats()
        stats["jobs"] = stats.get("jobs", 0) + 1
        stats.setdefault("models", {})[model] = stats["models"].get(model, 0) + 1
        stats["last_used"] = datetime.now().isoformat(timespec="seconds")
        write_json(stats_path(), stats)
        self.emit({"type": "stats"})
        if self.cfg["learn"] and not self.stop_event.is_set():
            threading.Thread(target=self._learn, args=(list(chat["messages"]),), daemon=True).start()

    def _learn(self, messages):
        lines = []
        for m in messages[-30:]:
            if m["role"] in ("user", "assistant") and m.get("content"):
                lines.append(f"{m['role'].title()}: {m['content']}")
        prompt = LEARN_PROMPT.format(memory=read_memory() or "(empty)", transcript="\n".join(lines)[-12000:])
        try:
            message, finish = self.engine.chat([{"role": "user", "content": prompt}], max_tokens=800)
            text = (message.get("content") or "").strip()
            if finish == "stop" and text:
                memory_path().write_text(text + "\n", encoding="utf-8")
        except Exception:
            log.exception("learning failed")

    def close(self):
        self.stop_event.set()
        self.engine.stop()
        self.mcp.close()


# ---------------------------------------------------------------- Google sign in

# Overridable so the sign in flow can be tested without Google.
GOOGLE_AUTH_URL = os.environ.get("DESKAGENT_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN_URL = os.environ.get("DESKAGENT_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")


class GoogleSignIn:
    """Google's sign in for desktop apps: the browser goes to Google, Google sends it
    back to this app on 127.0.0.1 with a one time code, and the app swaps the code for
    the user's name and email. PKCE stops anyone else using an intercepted code."""

    def __init__(self):
        self.pending: dict[str, dict] = {}  # state -> {"verifier", "redirect"}

    def start(self, cfg: dict, port: int) -> str:
        if not cfg.get("google_client_id"):
            raise ValueError("Google sign in isn't set up yet. Add your Google client ID in the sign in settings.")
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        redirect = f"http://127.0.0.1:{port}/oauth/callback"
        self.pending[state] = {"verifier": verifier, "redirect": redirect, "time": time.time()}
        query = urllib.parse.urlencode({
            "client_id": cfg["google_client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": "openid email profile", "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account",
        })
        return f"{GOOGLE_AUTH_URL}?{query}"

    def finish(self, cfg: dict, state: str, code: str) -> dict:
        attempt = self.pending.pop(state, None)
        if not attempt or time.time() - attempt["time"] > 600:
            raise ValueError("That sign in link has expired. Try again.")
        form = urllib.parse.urlencode({
            "code": code, "client_id": cfg["google_client_id"], "client_secret": cfg.get("google_client_secret", ""),
            "redirect_uri": attempt["redirect"], "grant_type": "authorization_code", "code_verifier": attempt["verifier"],
        }).encode()
        req = urllib.request.Request(GOOGLE_TOKEN_URL, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                tokens = json.load(r)
        except urllib.error.HTTPError as e:
            raise ValueError(f"Google refused the sign in: {e.read().decode('utf-8', 'replace')[:300]}")
        # The ID token came straight from Google over HTTPS, so its contents can be read
        # directly; the checks below make sure it was meant for this app and is current.
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims.get("aud") != cfg["google_client_id"] or claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
            raise ValueError("That sign in wasn't meant for this app.")
        if claims.get("exp", 0) < time.time():
            raise ValueError("That sign in has expired. Try again.")
        user = {"sub": claims["sub"], "email": claims.get("email", ""), "name": claims.get("name") or claims.get("email", ""),
                "picture": claims.get("picture", "")}
        write_json(app_dir() / "session.json", {"user": user, "signed_in": datetime.now().isoformat(timespec="seconds")})
        return user


def sign_out():
    (app_dir() / "session.json").unlink(missing_ok=True)


SIGNED_IN_PAGE = """<!doctype html><meta charset="utf-8"><title>Desk Agent</title>
<body style="font-family:Segoe UI,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;color:#1b2230">
<div style="text-align:center"><h2>{title}</h2><p>{text}</p><p>You can close this window.</p></div>
<script>setTimeout(() => window.close(), 1500)</script></body>"""


# ---------------------------------------------------------------- the web server

class Hub:
    """Fans events out to every open window."""

    def __init__(self):
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.last_seen = time.time()

    def emit(self, event):
        with self.lock:
            for q in self.clients:
                q.put(event)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


class Server:
    def __init__(self, port: int = 0):
        self.token = secrets.token_urlsafe(24)
        self.hub = Hub()
        self.brain = Brain(self.hub.emit)
        self.google = GoogleSignIn()
        self.port = port or free_port()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _allowed(self, query):
                # Only this machine, only pages that know the secret: stops websites from driving the app.
                host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")
                token = self.headers.get("X-Token") or query.get("t", [""])[0]
                return host_ok and secrets.compare_digest(token, server.token)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_DELETE(self):
                self._route("DELETE")

            def _route(self, method):
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                if method == "GET" and url.path == "/favicon.ico":
                    return self._send(200, ICON_SVG, "image/svg+xml")
                if method == "GET" and url.path == "/oauth/callback":
                    return self._oauth(query)
                if not self._allowed(query):
                    return self._send(403, {"error": "forbidden"})
                server.hub.last_seen = time.time()
                if method == "GET" and url.path == "/":
                    return self._send(200, PAGE.replace("__TOKEN__", server.token), "text/html")
                if method == "GET" and url.path == "/api/events":
                    return self._events()
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                try:
                    result = server.api(method, url.path, body)
                    if isinstance(result, tuple):
                        return self._send(200, result[1], result[0])
                    self._send(200, result if result is not None else {"ok": True})
                except KeyError:
                    self._send(404, {"error": "not found"})
                except (ValueError, RuntimeError, OSError) as e:
                    log.warning("api error: %s", e)
                    self._send(400, {"error": str(e)})

            def _oauth(self, query):
                # Google sends the browser here. It can't carry the app's secret, but it
                # must carry the one time state value the app made for this sign in.
                try:
                    if query.get("error"):
                        raise ValueError(query["error"][0])
                    if server.brain.busy_chat:
                        raise ValueError("Desk Agent is busy. Wait for the current job, then sign in again.")
                    user = server.google.finish(server.brain.cfg, query.get("state", [""])[0], query.get("code", [""])[0])
                    page = SIGNED_IN_PAGE.format(title=f"Signed in as {html.escape(user['name'])}", text=html.escape(user["email"]))
                    server.hub.emit({"type": "account"})
                except Exception as e:
                    log.exception("sign in failed")
                    page = SIGNED_IN_PAGE.format(title="Sign in didn't work", text=html.escape(str(e)))
                self._send(200, page, "text/html")

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q = server.hub.subscribe()
                try:
                    self.wfile.write(b": hello\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            event = q.get(timeout=15)
                            data = f"data: {json.dumps(event)}\n\n"
                        except queue.Empty:
                            data = ": ping\n\n"
                        self.wfile.write(data.encode())
                        self.wfile.flush()
                        server.hub.last_seen = time.time()
                except OSError:
                    pass
                finally:
                    server.hub.unsubscribe(q)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def api(self, method, path, body):
        brain, store, cfg = self.brain, self.brain.store, self.brain.cfg
        parts = path.strip("/").split("/")[1:]  # drop "api"
        head = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else None

        if head == "state" and method == "GET":
            return self.state()
        if head == "auth":
            if arg == "start" and method == "POST":
                return {"url": self.google.start(cfg, self.port)}
            if arg == "signout" and method == "POST":
                if brain.busy_chat:
                    raise RuntimeError("Wait for the current job to finish first.")
                sign_out()
                return None
            if arg == "setup" and method == "POST":
                changes = {"google_client_id": body.get("client_id", "").strip(), "require_sign_in": bool(body.get("require_sign_in"))}
                if (body.get("client_secret") or "").strip():  # blank keeps the saved secret
                    changes["google_client_secret"] = body["client_secret"].strip()
                brain.reload_config(changes)
                return None
        if head == "chats":
            if method == "POST" and not arg:
                return store.new_chat(body.get("project"), body.get("agent") or "assistant")
            if method == "GET" and arg:
                return store.get_chat(arg)
            if method == "DELETE" and arg:
                return store.delete_chat(arg)
            if method == "POST" and arg and len(parts) == 2:
                chat = store.get_chat(arg)
                for key in ("title", "agent", "project"):
                    if key in body:
                        chat[key] = body[key]
                store.save_chat(chat)
                return chat
            if method == "POST" and arg and parts[2] == "send":
                brain.start_job(arg, body)
                return None
            if method == "GET" and arg and parts[2] == "export":
                return ("text/markdown", export_markdown(store.get_chat(arg)))
        if head == "stop" and method == "POST":
            brain.stop_event.set()
            return None
        if head == "confirm" and method == "POST":
            brain.answer_confirm(body["id"], bool(body.get("ok")), bool(body.get("all")))
            return None
        if head == "extract" and method == "POST":
            data = base64.b64decode(body["data"])
            if len(data) > 25_000_000:
                raise ValueError("That file is too big (over 25 MB).")
            text = extract_text(body["name"], data)
            return {"name": body["name"], "text": text[:200_000], "chars": len(text)}
        if head == "projects":
            if method == "POST" and not arg:
                project = {"id": uuid.uuid4().hex[:10], "name": body.get("name") or "New project",
                           "instructions": body.get("instructions", ""), "files": []}
                store.save_project(project)
                return project
            if method == "POST" and arg:
                project = store.get_project(arg) or {}
                if not project:
                    raise KeyError(arg)
                for key in ("name", "instructions", "files"):
                    if key in body:
                        project[key] = body[key]
                store.save_project(project)
                return project
            if method == "DELETE" and arg:
                return store.delete_project(arg)
        if head == "agents":
            agents = dict(cfg.get("agents", {}))
            if method == "POST":
                key = arg or re.sub(r"[^a-z0-9]+", "-", (body.get("name") or "agent").lower()).strip("-")[:30] or "agent"
                if key in BUILTIN_AGENTS and not arg:
                    key += "-2"
                agents[key] = {"name": body.get("name") or key, "instructions": body.get("instructions", ""),
                               "tools": [t for t in body.get("tools", []) if t in TOOL_GROUPS or t == "mcp"],
                               "model": body.get("model") or AUTO}
                brain.reload_config({"agents": agents})
                return {"id": key}
            if method == "DELETE" and arg:
                agents.pop(arg, None)
                brain.reload_config({"agents": agents})
                return None
        if head == "models":
            if method == "POST" and arg == "custom":
                path = Path(body["path"].strip().strip('"'))
                if path.suffix.lower() != ".gguf" or not path.exists():
                    raise ValueError("Pick an existing .gguf file.")
                custom = dict(cfg.get("custom_models", {}))
                custom[re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem)[:40]] = str(path)
                brain.reload_config({"custom_models": custom})
                return None
            if method == "POST" and arg in CATALOG:
                if arg not in DOWNLOADS or DOWNLOADS[arg].get("error"):
                    threading.Thread(target=download_model, args=(arg,), daemon=True).start()
                return None
            if method == "DELETE" and arg:
                if brain.engine.model == arg:
                    brain.engine.stop()
                if arg in cfg.get("custom_models", {}):
                    custom = dict(cfg["custom_models"])
                    custom.pop(arg)
                    brain.reload_config({"custom_models": custom})
                elif arg in CATALOG:
                    shutil.rmtree(models_dir() / arg, ignore_errors=True)
                return None
        if head == "config" and method == "POST":
            allowed = {"model", "context", "threads", "learn", "confirm_actions", "max_steps", "workspace", "mcpServers", "require_sign_in"}
            changes = {k: v for k, v in body.items() if k in allowed}
            if "context" in changes and changes["context"] != cfg["context"]:
                brain.engine.stop()  # reload with the new size next time
            brain.reload_config(changes)
            return None
        if head == "memory":
            if method == "GET":
                return {"text": read_memory()}
            if method == "POST":
                memory_path().write_text(body.get("text", ""), encoding="utf-8")
                return None
        if head == "artifact-save" and method == "POST":
            folder = workspace_path(cfg, "artifacts")
            folder.mkdir(parents=True, exist_ok=True)
            ext = {"html": ".html", "svg": ".svg", "markdown": ".md"}.get(body["kind"], ".txt")
            name = re.sub(r"[^\w\- ]+", "", body["title"]).strip() or "artifact"
            path = folder / (name + ext)
            path.write_text(body["content"], encoding="utf-8")
            return {"path": str(path)}
        if head == "open-folder" and method == "POST":
            target = {"workspace": workspace_path(cfg, "."), "models": models_dir(), "data": app_dir()}[body["which"]]
            if sys.platform == "win32":
                os.startfile(target)
            return {"path": str(target)}
        raise KeyError(path)

    def state(self):
        cfg = self.brain.cfg
        installed = installed_models(cfg)
        models = []
        for key, entry in CATALOG.items():
            models.append({"key": key, "label": entry["label"], "detail": entry["detail"], "about": entry["about"],
                           "size_gb": entry["size_gb"], "installed": key in installed, "download": DOWNLOADS.get(key)})
        for key in cfg.get("custom_models", {}):
            models.append({"key": key, "label": key, "detail": "your file", "about": cfg["custom_models"][key],
                           "size_gb": None, "installed": key in installed, "custom": True})
        return {
            "config": {k: cfg[k] for k in ("model", "context", "threads", "learn", "confirm_actions", "max_steps", "workspace", "mcpServers")},
            "models": models,
            "agents": {k: {**a, "builtin": k in BUILTIN_AGENTS} for k, a in all_agents(cfg).items()},
            "tool_groups": list(TOOL_GROUPS) + ["mcp"],
            "styles": list(STYLES),
            "projects": self.brain.store.list_projects(),
            "chats": self.brain.store.list_chats(),
            "stats": load_stats(),
            "busy": self.brain.busy_chat,
            "engine": bool(find_llama_server()),
            "loaded": self.brain.engine.model,
            "mcp": {"tools": len(self.brain.mcp.tools), "failed": self.brain.mcp.failed},
            "user": current_user(),
            "google": {"ready": bool(cfg.get("google_client_id")), "client_id": cfg.get("google_client_id", ""),
                       "has_secret": bool(cfg.get("google_client_secret")), "require": bool(cfg.get("require_sign_in"))},
            "callback": f"http://127.0.0.1:{self.port}/oauth/callback",
        }

    def serve(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.brain.close()
        self.httpd.shutdown()


def export_markdown(chat) -> str:
    lines = [f"# {chat['title']}", ""]
    for m in chat["messages"]:
        if m["role"] == "user":
            lines += ["**You:**", "", m.get("content", "")]
            lines += [f"*(attached {a['name']})*" for a in m.get("attachments", [])]
        elif m["role"] == "assistant" and m.get("content"):
            lines += ["**Desk Agent:**", "", m["content"]]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- the window

def find_browser() -> str | None:
    """Edge comes with Windows; its app mode gives a plain window with no tabs or address bar."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("msedge") or shutil.which("google-chrome") or shutil.which("chromium")


def open_window(url: str):
    """Opens the app window."""
    browser = find_browser()
    if browser:
        profile = sub_dir("window")
        return subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                                 "--no-default-browser-check", "--disable-background-mode", "--window-size=1200,820"])
    import webbrowser

    webbrowser.open(url)
    return None


def main():
    logging.basicConfig(filename=app_dir() / "deskagent.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", encoding="utf-8")
    pyautogui.FAILSAFE = True  # slamming the mouse into a screen corner is the emergency stop

    # If Desk Agent is already running, just open another window onto it.
    running = read_json(app_dir() / "running.json", None)
    if running:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{running['port']}/api/state?t={running['token']}", timeout=2):
                open_window(running["url"])
                return
        except OSError:
            pass

    headless = "--no-window" in sys.argv
    port = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--port=")), 0))
    server = Server(port)
    server.serve()
    write_json(app_dir() / "running.json", {"port": server.port, "token": server.token, "url": server.url})
    log.info("serving on %s", server.url)
    if headless:
        print(server.url, flush=True)

    if not headless:
        open_window(server.url)
    # Load the likely model now, so the first reply doesn't wait for it.
    threading.Thread(target=server.brain.warm_up, daemon=True).start()

    # Stay running while a window has the page open. Edge often hands a new window to
    # an Edge process that is already running and the one we launched exits at once,
    # so the browser process can't tell us when the window closes; the page's live
    # connection can.
    had_window, idle_since = False, time.time()
    try:
        while True:
            time.sleep(1)
            if server.hub.clients or server.brain.busy_chat:
                had_window, idle_since = had_window or bool(server.hub.clients), time.time()
            elif not headless and time.time() - idle_since > (30 if had_window else 300):
                log.info("no window open, quitting")
                break
    except KeyboardInterrupt:
        pass
    finally:
        (app_dir() / "running.json").unlink(missing_ok=True)
        server.close()


# ---------------------------------------------------------------- the page

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect x="1" y="1" width="30" height="30" rx="8" fill="#0f766e"/><rect x="6" y="8" width="20" height="13" rx="2" fill="#fff"/><rect x="13" y="21" width="6" height="3" fill="#fff"/><rect x="10" y="24" width="12" height="2" rx="1" fill="#fff"/><path d="M14 11v7l2-1.6 1.4 2.8 1.4-.7-1.4-2.8 2.6-.3z" fill="#14213d"/></svg>"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Desk Agent</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --side: #14213d; --side-text: #dfe6f3; --side-dim: #8d9bb5; --side-hover: #1f2f52;
  --text: #1b2230; --dim: #667085; --line: #e3e7ee; --accent: #0f766e; --accent-soft: #e0f2ef; --user: #eef2ff;
  --code: #0f172a; --code-text: #e2e8f0; --warn: #b42318; --radius: 12px;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #0f1420; --panel: #161c2b; --side: #0a0f1a; --side-hover: #1a2337; --text: #e6e9ef; --dim: #98a2b3;
          --line: #263049; --accent: #2dd4bf; --accent-soft: #123b38; --user: #1c2540; --code: #070b14; }
}
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.55 "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; overflow: hidden; }
button { font: inherit; cursor: pointer; border: 1px solid var(--line); background: var(--panel); color: var(--text); border-radius: 8px; padding: 6px 12px; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.ghost { border-color: transparent; background: transparent; }
button:disabled { opacity: .5; cursor: default; }
input, select, textarea { font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px; }
textarea { resize: vertical; }
a { color: var(--accent); }

#side { width: 270px; background: var(--side); color: var(--side-text); display: flex; flex-direction: column; flex-shrink: 0; }
#side .brand { display: flex; align-items: center; gap: 10px; padding: 16px; font-weight: 600; font-size: 17px; }
#side .brand svg { width: 28px; height: 28px; }
#side .newbtn { margin: 0 12px 10px; background: var(--accent); color: #fff; border: 0; padding: 9px; border-radius: 10px; font-weight: 600; }
#side input.search { margin: 0 12px 8px; background: var(--side-hover); border-color: transparent; color: var(--side-text); }
#side .section { padding: 10px 16px 4px; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--side-dim); display: flex; justify-content: space-between; align-items: center; }
#side .section button { color: var(--side-dim); background: none; border: 0; padding: 0 4px; font-size: 18px; line-height: 1; }
#side .list { overflow-y: auto; flex: 1; padding-bottom: 8px; }
#side .item { padding: 7px 16px; margin: 1px 8px; border-radius: 8px; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 14px; display: flex; justify-content: space-between; gap: 6px; }
#side .item:hover, #side .item.active { background: var(--side-hover); }
#side .item .x { visibility: hidden; color: var(--side-dim); }
#side .item:hover .x { visibility: visible; }
#side .foot { border-top: 1px solid #ffffff14; padding: 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
#side .foot button { background: var(--side-hover); color: var(--side-text); border: 0; font-size: 13px; }
#side .stats { grid-column: 1 / -1; font-size: 12px; color: var(--side-dim); text-align: center; padding-top: 4px; }

#main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
#top { display: flex; align-items: center; gap: 8px; padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--panel); flex-wrap: wrap; }
#title { font-weight: 600; font-size: 16px; border: 1px solid transparent; background: transparent; min-width: 120px; flex: 1; }
#title:hover, #title:focus { border-color: var(--line); }
#top label { font-size: 13px; color: var(--dim); display: flex; align-items: center; gap: 5px; }
#top select { padding: 4px 8px; font-size: 13px; }
#top .short { display: none; }
body.panel-open #top .lbl { display: none; } body.panel-open #top .short { display: inline; } body.panel-open #title { min-width: 0; flex-basis: 100%; }
#status { font-size: 13px; color: var(--dim); padding: 4px 16px 0; min-height: 22px; }

#chat { flex: 1; overflow-y: auto; padding: 8px 0 24px; }
.msg { max-width: 820px; margin: 14px auto; padding: 0 20px; }
.msg.user .bubble { background: var(--user); border-radius: var(--radius); padding: 10px 14px; display: inline-block; max-width: 100%; white-space: pre-wrap; }
.msg.user { text-align: right; }
.msg.user .bubble { text-align: left; }
.msg .who { font-size: 12px; color: var(--dim); margin-bottom: 2px; }
.msg .actions { font-size: 12px; margin-top: 4px; opacity: 0; transition: opacity .15s; }
.msg:hover .actions { opacity: 1; }
.msg .actions button { padding: 2px 8px; font-size: 12px; }
.chip { display: inline-block; font-size: 12px; background: var(--accent-soft); color: var(--accent); border-radius: 20px; padding: 2px 10px; margin: 4px 4px 0 0; }
.md p { margin: .5em 0; } .md h1, .md h2, .md h3 { margin: .8em 0 .3em; line-height: 1.25; } .md h1 { font-size: 1.35em; } .md h2 { font-size: 1.2em; } .md h3 { font-size: 1.05em; }
.md ul, .md ol { padding-left: 1.4em; margin: .4em 0; } .md blockquote { border-left: 3px solid var(--line); margin: .5em 0; padding-left: 12px; color: var(--dim); }
.md table { border-collapse: collapse; margin: .6em 0; } .md th, .md td { border: 1px solid var(--line); padding: 4px 10px; }
.md code { background: var(--accent-soft); padding: 1px 5px; border-radius: 5px; font-family: Consolas, monospace; font-size: .92em; }
.codeblock { background: var(--code); color: var(--code-text); border-radius: 10px; margin: .6em 0; overflow: hidden; }
.codeblock .bar { display: flex; justify-content: space-between; align-items: center; font-size: 12px; padding: 4px 10px; background: #ffffff10; }
.codeblock .bar button { background: transparent; color: var(--code-text); border: 1px solid #ffffff30; padding: 1px 8px; font-size: 12px; }
.codeblock pre { margin: 0; padding: 12px; overflow-x: auto; } .codeblock code { background: none; padding: 0; color: inherit; }
.tool { font-size: 13px; border: 1px solid var(--line); border-radius: 10px; margin: 6px 0; background: var(--panel); }
.tool summary { cursor: pointer; padding: 6px 10px; color: var(--dim); list-style: none; }
.tool summary::before { content: "\25B8  "; } .tool[open] summary::before { content: "\25BE  "; }
.tool pre { margin: 0; padding: 8px 10px; border-top: 1px solid var(--line); white-space: pre-wrap; max-height: 260px; overflow: auto; font-size: 12px; }
.agentbox { border-left: 3px solid var(--accent); padding: 4px 0 4px 12px; margin: 8px 0; }
.agentbox .agentname { font-size: 12px; font-weight: 600; color: var(--accent); }
.thinking { font-size: 13px; color: var(--dim); border: 1px dashed var(--line); border-radius: 10px; margin: 6px 0; }
.thinking summary { cursor: pointer; padding: 6px 10px; } .thinking div { padding: 0 12px 8px; white-space: pre-wrap; }
.artifact-card { display: flex; align-items: center; gap: 10px; border: 1px solid var(--line); background: var(--panel); border-radius: 10px; padding: 10px 14px; margin: 8px 0; cursor: pointer; max-width: 420px; }
.artifact-card:hover { border-color: var(--accent); } .artifact-card b { display: block; }
.error { color: var(--warn); }
.cursor::after { content: "\2588"; animation: blink 1s steps(1) infinite; color: var(--accent); } @keyframes blink { 50% { opacity: 0; } }
#empty { max-width: 620px; margin: 12vh auto 0; text-align: center; color: var(--dim); padding: 0 20px; }
#empty h1 { color: var(--text); font-weight: 600; } #empty .ideas { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 18px; }
#empty .ideas button { text-align: left; padding: 10px 12px; font-size: 14px; }

#composer { max-width: 860px; width: 100%; margin: 0 auto; padding: 0 20px 16px; }
#composer .box { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 8px; box-shadow: 0 2px 12px #0000000d; }
#composer textarea { width: 100%; border: 0; resize: none; min-height: 52px; max-height: 260px; padding: 6px 8px; outline: none; background: transparent; }
#composer .row { display: flex; align-items: center; gap: 8px; }
#composer .row .grow { flex: 1; }
#attachments .chip button { border: 0; background: none; padding: 0 0 0 6px; color: inherit; }
#composer .hint { font-size: 12px; color: var(--dim); text-align: center; margin-top: 6px; }

#panel { width: 46%; min-width: 360px; border-left: 1px solid var(--line); background: var(--panel); display: none; flex-direction: column; }
#panel.open { display: flex; }
#panel .head { display: flex; align-items: center; gap: 6px; padding: 10px 12px; border-bottom: 1px solid var(--line); }
#panel .head b { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#panel .tabs button.on { background: var(--accent-soft); border-color: var(--accent); }
#panel .body { flex: 1; overflow: auto; }
#panel iframe { width: 100%; height: 100%; border: 0; background: #fff; }
#panel pre { margin: 0; padding: 14px; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 13px; }
#panel .md { padding: 14px 20px; }

.modal-bg { position: fixed; inset: 0; background: #0008; display: flex; align-items: center; justify-content: center; z-index: 10; }
.modal { background: var(--panel); border-radius: 14px; width: min(640px, 94vw); max-height: 88vh; overflow: auto; padding: 20px 22px; box-shadow: 0 10px 40px #0005; }
.modal h2 { margin: 0 0 12px; font-size: 19px; }
.modal .field { margin: 10px 0; } .modal .field label { display: block; font-size: 13px; color: var(--dim); margin-bottom: 4px; }
.modal .field input[type=text], .modal .field textarea, .modal .field select { width: 100%; }
.modal .buttons { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
.card { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin: 8px 0; }
.card .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.card small { color: var(--dim); }
.bar-outer { height: 6px; background: var(--line); border-radius: 4px; overflow: hidden; margin-top: 6px; } .bar-inner { height: 100%; background: var(--accent); }
.checks label { display: block; margin: 4px 0; }
.ok { color: #12805c; }
#side .account { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; padding: 4px 4px 8px; }
#side .account .who { flex: 1; min-width: 0; font-size: 13px; line-height: 1.3; }
#side .account .who small { color: var(--side-dim); display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#side .account button { padding: 4px 8px; font-size: 12px; }
.avatar { width: 30px; height: 30px; border-radius: 50%; background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 600; flex-shrink: 0; object-fit: cover; }
.gbtn { display: inline-flex; align-items: center; gap: 10px; background: #fff; color: #1f1f1f; border: 1px solid #747775; border-radius: 20px; padding: 8px 16px; font-weight: 500; }
.gbtn:hover { background: #f2f2f2; border-color: #747775; }
#gate { position: fixed; inset: 0; background: var(--bg); z-index: 20; display: flex; align-items: center; justify-content: center; text-align: center; }
#gate .box { max-width: 380px; padding: 20px; }
ol.steps li { margin: 6px 0; }
@media (max-width: 760px) { #side { display: none; } #panel { position: fixed; inset: 0; width: 100%; min-width: 0; z-index: 5; } }
</style>
</head>
<body>
<nav id="side">
  <div class="brand">
    <svg viewBox="0 0 32 32"><rect x="1" y="1" width="30" height="30" rx="8" fill="#0f766e"/><rect x="6" y="8" width="20" height="13" rx="2" fill="#fff"/><rect x="13" y="21" width="6" height="3" fill="#fff"/><rect x="10" y="24" width="12" height="2" rx="1" fill="#fff"/><path d="M14 11v7l2-1.6 1.4 2.8 1.4-.7-1.4-2.8 2.6-.3z" fill="#14213d"/></svg>
    Desk Agent
  </div>
  <button class="newbtn" onclick="newChat()">+ New chat</button>
  <input class="search" id="search" placeholder="Search chats" oninput="renderSide()">
  <div class="list">
    <div class="section">Projects <button title="New project" onclick="editProject()">+</button></div>
    <div id="projects"></div>
    <div class="section"><span id="chatsLabel">Chats</span></div>
    <div id="chats"></div>
  </div>
  <div class="foot">
    <div id="account" class="account"></div>
    <button onclick="showAgents()">Agents</button>
    <button onclick="showModels()">Models</button>
    <button onclick="showMemory()">Memory</button>
    <button onclick="showSettings()">Settings</button>
    <div class="stats" id="stats"></div>
  </div>
</nav>

<main id="main">
  <div id="top">
    <input id="title" value="New chat" onchange="renameChat(this.value)">
    <label><span class="lbl">Agent</span> <select id="agentSel" onchange="setChatField('agent', this.value)"></select></label>
    <label><span class="lbl">Model</span> <select id="modelSel" onchange="setConfig({model: this.value})"></select></label>
    <label><span class="lbl">Style</span> <select id="styleSel"></select></label>
    <label title="Ask the model to reason step by step before answering"><input type="checkbox" id="think"> <span class="lbl">Think first</span><span class="short">Think</span></label>
    <button class="ghost" title="Download this chat" onclick="exportChat()">&#8681;</button>
  </div>
  <div id="status"></div>
  <div id="chat"></div>
  <div id="composer">
    <div class="box">
      <div id="attachments"></div>
      <textarea id="input" placeholder="Ask anything, or describe a job" onkeydown="keydown(event)" oninput="autosize()"></textarea>
      <div class="row">
        <button class="ghost" title="Attach files" onclick="document.getElementById('file').click()">&#128206; Attach</button>
        <input type="file" id="file" multiple hidden onchange="attach(this.files)">
        <span class="grow"></span>
        <button id="stopBtn" onclick="stopJob()" hidden>Stop</button>
        <button id="sendBtn" class="primary" onclick="send()">Send</button>
      </div>
    </div>
    <div class="hint">Runs on this laptop. Nothing you type leaves it, apart from web searches.</div>
  </div>
</main>

<aside id="panel">
  <div class="head">
    <b id="artTitle"></b>
    <span class="tabs"><button id="tabPreview" onclick="artTab('preview')">Preview</button><button id="tabCode" onclick="artTab('code')">Code</button></span>
    <button onclick="copyText(currentArt.content)">Copy</button>
    <button onclick="downloadArt()">Download</button>
    <button onclick="saveArt()">Save</button>
    <button class="ghost" onclick="closePanel()">&#10005;</button>
  </div>
  <div class="body" id="artBody"></div>
</aside>

<script>
const TOKEN = "__TOKEN__";
let S = null, chat = null, attachments = [], live = null, currentArt = null, artMode = "preview", project = null;

// ------------------------------------------------ server calls
async function api(method, path, body) {
  const r = await fetch(path, {method, headers: {"Content-Type": "application/json", "X-Token": TOKEN}, body: body ? JSON.stringify(body) : undefined});
  const ctype = r.headers.get("Content-Type") || "";
  const data = ctype.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data.error || data);
  return data;
}
async function refresh() {
  S = await api("GET", "/api/state");
  renderSide(); renderTop(); renderAccount();
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c])); }
function $(id) { return document.getElementById(id); }
function setStatus(t) { $("status").textContent = t || ""; }
function toast(t) { setStatus(t); setTimeout(() => { if ($("status").textContent === t) setStatus(""); }, 5000); }

// ------------------------------------------------ markdown
function md(src) {
  const blocks = [];
  src = String(src || "").replace(/```([\w+-]*)[^\n]*\n([\s\S]*?)(```|$)/g, (_, lang, code) => {
    blocks.push({lang, code: code.replace(/\n$/, "")});
    return `\u0000${blocks.length - 1}\u0000`;
  });
  const inline = t => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  const lines = src.split("\n"); let out = "", i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const block = line.match(/^\u0000(\d+)\u0000$/);
    if (block) { const b = blocks[+block[1]]; out += codeBlock(b.code, b.lang); i++; continue; }
    if (/^#{1,3} /.test(line)) { const n = line.match(/^#+/)[0].length; out += `<h${n}>${inline(line.slice(n + 1))}</h${n}>`; i++; continue; }
    if (/^\s*[-*] /.test(line)) { out += "<ul>"; while (i < lines.length && /^\s*[-*] /.test(lines[i])) out += `<li>${inline(lines[i++].replace(/^\s*[-*] /, ""))}</li>`; out += "</ul>"; continue; }
    if (/^\s*\d+[.)] /.test(line)) { out += "<ol>"; while (i < lines.length && /^\s*\d+[.)] /.test(lines[i])) out += `<li>${inline(lines[i++].replace(/^\s*\d+[.)] /, ""))}</li>`; out += "</ol>"; continue; }
    if (/^> ?/.test(line)) { let q = []; while (i < lines.length && /^> ?/.test(lines[i])) q.push(lines[i++].replace(/^> ?/, "")); out += `<blockquote>${inline(q.join(" "))}</blockquote>`; continue; }
    if (/^\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = l => l.trim().slice(1, -1).split("|").map(c => inline(c.trim()));
      out += "<table><tr>" + cells(line).map(c => `<th>${c}</th>`).join("") + "</tr>"; i += 2;
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) out += "<tr>" + cells(lines[i++]).map(c => `<td>${c}</td>`).join("") + "</tr>";
      out += "</table>"; continue;
    }
    if (!line.trim()) { i++; continue; }
    let para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,3} |\s*[-*] |\s*\d+[.)] |> ?|\u0000|\|)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    out += `<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`;
  }
  return out;
}
function codeBlock(code, lang) {
  const id = "c" + Math.random().toString(36).slice(2);
  window[id] = code;
  const preview = /^(html|svg)$/i.test(lang) ? `<button onclick="openArt({title: 'Preview', kind: '${lang.toLowerCase()}', content: window['${id}']})">Preview</button> ` : "";
  return `<div class="codeblock"><div class="bar"><span>${esc(lang || "code")}</span><span>${preview}<button onclick="copyText(window['${id}'], this)">Copy</button></span></div><pre><code>${esc(code)}</code></pre></div>`;
}
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => { if (btn) { const t = btn.textContent; btn.textContent = "Copied"; setTimeout(() => btn.textContent = t, 1200); } });
}

// ------------------------------------------------ sidebar and top bar
function renderSide() {
  const q = $("search").value.toLowerCase();
  $("projects").innerHTML = S.projects.map(p => `<div class="item ${project === p.id ? "active" : ""}" onclick="pickProject('${p.id}')"><span>&#128193; ${esc(p.name)}</span><span class="x" onclick="event.stopPropagation(); editProject('${p.id}')">&#9998;</span></div>`).join("") || `<div class="item" style="color:var(--side-dim);cursor:default">None yet</div>`;
  const proj = S.projects.find(p => p.id === project);
  $("chatsLabel").innerHTML = proj ? `Chats in ${esc(proj.name)} <a href="#" style="color:var(--side-dim)" onclick="pickProject(null); return false">show all</a>` : "Chats";
  $("chats").innerHTML = S.chats.filter(c => (!project || c.project === project) && (!q || (c.title || "").toLowerCase().includes(q)))
    .map(c => `<div class="item ${chat && chat.id === c.id ? "active" : ""}" onclick="openChat('${c.id}')"><span>${esc(c.title)}</span><span class="x" title="Delete" onclick="event.stopPropagation(); deleteChat('${c.id}')">&#10005;</span></div>`).join("");
  const st = S.stats;
  $("stats").textContent = `${st.jobs || 0} job${st.jobs === 1 ? "" : "s"} since ${st.first_used || "today"} \u00b7 ${(st.words_written || 0).toLocaleString()} words`;
}
function renderTop() {
  const agentSel = $("agentSel");
  agentSel.innerHTML = Object.entries(S.agents).map(([k, a]) => `<option value="${k}">${esc(a.name)}</option>`).join("");
  agentSel.value = chat?.agent || "assistant";
  const ready = S.models.filter(m => m.installed);
  $("modelSel").innerHTML = `<option value="auto">Auto</option>` + ready.map(m => `<option value="${m.key}">${esc(m.label)}</option>`).join("");
  $("modelSel").value = ready.some(m => m.key === S.config.model) ? S.config.model : "auto";
  if (!$("styleSel").options.length) $("styleSel").innerHTML = S.styles.map(s => `<option value="${s}">${s[0].toUpperCase() + s.slice(1)}</option>`).join("");
  $("title").value = chat?.title || "New chat";
  $("sendBtn").hidden = !!S.busy; $("stopBtn").hidden = !S.busy;
}
function pickProject(id) { project = id; renderSide(); if (!id) return; const c = S.chats.find(c => c.project === id); c ? openChat(c.id) : newChat(); }

// ------------------------------------------------ chats
async function newChat() {
  chat = await api("POST", "/api/chats", {project, agent: $("agentSel").value || "assistant"});
  await refresh(); renderChat(); $("input").focus();
}
async function openChat(id) {
  chat = await api("GET", "/api/chats/" + id);
  if (chat.project) project = chat.project;
  closePanel(); renderSide(); renderTop(); renderChat();
}
async function deleteChat(id) {
  if (!confirm("Delete this chat?")) return;
  await api("DELETE", "/api/chats/" + id);
  if (chat && chat.id === id) { chat = null; renderChat(); }
  refresh();
}
async function renameChat(title) { if (chat) { chat = await api("POST", "/api/chats/" + chat.id, {title}); refresh(); } }
async function setChatField(key, value) { if (chat) { chat = await api("POST", "/api/chats/" + chat.id, {[key]: value}); } }
async function setConfig(changes) { await api("POST", "/api/config", changes); await refresh(); }
function exportChat() { if (chat) window.open(`/api/chats/${chat.id}/export?t=${TOKEN}`); }

function renderChat() {
  const box = $("chat");
  if (!chat || !chat.messages.length) {
    const noModel = S && !S.models.some(m => m.installed);
    box.innerHTML = `<div id="empty"><h1>What can I help with?</h1>
      ${noModel ? `<p>First, download a model. It only takes one click.</p><button class="primary" onclick="showModels()">Get a model</button>` : `<p>Your own AI, running on this laptop.</p>`}
      <div class="ideas">${["Make me a Python program that renames my photos by date", "Write a friendly email asking for a quote", "Build a simple to do list web page", "Search the web for this week's weather and summarise it"].map(t => `<button onclick="useIdea(this.textContent)">${t}</button>`).join("")}</div></div>`;
    return;
  }
  const results = {}; chat.messages.filter(m => m.role === "tool").forEach(m => results[m.tool_call_id] = m);
  let out = "";
  chat.messages.forEach((m, i) => {
    if (m.role === "user") {
      out += `<div class="msg user"><div class="bubble">${esc(m.content)}${(m.attachments || []).map(a => `<div><span class="chip">&#128196; ${esc(a.name)}</span></div>`).join("")}</div>
        <div class="actions"><button onclick="editMessage(${i})">Edit</button> <button onclick="copyText(chat.messages[${i}].content, this)">Copy</button></div></div>`;
    } else if (m.role === "assistant") {
      out += `<div class="msg ai">`;
      if (m.thinking) out += `<details class="thinking"><summary>Thought it through</summary><div>${esc(m.thinking)}</div></details>`;
      if (m.content) out += `<div class="md">${md(m.content)}</div>`;
      (m.tool_calls || []).forEach(c => out += toolHtml(c, results[c.id]));
      const isLast = !chat.messages.slice(i + 1).some(x => x.role === "assistant" && x.content);
      if (m.content && isLast) out += `<div class="actions"><button onclick="copyText(chat.messages[${i}].content, this)">Copy</button> <button onclick="regenerate()">Regenerate</button></div>`;
      out += `</div>`;
    }
  });
  box.innerHTML = out;
  box.scrollTop = box.scrollHeight;
}
function toolHtml(call, result) {
  let args = {}; try { args = JSON.parse(call.function.arguments || "{}"); } catch (e) {}
  const name = call.function.name;
  if (name === "create_artifact") {
    const art = (chat.artifacts || []).find(a => result && a.id === result.artifact) || (chat.artifacts || []).find(a => a.title === args.title);
    return art ? artifactCard(art) : "";
  }
  if (name === "ask_agent") {
    const log = (result?.agent_log || []).map(e => (e.content ? `<div class="md">${md(e.content)}</div>` : "") + (e.tools?.length ? `<div class="chip">used ${esc(e.tools.join(", "))}</div>` : "")).join("");
    return `<div class="agentbox"><div class="agentname">&#129302; ${esc(result?.agent || args.agent)} agent</div><details class="tool"><summary>Task: ${esc((args.task || "").slice(0, 120))}</summary><div style="padding: 4px 10px">${log || "<pre>" + esc(result?.content || "") + "</pre>"}</div></details></div>`;
  }
  return `<details class="tool"><summary>${esc(toolLabel(name, args))}</summary><pre>${esc(result ? result.content : "running...")}</pre></details>`;
}
function toolLabel(name, a) {
  const labels = {web_search: `Searched the web: ${a.query}`, read_webpage: `Read ${a.url}`, write_file: `Wrote ${a.path}`, read_file: `Read ${a.path}`,
    list_files: `Looked in ${a.path}`, run_command: `Ran ${a.command}`, open: `Opened ${a.target}`, type_text: `Typed text`, press_keys: `Pressed ${a.keys}`,
    focus_window: `Switched to ${a.title}`, list_windows: `Checked open windows`};
  return labels[name] || `${name.replace("__", ": ")} ${JSON.stringify(a).slice(0, 80)}`;
}
function artifactCard(a) {
  window["art_" + a.id] = a;
  const icon = {html: "&#127760;", svg: "&#127912;", markdown: "&#128221;", code: "&#128187;"}[a.kind] || "&#128196;";
  return `<div class="artifact-card" onclick="openArt(window['art_${a.id}'])"><span style="font-size:22px">${icon}</span><span><b>${esc(a.title)}</b><small style="color:var(--dim)">${a.kind}${a.version > 1 ? ", version " + a.version : ""} \u00b7 click to open</small></span></div>`;
}
function useIdea(t) { $("input").value = t; autosize(); $("input").focus(); }
function editMessage(i) {
  const m = chat.messages[i]; $("input").value = m.content; attachments = m.attachments || []; renderAttachments();
  $("input").dataset.edit = i; autosize(); $("input").focus(); setStatus("Editing an earlier message. Sending replaces it and everything after it.");
}

// ------------------------------------------------ sending
function keydown(e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }
function autosize() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 260) + "px"; }
async function attach(files) {
  for (const f of files) {
    try {
      const data = await new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(r.result.split(",")[1]); r.onerror = bad; r.readAsDataURL(f); });
      setStatus(`Reading ${f.name}...`);
      const res = await api("POST", "/api/extract", {name: f.name, data});
      attachments.push({name: res.name, text: res.text}); setStatus("");
    } catch (e) { toast(e.message); }
  }
  $("file").value = ""; renderAttachments();
}
function renderAttachments() {
  $("attachments").innerHTML = attachments.map((a, i) => `<span class="chip">&#128196; ${esc(a.name)}<button onclick="attachments.splice(${i}, 1); renderAttachments()">&#10005;</button></span>`).join("");
}
async function send(extra = {}) {
  const text = $("input").value.trim();
  if (!extra.regenerate && !text && !attachments.length) return;
  if (!chat) chat = await api("POST", "/api/chats", {project, agent: $("agentSel").value || "assistant"});
  const body = {text, attachments, style: $("styleSel").value, think: $("think").checked, ...extra};
  if ($("input").dataset.edit !== undefined && !extra.regenerate) { body.edit_index = +$("input").dataset.edit; delete $("input").dataset.edit; }
  try {
    await api("POST", `/api/chats/${chat.id}/send`, body);
  } catch (e) { toast(e.message); return; }
  $("input").value = ""; attachments = []; renderAttachments(); autosize();
  chat = await api("GET", "/api/chats/" + chat.id);
  renderChat(); S.busy = chat.id; renderTop(); refresh();
  startLive();
}
function regenerate() { send({regenerate: true}); }
async function stopJob() { await api("POST", "/api/stop"); }

// ------------------------------------------------ live updates while a reply is being written
function startLive() {
  const box = $("chat");
  live = {el: document.createElement("div"), subs: {}, current: null, raw: ""};
  live.el.className = "msg ai"; box.appendChild(live.el);
}
function liveTarget(sub) {
  if (!sub) return live.el;
  if (!live.subs[sub]) { const d = document.createElement("div"); d.className = "agentbox"; d.innerHTML = `<div class="agentname"></div>`; live.el.appendChild(d); live.subs[sub] = d; }
  return live.subs[sub];
}
function onEvent(ev) {
  if (ev.type === "stats" || ev.type === "state") { refresh(); return; }
  if (ev.type === "account") { onAccountChange(); return; }
  if (ev.type === "confirm") { askConfirm(ev); return; }
  if (!chat || ev.chat !== chat.id) { if (ev.type === "done") refresh(); return; }
  if (ev.type === "status") { setStatus(ev.text); return; }
  if (!live && ["assistant_start", "token", "tool_call", "agent_start", "artifact"].includes(ev.type)) startLive();
  const box = $("chat");
  if (ev.type === "assistant_start") {
    const t = liveTarget(ev.sub); live.current = document.createElement("div"); live.current.className = "md cursor"; live.raw = ""; t.appendChild(live.current); setStatus("Writing...");
  } else if (ev.type === "token" && live.current) {
    live.raw += ev.text;
    const shown = live.raw.replace(/<think>[\s\S]*?(<\/think>|$)/g, m => m.includes("</think>") ? "" : "_Thinking..._");
    live.current.innerHTML = md(shown);
  } else if (ev.type === "assistant_end" && live.current) {
    live.current.classList.remove("cursor"); if (!ev.message.content) live.current.remove();
  } else if (ev.type === "tool_call") {
    const d = document.createElement("details"); d.className = "tool"; d.id = "t_" + ev.id;
    d.innerHTML = `<summary>${esc(toolLabel(ev.name, ev.args))}</summary><pre>running...</pre>`;
    liveTarget(ev.sub).appendChild(d); setStatus(toolLabel(ev.name, ev.args));
  } else if (ev.type === "tool_result") {
    const d = $("t_" + ev.id); if (d) d.querySelector("pre").textContent = ev.result;
  } else if (ev.type === "agent_start") {
    const t = liveTarget(ev.id); t.querySelector(".agentname").innerHTML = `&#129302; ${esc(ev.agent)} agent is working...`;
  } else if (ev.type === "agent_end") {
    const t = live.subs[ev.id]; if (t) t.querySelector(".agentname").innerHTML = t.querySelector(".agentname").innerHTML.replace(" is working...", "");
  } else if (ev.type === "artifact") {
    const d = document.createElement("div"); d.innerHTML = artifactCard(ev.artifact); live.el.appendChild(d); openArt(ev.artifact);
  } else if (ev.type === "error") {
    const d = document.createElement("div"); d.className = "msg error"; d.textContent = ev.text; box.appendChild(d);
  } else if (ev.type === "done") {
    live = null; setStatus("");
    api("GET", "/api/chats/" + chat.id).then(c => { chat = c; renderChat(); const err = [...box.querySelectorAll(".error")]; refresh(); });
  }
  box.scrollTop = box.scrollHeight;
}
function connect() {
  const es = new EventSource(`/api/events?t=${TOKEN}`);
  es.onmessage = e => onEvent(JSON.parse(e.data));
  es.onerror = () => { es.close(); setTimeout(connect, 2000); };
}

// ------------------------------------------------ side panel for artifacts
function openArt(a) { currentArt = a; $("panel").classList.add("open"); document.body.classList.add("panel-open"); $("artTitle").textContent = a.title; artTab(a.kind === "code" ? "code" : "preview"); }
function closePanel() { $("panel").classList.remove("open"); document.body.classList.remove("panel-open"); currentArt = null; }
function artTab(mode) {
  artMode = mode; const a = currentArt; if (!a) return;
  $("tabPreview").classList.toggle("on", mode === "preview"); $("tabCode").classList.toggle("on", mode === "code");
  $("tabPreview").hidden = a.kind === "code";
  const body = $("artBody");
  if (mode === "code" || a.kind === "code") { body.innerHTML = codeBlock(a.content, a.language || a.kind).replace('class="codeblock"', 'class="codeblock" style="margin:0;border-radius:0;min-height:100%"'); return; }
  if (a.kind === "markdown") { body.innerHTML = `<div class="md">${md(a.content)}</div>`; return; }
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts allow-forms allow-modals");
  frame.srcdoc = a.kind === "svg" ? `<body style="margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh">${a.content}</body>` : a.content;
  body.innerHTML = ""; body.appendChild(frame);
}
function downloadArt() {
  const a = currentArt, ext = {html: "html", svg: "svg", markdown: "md"}[a.kind] || "txt";
  const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([a.content])); link.download = `${a.title}.${ext}`; link.click();
}
async function saveArt() { const r = await api("POST", "/api/artifact-save", currentArt); toast("Saved to " + r.path); }

// ------------------------------------------------ dialogs
function modal(html, onReady) {
  const bg = document.createElement("div"); bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal">${html}</div>`;
  bg.onclick = e => { if (e.target === bg) bg.remove(); };
  document.body.appendChild(bg); if (onReady) onReady(bg); return bg;
}
function askConfirm(ev) {
  const bg = modal(`<h2>Allow this action?</h2><p>The AI wants to:</p><pre style="white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:8px">${esc(ev.description)}</pre>
    <div class="buttons"><button data-a="no">Deny</button><button data-a="all">Allow all for now</button><button class="primary" data-a="yes">Allow</button></div>`);
  bg.onclick = null;
  bg.querySelectorAll("button").forEach(b => b.onclick = async () => {
    await api("POST", "/api/confirm", {id: ev.id, ok: b.dataset.a !== "no", all: b.dataset.a === "all"}); bg.remove();
  });
}

let modelTimer = null;
function showModels() {
  const draw = () => {
    const rows = S.models.map(m => {
      const dl = m.download;
      let action = m.installed ? `<span class="ok">Ready</span> <button onclick="delModel('${m.key}')">Delete</button>`
        : dl && !dl.error ? `<small>${(dl.done / 1e9).toFixed(2)} of ${dl.total ? (dl.total / 1e9).toFixed(2) : "?"} GB</small>`
        : `<button class="primary" onclick="getModel('${m.key}')">${dl && dl.error ? "Try again" : "Download"}</button>`;
      const bar = dl && !dl.error && dl.total ? `<div class="bar-outer"><div class="bar-inner" style="width:${100 * dl.done / dl.total}%"></div></div>` : "";
      const err = dl && dl.error ? `<div class="error"><small>${esc(dl.error)}</small></div>` : "";
      return `<div class="card"><div class="row"><div><b>${esc(m.label)}</b> <small>${esc(m.detail)}${m.size_gb ? ", " + m.size_gb + " GB" : ""}</small><br><small>${esc(m.about)}</small></div><div style="white-space:nowrap">${action}</div></div>${bar}${err}</div>`;
    }).join("");
    return `<h2>Models</h2><p style="color:var(--dim);margin-top:0">Download once, then they work offline. Start with General.</p>${rows}
      ${S.engine ? "" : `<p class="error">The AI engine (llama-server) is missing. Reinstall Desk Agent.</p>`}
      <div class="field"><label>Add your own .gguf model file (full path)</label><div style="display:flex;gap:6px"><input type="text" id="customPath" placeholder="C:\\Users\\you\\Downloads\\model.gguf" style="flex:1"><button onclick="addCustom()">Add</button></div></div>
      <div class="buttons"><button onclick="api('POST','/api/open-folder',{which:'models'})">Open models folder</button><button class="primary" onclick="this.closest('.modal-bg').remove()">Done</button></div>`;
  };
  const bg = modal(draw());
  clearInterval(modelTimer);
  modelTimer = setInterval(async () => {
    if (!document.body.contains(bg)) { clearInterval(modelTimer); return; }
    if (S.models.some(m => m.download && !m.download.error)) { await refresh(); const keep = bg.querySelector("#customPath")?.value; bg.firstChild.innerHTML = draw(); if (keep) bg.querySelector("#customPath").value = keep; }
  }, 1000);
}
async function getModel(key) { await api("POST", "/api/models/" + key); setTimeout(async () => { await refresh(); document.querySelector(".modal-bg")?.remove(); showModels(); }, 600); }
async function delModel(key) { if (!confirm("Delete this model from the laptop?")) return; await api("DELETE", "/api/models/" + key); await refresh(); document.querySelector(".modal-bg")?.remove(); showModels(); }
async function addCustom() { try { await api("POST", "/api/models/custom", {path: $("customPath").value}); await refresh(); document.querySelector(".modal-bg")?.remove(); showModels(); } catch (e) { alert(e.message); } }

function showAgents() {
  const groups = {code: "Write and run code", desktop: "Open apps and type", web: "Search the web", artifacts: "Show work in the side panel", delegate: "Hand work to other agents", mcp: "MCP server tools"};
  const list = Object.entries(S.agents).map(([k, a]) => `<div class="card"><div class="row"><div><b>${esc(a.name)}</b> <small>${a.builtin ? "built in" : "yours"} \u00b7 model: ${esc(a.model)} \u00b7 ${esc((a.tools || []).join(", ") || "no tools")}</small><br><small>${esc(a.instructions)}</small></div>
    <div style="white-space:nowrap"><button onclick="useAgent('${k}')">Chat</button> ${a.builtin ? `<button onclick="editAgent('${k}', true)">Copy</button>` : `<button onclick="editAgent('${k}')">Edit</button> <button onclick="delAgent('${k}')">Delete</button>`}</div></div></div>`).join("");
  modal(`<h2>Agents</h2><p style="color:var(--dim);margin-top:0">Each agent is a role with its own instructions and tools. Pick one for a chat, or let Assistant and Planner hand work to the others.</p>${list}
    <div class="buttons"><button onclick="editAgent()">+ New agent</button><button class="primary" onclick="this.closest('.modal-bg').remove()">Done</button></div>`);
  window.agentGroups = groups;
}
function editAgent(key, copy) {
  document.querySelector(".modal-bg")?.remove();
  const a = key ? S.agents[key] : {name: "", instructions: "", tools: ["web"], model: "auto"};
  const models = [["auto", "Auto"], ...S.models.map(m => [m.key, m.label])];
  modal(`<h2>${key && !copy ? "Edit agent" : "New agent"}</h2>
    <div class="field"><label>Name</label><input type="text" id="agName" value="${esc(copy ? a.name + " (copy)" : a.name)}"></div>
    <div class="field"><label>Instructions: who it is and how it should work</label><textarea id="agInstr" rows="6">${esc(a.instructions)}</textarea></div>
    <div class="field"><label>Preferred model</label><select id="agModel">${models.map(([k, l]) => `<option value="${k}" ${a.model === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
    <div class="field checks"><label>Tools it can use</label>${Object.entries(window.agentGroups).map(([g, l]) => `<label><input type="checkbox" value="${g}" ${a.tools.includes(g) ? "checked" : ""}> ${l}</label>`).join("")}</div>
    <div class="buttons"><button onclick="this.closest('.modal-bg').remove(); showAgents()">Cancel</button><button class="primary" id="agSave">Save</button></div>`, bg => {
    bg.querySelector("#agSave").onclick = async () => {
      const tools = [...bg.querySelectorAll(".checks input:checked")].map(i => i.value);
      await api("POST", "/api/agents" + (key && !copy ? "/" + key : ""), {name: $("agName").value, instructions: $("agInstr").value, model: $("agModel").value, tools});
      bg.remove(); await refresh(); showAgents();
    };
  });
}
async function delAgent(k) { if (!confirm("Delete this agent?")) return; await api("DELETE", "/api/agents/" + k); document.querySelector(".modal-bg")?.remove(); await refresh(); showAgents(); }
async function useAgent(k) { document.querySelector(".modal-bg")?.remove(); $("agentSel").value = k; await newChat(); await setChatField("agent", k); renderTop(); }

function editProject(id) {
  const p = id ? S.projects.find(p => p.id === id) : {name: "", instructions: "", files: []};
  let files = [...(p.files || [])];
  const fileList = () => files.map((f, i) => `<span class="chip">&#128196; ${esc(f.name)}<button style="border:0;background:none;color:inherit" onclick="window.prjFiles.splice(${i},1); window.drawPrjFiles()">&#10005;</button></span>`).join("") || `<small style="color:var(--dim)">No files yet</small>`;
  window.prjFiles = files;
  modal(`<h2>${id ? "Edit project" : "New project"}</h2>
    <div class="field"><label>Name</label><input type="text" id="prName" value="${esc(p.name)}"></div>
    <div class="field"><label>Instructions for every chat in this project</label><textarea id="prInstr" rows="5" placeholder="For example: I'm writing a fantasy novel. Keep the tone dark and the chapters short.">${esc(p.instructions)}</textarea></div>
    <div class="field"><label>Knowledge files the AI reads in every chat</label><div id="prFiles"></div><button style="margin-top:6px" onclick="$('prFileInput').click()">Add files</button><input type="file" id="prFileInput" multiple hidden></div>
    <div class="buttons">${id ? `<button style="margin-right:auto" onclick="delProject('${id}')">Delete project</button>` : ""}<button onclick="this.closest('.modal-bg').remove()">Cancel</button><button class="primary" id="prSave">Save</button></div>`, bg => {
    window.drawPrjFiles = () => bg.querySelector("#prFiles").innerHTML = fileList(); window.drawPrjFiles();
    bg.querySelector("#prFileInput").onchange = async e => {
      for (const f of e.target.files) {
        const data = await new Promise(ok => { const r = new FileReader(); r.onload = () => ok(r.result.split(",")[1]); r.readAsDataURL(f); });
        try { const res = await api("POST", "/api/extract", {name: f.name, data}); files.push({name: res.name, text: res.text}); } catch (err) { alert(err.message); }
      }
      window.drawPrjFiles();
    };
    bg.querySelector("#prSave").onclick = async () => {
      const body = {name: $("prName").value || "New project", instructions: $("prInstr").value, files};
      const saved = await api("POST", "/api/projects" + (id ? "/" + id : ""), body);
      bg.remove(); await refresh(); if (!id) pickProject(saved.id);
    };
  });
}
async function delProject(id) { if (!confirm("Delete this project? Its chats are kept.")) return; await api("DELETE", "/api/projects/" + id); document.querySelector(".modal-bg")?.remove(); project = null; refresh(); }

async function showMemory() {
  const m = await api("GET", "/api/memory");
  modal(`<h2>Memory</h2><p style="color:var(--dim);margin-top:0">What Desk Agent has learned about you. It's added to every chat. Edit or clear it any time.</p>
    <textarea id="memText" rows="14" style="width:100%">${esc(m.text)}</textarea>
    <div class="buttons"><button onclick="$('memText').value=''">Clear</button><button onclick="this.closest('.modal-bg').remove()">Cancel</button><button class="primary" id="memSave">Save</button></div>`, bg => {
    bg.querySelector("#memSave").onclick = async () => { await api("POST", "/api/memory", {text: $("memText").value}); bg.remove(); toast("Memory saved"); };
  });
}
function showSettings() {
  const c = S.config;
  modal(`<h2>Settings</h2>
    <div class="field"><label>Projects folder (where the AI saves code and files)</label><input type="text" id="seWs" value="${esc(c.workspace)}"></div>
    <div class="field"><label>Chat memory size in tokens (bigger remembers more, uses more RAM)</label><select id="seCtx">${[4096, 8192, 16384, 32768].map(n => `<option ${c.context === n ? "selected" : ""}>${n}</option>`).join("")}</select></div>
    <div class="field"><label>Most tool steps per reply</label><input type="text" id="seSteps" value="${c.max_steps}"></div>
    <div class="field checks"><label><input type="checkbox" id="seConfirm" ${c.confirm_actions ? "checked" : ""}> Ask me before it runs commands, types, opens things or changes files</label>
      <label><input type="checkbox" id="seLearn" ${c.learn ? "checked" : ""}> Learn from my conversations</label></div>
    <div class="field"><label>MCP servers (same format as Claude Desktop's mcpServers)</label><textarea id="seMcp" rows="7" style="width:100%;font-family:Consolas,monospace;font-size:13px">${esc(JSON.stringify(c.mcpServers, null, 2))}</textarea>
      <small style="color:var(--dim)">${S.mcp.tools} MCP tools connected${Object.keys(S.mcp.failed).length ? ". Failed: " + esc(Object.entries(S.mcp.failed).map(([k, v]) => k + " (" + v + ")").join(", ")) : ""}</small></div>
    <div class="field"><label>Google sign in</label><button onclick="this.closest('.modal-bg').remove(); googleSetup()">${S.google.ready ? "Change Google sign in settings" : "Set up Google sign in"}</button></div>
    <div class="buttons"><button style="margin-right:auto" onclick="api('POST','/api/open-folder',{which:'data'})">Open data folder</button><button onclick="this.closest('.modal-bg').remove()">Cancel</button><button class="primary" id="seSave">Save</button></div>`, bg => {
    bg.querySelector("#seSave").onclick = async () => {
      let mcp; try { mcp = JSON.parse($("seMcp").value || "{}"); } catch (e) { alert("The MCP servers box isn't valid JSON."); return; }
      await setConfig({workspace: $("seWs").value, context: +$("seCtx").value, max_steps: +$("seSteps").value || 25,
        confirm_actions: $("seConfirm").checked, learn: $("seLearn").checked, mcpServers: mcp});
      bg.remove(); toast("Settings saved");
    };
  });
}

// ------------------------------------------------ accounts
const G_ICON = `<svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.7 13.2l7.9 6.2C12.5 13.6 17.8 9.5 24 9.5z"/><path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.8c4.3-4 6.9-9.9 6.9-16.7z"/><path fill="#FBBC05" d="M10.6 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.9-6.2C1 16.6 0 20.2 0 24s1 7.4 2.7 10.8l7.9-6.2z"/><path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.8c-2.1 1.4-4.8 2.3-8.5 2.3-6.2 0-11.5-4.1-13.4-9.9l-7.9 6.2C6.6 42.6 14.6 48 24 48z"/></svg>`;
function avatar(u) {
  return u.picture ? `<img class="avatar" src="${esc(u.picture)}" referrerpolicy="no-referrer" alt="">` : `<div class="avatar">${esc((u.name || u.email || "?")[0].toUpperCase())}</div>`;
}
function renderAccount() {
  const u = S.user;
  $("account").innerHTML = u
    ? `${avatar(u)}<div class="who">${esc(u.name)}<small>${esc(u.email)}</small></div><button onclick="signOut()">Sign out</button>`
    : `<div class="avatar" style="background:var(--side-hover)">&#128100;</div><div class="who">Guest<small>Not signed in</small></div><button onclick="signIn()">Sign in</button>`;
  let gate = $("gate");
  if (S.google.require && !u) {
    if (!gate) { gate = document.createElement("div"); gate.id = "gate"; document.body.appendChild(gate); }
    gate.innerHTML = `<div class="box"><h1>Desk Agent</h1><p style="color:var(--dim)">Sign in to open your chats.</p><button class="gbtn" onclick="signIn()">${G_ICON} Sign in with Google</button></div>`;
  } else if (gate) gate.remove();
}
async function signIn() {
  if (!S.google.ready) { googleSetup(); return; }
  try {
    const r = await api("POST", "/api/auth/start");
    window.open(r.url, "deskagent_signin", "width=520,height=680");
    setStatus("Finish signing in in the Google window...");
  } catch (e) { toast(e.message); }
}
async function signOut() {
  try { await api("POST", "/api/auth/signout"); } catch (e) { toast(e.message); return; }
  chat = null; project = null; await refresh(); renderChat(); toast("Signed out");
}
async function onAccountChange() {
  chat = null; project = null; closePanel(); setStatus("");
  await refresh();
  if (S.chats.length) await openChat(S.chats[0].id); else renderChat();
  if (S.user) toast(`Signed in as ${S.user.name}`);
}
function googleSetup() {
  const g = S.google;
  modal(`<h2>Sign in with Google</h2>
    <p style="color:var(--dim);margin-top:0">Signing in gives you your own chats, projects and memory on this laptop. Everything still stays on the laptop; Google only tells Desk Agent who you are.</p>
    <p>Google needs to know about your copy of Desk Agent first. This takes about five minutes, once:</p>
    <ol class="steps">
      <li>Go to <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">console.cloud.google.com/apis/credentials</a> and sign in.</li>
      <li>Make a project if it asks (any name, for example Desk Agent).</li>
      <li>Set up the <b>OAuth consent screen</b>: choose External, fill in the app name and your email, and add yourself as a test user.</li>
      <li>Back on Credentials, press <b>Create credentials</b>, <b>OAuth client ID</b>, and choose <b>Desktop app</b>.</li>
      <li>Copy the Client ID and Client secret into the boxes below.</li>
    </ol>
    <div class="field"><label>Client ID</label><input type="text" id="gId" value="${esc(g.client_id)}" placeholder="something.apps.googleusercontent.com"></div>
    <div class="field"><label>Client secret ${g.has_secret ? "(saved, leave blank to keep)" : ""}</label><input type="text" id="gSecret" placeholder="GOCSPX-..."></div>
    <div class="field checks"><label><input type="checkbox" id="gRequire" ${g.require ? "checked" : ""}> Ask for sign in every time Desk Agent opens</label></div>
    <div class="buttons"><button onclick="this.closest('.modal-bg').remove()">Cancel</button><button class="primary" id="gSave">Save and sign in</button></div>`, bg => {
    bg.querySelector("#gSave").onclick = async () => {
      const id = $("gId").value.trim(); if (!id) { alert("Paste the Client ID first."); return; }
      await api("POST", "/api/auth/setup", {client_id: id, client_secret: $("gSecret").value.trim(), require_sign_in: $("gRequire").checked});
      bg.remove(); await refresh(); signIn();
    };
  });
}

// ------------------------------------------------ start
(async () => {
  await refresh();
  connect();
  if (S.chats.length) await openChat(S.chats[0].id); else renderChat();
  if (!S.models.some(m => m.installed)) showModels();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
