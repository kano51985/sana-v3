"""Semantic planning values that contain no conversational query suffix."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Mapping


class FactType(StrEnum):
    CHARACTER_CHANGES = "character_changes"
    VERSION = "version"
    PATCH_NOTES = "patch_notes"
    TEAM_META = "team_meta"
    CURRENT_VALUE = "current_value"
    COMPARISON = "comparison"
    BACKGROUND = "background"


class Freshness(StrEnum):
    STABLE = "STABLE"
    RECENT = "RECENT"
    CURRENT = "CURRENT"


class Consequence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_CONVERSATIONAL_TERM = re.compile(
    r"(可以告诉我|你不是|我好久|请问一下|please tell me|could you|i haven'?t)",
    re.I,
)


def _validate_search_term(value: str, field_name: str, max_length: int) -> None:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be a short semantic term")
    if re.search(r"[!?！？。\r\n]", normalized) or _CONVERSATIONAL_TERM.search(normalized):
        raise ValueError(f"{field_name} contains conversational text")


@dataclass(frozen=True, slots=True)
class FactRequirement:
    key: str
    fact_type: FactType
    description: str
    subject: str
    required: bool = True
    freshness: Freshness = Freshness.STABLE
    consequence: Consequence = Consequence.LOW
    preferred_source_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.description.strip() or not self.subject.strip():
            raise ValueError("Fact key, description and subject are required")
        _validate_search_term(self.subject, "Fact subject", 48)


@dataclass(frozen=True, slots=True)
class NormalizedIntent:
    entity: str
    aliases: tuple[str, ...]
    locale: str
    facts: tuple[FactRequirement, ...]
    requires_comparison: bool = False
    requires_complete_sources: bool = False

    def __post_init__(self) -> None:
        if not self.entity.strip():
            raise ValueError("Normalized entity cannot be empty")
        if not self.locale.strip():
            raise ValueError("Locale cannot be empty")
        if not self.facts:
            raise ValueError("At least one fact requirement is required")
        _validate_search_term(self.entity, "Normalized entity", 64)
        for alias in self.aliases:
            _validate_search_term(alias, "Entity alias", 48)
        keys = [fact.key for fact in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("Fact keys must be unique")


@dataclass(frozen=True, slots=True)
class QuerySpec:
    key: str
    fact_key: str
    text: str
    signature: str
    locale: str
    freshness_days: int | None
    plan_revision: int
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((self.key.strip(), self.fact_key.strip(), self.text.strip())):
            raise ValueError("Query key, fact key and text are required")
        if self.plan_revision < 1:
            raise ValueError("plan_revision must be positive")
        if self.freshness_days is not None and self.freshness_days < 1:
            raise ValueError("freshness_days must be positive")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
