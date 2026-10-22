"""Settings live in a JSON file next to the user's other app data.

On Windows that is %APPDATA%\\DeskAgent\\config.json. The file is created with
defaults on first run so it can be edited by hand.
"""

import json
import os
from pathlib import Path

DEFAULTS = {
    "api_key": "",
    "model": "claude-opus-5-5",
    "effort": "medium",
    "computer_use": True,
    "web_search": True,
    "confirm_actions": True,
    "max_steps": 60,
    "screenshot_max_width": 1280,
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


def load() -> dict:
    path = config_path()
    if not path.exists():
        save(DEFAULTS)
        return dict(DEFAULTS)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {**DEFAULTS, **data}


def save(cfg: dict) -> None:
    with config_path().open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
