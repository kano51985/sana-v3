from dataclasses import dataclass, field
import json
import re

from sana.config import registry
from sana.services.web_alias_store import WebAliasStore


@dataclass
class EntityResolution:
    raw: str = ""
    canonical: str = ""
    aliases: list = field(default_factory=list)
    confidence: float = 0.0
    need_clarify: bool = False
    evidence: str = ""
    source: str = "none"
    source_urls: list = field(default_factory=list)


class EntityResolver:
    THRESHOLD = 0.85

    def __init__(self, alias_store: WebAliasStore | None = None, backend_role: str = "perception"):
        self.alias_store = alias_store or WebAliasStore()
        self.backend_role = backend_role

    def self_check(self, user_input: str, perception_entities: list[str] | None = None, recent_messages: list[str] | None = None) -> EntityResolution:
        raw = (user_input or "").strip()
        entities = [e for e in (perception_entities or []) if e]

        cached = self._resolve_from_cache(raw)
        matched_raw = raw
        if not cached:
            for entity in entities:
                cached = self._resolve_from_cache(entity)
                if cached:
                    matched_raw = entity
                    break
        if cached:
            return EntityResolution(
                raw=matched_raw or raw,
                canonical=cached,
                aliases=self._aliases_for(cached),
                confidence=0.95,
                source="alias_cache",
            )

        system = (
            "You are an entity normalization assistant. Output ONLY valid JSON.\n"
            "Fields:\n"
            "- recognized: bool\n"
            "- canonical: str or ''\n"
            "- aliases: list[str]\n"
            "- confidence: float 0.0-1.0\n"
            "- evidence: str\n"
            "Only set recognized=true and confidence>=0.85 when you are confident about a canonical entity.\n"
            "Example:\n"
            '{"recognized": true, "canonical": "王者荣耀", "aliases": ["农", "农药"], "confidence": 0.95, "evidence": "游戏简称"}'
        )
        user_prompt = (
            f"User input: {user_input}\n"
            f"Perception entities: {entities}\n"
            f"Recent messages: {recent_messages or []}\n"
            "Resolve the main entity."
        )
        result = self._llm_json(system, user_prompt)
        return self._apply_llm_result(raw, result)

    def clarify_from_results(self, resolution: EntityResolution, results: list[dict]) -> EntityResolution:
        if not results:
            return resolution
        evidence_lines = []
        for item in results[:10]:
            evidence_lines.append(
                f"- {item.get('title', '')} | {item.get('snippet', '')} | {item.get('url', '')}"
            )
        system = (
            "You are an entity disambiguation assistant. Output ONLY valid JSON.\n"
            "Decide whether the search evidence identifies the raw term as a canonical entity.\n"
            "Fields:\n"
            "- recognized: bool\n"
            "- canonical: str or ''\n"
            "- aliases: list[str]\n"
            "- confidence: float 0.0-1.0\n"
            "- evidence: str\n"
            'Example: {"recognized": true, "canonical": "王者荣耀", "aliases": ["农"], "confidence": 0.9, "evidence": "多个来源指向农药/王者荣耀"}'
        )
        user_prompt = (
            f"Raw entity: {resolution.raw}\n"
            f"Search evidence:\n{chr(10).join(evidence_lines)}\n"
            "Return the canonical entity only if the evidence is consistent."
        )
        result = self._llm_json(system, user_prompt)
        confirmed = self._apply_llm_result(resolution.raw, result, learned=True)
        if confirmed.confidence >= self.THRESHOLD and confirmed.canonical:
            confirmed.source_urls = [item.get("url", "") for item in results if item.get("url")]
        return confirmed

    def learn_alias(self, resolution: EntityResolution) -> None:
        if (
            resolution.source == "ai_learned"
            and resolution.canonical
            and resolution.aliases
            and resolution.confidence >= self.THRESHOLD
        ):
            self.alias_store.add_learned(
                canonical=resolution.canonical,
                aliases=resolution.aliases,
                confidence=resolution.confidence,
                source_urls=resolution.source_urls,
            )

    def _resolve_from_cache(self, text: str) -> str | None:
        if self.alias_store is None:
            return None
        return self.alias_store.resolve(text)

    def _aliases_for(self, canonical: str) -> list[str]:
        if self.alias_store is None:
            return []
        return self.alias_store.aliases_for(canonical)

    def _apply_llm_result(self, raw: str, result: dict | None, learned: bool = False) -> EntityResolution:
        if not result or not result.get("recognized"):
            return EntityResolution(raw=raw, canonical="", confidence=0.0, need_clarify=True, source="unknown")
        try:
            confidence = float(result.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        canonical = str(result.get("canonical") or "").strip()
        aliases = [str(a).strip() for a in result.get("aliases", []) if str(a).strip()]
        source = "ai_learned" if learned else "ai_self"
        if canonical and confidence >= self.THRESHOLD:
            return EntityResolution(
                raw=raw,
                canonical=canonical,
                aliases=aliases or [raw],
                confidence=confidence,
                need_clarify=False,
                evidence=str(result.get("evidence", "")),
                source=source,
            )
        return EntityResolution(
            raw=raw,
            canonical="",
            confidence=confidence,
            need_clarify=True,
            evidence=str(result.get("evidence", "")),
            source="unknown",
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
