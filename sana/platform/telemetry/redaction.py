"""Allowlist telemetry attributes and recursively redact diagnostic payloads."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any


_SAFE_STRING = re.compile(r"^[\w./:@+-]{1,128}$", re.UNICODE)
_SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|password|secret|api.?key|token|prompt|message|"
    r"body|content|snippet|quote|query|url)",
    re.I,
)
_DIAGNOSTIC_CONTAINER_KEYS = frozenset(
    {"metrics", "baseline", "candidate", "delta", "coverage", "usage"}
)
_DIAGNOSTIC_VALUE_KEYS = frozenset(
    {
        "mode",
        "status",
        "stop_reason",
        "answer_quality",
        "policy_version",
        "provider",
        "model",
        "role",
        "error_type",
        "error_code",
        "reason_codes",
        "latency_ms",
        "duration_ms",
        "count",
        "total",
        "open",
        "covered",
        "verified",
        "partial",
        "ratio",
        "cost_usd",
        "upgraded",
        "plan_revision",
        "covered_facts",
        "total_facts",
        "citation_traceability",
        "query_pollution_count",
    }
)


class TelemetryRedactor:
    ALLOWED_ATTRIBUTES = frozenset(
        {
            "search.mode",
            "search.policy_version",
            "search.stop_reason",
            "search.answer_quality",
            "search.upgraded",
            "search.plan_revision",
            "search.fact.total",
            "search.fact.open",
            "search.fact.covered",
            "search.fact.verified",
            "search.fact.partial",
            "search.usage.queries",
            "search.usage.providers",
            "search.usage.fetches",
            "search.usage.llm_calls",
            "search.usage.expansion_rounds",
            "workflow.step.type",
            "workflow.step.status",
            "provider.name",
            "provider.status",
            "model.role",
            "model.name",
            "model.input_tokens",
            "model.output_tokens",
            "http.status_code",
            "error.type",
            "error.code",
            "duration_ms",
            "cost.usd",
            "retry.count",
            "lease.expired",
            "queue.name",
        }
    )
    HASHED_ATTRIBUTES = frozenset(
        {
            "tenant.id_hash",
            "user.id_hash",
            "run.id_hash",
            "conversation.id_hash",
        }
    )

    def __init__(self, *, hash_salt: str = "sana-telemetry-v1") -> None:
        if not hash_salt:
            raise ValueError("Telemetry hash salt cannot be empty")
        self._salt = hash_salt.encode("utf-8")

    def hash_identifier(self, value: object) -> str:
        digest = hashlib.sha256(self._salt + str(value).encode("utf-8")).hexdigest()
        return f"sha256:{digest[:20]}"

    def attributes(self, values: Mapping[str, Any]) -> dict[str, bool | int | float | str]:
        sanitized: dict[str, bool | int | float | str] = {}
        for key, value in values.items():
            if key in self.HASHED_ATTRIBUTES:
                sanitized[key] = self.hash_identifier(value)
                continue
            if key not in self.ALLOWED_ATTRIBUTES or value is None:
                continue
            if isinstance(value, bool):
                sanitized[key] = value
            elif isinstance(value, int):
                sanitized[key] = value
            elif isinstance(value, float):
                if value == value and value not in {float("inf"), float("-inf")}:
                    sanitized[key] = value
            elif isinstance(value, str) and _SAFE_STRING.fullmatch(value):
                sanitized[key] = value
        return sanitized

    def diagnostic_payload(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 5:
            return "[TRUNCATED]"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = str(raw_key)[:128]
                if key in _DIAGNOSTIC_CONTAINER_KEYS | _DIAGNOSTIC_VALUE_KEYS:
                    result[key] = self.diagnostic_payload(item, depth=depth + 1)
                elif _SENSITIVE_KEY.search(key):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = "[REDACTED]"
            return result
        if isinstance(value, (list, tuple)):
            return [self.diagnostic_payload(item, depth=depth + 1) for item in value[:50]]
        if isinstance(value, str):
            if not _SAFE_STRING.fullmatch(value):
                return "[REDACTED]"
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return f"<{type(value).__name__}>"
