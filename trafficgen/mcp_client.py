from __future__ import annotations

import itertools
import logging
from typing import Any

import aiohttp

from .config import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPError(RuntimeError):
    pass


class MCPClient:
    """Minimal JSON-RPC 2.0 client for an MCP server exposed as a single HTTP POST
    endpoint (request/response per call -- no persistent SSE stream, matching the
    gateway shape shown in mcp-code-sample.txt).

    If the gateway returns an `Mcp-Session-Id` response header (as the standard MCP
    "Streamable HTTP" transport does), it is captured and echoed back on subsequent
    calls. If the gateway doesn't use sessions at all, this is simply a no-op.
    """

    def __init__(self, cfg: MCPServerConfig, session: aiohttp.ClientSession):
        self.cfg = cfg
        self._session = session
        self._id_counter = itertools.count(1)
        self._session_id: str | None = None
        self.tools: list[dict[str, Any]] = []

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        api_key = self.cfg.resolve_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            logger.info("API key provided for MCP authentication")
        else:
            logger.info("No API key passed")
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.update(self.cfg.extra_headers)
        return headers

    async def _call(self, method: str, params: dict[str, Any] | None = None, notification: bool = False) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            body["id"] = next(self._id_counter)
        async with self._session.post(
            self.cfg.base_url,
            json=body,
            headers=self._headers(),
            ssl=None if self.cfg.verify_ssl else False,
        ) as resp:
            session_id = resp.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id
            if notification:
                resp.raise_for_status()
                return None
            # For standard calls, ensure success status and parse JSON safely.
            resp.raise_for_status()
            # Attempt to parse JSON; if the body is empty or not JSON, treat as empty dict.
            try:
                data = await resp.json(content_type=None)
            except Exception as exc:
                # aiohttp raises ContentTypeError for non‑JSON, and json.JSONDecodeError for empty body.
                from json import JSONDecodeError
                if isinstance(exc, JSONDecodeError) or getattr(exc, "status", None) == 204:
                    data = {}
                else:
                    raise

        if isinstance(data, dict) and data.get("error"):
            raise MCPError(f"MCP server '{self.cfg.name}' error calling {method}: {data['error']}")
        return (data or {}).get("result") if isinstance(data, dict) else data

    async def initialize(self) -> None:
        await self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "llm-mcp-trafficgen", "version": "0.1"},
            },
        )
        try:
            await self._call("notifications/initialized", notification=True)
        except Exception as exc:  # noqa: BLE001 -- optional per MCP spec, gateway may not need it
            logger.debug("MCP server '%s' did not accept notifications/initialized: %s", self.cfg.name, exc)

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._call("tools/list")
        self.tools = (result or {}).get("tools", [])
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._call("tools/call", {"name": name, "arguments": arguments})
