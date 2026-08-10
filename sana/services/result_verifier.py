from dataclasses import dataclass, field
import json
import re

from sana.config import registry


@dataclass
class ResultVerification:
    has_answer: bool = False
    confidence: float = 0.0
    missing_facts: list[str] = field(default_factory=list)
    suggested_retry_query: str = ""
    retry_allowed: bool = True

    def should_retry(self) -> bool:
        return self.retry_allowed and (not self.has_answer or self.confidence < 0.6)


class ToolResultVerifier:
    THRESHOLD = 0.6

    def __init__(self, backend_role: str = "perception"):
        self.backend_role = backend_role

    def verify(
        self,
        user_input: str,
        queries: list[str],
        results: list[dict],
        current_time: str = "",
    ) -> ResultVerification:
        if not results:
            return ResultVerification(has_answer=False, confidence=0.0)

        result_lines = []
        for item in results[:10]:
            result_lines.append(
                f"- {item.get('title', '')} | {item.get('snippet', '')} | {item.get('url', '')}"
            )
        system = (
            "You are a search result verifier. Output ONLY valid JSON.\n"
            "Decide whether the provided search results can answer the user's factual question.\n"
            "Fields:\n"
            "- has_answer: bool\n"
            "- confidence: float 0.0-1.0\n"
            "- missing_facts: list[str]\n"
            "- suggested_retry_query: str or ''\n"
            "- retry_allowed: bool\n"
            "Set has_answer=false when the results are stale, insufficient, or do not contain the facts asked for.\n"
            'Example: {"has_answer": false, "confidence": 0.3, "missing_facts": ["当前版本"], "suggested_retry_query": "原神 7.0 更新时间", "retry_allowed": true}'
        )
        user_prompt = (
            f"User input: {user_input}\n"
            f"Queries: {queries}\n"
            f"Current time: {current_time}\n"
            f"Search results:\n{chr(10).join(result_lines)}\n"
            "Verify whether the results are sufficient."
        )
        data = self._llm_json(system, user_prompt)
        if not data:
            return ResultVerification(retry_allowed=False)

        try:
            confidence = float(data.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        missing = [str(x) for x in data.get("missing_facts", []) if str(x).strip()]
        return ResultVerification(
            has_answer=bool(data.get("has_answer")),
            confidence=confidence,
            missing_facts=missing,
            suggested_retry_query=str(data.get("suggested_retry_query") or "").strip(),
            retry_allowed=bool(data.get("retry_allowed", True)),
        )

    def _llm_json(self, system: str, user_prompt: str) -> dict | None:
        try:
            backend = registry.get_backend(self.backend_role)
            cfg = registry.get_config(self.backend_role)
            resp = backend.chat(
                cfg.model_id,
                [{"role": "user", "content": user_prompt}],
                system_prompt=system,
                timeout=10,
            )
            match = re.search(r"\{.*\}", resp.content or "", re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except Exception:
            return None
