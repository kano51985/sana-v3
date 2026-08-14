"""Immutable per-call discovery results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from sana.modules.shared.errors import TypedError


@dataclass(frozen=True, slots=True)
class DiscoveryQuery:
    key: str
    text: str
    locale: str
    freshness_days: int | None = None
    direct_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.text.strip() or not self.locale.strip():
            raise ValueError("Discovery query key, text and locale are required")


@dataclass(frozen=True, slots=True)
class SearchHit:
    provider: str
    query_key: str
    rank: int
    url: str
    canonical_url: str
    title: str
    snippet: str
    score: float
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("Search hit rank must be positive")
        if not self.provider.strip() or not self.query_key.strip() or not self.url.strip():
            raise ValueError("Search hit provider, query and URL are required")
        if not math.isfinite(self.score) or self.score < 0:
            raise ValueError("Search hit score must be finite and non-negative")
        if (
            self.published_at is not None
            and (
                self.published_at.tzinfo is None
                or self.published_at.utcoffset() is None
            )
        ):
            raise ValueError("Search hit publication time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProviderMetrics:
    elapsed_ms: int
    response_bytes: int = 0
    raw_hit_count: int = 0

    def __post_init__(self) -> None:
        if self.elapsed_ms < 0 or self.response_bytes < 0 or self.raw_hit_count < 0:
            raise ValueError("Provider metrics cannot be negative")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    provider: str
    query_key: str
    hits: tuple[SearchHit, ...]
    metrics: ProviderMetrics
    error: TypedError | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.query_key.strip():
            raise ValueError("Provider response identity is required")
        if any(
            hit.provider != self.provider or hit.query_key != self.query_key
            for hit in self.hits
        ):
            raise ValueError("Provider response contains hits from another call")

    @property
    def ok(self) -> bool:
        return self.error is None
