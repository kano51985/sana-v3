import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus

import requests

from sana.services.web_tool_config import WebToolConfig, WebToolConfigStore
from sana.services.search_discovery_service import SearchDiscoveryService
from sana.services.katana_crawler import KatanaCrawler
from sana.services.content_extractor import ContentExtractor


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class DirectSourceRegistry:
    def __init__(self):
        self.sites = {
            "王者荣耀": ["https://pvp.qq.com/web201605/news.shtml"],
            "英雄联盟": ["https://lol.qq.com/"],
            "和平精英": ["https://gp.qq.com/"],
            "金铲铲之战": ["https://jcc.qq.com/"],
            "原神": ["https://ys.mihoyo.com/"],
        }

    def urls_for(self, canonical: str) -> list[str]:
        return list(self.sites.get(canonical, []))


class BingParser:
    def parse(self, html_text: str) -> list[dict]:
        results = []
        blocks = re.split(r'<li class="b_algo"', html_text or "", flags=re.IGNORECASE)
        for block in blocks[1:]:
            item = self._parse_block(block)
            if item:
                results.append(item)
        return results

    @staticmethod
    def _parse_block(block: str) -> dict | None:
        match = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        url = html.unescape(match.group(1)).strip()
        title = _clean_html(match.group(2))
        if not url.startswith(("http://", "https://")) or not title:
            return None
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.IGNORECASE | re.DOTALL)
        return {
            "title": title,
            "url": url,
            "snippet": _clean_html(snippet_match.group(1)) if snippet_match else "",
            "source": "bing",
        }


class BaiduParser:
    def parse(self, html_text: str) -> list[dict]:
        results = []
        blocks = re.split(r'<div[^>]*class="[^"]*result[^"]*"', html_text or "", flags=re.IGNORECASE)
        for block in blocks[1:]:
            item = self._parse_block(block)
            if item:
                results.append(item)
        return results

    @staticmethod
    def _parse_block(block: str) -> dict | None:
        match = re.search(
            r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        url = html.unescape(match.group(1)).strip()
        title = _clean_html(match.group(2))
        if not url.startswith(("http://", "https://")) or not title:
            return None
        snippet = ""
        for pattern in (
            r'<span[^>]*class="[^"]*content-right[^"]*"[^>]*>(.*?)</span>',
            r'<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>',
        ):
            sm = re.search(pattern, block, flags=re.IGNORECASE | re.DOTALL)
            if sm:
                snippet = _clean_html(sm.group(1))
                break
        return {
            "title": title,
            "url": url,
            "snippet": snippet,
            "source": "baidu",
        }


class WebSearchService:
    def __init__(self, config_store: WebToolConfigStore | None = None, config: WebToolConfig | None = None):
        self.config_store = config_store or WebToolConfigStore()
        self._config = config
        self.direct_registry = DirectSourceRegistry()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.unavailable_sources: set[str] = set()
        self.discovery = SearchDiscoveryService()
        self.crawler = KatanaCrawler()
        self.content_extractor = ContentExtractor()
        self.last_trace: dict = {}

    def search(self, heads: list[str], direct_canonical: str | None = None, config: WebToolConfig | None = None) -> list[dict]:
        cfg = config or self._config or self.config_store.load()
        heads = [h for h in heads if h][:max(1, cfg.max_query_heads)]
        results: list[dict] = []
        if not heads:
            return results
        with ThreadPoolExecutor(max_workers=min(3, len(heads))) as executor:
            futures = {
                executor.submit(self._search_head, head, cfg, direct_canonical): head
                for head in heads
            }
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception:
                    continue

        discovery_results = self._discover(heads, cfg)
        crawl_results = self._crawl(discovery_results, direct_canonical, heads, cfg)
        results.extend(discovery_results)
        results.extend(crawl_results)
        self.last_trace = {**self.discovery.last_trace, **self.crawler.last_trace}
        return results

    def _search_head(self, head: str, cfg: WebToolConfig, direct_canonical: str | None) -> list[dict]:
        provider_results: list[dict] = []

        if cfg.allow_bing and "bing" not in self.unavailable_sources:
            try:
                provider_results.extend(self._fetch_bing(head, cfg))
            except Exception:
                pass
        if cfg.allow_baidu and "baidu" not in self.unavailable_sources:
            try:
                provider_results.extend(self._fetch_baidu(head, cfg))
            except Exception:
                pass
        if cfg.allow_direct and direct_canonical:
            try:
                provider_results.extend(self._fetch_direct(head, direct_canonical, cfg))
            except Exception:
                pass

        for item in provider_results[:cfg.results_per_head]:
            item["query_head"] = head
            item["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return provider_results[: cfg.results_per_head * 3]

    def _discover(self, heads: list[str], cfg: WebToolConfig) -> list[dict]:
        if not (cfg.allow_bing_rss or cfg.allow_duckduckgo):
            return []
        results = []
        for head in heads[:2]:
            found = self.discovery.discover(head, cfg)
            for item in found:
                item["query_head"] = head
            results.extend(found)
        return results[: cfg.max_injected_results * 2]

    def _crawl(
        self,
        discovery_results: list[dict],
        direct_canonical: str | None,
        heads: list[str],
        cfg: WebToolConfig,
    ) -> list[dict]:
        if not cfg.allow_katana:
            return []
        seed_urls = [item.get("url") for item in discovery_results if item.get("url")]
        seed_urls = seed_urls[: cfg.katana_max_pages]
        if direct_canonical:
            seed_urls.extend(self.direct_registry.urls_for(direct_canonical))
        records = self.crawler.crawl(seed_urls, cfg)
        items = self.content_extractor.extract_many(records)
        head = heads[0] if heads else ""
        for item in items:
            item["query_head"] = head
        return items

    def _fetch_bing(self, head: str, cfg: WebToolConfig) -> list[dict]:
        text = self._get("https://cn.bing.com/search", {"q": head}, cfg, "bing")
        return BingParser().parse(text)

    def _fetch_baidu(self, head: str, cfg: WebToolConfig) -> list[dict]:
        text = self._get("https://www.baidu.com/s", {"wd": head}, cfg, "baidu")
        return BaiduParser().parse(text)

    def _fetch_direct(self, head: str, canonical: str, cfg: WebToolConfig) -> list[dict]:
        results = []
        for url in self.direct_registry.urls_for(canonical):
            try:
                resp = self.session.get(url, timeout=cfg.timeout_seconds)
                resp.raise_for_status()
                title_match = re.search(r"<title[^>]*>(.*?)</title>", resp.text or "", re.IGNORECASE | re.DOTALL)
                title = _clean_html(title_match.group(1)) if title_match else canonical
                results.append({
                    "title": title or canonical,
                    "url": url,
                    "snippet": "官网/百科直抓兜底结果",
                    "source": "direct",
                })
            except Exception:
                continue
        return results

    def _get(self, url: str, params: dict, cfg: WebToolConfig, source: str) -> str:
        params = {k: v for k, v in params.items() if v}
        try:
            resp = self.session.get(url, params=params, timeout=cfg.timeout_seconds)
        except Exception:
            self.unavailable_sources.add(source)
            raise
        if resp.status_code in (403, 429):
            self.unavailable_sources.add(source)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.text


def _clean_html(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()
