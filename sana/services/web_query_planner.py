import json
import re

from sana.config import registry
from sana.models.search_context import EntityContext, SearchIntent
from sana.services.entity_resolver import EntityResolution
from sana.services.llm_query_refiner import LLMQueryRefiner


_CONTEXT_STOPWORDS = {
    "当前", "版本", "更新", "2026", "年", "月", "这个", "那个",
    "什么", "怎么", "改成", "什么样", "啦", "听说", "很大",
    "问题", "推荐", "最强", "情况", "了吗", "吗", "了", "的",
}


class WebQueryPlanner:
    def __init__(
        self,
        backend_role: str = "perception",
        query_refiner: LLMQueryRefiner | None = None,
    ):
        self.backend_role = backend_role
        self.query_refiner = query_refiner

    def build_heads(
        self,
        user_input: str,
        resolution: EntityResolution | None = None,
        perception_data: dict | None = None,
        current_time: str = "",
        max_heads: int = 3,
        entity_context: EntityContext | None = None,
        search_intent: SearchIntent | None = None,
    ) -> list[str]:
        resolution = resolution or EntityResolution()
        try:
            queries = self._plan_with_llm(
                user_input=user_input,
                resolution=resolution,
                perception_data=perception_data or {},
                current_time=current_time,
                max_heads=max_heads,
                entity_context=entity_context,
                search_intent=search_intent,
            )
            if queries:
                cleaned = [_rule_clean_query(query) for query in queries[:max_heads]]
                if self.query_refiner is not None:
                    cleaned = [
                        self.query_refiner.refine(
                            query,
                            user_input,
                            entity_context,
                            search_intent,
                        )
                        for query in cleaned
                    ]
                return [
                    self._augment_query(_rule_clean_query(query), entity_context)
                    for query in cleaned
                ]
        except Exception:
            pass
        return self._fallback_heads(
            user_input,
            resolution,
            max_heads,
            entity_context,
        )

    def _plan_with_llm(
        self,
        user_input: str,
        resolution: EntityResolution,
        perception_data: dict,
        current_time: str,
        max_heads: int,
        entity_context: EntityContext | None,
        search_intent: SearchIntent | None,
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
            f"Entity context: {entity_context.to_dict() if entity_context else {}}\n"
            f"Search intent: {search_intent.to_dict() if search_intent else {}}\n"
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
        entity_context: EntityContext | None = None,
    ) -> list[str]:
        raw = (resolution.raw or user_input or "").strip()[:120]
        canonical = (resolution.canonical or "").strip()
        heads = []
        if raw:
            heads.append(raw)
        if canonical and canonical not in heads:
            extra = " ".join((entity_context.context_terms or [])[:3])
            heads.append(f"{canonical} {extra} {user_input}".strip()[:120])
        elif raw:
            context = f"{raw} {user_input}".strip()[:120]
            if context not in heads:
                heads.append(context)
        return heads[:max(1, max_heads)]

    @staticmethod
    def _augment_query(query: str, entity_context: EntityContext | None) -> str:
        if not entity_context or not query:
            return query
        alias_values = {str(alias).lower() for alias in entity_context.aliases}
        extra = [
            term
            for term in (entity_context.context_terms or [])
            if term.lower() not in alias_values and term not in _CONTEXT_STOPWORDS
        ] or list(entity_context.context_terms or [])
        if not extra:
            return query
        if any(term in query.lower() for term in extra[:3]):
            return query
        return f"{query} {' '.join(extra[:2])}".strip()

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


def _rule_clean_query(query: str) -> str:
    text = str(query or "")
    for filler in ("sana", "我听说", "你知道", "具体", "什么吗", "吗？", "？", "！", "~"):
        text = text.replace(filler, " ")
    tokens = []
    for token in re.split(r"[\s,，。；;、/]+", text):
        token = token.strip()
        if token and token not in tokens:
            tokens.append(token)
    return " ".join(tokens)[:60]
