from dataclasses import dataclass
import json
import re

from sana.config import registry
from sana.services.tool_registry import ToolRegistry


@dataclass
class ToolIntentResult:
    needs_tool: bool = False
    tool_name: str = ""
    query: str = ""
    confidence: float = 0.0
    reason: str = ""


class ToolIntentDetector:
    CONFIDENCE_THRESHOLD = 0.7

    def __init__(self, backend_role: str = "perception", tool_registry: ToolRegistry | None = None):
        self.backend_role = backend_role
        self.tool_registry = tool_registry or ToolRegistry()

    def detect(
        self,
        user_input: str,
        perception_data: dict | None = None,
        raw_response: str = "",
    ) -> ToolIntentResult:
        perception_data = perception_data or {}
        system = (
            "You are a tool intent classifier. Output ONLY valid JSON.\n"
            "Decide whether the user request needs external, real-time, factual, or up-to-date "
            "information that the assistant cannot reliably answer from memory or conversation alone.\n"
            "Available tools:\n"
            f"{self.tool_registry.descriptions()}\n"
            "Fields:\n"
            "- needs_tool: bool\n"
            "- tool_name: str, use 'web' for external information; leave '' when not needed\n"
            "- query: str, a concrete search query when needs_tool is true\n"
            "- confidence: float 0.0-1.0\n"
            "- reason: str\n"
            "Only set needs_tool=true when there is a real need; do not trigger for ordinary chat.\n"
            'Example: {"needs_tool": true, "tool_name": "web", "query": "原神 当前版本", "confidence": 0.9, "reason": "需要实时版本信息"}'
        )
        user_prompt = (
            f"User input: {user_input}\n"
            f"Perception: {json.dumps(perception_data, ensure_ascii=False)}\n"
            f"Assistant raw response so far: {raw_response[:500]}\n"
            "Return whether the assistant should call a tool."
        )
        result = self._llm_json(system, user_prompt)
        if not result:
            return ToolIntentResult()
        try:
            confidence = float(result.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not result.get("needs_tool") or confidence < self.CONFIDENCE_THRESHOLD:
            return ToolIntentResult(confidence=confidence)

        tool_name = str(result.get("tool_name") or "").strip()
        if self.tool_registry.get(tool_name) is None:
            return ToolIntentResult(confidence=confidence)
        query = str(result.get("query") or "").strip() or (user_input or "").strip()[:120]
        return ToolIntentResult(
            needs_tool=True,
            tool_name=tool_name,
            query=query[:120],
            confidence=confidence,
            reason=str(result.get("reason") or ""),
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
