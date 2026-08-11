import html
import re
import time

from sana.models.search import ContentDocument


class ContentExtractor:
    def extract_many(self, records: list[dict]) -> list[dict]:
        results = []
        for record in records:
            item = self.extract(record.get("html", ""), record)
            if item:
                results.append(item)
        return results

    def extract_documents(self, records: list[dict]) -> list[ContentDocument]:
        return [self.extract_document(record.get("html", ""), record) for record in records]

    def extract_document(self, html_text: str, record: dict | None = None) -> ContentDocument:
        item = self.extract(html_text, record)
        return ContentDocument(
            title=item.get("title", ""),
            url=item.get("url", ""),
            text=item.get("text", item.get("snippet", "")),
            published_at=item.get("date", item.get("published_at", "")),
            version=item.get("version", ""),
            source=item.get("source", ""),
        )

    def extract(self, html_text: str, record: dict | None = None) -> dict:
        record = record or {}
        url = record.get("url", "")
        if html_text:
            title = self._extract_title(html_text) or record.get("title", "")
            text = self._extract_main_text(html_text)
            snippet = text[:200]
            date = self._extract_date(html_text)
            version = self._extract_version(html_text)
        else:
            title = record.get("title", "")
            text = record.get("text", record.get("snippet", ""))
            snippet = record.get("snippet", "")
            date = record.get("published", "")
            version = ""
        if not url and not title:
            return {}
        return {
            "title": title or url,
            "url": url,
            "text": text,
            "snippet": snippet,
            "source": record.get("source", "katana"),
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "date": date,
            "published_at": date,
            "version": version,
        }

    @staticmethod
    def _extract_title(html_text: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))).strip()
        return ""

    @staticmethod
    def _clean_text(html_text: str) -> str:
        text = re.sub(
            r"<script.*?</script>|<style.*?</style>",
            " ",
            html_text or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    @staticmethod
    def _extract_main_text(html_text: str) -> str:
        text = html_text or ""
        text = re.sub(
            r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>",
            " ",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            r"<(nav|header|footer|aside|form|button)[^>]*>.*?</\1>",
            " ",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        main_match = re.search(
            r"<main[^>]*>(.*?)</main>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if main_match:
            text = main_match.group(1)
        else:
            article_match = re.search(
                r"<article[^>]*>(.*?)</article>",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if article_match:
                text = article_match.group(1)
        return ContentExtractor._clean_text(text)

    @staticmethod
    def _extract_date(html_text: str) -> str:
        match = re.search(
            r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
            html_text,
        )
        return match.group(1) if match else ""

    @staticmethod
    def _extract_version(html_text: str) -> str:
        match = re.search(r"\b\d+\.\d+\b", html_text)
        return match.group(0) if match else ""
