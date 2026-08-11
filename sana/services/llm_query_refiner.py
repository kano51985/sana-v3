import hashlib
import json
import queue
import re
import threading

from sana.config import registry
from sana.models.search_context import EntityContext, SearchIntent


class LLMQueryRefiner:
    def __init__(self, backend_role: str = "perception", timeout: float = 5.0):
        self.backend_role = backend_role
        self.timeout = timeout
        self._cache: dict[str, str] = {}

    def refine(
        self,
        query: str,
        user_input: str,
        entity_context: EntityContext | None = None,
        search_intent: SearchIntent | None = None,
    ) -> str:
        key = self._cache_key(query, user_input, entity_context, search_intent)
        if key in self._cache:
            return self._cache[key]
        data = self._llm_json(query, user_input, entity_context, search_intent)
        refined = self._validate(data, query)
        self._cache[key] = refined
        return refined

    def _llm_json(
        self,
        query: str,
        user_input: str,
        entity_context: EntityContext | None,
        search_intent: SearchIntent | None,
    ) -> dict | None:
        system = (
            "You are a search query refiner. Output ONLY valid JSON.\n"
            "Fields:\n"
            '- "keep": bool\n'
            '- "query": str\n'
            '- "reason": str\n'
            "Rules:\n"
            "- query must be compact and under 60 characters.\n"
            "- Remove conversational filler: sana, 我听说, 你知道, 具体, 什么吗, ~.\n"
            "- Remove duplicate canonical/context terms.\n"
            "- Keep meaningful entity, context, and fact terms.\n"
        )
        user_prompt = (
            f"Original query: {query}\n"
            f"User input: {user_input}\n"
            f"Entity context: {entity_context.to_dict() if entity_context else {}}\n"
            f"Search intent: {search_intent.to_dict() if search_intent else {}}\n"
            "Return the refined query."
        )
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def run():
            try:
                backend = registry.get_backend(self.backend_role)
                cfg = registry.get_config(self.backend_role)
                resp = backend.chat(
                    cfg.model_id,
                    [{"role": "user", "content": user_prompt}],
                    system_prompt=system,
                    timeout=self.timeout,
                )
                result_queue.put(("ok", resp))
            except Exception as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            status, value = result_queue.get(timeout=self.timeout)
        except queue.Empty:
            return None
        if status == "error":
            return None
        match = re.search(r"\{.*\}", value.content or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _validate(data: dict | None, original: str) -> str:
        if not isinstance(data, dict):
            return original
        query = str(data.get("query") or "").strip()
        if not query or not 1 <= len(query) <= 80:
            return original
        return query

    @staticmethod
    def _cache_key(
        query: str,
        user_input: str,
        entity_context: EntityContext | None,
        search_intent: SearchIntent | None,
    ) -> str:
        raw = json.dumps(
            {
                "q": query,
                "u": user_input,
                "e": entity_context.to_dict() if entity_context else {},
                "i": search_intent.to_dict() if search_intent else {},
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
