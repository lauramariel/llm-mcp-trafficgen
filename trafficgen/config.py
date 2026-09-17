from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LLMConfig:
    name: str
    base_url: str
    model: str
    chat_path: str = "/chat/completions"
    api_key: str | None = None
    api_key_env: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = False
    requests_per_second: float | None = None
    concurrency: int | None = None

    def resolve_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + self.chat_path


@dataclass
class MCPServerConfig:
    name: str
    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = False

    def resolve_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None


@dataclass
class TrafficConfig:
    duration_seconds: float | None = None
    total_requests: int | None = None
    requests_per_second: float = 1.0
    concurrency: int = 1
    max_tool_call_rounds: int = 5
    request_timeout_seconds: float = 60.0


@dataclass
class PromptsConfig:
    file: str | None = None
    list: list[str] = field(default_factory=list)
    mode: str = "random"  # or "round_robin"

    def load(self, base_dir: Path) -> list[str]:
        prompts: list[str] = list(self.list)
        if self.file:
            path = Path(self.file)
            if not path.is_absolute():
                path = base_dir / path
            with open(path, "r", encoding="utf-8") as f:
                prompts.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))
        if not prompts:
            raise ValueError("No prompts configured: provide prompts.list and/or prompts.file")
        return prompts


@dataclass
class OutputConfig:
    log_file: str | None = "trafficgen_log.jsonl"
    summary_interval_seconds: float = 10.0


@dataclass
class AppConfig:
    llms: list[LLMConfig]
    mcp_servers: list[MCPServerConfig]
    traffic: TrafficConfig
    prompts: list[str]
    output: OutputConfig


def _dc_from_dict(cls, data: dict[str, Any]):
    valid_fields = set(cls.__dataclass_fields__)
    unknown = set(data) - valid_fields
    if unknown:
        raise ValueError(f"Unknown field(s) for {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str) -> AppConfig:
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    llms_raw = raw.get("llms") or []
    if not llms_raw:
        raise ValueError("Config must define at least one entry under 'llms'")
    llms = [_dc_from_dict(LLMConfig, item) for item in llms_raw]

    mcp_raw = raw.get("mcp_servers") or []
    mcp_servers = [_dc_from_dict(MCPServerConfig, item) for item in mcp_raw]

    traffic = _dc_from_dict(TrafficConfig, raw.get("traffic") or {})

    prompts_cfg = _dc_from_dict(PromptsConfig, raw.get("prompts") or {})
    prompts = prompts_cfg.load(config_path.parent)

    output = _dc_from_dict(OutputConfig, raw.get("output") or {})

    return AppConfig(llms=llms, mcp_servers=mcp_servers, traffic=traffic, prompts=prompts, output=output)
