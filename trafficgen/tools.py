from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mcp_client import MCPClient


@dataclass
class ToolBinding:
    openai_name: str
    server: MCPClient
    mcp_name: str


class ToolRegistry:
    """Aggregates tools discovered across one or more MCP servers into a single
    OpenAI-style `tools` list, and routes tool-call requests from the LLM back to
    the correct MCP server."""

    def __init__(self) -> None:
        self._bindings: dict[str, ToolBinding] = {}
        self.openai_tools: list[dict[str, Any]] = []

    def register_server(self, client: MCPClient) -> None:
        seen_names = set(self._bindings)
        for tool in client.tools:
            mcp_name = tool["name"]
            openai_name = mcp_name
            if openai_name in seen_names:
                # Avoid collisions when multiple MCP servers expose a tool with the same name.
                openai_name = f"{client.cfg.name}__{mcp_name}"
            seen_names.add(openai_name)
            self._bindings[openai_name] = ToolBinding(openai_name, client, mcp_name)
            self.openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": openai_name,
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )

    async def call(self, openai_name: str, arguments: dict[str, Any]) -> Any:
        binding = self._bindings.get(openai_name)
        if binding is None:
            raise KeyError(f"Unknown tool requested by model: {openai_name}")
        return await binding.server.call_tool(binding.mcp_name, arguments)

    def has_tools(self) -> bool:
        return bool(self.openai_tools)
