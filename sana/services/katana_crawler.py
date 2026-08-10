import json
import os
import shutil
import subprocess
from urllib.parse import urlsplit

from sana.services.web_tool_config import WebToolConfig


class KatanaCrawler:
    def __init__(self):
        self.last_trace: dict = {}

    def crawl(self, seed_urls: list[str], config: WebToolConfig) -> list[dict]:
        self.last_trace = {}
        if not config.allow_katana or not seed_urls:
            return []
        allowed_seeds = [url for url in seed_urls if self._is_allowed(url, config)]
        if not allowed_seeds:
            return []
        self.last_trace["crawl_sources"] = allowed_seeds

        records = []
        per_seed_pages = max(1, config.katana_max_pages // len(allowed_seeds))
        binary = self._resolve_bin(config)
        self.last_trace["katana_resolved"] = binary
        for seed in allowed_seeds[:20]:
            command = [
                binary,
                "-u",
                seed,
                "-d",
                str(config.katana_max_depth),
                "-jc",
                "-json",
                "-silent",
                "-timeout",
                str(int(config.katana_timeout_seconds)),
                "-concurrency",
                str(config.katana_concurrency),
            ]
            try:
                proc = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=config.katana_timeout_seconds + 5,
                )
                self.last_trace["katana_available"] = True
                records.extend(self._parse_output(proc.stdout, config))
            except Exception as exc:
                self.last_trace["katana_available"] = False
                self.last_trace["katana_error"] = str(exc)

        self.last_trace["katana_records"] = len(records)
        return records[: config.katana_max_pages]

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
            if not url or not self._is_allowed(url, config):
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
        response = data.get("response")
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            value = response.get("body") or response.get("content") or response.get("html")
            return value if isinstance(value, str) else ""
        return ""

    def _is_allowed(self, url: str, config: WebToolConfig) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme != "https":
            return False
        host = (parts.hostname or "").lower()
        allowed = [domain.lower() for domain in config.katana_allowed_domains if domain]
        if not allowed:
            return True
        return any(host == domain or host.endswith("." + domain) for domain in allowed)
