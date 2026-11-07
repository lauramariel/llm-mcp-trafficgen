"""Local web UI: edit the YAML config through a form, and start/stop runs."""
from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web

from .config import default_config_dict, parse_config, read_config_dict
from .main import run_traffic
from .metrics import MetricsCollector

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PASSWORD_ENV = "TRAFFICGEN_UI_PASSWORD"


def password_middleware(password: str):
    """HTTP Basic auth with a single shared password; the username is ignored.
    The browser shows its own login prompt and remembers it for the session."""
    expected = password.encode("utf-8")

    @web.middleware
    async def check(request: web.Request, handler):
        scheme, _, encoded = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                _user, _, given = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError):
                given = ""
            if hmac.compare_digest(given.encode("utf-8"), expected):
                return await handler(request)
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="trafficgen", charset="UTF-8"'})

    return check


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines so the UI can show them."""

    def __init__(self, maxlen: int = 300) -> None:
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class RunManager:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.task: asyncio.Task | None = None
        self.stop_event: asyncio.Event | None = None
        self.force_event: asyncio.Event | None = None
        self.metrics: MetricsCollector | None = None
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("A run is already in progress")
        cfg = parse_config(read_config_dict(self.config_path), Path(self.config_path).parent)
        self.metrics = MetricsCollector(cfg.output.log_file)
        self.stop_event = asyncio.Event()
        self.force_event = asyncio.Event()
        self.started_at, self.ended_at, self.error = time.time(), None, None
        self.task = asyncio.ensure_future(self._run(cfg, self.metrics))

    async def _run(self, cfg, metrics: MetricsCollector) -> None:
        try:
            await run_traffic(cfg, metrics, self.stop_event, self.force_event)
        except Exception as exc:  # noqa: BLE001 -- surfaced in the UI
            self.error = str(exc) or type(exc).__name__
            logger.exception("Run failed")
        finally:
            metrics.close()
            self.ended_at = time.time()

    def stop(self) -> None:
        if not self.running:
            return
        if self.stop_event.is_set():
            self.force_event.set()
        else:
            self.stop_event.set()

    async def shutdown(self) -> None:
        if self.running:
            self.stop_event.set()
            self.force_event.set()
            await asyncio.gather(self.task, return_exceptions=True)

    def status(self) -> dict[str, Any]:
        if self.running:
            state = "stopping" if self.stop_event.is_set() else "running"
        elif self.task is None:
            state = "idle"
        else:
            state = "error" if self.error else "finished"
        return {
            "state": state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "stats": self.metrics.snapshot() if self.metrics else {},
        }


def _env_status(raw: dict[str, Any]) -> dict[str, bool]:
    names = {
        item.get("api_key_env")
        for section in ("llms", "mcp_servers")
        for item in (raw.get(section) or [])
        if isinstance(item, dict)
    }
    return {n: n in os.environ for n in names if n}


def _clean(value: Any) -> Any:
    """Drops empty/None values so the saved YAML only contains what was set."""
    if isinstance(value, dict):
        out = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def build_app(config_path: str, password: str | None = None) -> web.Application:
    runner = RunManager(config_path)
    log_buffer = LogBuffer()
    logging.getLogger().addHandler(log_buffer)
    app = web.Application(middlewares=[password_middleware(password)] if password else [])

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def get_config(_request: web.Request) -> web.Response:
        exists = os.path.exists(config_path)
        raw = read_config_dict(config_path) if exists else default_config_dict()
        return web.json_response(
            {"path": os.path.abspath(config_path), "exists": exists, "config": raw, "env": _env_status(raw)}
        )

    async def put_config(request: web.Request) -> web.Response:
        body = await request.json()
        raw = _clean(body)
        # A null log_file means "no log file", which differs from leaving it out (the default file).
        if "log_file" in (body.get("output") or {}) and body["output"]["log_file"] is None:
            raw.setdefault("output", {})["log_file"] = None
        try:
            parse_config(raw, Path(config_path).parent)
        except Exception as exc:  # noqa: BLE001 -- validation message for the UI
            return web.json_response({"error": str(exc)}, status=400)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False, allow_unicode=True)
        logger.info("Saved config to %s", config_path)
        return web.json_response({"ok": True, "env": _env_status(raw)})

    async def start(_request: web.Request) -> web.Response:
        try:
            runner.start()
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(runner.status())

    async def stop(_request: web.Request) -> web.Response:
        runner.stop()
        return web.json_response(runner.status())

    async def status(_request: web.Request) -> web.Response:
        return web.json_response({**runner.status(), "logs": list(log_buffer.lines)})

    async def on_shutdown(_app: web.Application) -> None:
        await runner.shutdown()

    app.router.add_get("/", index)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_post("/api/start", start)
    app.router.add_post("/api/stop", stop)
    app.router.add_get("/api/status", status)
    app.on_shutdown.append(on_shutdown)
    return app


def serve(config_path: str, host: str, port: int) -> None:
    # Keep aiohttp's per-request access log out of the console and the UI log view.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    password = os.environ.get(PASSWORD_ENV) or None
    print(f"trafficgen UI: http://{host}:{port}  (editing {os.path.abspath(config_path)})")
    if password:
        print(f"Password protection is on ({PASSWORD_ENV}).")
    else:
        logger.warning(
            "No %s set: anyone who can reach this UI can edit the config and start traffic.", PASSWORD_ENV
        )
    web.run_app(build_app(config_path, password), host=host, port=port, print=None)
