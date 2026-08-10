import html
import re
import time
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import requests

from sana.services.web_tool_config import WebToolConfig


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class BingRssParser:
    def parse(self, xml_text: str) -> list[dict]:
        try:
            root = ET.fromstring((xml_text or "").strip())
        except ET.ParseError:
            return []
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        results = []
        for item in items:
            title = _find_text(item, ("title",))
            url = _find_link(item)
            snippet = _find_text(item, ("description", "summary", "content"))
            published = _find_text(item, ("pubDate", "published", "updated"))
            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "source": "bing_rss",
                    "published": published,
                    "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
        return results


class DuckDuckGoParser:
    def parse(self, html_text: str) -> list[dict]:
        results = []
        pattern = re.compile(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(html_text or ""):
            url = self._decode_url(html.unescape(match.group(1)).strip())
            title = _clean_html(match.group(2))
            if not url.startswith(("http://", "https://")) or not title:
                continue
            tail = (html_text or "")[match.end():match.end() + 3000]
            snippet_match = re.search(
                r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
                tail,
                flags=re.IGNORECASE | re.DOTALL,
            )
            results.append({
                "title": title,
                "url": url,
                "snippet": _clean_html(snippet_match.group(1)) if snippet_match else "",
                "source": "duckduckgo",
                "published": "",
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        return results

    @staticmethod
    def _decode_url(url: str) -> str:
        if "uddg=" in url:
            match = re.search(r"[?&]uddg=([^&]+)", url)
            if match:
                return unquote(match.group(1))
        return url


class SearchDiscoveryService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.last_trace: dict = {}

    def discover(self, query: str, config: WebToolConfig) -> list[dict]:
        results = []
        sources = []
        if config.allow_bing_rss:
            try:
                results.extend(self._fetch_bing_rss(query, config))
                sources.append("bing_rss")
            except Exception as exc:
                self.last_trace["bing_rss_error"] = str(exc)
        if config.allow_duckduckgo:
            try:
                results.extend(self._fetch_duckduckgo(query, config))
                sources.append("duckduckgo")
            except Exception as exc:
                self.last_trace["duckduckgo_error"] = str(exc)
        self.last_trace["discovery_sources"] = sources
        self.last_trace["discovery_count"] = len(results)
        return results

    def _fetch_bing_rss(self, query: str, config: WebToolConfig) -> list[dict]:
        resp = self.session.get(
            "https://www.bing.com/search",
            params={"format": "rss", "q": query},
            timeout=config.timeout_seconds,
        )
        resp.raise_for_status()
        return BingRssParser().parse(resp.text)

    def _fetch_duckduckgo(self, query: str, config: WebToolConfig) -> list[dict]:
        resp = self.session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=config.timeout_seconds,
        )
        resp.raise_for_status()
        return DuckDuckGoParser().parse(resp.text)


def _find_text(item: ET.Element, tags: tuple[str, ...]) -> str:
    for element in item.iter():
        name = element.tag.rsplit("}", 1)[-1].lower()
        if name in tags and element.text and element.text.strip():
            return _clean_html(element.text)
    return ""


def _find_link(item: ET.Element) -> str:
    for element in item.iter():
        name = element.tag.rsplit("}", 1)[-1].lower()
        if name != "link":
            continue
        href = element.attrib.get("href", "").strip()
        if href:
            return href
        if element.text and element.text.strip().startswith(("http://", "https://")):
            return element.text.strip()
    return ""


def _clean_html(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()
