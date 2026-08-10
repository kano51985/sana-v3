import json
import re

from sana.config import registry
from sana.services.entity_resolver import EntityResolution


class WebQueryPlanner:
    def __init__(self, backend_role: str = "perception"):
        self.backend_role = backend_role

    def build_heads(
        self,
        user_input: str,
        resolution: EntityResolution | None = None,
        perception_data: dict | None = None,
        current_time: str = "",
        max_heads: int = 3,
    ) -> list[str]:
        resolution = resolution or EntityResolution()
        try:
            queries = self._plan_with_llm(
                user_input=user_input,
                resolution=resolution,
                perception_data=perception_data or {},
                current_time=current_time,
                max_heads=max_heads,
            )
            if queries:
                return queries[:max_heads]
        except Exception:
            pass
        return self._fallback_heads(user_input, resolution, max_heads)

    def _plan_with_llm(
        self,
        user_input: str,
        resolution: EntityResolution,
        perception_data: dict,
        current_time: str,
        max_heads: int,
    ) -> list[str]:
        system = (
            "You are a web search query planner. Output ONLY valid JSON.\n"
            "Generate short, distinct search queries for the user request.\n"
            "Fields:\n"
            '- "queries": list of {"type": str, "query": str}\n'
            "Rules:\n"
            "- Each query must be under 60 characters.\n"
            "- Do not repeat the full user message as a query.\n"
            "- Prefer compact noun phrases with a time anchor when the question asks about current or recent facts.\n"
            "- Queries must be semantically different from each other.\n"
            'Example: {"queries": [{"type": "version", "query": "原神 当前版本 2026年8月"}, {"type": "new_character", "query": "原神 最近新角色 2026"}]}'
        )
        user_prompt = (
            f"User input: {user_input}\n"
            f"Canonical entity: {resolution.canonical or ''}\n"
            f"Entity aliases: {resolution.aliases}\n"
            f"Perception: {json.dumps(perception_data, ensure_ascii=False)}\n"
            f"Current time: {current_time}\n"
            f"Max query count: {max_heads}\n"
            "Return the query plan."
        )
        data = self._llm_json(system, user_prompt)
        if not data:
            return []
        queries = []
        for item in data.get("queries", []):
            text = str(item.get("query") or "").strip()
            if text and 1 <= len(text) <= 120 and text not in queries:
                queries.append(text)
        return queries

    def _fallback_heads(
        self,
        user_input: str,
        resolution: EntityResolution,
        max_heads: int,
    ) -> list[str]:
        raw = (resolution.raw or user_input or "").strip()[:120]
        canonical = (resolution.canonical or "").strip()
        heads = []
        if raw:
            heads.append(raw)
        if canonical and canonical not in heads:
            heads.append(f"{canonical} {user_input}".strip()[:120])
        elif raw:
            context = f"{raw} {user_input}".strip()[:120]
            if context not in heads:
                heads.append(context)
        return heads[:max(1, max_heads)]

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
