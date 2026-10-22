"""The agent loop: send the conversation to Claude, run the tools it asks for, repeat."""

import json
import logging
import platform
import threading
from datetime import date

import anthropic
import jsonschema
import pyautogui

from .computer import Computer
from .mcp_bridge import MCPBridge

log = logging.getLogger(__name__)

BETAS = ["server-side-fallback-2026-07-01", "thinking-display-updates-2026-08-18"]

SYSTEM = f"""You are Desk Agent, an assistant running on the user's own {platform.system()} {platform.release()} laptop.
Today is {date.today().isoformat()}.

You can see and control the screen with the computer tools, search the web, and use any MCP tools listed.
Prefer an MCP tool over clicking through an app when one can do the job; it is faster and more reliable.
Take a screenshot before acting on the screen, and again after anything that changes it, so you work from what is actually there.
Ask the user before anything that spends money, sends a message on their behalf, deletes files, or changes account or security settings.
If a tool result says the user declined an action, stop and ask what they want instead of trying another route.
Keep replies short and plain. When the job is done, say what you did in a sentence or two."""


class Agent:
    """One conversation. Not thread safe: run() is called from one worker thread at a time."""

    def __init__(self, cfg: dict, ui, errlog):
        self.cfg = cfg
        self.ui = ui  # gui callbacks: say(), progress(), tool(), confirm()
        self.client = anthropic.Anthropic(api_key=cfg["api_key"] or None, max_retries=4)
        self.computer = Computer(cfg["screenshot_max_width"]) if cfg["computer_use"] else None
        self.mcp = MCPBridge(cfg.get("mcpServers", {}), errlog)
        self.messages: list = []
        self.stop_event = threading.Event()

    def start(self):
        self.mcp.start()
        return self.mcp.tools, self.mcp.failed

    def close(self):
        self.mcp.close()

    def reset(self):
        self.messages = []

    def _tools(self) -> list:
        tools = []
        if self.computer:
            tools.append({"type": "computer_toolset_20260801"})
        if self.cfg["web_search"]:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 5})
        tools.extend(self.mcp.tools)
        return tools

    def _request(self):
        # Python raises ValueError when a streamed tool input is not valid JSON. Retry a couple of
        # times; API errors (auth, rate limit) are left to propagate.
        for attempt in range(3):
            try:
                with self.client.beta.messages.stream(
                    model=self.cfg["model"],
                    max_tokens=64000,
                    system=SYSTEM,
                    tools=self._tools(),
                    messages=self.messages,
                    thinking={"type": "adaptive", "display": "updates"},
                    output_config={"effort": self.cfg["effort"]},
                    fallbacks="default",
                    betas=BETAS,
                ) as stream:
                    return stream.get_final_message()
            except ValueError:
                if attempt == 2:
                    raise
                log.warning("Unparseable tool input, retrying")

    def run(self, text: str):
        self.stop_event.clear()
        if self.messages and self.messages[-1]["role"] == "user":
            # The last turn ended without a reply (refusal), so add to that user message.
            last = self.messages[-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].append({"type": "text", "text": text})
        else:
            self.messages.append({"role": "user", "content": text})
        try:
            self._loop()
        except pyautogui.FailSafeException:
            self._abandon_pending()
            self.ui.say("Stopped: the mouse hit a screen corner (emergency stop).", kind="error")
        except anthropic.AuthenticationError:
            self.ui.say("Your API key was rejected. Check it in Settings.", kind="error")
        except anthropic.APIError as e:
            self._abandon_pending()
            self.ui.say(f"API error: {e}", kind="error")

    def _abandon_pending(self):
        # A tool_use with no tool_result would make every later request fail, so answer it.
        if self.messages and self.messages[-1]["role"] == "assistant":
            pending = [b for b in self.messages[-1]["content"] if b.type == "tool_use"]
            if pending:
                self.messages.append({"role": "user", "content": [self._error(b, "Stopped by the user.") for b in pending]})

    def _loop(self):
        for _ in range(self.cfg["max_steps"]):
            if self.stop_event.is_set():
                self.ui.say("Stopped.", kind="error")
                return

            resp = self._request()
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
                results = [self._error(b, "Output was cut off before this call finished. Try again with less.") for b in calls]
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
        harmless = {"screenshot", "zoom", "cursor_position", "wait", "mouse_move", "scroll"}
        return not (call.toolset_name == "computer" and call.name in harmless)

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

    def _mcp(self, call):
        self.ui.tool(self._describe(call))
        try:
            jsonschema.validate(call.input, self.mcp.schema(call.name))
        except jsonschema.ValidationError as e:
            return self._error(call, json.dumps({"INVALID_JSON": json.dumps(call.input), "problem": e.message}))
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
