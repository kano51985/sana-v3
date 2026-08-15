"""Versioned, reviewed URLs for deterministic direct discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from sana.modules.search_planning.domain import FactRequirement, FactType
from sana.modules.shared.entity_matching import match_configured_entity


@dataclass(frozen=True, slots=True)
class DirectSourceRule:
    """A reviewed page selected by Fact semantics, not model-provided URLs."""

    fact_types: frozenset[FactType]
    any_terms: tuple[str, ...]
    urls: tuple[str, ...]

    def matches(self, fact: FactRequirement) -> bool:
        if fact.fact_type not in self.fact_types:
            return False
        value = f"{fact.key} {fact.description} {fact.subject}".casefold()
        return any(term.casefold() in value for term in self.any_terms)


@dataclass(frozen=True, slots=True)
class DirectSourcePolicy:
    """Reviewed direct URLs; authority remains owned by SourceAuthorityPolicy."""

    version: str = "direct-sources-v6"
    sources: Mapping[str, Mapping[str, tuple[str, ...]]] = field(
        default_factory=lambda: {
            "python": {
                FactType.VERSION.value: ("https://www.python.org/downloads/",),
                FactType.CURRENT_VALUE.value: (
                    "https://www.python.org/downloads/",
                ),
                FactType.BACKGROUND.value: (
                    "https://docs.python.org/3/license.html#history-of-the-software",
                ),
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
                    "https://www.ea.com/games/apex-legends/apex-legends/news/"
                    "overclocked-patch-notes",
                ),
                FactType.PATCH_NOTES.value: (
                    "https://www.ea.com/games/apex-legends/apex-legends/news/"
                    "overclocked-midseason-patch-notes",
                ),
                FactType.CHARACTER_CHANGES.value: (
                    "https://www.ea.com/games/apex-legends/apex-legends/news/"
                    "breach-patch-notes",
                ),
                FactType.TEAM_META.value: (
                    "https://apexranked.com/meta",
                    "https://games.gg/apex-legends/guides/"
                    "apex-legends-season-29-tier-list/",
                ),
            },
            "http": {
                FactType.BACKGROUND.value: (
                    "https://www.rfc-editor.org/rfc/rfc9110.html",
                ),
                FactType.COMPARISON.value: (
                    "https://www.rfc-editor.org/rfc/rfc9110.html",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://www.iana.org/assignments/http-status-codes/"
                    "http-status-codes-1.csv",
                    "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
                ),
            },
            "git": {
                FactType.BACKGROUND.value: (
                    "https://www.kernel.org/pub/software/scm/git/docs/"
                    "gitglossary.html",
                ),
                FactType.VERSION.value: ("https://git-scm.com/downloads",),
                FactType.CURRENT_VALUE.value: ("https://git-scm.com/downloads",),
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
                FactType.BACKGROUND.value: (
                    "https://developers.openai.com/api/docs/models/all",
                ),
                FactType.VERSION.value: (
                    "https://developers.openai.com/api/docs/models/all",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://developers.openai.com/api/docs/models/all",
                ),
                FactType.COMPARISON.value: (
                    "https://developers.openai.com/api/docs/models/all",
                ),
            },
            "json": {
                FactType.BACKGROUND.value: (
                    "https://www.rfc-editor.org/rfc/rfc8259.html",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://www.rfc-editor.org/rfc/rfc8259.html",
                ),
            },
            "sha-256": {
                FactType.BACKGROUND.value: (
                    "https://www.rfc-editor.org/rfc/rfc6234.html",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://www.rfc-editor.org/rfc/rfc6234.html",
                ),
            },
            "dns": {
                FactType.BACKGROUND.value: (
                    "https://www.iana.org/assignments/service-names-port-numbers/"
                    "service-names-port-numbers.csv",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://www.iana.org/assignments/service-names-port-numbers/"
                    "service-names-port-numbers.csv",
                ),
            },
            "tls 1.3": {
                FactType.BACKGROUND.value: (
                    "https://www.rfc-editor.org/rfc/rfc8446.txt",
                ),
                FactType.VERSION.value: (
                    "https://www.rfc-editor.org/rfc/rfc8446.txt",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://www.rfc-editor.org/rfc/rfc8446.txt",
                ),
            },
            "rfc 3339": {
                FactType.BACKGROUND.value: (
                    "https://www.rfc-editor.org/rfc/rfc3339.html",
                ),
                FactType.COMPARISON.value: (
                    "https://www.rfc-editor.org/rfc/rfc3339.html",
                ),
            },
            "sql transaction isolation": {
                FactType.BACKGROUND.value: (
                    "https://www.postgresql.org/docs/current/transaction-iso.html",
                ),
                FactType.COMPARISON.value: (
                    "https://www.postgresql.org/docs/current/transaction-iso.html",
                ),
            },
            "postgresql": {
                FactType.BACKGROUND.value: (
                    "https://www.postgresql.org/support/versioning/",
                ),
                FactType.VERSION.value: (
                    "https://www.postgresql.org/support/versioning/",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://www.postgresql.org/support/versioning/",
                ),
                FactType.COMPARISON.value: (
                    "https://www.postgresql.org/support/versioning/",
                ),
            },
            "sqlite": {
                FactType.BACKGROUND.value: (
                    "https://www.sqlite.org/copyright.html",
                ),
            },
            "cap theorem": {
                FactType.BACKGROUND.value: (
                    "https://www.ibm.com/think/topics/cap-theorem",
                ),
                FactType.COMPARISON.value: (
                    "https://www.ibm.com/think/topics/cap-theorem",
                ),
            },
            "database acid": {
                FactType.BACKGROUND.value: (
                    "https://www.ibm.com/think/topics/transaction-management",
                ),
            },
            "acid": {
                FactType.BACKGROUND.value: (
                    "https://www.ibm.com/think/topics/transaction-management",
                ),
            },
            "node.js": {
                FactType.VERSION.value: (
                    "https://nodejs.org/en/about/previous-releases",
                ),
                FactType.CURRENT_VALUE.value: (
                    "https://nodejs.org/en/about/previous-releases",
                ),
            },
        }
    )
    rules: Mapping[str, tuple[DirectSourceRule, ...]] = field(
        default_factory=lambda: {
            "python": (
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND}),
                    ("creator", "first_release", "首次公开", "创建者"),
                    (
                        "https://docs.python.org/3/"
                        "license.html#history-of-the-software",
                    ),
                ),
                DirectSourceRule(
                    frozenset({FactType.CURRENT_VALUE, FactType.VERSION}),
                    ("security_support", "security support", "release_date", "release date"),
                    ("https://devguide.python.org/versions/",),
                ),
            ),
            "git": (
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND}),
                    ("file_states", "staging_area", "working_tree", "directory_relation"),
                    (
                        "https://git-scm.com/book/en/v2/"
                        "Getting-Started-What-is-Git%3F",
                    ),
                ),
            ),
            "http": (
                DirectSourceRule(
                    frozenset(
                        {
                            FactType.BACKGROUND,
                            FactType.COMPARISON,
                            FactType.CURRENT_VALUE,
                        }
                    ),
                    (
                        "get_safe_idempotent",
                        "get_safe",
                        "get_idempotent",
                        "safe and idempotent",
                        "safe according",
                        "idempotent according",
                        "http_semantics_standard",
                    ),
                    (
                        "https://www.iana.org/assignments/http-methods/"
                        "http-methods.xhtml",
                    ),
                ),
            ),
            "dns": (
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND, FactType.CURRENT_VALUE}),
                    (
                        "dns_default_port",
                        "dns_transport_protocols",
                        "port number",
                        "transport protocols",
                    ),
                    ("https://www.rfc-editor.org/rfc/rfc1035.html",),
                ),
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND, FactType.CURRENT_VALUE}),
                    ("dns_registry_authority", "authoritative registry"),
                    (
                        "https://www.iana.org/assignments/"
                        "service-names-port-numbers/"
                        "service-names-port-numbers.xhtml",
                    ),
                ),
            ),
            "json": (
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND, FactType.CURRENT_VALUE}),
                    ("json_media_type", "media type", "registered"),
                    (
                        "https://www.iana.org/assignments/media-types/"
                        "application/json",
                    ),
                ),
            ),
            "sha-256": (
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND, FactType.CURRENT_VALUE}),
                    ("digest", "length", "size", "standard_reference"),
                    ("https://www.rfc-editor.org/rfc/rfc6234.txt",),
                ),
            ),
            "rfc 3339": (
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND, FactType.COMPARISON}),
                    ("semantics", "example", "citation", "+00:00", "designator"),
                    ("https://www.rfc-editor.org/rfc/rfc3339.txt",),
                ),
            ),
            "postgresql": (
                DirectSourceRule(
                    frozenset({FactType.BACKGROUND, FactType.COMPARISON}),
                    (
                        "isolation",
                        "anomaly",
                        "read_uncommitted",
                        "read_committed",
                        "repeatable_read",
                        "serializable",
                    ),
                    (
                        "https://www.postgresql.org/docs/current/"
                        "transaction-iso.html",
                    ),
                ),
            ),
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
        normalized_rules = {
            entity.strip().casefold(): tuple(rules)
            for entity, rules in self.rules.items()
            if entity.strip() and rules
        }
        object.__setattr__(self, "rules", MappingProxyType(normalized_rules))

    def urls_for(self, entity: str, fact_type: FactType) -> tuple[str, ...]:
        matched_key = match_configured_entity(entity, self.sources)
        if matched_key is None:
            return ()
        return tuple(self.sources[matched_key].get(fact_type.value, ()))

    def urls_for_fact(
        self,
        entity: str,
        fact: FactRequirement,
    ) -> tuple[str, ...]:
        matched_key = match_configured_entity(entity, self.rules)
        if matched_key is not None:
            for rule in self.rules[matched_key]:
                if rule.matches(fact):
                    return rule.urls
        return self.urls_for(entity, fact.fact_type)


OfficialSourcePolicy = DirectSourcePolicy


__all__ = ["DirectSourcePolicy", "DirectSourceRule", "OfficialSourcePolicy"]
