"""Offline source identity and versioned authority classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

import tldextract

from sana.modules.evidence.domain import SourceAuthority
from sana.modules.shared.entity_matching import match_configured_entity


_PSL = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=None,
    include_psl_private_domains=True,
)


def registrable_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").strip(".").casefold()
    if not host:
        raise ValueError("Source URL has no hostname")
    extracted = _PSL(host)
    identity = extracted.top_domain_under_public_suffix
    return (identity or host).casefold()


@dataclass(frozen=True, slots=True)
class SourceAuthorityPolicy:
    """Authority is configuration-owned and entity-specific, never model-owned."""

    version: str = "source-authority-v3"
    official_domains_by_entity: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: {
            "apex legends": frozenset({"ea.com", "respawn.com"}),
            "deepseek": frozenset({"deepseek.com"}),
            "openai": frozenset({"openai.com"}),
            "python": frozenset({"python.org"}),
            "http": frozenset({"rfc-editor.org"}),
            "git": frozenset({"git-scm.com"}),
            "rust": frozenset({"rust-lang.org"}),
        }
    )

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Source authority policy version cannot be empty")
        normalized = {
            entity.strip().casefold(): frozenset(
                domain.strip().casefold() for domain in domains if domain.strip()
            )
            for entity, domains in self.official_domains_by_entity.items()
            if entity.strip()
        }
        object.__setattr__(
            self,
            "official_domains_by_entity",
            MappingProxyType(normalized),
        )

    def classify(self, url: str, *, entity: str) -> tuple[str, SourceAuthority]:
        identity = registrable_domain(url)
        matched_key = match_configured_entity(
            entity,
            self.official_domains_by_entity,
        )
        official = (
            self.official_domains_by_entity[matched_key]
            if matched_key is not None
            else ()
        )
        return (
            identity,
            SourceAuthority.OFFICIAL
            if identity in official
            else SourceAuthority.INDEPENDENT,
        )
