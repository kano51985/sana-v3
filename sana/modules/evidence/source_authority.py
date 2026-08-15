"""Offline source identity and versioned authority classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

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


def _normalized_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").strip(".").casefold()
    if not host:
        raise ValueError("Source URL has no hostname")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            f"{host}{port}",
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class SourceAuthorityPolicy:
    """Authority is configuration-owned and entity-specific, never model-owned."""

    version: str = "source-authority-v6"
    official_domains_by_entity: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: {
            "apex legends": frozenset({"ea.com", "respawn.com"}),
            "deepseek": frozenset({"deepseek.com"}),
            "openai": frozenset({"openai.com"}),
            "python": frozenset({"python.org"}),
            "http": frozenset({"iana.org", "rfc-editor.org"}),
            "git": frozenset({"git-scm.com"}),
            "json": frozenset({"iana.org", "rfc-editor.org"}),
            "sha-256": frozenset({"rfc-editor.org"}),
            "dns": frozenset({"iana.org", "rfc-editor.org"}),
            "tls 1.3": frozenset({"rfc-editor.org"}),
            "rfc 3339": frozenset({"rfc-editor.org"}),
            "sql transaction isolation": frozenset({"postgresql.org"}),
            "postgresql": frozenset({"postgresql.org"}),
            "sqlite": frozenset({"sqlite.org"}),
            "node.js": frozenset({"nodejs.org"}),
            "rust": frozenset({"rust-lang.org"}),
        }
    )
    official_url_prefixes_by_entity: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "git": (
                "https://www.kernel.org/pub/software/scm/git/docs/",
            ),
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
        normalized_prefixes = {
            entity.strip().casefold(): tuple(
                _normalized_url(prefix)
                for prefix in prefixes
                if prefix.strip()
            )
            for entity, prefixes in self.official_url_prefixes_by_entity.items()
            if entity.strip()
        }
        object.__setattr__(
            self,
            "official_url_prefixes_by_entity",
            MappingProxyType(normalized_prefixes),
        )

    def classify(self, url: str, *, entity: str) -> tuple[str, SourceAuthority]:
        identity = registrable_domain(url)
        matched_domain_key = match_configured_entity(
            entity,
            self.official_domains_by_entity,
        )
        official = (
            self.official_domains_by_entity[matched_domain_key]
            if matched_domain_key is not None
            else ()
        )
        matched_prefix_key = match_configured_entity(
            entity,
            self.official_url_prefixes_by_entity,
        )
        prefixes = (
            self.official_url_prefixes_by_entity[matched_prefix_key]
            if matched_prefix_key is not None
            else ()
        )
        normalized_url = _normalized_url(url)
        return (
            identity,
            SourceAuthority.OFFICIAL
            if identity in official
            or any(normalized_url.startswith(prefix) for prefix in prefixes)
            else SourceAuthority.INDEPENDENT,
        )
