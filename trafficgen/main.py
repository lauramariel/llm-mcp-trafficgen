from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import aiohttp

from .config import AppConfig, load_config
from .mcp_client import MCPClient
from .metrics import MetricsCollector
from .tools import ToolRegistry
from .worker import RateLimiter, llm_worker

logger = logging.getLogger(__name__)

# After a stop is requested, in-flight requests get this long to finish before
# they're cancelled. A second stop request (e.g. Ctrl+C again) cancels at once.
SHUTDOWN_GRACE_SECONDS = 5.0


async def summary_loop(metrics: MetricsCollector, interval: float, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            logger.info(metrics.summary_line())


async def _until(event: asyncio.Event, aw, timeout: float | None = None) -> bool:
    """Runs `aw` until it finishes, `event` is set, or `timeout` passes.
    Returns True if `aw` finished; otherwise cancels it and returns False."""
    main = asyncio.ensure_future(aw)
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait({main, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if main.done():
        return True
    main.cancel()
    await asyncio.gather(main, return_exceptions=True)
    return False


async def _connect_mcp_servers(cfg: AppConfig, session: aiohttp.ClientSession) -> ToolRegistry:
    tools = ToolRegistry()
    for mcp_cfg in cfg.mcp_servers:
        client = MCPClient(mcp_cfg, session, cfg.traffic.request_timeout_seconds)
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
    return tools


async def run_traffic(
    cfg: AppConfig,
    metrics: MetricsCollector,
    stop_event: asyncio.Event,
    force_event: asyncio.Event,
) -> None:
    """Generates traffic until `stop_event` is set or a configured limit is hit.
    In-flight requests then get SHUTDOWN_GRACE_SECONDS to finish (or until
    `force_event` is set) before being cancelled."""
    # Summarize configured LLMs and MCP servers before establishing connections
    logger.info("Configured LLMs: %s", [llm.name for llm in cfg.llms])
    logger.info("Configured MCP servers: %s", [mcp.name for mcp in cfg.mcp_servers])

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        connect = asyncio.ensure_future(_connect_mcp_servers(cfg, session))
        if not await _until(stop_event, connect):
            logger.info("Stopped during MCP setup")
            return
        tools = connect.result()

        counters = {"sent": 0}
        counters_lock = asyncio.Lock()
        worker_tasks: list[asyncio.Task] = []
        for llm in cfg.llms:
            rate = llm.requests_per_second or cfg.traffic.requests_per_second
            concurrency = llm.concurrency or cfg.traffic.concurrency
            limiter = RateLimiter(rate)
            logger.info("Starting LLM '%s': rate=%.2f req/s, concurrency=%d", llm.name, rate, concurrency)
            for _ in range(concurrency):
                worker_tasks.append(
                    asyncio.ensure_future(
                        llm_worker(
                            session,
                            llm,
                            cfg.traffic,
                            cfg.prompts,
                            tools,
                            metrics.record,
                            stop_event,
                            counters,
                            counters_lock,
                            limiter,
                        )
                    )
                )

        summary_task = asyncio.ensure_future(
            summary_loop(metrics, cfg.output.summary_interval_seconds, stop_event)
        )
        all_workers = asyncio.gather(*worker_tasks, return_exceptions=True)
        try:
            # Run until stopped, all workers finish (total_requests), or duration elapses.
            await _until(stop_event, asyncio.shield(all_workers), cfg.traffic.duration_seconds)
        finally:
            stop_event.set()
            if not all_workers.done():
                logger.info(
                    "Stopping: waiting up to %.0fs for in-flight requests (stop again to force)",
                    SHUTDOWN_GRACE_SECONDS,
                )
                if not await _until(force_event, asyncio.shield(all_workers), SHUTDOWN_GRACE_SECONDS):
                    logger.info("Cancelling in-flight requests")
                    all_workers.cancel()
            await asyncio.gather(all_workers, summary_task, return_exceptions=True)
            logger.info("Final summary: %s", metrics.summary_line())


async def cli_main(config_path: str) -> None:
    cfg = load_config(config_path)
    metrics = MetricsCollector(cfg.output.log_file)
    stop_event = asyncio.Event()
    force_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        if not stop_event.is_set():
            stop_event.set()
        elif not force_event.is_set():
            force_event.set()
            # Restore default handling so a third Ctrl+C always kills the process.
            for s in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(s)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except NotImplementedError:
            pass  # e.g. Windows: Ctrl+C raises KeyboardInterrupt instead

    try:
        await run_traffic(cfg, metrics, stop_event, force_event)
    finally:
        metrics.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continually query one or more LLMs with MCP tool access, as a traffic generator."
    )
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--ui", action="store_true", help="Start the web UI instead of running immediately")
    parser.add_argument("--host", default="127.0.0.1", help="Web UI bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Web UI port (default: 8080)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.ui:
        from .web import serve

        serve(args.config, args.host, args.port)
        return

    try:
        asyncio.run(cli_main(args.config))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
