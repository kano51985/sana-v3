import html
import re


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


def _clean_html(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()
