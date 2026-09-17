from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .config import LLMConfig
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


class LLMRequestError(RuntimeError):
    pass


async def run_conversation(
    session: aiohttp.ClientSession,
    llm: LLMConfig,
    prompt: str,
    tools: ToolRegistry,
    max_tool_rounds: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Runs one full conversation turn against an OpenAI-compatible chat/completions
    endpoint, including any tool-call round trips against MCP-backed tools.
    Returns the final assistant content plus call counts."""

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tool_call_count = 0
    llm_call_count = 0

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = llm.resolve_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(llm.extra_headers)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    for _round in range(max_tool_rounds + 1):
        body: dict[str, Any] = {
            "model": llm.model,
            "messages": messages,
            "stream": False,
        }
        if tools.has_tools():
            body["tools"] = tools.openai_tools

        async with session.post(
            llm.chat_url,
            json=body,
            headers=headers,
            timeout=timeout,
            ssl=None if llm.verify_ssl else False,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise LLMRequestError(f"LLM '{llm.name}' HTTP {resp.status}: {text[:500]}")
            data = json.loads(text)
        llm_call_count += 1

        choice = data["choices"][0]
        message = choice["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return {
                "content": message.get("content"),
                "llm_calls": llm_call_count,
                "tool_calls": tool_call_count,
            }

        for tool_call in tool_calls:
            tool_call_count += 1
            fn = tool_call["function"]
            name = fn["name"]
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            try:
                result = await tools.call(name, arguments)
                content = json.dumps(result)
            except Exception as exc:  # noqa: BLE001 -- fed back to the model, not raised
                content = json.dumps({"error": str(exc)})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id"),
                    "content": content,
                }
            )

    raise LLMRequestError(f"LLM '{llm.name}' exceeded max tool-call rounds ({max_tool_rounds})")
