import re
import time
from abc import ABC, abstractmethod

import requests

from sana.models.search import SearchResult
from sana.services.search_discovery_service import BingRssParser, DuckDuckGoParser
from sana.services.search_parsers import BaiduParser, BingParser, DirectSourceRegistry, USER_AGENT
from sana.services.web_tool_config import WebToolConfig


class SearchProvider(ABC):
    name: str = "abstract"

    def enabled(self, config: WebToolConfig) -> bool:
        return True

    @abstractmethod
    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        raise NotImplementedError


class _HttpProvider(SearchProvider):
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    def _get(self, url: str, params: dict, timeout: float) -> str:
        resp = self.session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.text


class BingRssProvider(_HttpProvider):
    name = "bing_rss"

    def enabled(self, config: WebToolConfig) -> bool:
        return bool(config.allow_bing_rss)

    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        text = self._get(
            "https://www.bing.com/search",
            {"format": "rss", "q": query},
            config.timeout_seconds,
        )
        return [_to_search_result(item, query) for item in BingRssParser().parse(text)]


class DuckDuckGoProvider(_HttpProvider):
    name = "duckduckgo"

    def enabled(self, config: WebToolConfig) -> bool:
        return bool(config.allow_duckduckgo)

    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        text = self._get(
            "https://html.duckduckgo.com/html/",
            {"q": query},
            config.timeout_seconds,
        )
        return [_to_search_result(item, query) for item in DuckDuckGoParser().parse(text)]


class BingProvider(_HttpProvider):
    name = "bing"

    def enabled(self, config: WebToolConfig) -> bool:
        return bool(config.allow_bing)

    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        text = self._get("https://cn.bing.com/search", {"q": query}, config.timeout_seconds)
        return [_to_search_result(item, query) for item in BingParser().parse(text)]


class BaiduProvider(_HttpProvider):
    name = "baidu"

    def enabled(self, config: WebToolConfig) -> bool:
        return bool(config.allow_baidu)

    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                text = self._get("https://www.baidu.com/s", {"wd": query}, config.timeout_seconds)
                items = BaiduParser().parse(text)
                if items:
                    return [_to_search_result(item, query) for item in items]
                last_error = RuntimeError("baidu returned empty results")
            except Exception as exc:
                last_error = exc
            if attempt == 0:
                time.sleep(0.5)
        if last_error:
            raise last_error
        return []


class DirectSourceProvider(_HttpProvider):
    name = "direct"

    def __init__(self, session: requests.Session | None = None, registry: DirectSourceRegistry | None = None):
        super().__init__(session)
        self.direct_registry = registry or DirectSourceRegistry()

    def enabled(self, config: WebToolConfig) -> bool:
        return bool(config.allow_direct)

    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        if not canonical:
            return []
        results = []
        for url in self.direct_registry.urls_for(canonical):
            try:
                resp = self.session.get(url, timeout=config.timeout_seconds)
                resp.raise_for_status()
            except Exception:
                continue
            title_match = re.search(r"<title[^>]*>(.*?)</title>", resp.text or "", re.IGNORECASE | re.DOTALL)
            title = _clean_html(title_match.group(1)) if title_match else canonical
            results.append(SearchResult(
                title=title or canonical,
                url=url,
                snippet="官网/百科直抓兜底结果",
                source=self.name,
                published_at="",
                fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                query_head=query,
                url_kind="site_homepage",
                raw={"official": True},
            ))
        return results


class SearXNGProvider(_HttpProvider):
    name = "searxng"

    def enabled(self, config: WebToolConfig) -> bool:
        return bool(config.allow_searxng and config.searxng_url)

    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        text = self._get(
            config.searxng_url.rstrip("/") + "/search",
            {"q": query, "format": "json"},
            config.searxng_timeout_seconds,
        )
        return _parse_searxng(text, query)


def _to_search_result(item: dict, query: str) -> SearchResult:
    return SearchResult(
        title=item.get("title", ""),
        url=item.get("url", ""),
        snippet=item.get("snippet", ""),
        source=item.get("source", ""),
        published_at=item.get("published", item.get("published_at", "")),
        fetched_at=item.get("fetched_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        query_head=query,
        raw=item,
    )


def _parse_searxng(text: str, query: str) -> list[SearchResult]:
    import json

    try:
        data = json.loads(text or "{}")
        raw_results = data.get("results", []) if isinstance(data, dict) else []
    except ValueError:
        return []
    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url.startswith(("http://", "https://")) or not title:
            continue
        results.append(SearchResult(
            title=title,
            url=url,
            snippet=str(item.get("content") or "").strip(),
            source="searxng",
            published_at=str(item.get("publishedDate") or "").strip(),
            fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            query_head=query,
            raw=item,
        ))
    return results


def _clean_html(fragment: str) -> str:
    import html as html_lib

    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return html_lib.unescape(re.sub(r"\s+", " ", text)).strip()
