"""Versioned, configuration-owned official URLs for direct discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from sana.modules.search_planning.domain import FactType


@dataclass(frozen=True, slots=True)
class OfficialSourcePolicy:
    version: str = "official-sources-v1"
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
        normalized = entity.strip().casefold()
        matched = next(
            (
                configured
                for key, configured in self.sources.items()
                if normalized == key or normalized.startswith(f"{key} ")
            ),
            None,
        )
        if matched is None:
            return ()
        return tuple(matched.get(fact_type.value, ()))


__all__ = ["OfficialSourcePolicy"]
