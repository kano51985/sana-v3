from dataclasses import asdict, dataclass, field


class FactType:
    VERSION = "version"
    PATCH_NOTES = "patch_notes"
    CHARACTER_CHANGES = "character_changes"
    TEAM_META = "team_meta"
    GUIDE = "guide"
    NEWS = "news"
    GENERAL = "general"


@dataclass
class EntityContext:
    canonical: str = ""
    aliases: list[str] = field(default_factory=list)
    domain: str = "general"
    entity_kind: str = "general"
    context_terms: list[str] = field(default_factory=list)
    preferred_domains: list[str] = field(default_factory=list)
    ambiguous: bool = False
    evidence: str = ""
    context_source: str = "rules"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchIntent:
    fact_types: list[str] = field(default_factory=lambda: [FactType.GENERAL])
    required_page_types: list[str] = field(default_factory=list)
    answer_strategy: str = "summarize"

    def to_dict(self) -> dict:
        return asdict(self)
