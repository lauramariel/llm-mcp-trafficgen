from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import aiohttp

from .config import load_config
from .mcp_client import MCPClient
from .metrics import MetricsCollector
from .tools import ToolRegistry
from .worker import RateLimiter, RequestResult, llm_worker

logger = logging.getLogger(__name__)


async def summary_loop(metrics: MetricsCollector, interval: float, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        logger.info(metrics.summary_line())


async def drain_results(
    queue: "asyncio.Queue[RequestResult]", metrics: MetricsCollector, stop_event: asyncio.Event
) -> None:
    while True:
        try:
            result = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            if stop_event.is_set() and queue.empty():
                return
            continue
        metrics.record(result)


async def async_main(config_path: str) -> None:
    cfg = load_config(config_path)

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        tools = ToolRegistry()
        for mcp_cfg in cfg.mcp_servers:
            client = MCPClient(mcp_cfg, session)
            logger.info("Initializing MCP server '%s' at %s", mcp_cfg.name, mcp_cfg.base_url)
            await client.initialize()
            discovered = await client.list_tools()
            logger.info(
                "MCP server '%s' exposes %d tool(s): %s",
                mcp_cfg.name,
                len(discovered),
                [t["name"] for t in discovered],
            )
            tools.register_server(client)

        if not tools.has_tools():
            logger.warning("No MCP tools discovered; LLMs will be queried without tool access")

        metrics = MetricsCollector(cfg.output.log_file)
        stop_event = asyncio.Event()
        results_queue: "asyncio.Queue[RequestResult]" = asyncio.Queue()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # e.g. Windows

        counters = {"sent": 0}
        counters_lock = asyncio.Lock()

        worker_tasks: list[asyncio.Task] = []
        for llm in cfg.llms:
            rate = llm.requests_per_second or cfg.traffic.requests_per_second
            concurrency = llm.concurrency or cfg.traffic.concurrency
            limiter = RateLimiter(rate)
            logger.info(
                "Starting LLM '%s': rate=%.2f req/s, concurrency=%d", llm.name, rate, concurrency
            )
            for _ in range(concurrency):
                worker_tasks.append(
                    asyncio.create_task(
                        llm_worker(
                            session,
                            llm,
                            cfg.traffic,
                            cfg.prompts,
                            tools,
                            results_queue,
                            stop_event,
                            counters,
                            counters_lock,
                            limiter,
                        )
                    )
                )

        drain_task = asyncio.create_task(drain_results(results_queue, metrics, stop_event))
        summary_task = asyncio.create_task(
            summary_loop(metrics, cfg.output.summary_interval_seconds, stop_event)
        )

        try:
            if cfg.traffic.duration_seconds is not None:
                duration_task = asyncio.create_task(asyncio.sleep(cfg.traffic.duration_seconds))
                await asyncio.wait([*worker_tasks, duration_task], return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.gather(*worker_tasks)
        finally:
            stop_event.set()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            await drain_task
            summary_task.cancel()
            logger.info("Final summary: %s", metrics.summary_line())
            metrics.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continually query one or more LLMs with MCP tool access, as a traffic generator."
    )
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        asyncio.run(async_main(args.config))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
