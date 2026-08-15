"""Allowlist telemetry attributes and recursively redact diagnostic payloads."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


_SAFE_STRING = re.compile(r"^[\w./:@+-]{1,128}$", re.UNICODE)
_SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|password|secret|api.?key|token|prompt|message|"
    r"body|content|snippet|quote|query|url|reasoning|raw.?provider)",
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

_REPORT_FORBIDDEN_KEYS = frozenset(
    {
        "access_token",
        "answer",
        "api_key",
        "authorization",
        "content",
        "cookie",
        "message",
        "password",
        "prompt",
        "provider_response_id",
        "provider_token",
        "query",
        "query_text",
        "quote",
        "raw_provider",
        "reasoning",
        "rendered_url",
        "reviewed_at",
        "reviewer_user_id",
        "secret",
        "snippet",
        "token",
        "url",
    }
)
_REPORT_SECRET_VALUE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:sk|ds|api)[_-][A-Za-z0-9._-]{12,}|"
    r"https?://[^\s/:]+:[^\s/@]+@|"
    r"[?&](?:access_token|api_key|key|password|secret|token)=[^\s&#]+|"
    r"\b(?:password|secret|token)\s*[=:]\s*[^\s,;]{6,})",
    re.I,
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
            "search.usage.prompt_tokens",
            "search.usage.completion_tokens",
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


class ReportPrivacyGuard:
    """Fail closed when a generated report contains non-allowlisted content."""

    @classmethod
    def validate_payload(cls, value: Any, *, path: str = "$") -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key)
                if key.casefold() in _REPORT_FORBIDDEN_KEYS:
                    raise ValueError(f"Report contains forbidden field at {path}.{key}")
                cls.validate_payload(item, path=f"{path}.{key}")
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                cls.validate_payload(item, path=f"{path}[{index}]")
            return
        if isinstance(value, str) and _REPORT_SECRET_VALUE.search(value):
            raise ValueError(f"Report contains a credential-like value at {path}")
        if isinstance(value, float) and (
            value != value or value in {float("inf"), float("-inf")}
        ):
            raise ValueError(f"Report contains a non-finite number at {path}")

    @classmethod
    def validate_json_bytes(cls, payload: bytes) -> None:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Report is not canonical UTF-8 JSON") from error
        cls.validate_payload(decoded)

    @staticmethod
    def validate_text_bytes(payload: bytes) -> None:
        try:
            value = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Report text is not UTF-8") from error
        if _REPORT_SECRET_VALUE.search(value):
            raise ValueError("Report text contains a credential-like value")
