import json
import os
import re
from urllib.parse import urlsplit

from sana.services.llm_official_source_judge import LLMOfficialSourceJudge


class OfficialSourceLearner:
    """Learn official source URLs from search candidates and persist them."""

    def __init__(
        self,
        file_path: str = "user_profile.json",
        judge: LLMOfficialSourceJudge | None = None,
    ):
        self.file_path = file_path
        self.judge = judge
        self.last_judge_trace: dict = {}

    def urls_for(self, canonical: str) -> list[str]:
        if not canonical:
            return []
        data = self._load()
        sources = data.get("official_sources", {}) if isinstance(data, dict) else {}
        return list(sources.get(canonical, []))

    def validate_learned(
        self,
        canonical: str,
        context_terms: list[str] | None = None,
    ) -> list[str]:
        if self.judge is None:
            return self.urls_for(canonical)
        stored = self.urls_for(canonical)
        if not stored:
            return []
        kept = []
        for url in stored:
            verdict = self.judge.judge(
                {
                    "url": url,
                    "title": canonical,
                    "snippet": "",
                },
                canonical,
                context_terms=context_terms or [],
            )
            if verdict is None:
                kept.append(url)
            elif verdict.get("official") and verdict.get("confidence", 0) >= 0.8:
                kept.append(url)
        if kept != stored:
            self.learn(canonical, kept)
        return kept

    def learn(self, canonical: str, urls: list[str]) -> None:
        clean_urls = list(dict.fromkeys(str(url).strip() for url in (urls or []) if str(url).strip()))
        if not canonical or not clean_urls:
            return
        data = self._load()
        if not isinstance(data, dict):
            data = {}
        sources = data.setdefault("official_sources", {})
        sources[canonical] = clean_urls
        self._save(data)

    def recognize_from_candidates(
        self,
        candidates: list,
        canonical: str,
        aliases: list[str] | None = None,
        context_terms: list[str] | None = None,
    ) -> list[str]:
        if not canonical:
            return []
        terms = {
            str(term).lower()
            for term in [canonical, *(aliases or [])]
            if str(term).strip()
        }
        context_terms = [str(term).lower() for term in (context_terms or []) if str(term).strip()]
        best_by_host: dict[str, tuple[int, str]] = {}

        for item in candidates:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            snippet = str(item.get("snippet") or "")
            text = f"{title} {snippet}".lower()
            host = _host(url)
            if not url or not host:
                continue
            context_match = any(term in text for term in context_terms)
            if context_terms and not context_match:
                continue

            score = 0
            if any(term and term in text for term in terms):
                score += 15
            if any(hint in text for hint in ("官网", "官方", "official", "公告", "新闻中心")):
                score += 30
            if _brand_match(host, terms):
                score += 25
            if _is_root(url):
                score += 5

            if score >= 30:
                current = best_by_host.get(host)
                if current is None or score > current[0]:
                    best_by_host[host] = (score, url)

        ranked = sorted(best_by_host.values(), key=lambda entry: entry[0], reverse=True)
        if self.judge is None:
            self.last_judge_trace = {
                "judged_count": 0,
                "official_urls": [url for _, url in ranked],
                "fallback": True,
            }
            return [url for _, url in ranked]
        official_urls = []
        fallback = False
        for score, url in ranked[:5]:
            candidate = next((item for item in candidates if item.get("url") == url), {})
            verdict = self.judge.judge(
                candidate,
                canonical,
                context_terms=context_terms,
            )
            if verdict is None:
                fallback = True
                official_urls.append(url)
            elif verdict.get("official") and verdict.get("confidence", 0) >= 0.8:
                official_urls.append(url)
        self.last_judge_trace = {
            "judged_count": len(ranked[:5]),
            "official_urls": official_urls,
            "fallback": fallback,
        }
        return official_urls

    def _load(self) -> dict:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.file_path)


def _host(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def _is_root(url: str) -> bool:
    try:
        parts = urlsplit(url or "")
        return not parts.path or parts.path == "/"
    except ValueError:
        return False


def _brand_match(host: str, terms: set[str]) -> bool:
    host_tokens = {token for token in re.split(r"[^a-z0-9]+", host) if token}
    for term in terms:
        term_tokens = {token for token in re.split(r"[^a-z0-9]+", term) if token}
        if any(token in host_tokens for token in term_tokens):
            return True
        if term in host or host in term:
            return True
    return False
