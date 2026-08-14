"""Optional JSON adapter for a tenant-controlled SearXNG deployment."""

from __future__ import annotations

import json
from datetime import datetime
from time import perf_counter

import httpx

from sana.modules.discovery.domain import (
    DiscoveryQuery,
    ProviderMetrics,
    ProviderResponse,
    SearchHit,
)
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.search._http import bounded_get, canonicalize_url


class SearxngProvider:
    name = "searxng"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        max_results: int = 10,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self._client = client
        self._endpoint = f"{base_url.rstrip('/')}/search"
        self._max_results = max_results
        self._max_response_bytes = max_response_bytes

    async def search(
        self,
        query: DiscoveryQuery,
        *,
        timeout_seconds: float,
    ) -> ProviderResponse:
        started = perf_counter()
        response_bytes = 0
        try:
            content, response_bytes = await bounded_get(
                self._client,
                self._endpoint,
                params={"q": query.text, "format": "json", "language": query.locale},
                timeout_seconds=timeout_seconds,
                max_response_bytes=self._max_response_bytes,
            )
            payload = json.loads(content)
            raw_results = payload.get("results", [])
            if not isinstance(raw_results, list):
                raise ValueError("results is not a list")
            hits = []
            for raw in raw_results[: self._max_results]:
                if not isinstance(raw, dict) or not raw.get("url"):
                    continue
                try:
                    canonical = canonicalize_url(str(raw["url"]))
                except ValueError:
                    continue
                published = None
                if raw.get("publishedDate"):
                    try:
                        published = datetime.fromisoformat(
                            str(raw["publishedDate"]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                rank = len(hits) + 1
                hits.append(
                    SearchHit(
                        provider=self.name,
                        query_key=query.key,
                        rank=rank,
                        url=str(raw["url"]),
                        canonical_url=canonical,
                        title=str(raw.get("title") or canonical),
                        snippet=str(raw.get("content") or ""),
                        score=float(raw.get("score") or (1.0 / rank)),
                        published_at=published,
                    )
                )
            return ProviderResponse(
                self.name,
                query.key,
                tuple(hits),
                ProviderMetrics(
                    int((perf_counter() - started) * 1000),
                    response_bytes=response_bytes,
                    raw_hit_count=len(raw_results),
                ),
            )
        except TypedError as exc:
            error = exc
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            error = TypedError(
                ErrorCategory.CONTENT,
                "invalid_searxng_response",
                "SearXNG returned invalid JSON data",
                retryable=False,
                cause=exc,
            )
        return ProviderResponse(
            self.name,
            query.key,
            (),
            ProviderMetrics(
                int((perf_counter() - started) * 1000),
                response_bytes=response_bytes,
            ),
            error,
        )
