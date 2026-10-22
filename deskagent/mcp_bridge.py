"""Connects to MCP servers and exposes their tools to Claude.

The MCP client is async, the rest of the app is not, so all servers share one
event loop running in a background thread. Tool names are prefixed with the
server name (server__tool) so two servers can expose tools with the same name.
"""

import asyncio
import logging
import threading
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger(__name__)
SEP = "__"


class MCPBridge:
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

    def _call(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self.loop)
        self._ready.wait(timeout=60 * max(1, len(self.servers)) + 5)

    async def _main(self):
        # The transports use anyio cancel scopes, which must be entered and exited
        # in the same task, so this one task owns every connection for its lifetime.
        self._shutdown = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                self._stack = stack
                for name, spec in self.servers.items():
                    if spec.get("disabled"):
                        continue
                    try:
                        await asyncio.wait_for(self._connect(name, spec), timeout=60)
                    except Exception as e:
                        log.exception("MCP server %s failed", name)
                        self.failed[name] = str(e) or type(e).__name__
                self._ready.set()
                await self._shutdown.wait()
        finally:
            self._ready.set()

    async def _connect(self, name, spec):
        if "url" in spec:
            read, write = await self._stack.enter_async_context(streamable_http_client(spec["url"]))
        else:
            params = StdioServerParameters(
                command=spec["command"],
                args=spec.get("args", []),
                env=spec.get("env") or None,
            )
            # A windowed EXE has no stderr, so server logs go to a file instead.
            read, write = await self._stack.enter_async_context(stdio_client(params, errlog=self.errlog))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        self.sessions[name] = session
        for tool in result.tools:
            self.tools.append(
                {
                    "name": f"{name}{SEP}{tool.name}"[:64],
                    "description": (tool.description or tool.title or tool.name)[:1024],
                    "input_schema": tool.input_schema or {"type": "object", "properties": {}},
                    "eager_input_streaming": True,
                }
            )
        log.info("MCP server %s: %d tools", name, len(result.tools))

    def owns(self, tool_name: str) -> bool:
        return any(t["name"] == tool_name for t in self.tools)

    def schema(self, tool_name: str) -> dict:
        return next(t["input_schema"] for t in self.tools if t["name"] == tool_name)

    def call(self, tool_name: str, arguments: dict):
        """Returns (content_blocks, is_error) in Claude tool_result format."""
        server, _, tool = tool_name.partition(SEP)
        result = self._call(self.sessions[server].call_tool(tool, arguments), timeout=600)
        blocks = []
        for item in getattr(result, "content", None) or []:
            if item.type == "text":
                blocks.append({"type": "text", "text": item.text})
            elif item.type == "image":
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": item.mime_type, "data": item.data}}
                )
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
