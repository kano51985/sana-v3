"""Bing RSS discovery adapter with bounded response parsing."""

from __future__ import annotations

import html
import re
from email.utils import parsedate_to_datetime
from time import perf_counter
from xml.etree import ElementTree

import httpx

from sana.modules.discovery.domain import (
    DiscoveryQuery,
    ProviderMetrics,
    ProviderResponse,
    SearchHit,
)
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.search._http import bounded_get, canonicalize_url


_HTML_TAG = re.compile(r"<[^>]+>")


class BingRssProvider:
    name = "bing_rss"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint: str = "https://www.bing.com/search",
        max_results: int = 10,
        max_response_bytes: int = 1_000_000,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
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
                params={"q": query.text, "format": "rss", "setlang": query.locale},
                timeout_seconds=timeout_seconds,
                max_response_bytes=self._max_response_bytes,
            )
            root = ElementTree.fromstring(content)
            items = root.findall(".//item")
            hits = []
            for item in items[: self._max_results]:
                url = (item.findtext("link") or "").strip()
                if not url:
                    continue
                try:
                    canonical = canonicalize_url(url)
                except ValueError:
                    continue
                description = html.unescape(item.findtext("description") or "")
                description = _HTML_TAG.sub(" ", description)
                published = None
                if item.findtext("pubDate"):
                    try:
                        published = parsedate_to_datetime(item.findtext("pubDate"))
                    except (TypeError, ValueError):
                        pass
                rank = len(hits) + 1
                hits.append(
                    SearchHit(
                        provider=self.name,
                        query_key=query.key,
                        rank=rank,
                        url=url,
                        canonical_url=canonical,
                        title=(item.findtext("title") or canonical).strip(),
                        snippet=" ".join(description.split()),
                        score=1.0 / rank,
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
                    raw_hit_count=len(items),
                ),
            )
        except TypedError as exc:
            error = exc
        except ElementTree.ParseError as exc:
            error = TypedError(
                ErrorCategory.CONTENT,
                "invalid_bing_rss",
                "Bing returned invalid RSS",
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
