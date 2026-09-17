from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .worker import RequestResult


class MetricsCollector:
    def __init__(self, log_file: str | None) -> None:
        self.log_file = log_file
        self._fh = open(log_file, "a", encoding="utf-8") if log_file else None
        self.per_llm: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "errors": 0, "latencies": [], "tool_calls": 0}
        )

    def record(self, result: RequestResult) -> None:
        stats = self.per_llm[result.llm_name]
        stats["count"] += 1
        if not result.ok:
            stats["errors"] += 1
        stats["latencies"].append(result.latency_seconds)
        stats["tool_calls"] += result.tool_calls
        if self._fh:
            self._fh.write(json.dumps(asdict(result)) + "\n")
            self._fh.flush()

    def summary_line(self) -> str:
        if not self.per_llm:
            return "no requests yet"
        parts = []
        for name, stats in self.per_llm.items():
            latencies = sorted(stats["latencies"])
            n = len(latencies)
            p50 = latencies[int(n * 0.5)] if n else 0.0
            p95 = latencies[min(int(n * 0.95), n - 1)] if n else 0.0
            parts.append(
                f"[{name}] sent={stats['count']} errors={stats['errors']} "
                f"tool_calls={stats['tool_calls']} p50={p50:.2f}s p95={p95:.2f}s"
            )
        return " | ".join(parts)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
