import hashlib
import json
import re

from sana.config import registry


class LLMOfficialSourceJudge:
    def __init__(self, backend_role: str = "perception", timeout: float = 5.0):
        self.backend_role = backend_role
        self.timeout = timeout
        self._cache: dict[str, dict | None] = {}

    def judge(
        self,
        candidate: dict,
        canonical: str,
        context_terms: list[str] | None = None,
        domain: str = "general",
        entity_kind: str = "general",
    ) -> dict | None:
        key = self._cache_key(candidate, canonical, context_terms or [], domain, entity_kind)
        if key in self._cache:
            return self._cache[key]
        data = self._llm_json(candidate, canonical, context_terms or [], domain, entity_kind)
        validated = self._validate(data)
        self._cache[key] = validated
        return validated

    def _llm_json(
        self,
        candidate: dict,
        canonical: str,
        context_terms: list[str],
        domain: str,
        entity_kind: str,
    ) -> dict | None:
        system = (
            "You are an official source judge. Output ONLY valid JSON.\n"
            "Fields:\n"
            '- "official": bool\n'
            '- "confidence": float 0.0-1.0\n'
            '- "reason": str\n'
            "Rules:\n"
            "- official must be true only when the URL is genuinely the official source for the canonical entity.\n"
            "- Do not accept rental, reseller, tutorial aggregator, or unrelated tool sites as official.\n"
            "- Use domain ownership, title, snippet, and context terms as evidence.\n"
        )
        user_prompt = (
            f"Canonical entity: {canonical}\n"
            f"Domain: {domain}\n"
            f"Entity kind: {entity_kind}\n"
            f"Context terms: {context_terms}\n"
            f"Candidate URL: {candidate.get('url', '')}\n"
            f"Candidate title: {candidate.get('title', '')}\n"
            f"Candidate snippet: {candidate.get('snippet', '')}\n"
            "Return the official source verdict."
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
        try:
            confidence = float(data.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not isinstance(data.get("official"), bool) or not 0.0 <= confidence <= 1.0:
            return None
        return {
            "official": data["official"],
            "confidence": confidence,
            "reason": str(data.get("reason") or ""),
        }

    @staticmethod
    def _cache_key(
        candidate: dict,
        canonical: str,
        context_terms: list[str],
        domain: str,
        entity_kind: str,
    ) -> str:
        raw = json.dumps(
            {
                "u": candidate.get("url", ""),
                "t": candidate.get("title", ""),
                "s": candidate.get("snippet", ""),
                "c": canonical,
                "x": context_terms,
                "d": domain,
                "k": entity_kind,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
