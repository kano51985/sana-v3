"""Discovery adapter for explicit trusted/official URLs from planning."""

from time import perf_counter

from sana.modules.discovery.domain import (
    DiscoveryQuery,
    ProviderMetrics,
    ProviderResponse,
    SearchHit,
)
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.search._http import canonicalize_url


class DirectSourceProvider:
    name = "direct"

    async def search(
        self,
        query: DiscoveryQuery,
        *,
        timeout_seconds: float,
    ) -> ProviderResponse:
        del timeout_seconds
        started = perf_counter()
        hits = []
        try:
            for rank, url in enumerate(query.direct_urls, start=1):
                canonical = canonicalize_url(url)
                hits.append(
                    SearchHit(
                        provider=self.name,
                        query_key=query.key,
                        rank=rank,
                        url=url,
                        canonical_url=canonical,
                        title=canonical,
                        snippet="",
                        score=max(0.0, 1.0 - (rank - 1) * 0.05),
                    )
                )
        except ValueError as exc:
            return ProviderResponse(
                self.name,
                query.key,
                (),
                ProviderMetrics(int((perf_counter() - started) * 1000)),
                TypedError(
                    ErrorCategory.CONTENT,
                    "invalid_direct_source_url",
                    str(exc),
                    retryable=False,
                    cause=exc,
                ),
            )
        return ProviderResponse(
            self.name,
            query.key,
            tuple(hits),
            ProviderMetrics(
                int((perf_counter() - started) * 1000),
                raw_hit_count=len(hits),
            ),
        )
