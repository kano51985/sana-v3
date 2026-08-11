import html as html_lib
import json
import os
import re
import shutil
import subprocess
import time
from urllib.parse import urljoin, urlsplit

from sana.models.search import CrawlTask
from sana.services.web_tool_config import WebToolConfig


LOW_VALUE_SUFFIXES = (
    ".css", ".js", ".mjs", ".json", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map",
)
LOW_VALUE_SEGMENTS = {
    "api", "assets", "static", "images", "img", "media", "css", "js",
    "uploads", "files", "fonts",
}
LOW_VALUE_HOSTS = {"beian.miit.gov.cn"}


class KatanaCrawler:
    def __init__(self):
        self.last_trace: dict = {}

    def crawl(
        self,
        seed_urls: list[str] | list[CrawlTask],
        config: WebToolConfig,
        deadline: float | None = None,
    ) -> list[dict]:
        self.last_trace = {}
        tasks = self._normalize_tasks(seed_urls)
        self.last_trace["crawl_tasks"] = [task.to_dict() for task in tasks]
        if not config.allow_katana or not tasks:
            return []
        allowed_tasks = [task for task in tasks if self._is_allowed(task.url, config)]
        if not allowed_tasks:
            return []
        self.last_trace["crawl_sources"] = [task.url for task in allowed_tasks]

        records = []
        per_seed_pages = max(1, config.katana_max_pages // len(allowed_tasks))
        binary = self._resolve_bin(config)
        self.last_trace["katana_resolved"] = binary
        if deadline is None:
            deadline = time.monotonic() + max(
                10.0,
                float(getattr(config, "katana_total_timeout_seconds", 20.0) or 20.0),
            )
        slow_hosts: set[str] = set()
        for task in allowed_tasks[:20]:
            task_host = urlsplit(task.url).hostname.lower() if task.url else ""
            if task_host in slow_hosts:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.last_trace["katana_error"] = "katana total crawl budget exceeded"
                break
            process_timeout = min(
                max(15.0, config.katana_timeout_seconds + 10),
                remaining,
            )
            depth = 1 if task.mode == "article_mode" else max(2, config.katana_max_depth)
            if task.mode == "site_mode":
                depth = max(3, depth)
            command = [
                binary,
                "-u",
                task.url,
                "-d",
                str(depth),
                "-jc",
                "-j",
                "-silent",
                "-timeout",
                str(int(config.katana_timeout_seconds)),
                "-concurrency",
                str(config.katana_concurrency),
            ]
            if task.mode == "site_mode":
                command.extend(["-kf", "all"])
            try:
                proc = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=max(1.0, process_timeout),
                )
                stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
                stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
                stderr_text = stderr.strip()
                if proc.returncode != 0 or (not stdout.strip() and stderr_text):
                    self.last_trace["katana_available"] = False
                    error_text = stderr_text or stdout.strip()
                    error_text = re.sub(r"\x1b\[[0-9;]*m", "", error_text)
                    self.last_trace["katana_error"] = (
                        error_text[-500:]
                        or f"katana exited with code {proc.returncode}"
                    )
                    continue
                self.last_trace["katana_available"] = True
                records.extend(self._parse_output(stdout, config))
            except subprocess.TimeoutExpired as exc:
                if task_host:
                    slow_hosts.add(task_host)
                self.last_trace["katana_available"] = False
                self.last_trace["katana_error"] = str(exc)
            except Exception as exc:
                self.last_trace["katana_available"] = False
                self.last_trace["katana_error"] = str(exc)

        self.last_trace["katana_records"] = len(records)
        self.last_trace["katana_skipped_slow_hosts"] = sorted(slow_hosts)
        self.last_trace["katana_visited_urls"] = list(
            dict.fromkeys(record.get("url", "") for record in records if record.get("url"))
        )
        return records[: config.katana_max_pages]

    def extract_relevant_links(
        self,
        records: list[dict],
        keywords: list[str],
        config: WebToolConfig,
    ) -> list[dict]:
        clean_keywords = [str(keyword).lower() for keyword in (keywords or []) if str(keyword).strip()]
        if not clean_keywords:
            return []
        links = []
        seen = set()
        for record in records:
            base = str(record.get("url", "") or "")
            page_html = self._extract_html(record) or ""
            for href, anchor_text in _iter_anchors(page_html):
                url = urljoin(base, href)
                if not url.startswith(("http://", "https://")) or not self._is_allowed(url, config):
                    continue
                if self._is_low_value_url(url):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                haystack = f"{anchor_text} {url}".lower()
                if any(keyword in haystack for keyword in clean_keywords):
                    clean_text = _clean_fragment(anchor_text)
                    links.append({
                        "url": url,
                        "title": clean_text,
                        "snippet": clean_text[:200],
                        "source": "katana_link",
                    })
        return links

    @staticmethod
    def _normalize_tasks(seed_urls: list[str] | list[CrawlTask]) -> list[CrawlTask]:
        if not seed_urls:
            return []
        if isinstance(seed_urls[0], CrawlTask):
            return seed_urls
        return [
            CrawlTask(url=str(url), mode="article_mode", priority=0.0, reason="seed")
            for url in seed_urls
            if url
        ]

    @staticmethod
    def _resolve_bin(config: WebToolConfig) -> str:
        candidate = config.katana_bin or "katana"
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        if candidate == "katana" and os.name == "nt":
            fallback = r"D:\Tools\katana\katana.exe"
            if os.path.exists(fallback):
                return fallback
        return candidate

    def _parse_output(self, output: str, config: WebToolConfig) -> list[dict]:
        records = []
        for line in (output or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            url = self._extract_url(data)
            if not url or not self._is_allowed(url, config) or self._is_low_value_url(url):
                continue
            records.append({
                "url": url,
                "title": str(data.get("title") or ""),
                "snippet": str(data.get("snippet") or ""),
                "source": "katana",
                "html": self._extract_html(data),
            })
        return records

    @staticmethod
    def _extract_url(data: dict) -> str:
        for key in ("endpoint", "url", "path"):
            value = data.get(key)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()
        request = data.get("request")
        if isinstance(request, dict):
            value = request.get("endpoint") or request.get("url")
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()
        return ""

    @staticmethod
    def _extract_html(data: dict) -> str:
        for key in ("html", "body", "content"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        response = data.get("response")
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            for key in ("body", "content", "html"):
                value = response.get(key)
                if isinstance(value, str):
                    return value
        return ""

    def _is_allowed(self, url: str, config: WebToolConfig) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or "").lower()
        allowed = [domain.lower() for domain in config.katana_allowed_domains if domain]
        if not allowed:
            return True
        return any(host == domain or host.endswith("." + domain) for domain in allowed)

    @staticmethod
    def _is_low_value_url(url: str) -> bool:
        try:
            parts = urlsplit(url or "")
        except ValueError:
            return True
        host = (parts.hostname or "").lower()
        if host in LOW_VALUE_HOSTS:
            return True
        path = (parts.path or "").lower()
        if path.endswith(LOW_VALUE_SUFFIXES):
            return True
        segments = [segment for segment in path.split("/") if segment]
        if any(segment in LOW_VALUE_SEGMENTS for segment in segments):
            return True
        return False


def _iter_anchors(html_text: str):
    pattern = re.compile(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html_text or ""):
        href = match.group(1).strip()
        anchor_text = re.sub(r"<[^>]+>", " ", match.group(2))
        yield href, anchor_text


def _clean_fragment(text: str) -> str:
    return html_lib.unescape(re.sub(r"\s+", " ", text or "")).strip()
