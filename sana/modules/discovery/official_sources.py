"""Versioned, configuration-owned official URLs for direct discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from sana.modules.search_planning.domain import FactType
from sana.modules.shared.entity_matching import match_configured_entity


@dataclass(frozen=True, slots=True)
class OfficialSourcePolicy:
    version: str = "official-sources-v3"
    sources: Mapping[str, Mapping[str, tuple[str, ...]]] = field(
        default_factory=lambda: {
            "python": {
                FactType.VERSION.value: ("https://www.python.org/downloads/",),
                FactType.CURRENT_VALUE.value: (
                    "https://www.python.org/downloads/",
                ),
                FactType.BACKGROUND.value: ("https://www.python.org/about/",),
            },
            "deepseek": {
                FactType.BACKGROUND.value: ("https://api-docs.deepseek.com/",),
                FactType.VERSION.value: ("https://api-docs.deepseek.com/news/",),
                FactType.CURRENT_VALUE.value: (
                    "https://api-docs.deepseek.com/",
                ),
            },
            "apex legends": {
                FactType.VERSION.value: (
                    "https://www.ea.com/games/apex-legends/apex-legends/news"
                    "?page=1&type=game-updates",
                ),
                FactType.PATCH_NOTES.value: (
                    "https://www.ea.com/games/apex-legends/apex-legends/news"
                    "?page=1&type=game-updates",
                ),
                FactType.CHARACTER_CHANGES.value: (
                    "https://www.ea.com/games/apex-legends/apex-legends/news"
                    "?page=1&type=game-updates",
                ),
            },
            "http": {
                FactType.BACKGROUND.value: (
                    "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
                ),
            },
            "git": {
                FactType.BACKGROUND.value: (
                    "https://git-scm.com/book/en/v2/Git-Internals-Git-Objects",
                ),
            },
            "rust": {
                FactType.VERSION.value: (
                    "https://doc.rust-lang.org/stable/releases.html",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://doc.rust-lang.org/stable/releases.html",
                ),
                FactType.BACKGROUND.value: (
                    "https://doc.rust-lang.org/stable/releases.html",
                ),
            },
            "openai": {
                FactType.BACKGROUND.value: ("https://openai.com/",),
                FactType.VERSION.value: ("https://openai.com/news/",),
                FactType.CURRENT_VALUE.value: ("https://openai.com/",),
                FactType.COMPARISON.value: (
                    "https://openai.com/",
                    "https://openai.com/news/",
                ),
            },
        }
    )

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Official source policy version cannot be empty")
        normalized = {
            entity.strip().casefold(): MappingProxyType(
                {
                    fact_type.strip(): tuple(urls)
                    for fact_type, urls in by_type.items()
                    if fact_type.strip() and urls
                }
            )
            for entity, by_type in self.sources.items()
            if entity.strip()
        }
        object.__setattr__(self, "sources", MappingProxyType(normalized))

    def urls_for(self, entity: str, fact_type: FactType) -> tuple[str, ...]:
        matched_key = match_configured_entity(entity, self.sources)
        if matched_key is None:
            return ()
        return tuple(self.sources[matched_key].get(fact_type.value, ()))


__all__ = ["OfficialSourcePolicy"]
