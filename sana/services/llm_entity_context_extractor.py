import hashlib
import json
import re

from sana.config import registry
from sana.services.entity_resolver import EntityResolution


_ALLOWED_DOMAINS = {"game", "product", "person", "general"}
_ALLOWED_KINDS = {"game", "character", "product", "person", "general"}


class LLMEntityContextExtractor:
    def __init__(self, backend_role: str = "perception", timeout: float = 5.0):
        self.backend_role = backend_role
        self.timeout = timeout
        self._cache: dict[str, dict | None] = {}

    def extract(
        self,
        user_input: str,
        resolution: EntityResolution,
        heads: list[str] | None = None,
    ) -> dict | None:
        key = self._cache_key(user_input, resolution, heads)
        if key in self._cache:
            return self._cache[key]
        data = self._llm_json(user_input, resolution, heads or [])
        validated = self._validate(data)
        self._cache[key] = validated
        return validated

    def _llm_json(
        self,
        user_input: str,
        resolution: EntityResolution,
        heads: list[str],
    ) -> dict | None:
        system = (
            "You are an entity context extractor. Output ONLY valid JSON.\n"
            "Fields:\n"
            '- "context_terms": list[str]\n'
            '- "domain": "game" | "product" | "person" | "general"\n'
            '- "entity_kind": "game" | "character" | "product" | "person" | "general"\n'
            '- "ambiguous": bool\n'
            '- "evidence": str\n'
            "Rules:\n"
            "- context_terms must be short keywords or aliases, not full user sentences.\n"
            "- Never include conversational filler such as sana, 我听说, 你知道, 具体改动了什么吗.\n"
            "- Prefer disambiguating terms like Apex Legends, 猎犬, rework, patch notes.\n"
            "- Keep context_terms under 10 items.\n"
        )
        user_prompt = (
            f"User input: {user_input}\n"
            f"Canonical entity: {resolution.canonical or ''}\n"
            f"Aliases: {resolution.aliases}\n"
            f"Query heads: {heads}\n"
            "Return the entity context."
        )
        try:
            backend = registry.get_backend(self.backend_role)
            cfg = registry.get_config(self.backend_role)
            resp = backend.chat(
                cfg.model_id,
                [{"role": "user", "content": user_prompt}],
                system_prompt=system,
                timeout=self.timeout,
            )
            match = re.search(r"\{.*\}", resp.content or "", re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _validate(data: dict | None) -> dict | None:
        if not isinstance(data, dict):
            return None
        context_terms = data.get("context_terms", [])
        if not isinstance(context_terms, list):
            return None
        clean_terms = []
        for term in context_terms:
            text = str(term or "").strip()
            if text and len(text) <= 16 and text not in clean_terms:
                clean_terms.append(text)
        domain = str(data.get("domain") or "general")
        entity_kind = str(data.get("entity_kind") or "general")
        if domain not in _ALLOWED_DOMAINS or entity_kind not in _ALLOWED_KINDS:
            return None
        return {
            "context_terms": clean_terms[:10],
            "domain": domain,
            "entity_kind": entity_kind,
            "ambiguous": bool(data.get("ambiguous", False)),
            "evidence": str(data.get("evidence") or ""),
        }

    @staticmethod
    def _cache_key(
        user_input: str,
        resolution: EntityResolution,
        heads: list[str] | None,
    ) -> str:
        raw = json.dumps(
            {
                "u": user_input,
                "c": resolution.canonical,
                "a": resolution.aliases,
                "h": heads or [],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
