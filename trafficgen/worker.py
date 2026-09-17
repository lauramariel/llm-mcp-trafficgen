from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import aiohttp

from .config import LLMConfig, TrafficConfig
from .llm_client import LLMRequestError, run_conversation
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class RequestResult:
    llm_name: str
    ok: bool
    latency_seconds: float
    llm_calls: int = 0
    tool_calls: int = 0
    error: str | None = None
    content: str | None = None
    timestamp: float = field(default_factory=time.time)


class RateLimiter:
    """Simple token-bucket-style pacer: spaces out acquire() calls to roughly
    `rate_per_second`. A rate <= 0 means unlimited (no pacing)."""

    def __init__(self, rate_per_second: float) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._next_time = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_time - now
            self._next_time = max(self._next_time, now) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)


async def llm_worker(
    session: aiohttp.ClientSession,
    llm: LLMConfig,
    traffic: TrafficConfig,
    prompts: list[str],
    tools: ToolRegistry,
    results_queue: "asyncio.Queue[RequestResult]",
    stop_event: asyncio.Event,
    counters: dict[str, int],
    counters_lock: asyncio.Lock,
    limiter: RateLimiter,
) -> None:
    while not stop_event.is_set():
        if traffic.total_requests is not None:
            async with counters_lock:
                if counters["sent"] >= traffic.total_requests:
                    return
                counters["sent"] += 1

        await limiter.acquire()
        if stop_event.is_set():
            return

        prompt = random.choice(prompts)
        start = time.monotonic()
        try:
            outcome = await run_conversation(
                session, llm, prompt, tools, traffic.max_tool_call_rounds, traffic.request_timeout_seconds
            )
            latency = time.monotonic() - start
            result = RequestResult(
                llm_name=llm.name,
                ok=True,
                latency_seconds=latency,
                llm_calls=outcome["llm_calls"],
                tool_calls=outcome["tool_calls"],
                content=str(outcome.get("content"))[:200],
            )
        except (LLMRequestError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            latency = time.monotonic() - start
            result = RequestResult(llm_name=llm.name, ok=False, latency_seconds=latency, error=str(exc))
            logger.warning("Request failed for LLM '%s': %s", llm.name, exc)
        await results_queue.put(result)
