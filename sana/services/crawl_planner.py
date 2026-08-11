from urllib.parse import urlsplit, urlunsplit

from sana.models.search import CrawlTask
from sana.services.snippet_ranker import SnippetFirstRanker


class CrawlPlanner:
    MIN_SCORE = SnippetFirstRanker.MIN_SCORE

    def plan(
        self,
        candidates: list[dict],
        official_urls: list[str] | None = None,
        max_tasks: int = 20,
        query_heads: list[str] | None = None,
        fact_types: list[str] | None = None,
    ) -> list[CrawlTask]:
        articles = []
        categories = []
        unknowns = []
        intent = self._intent_from_facts(fact_types) if fact_types else self._intent(query_heads)
        seen = set()

        for item in candidates:
            url = str(item.get("url", "") or "")
            if not url:
                continue
            key = _normalize_url(url)
            if key in seen:
                continue
            seen.add(key)

            kind = item.get("_url_kind", "unknown")
            score = float(item.get("_candidate_score") or item.get("_snippet_score") or 0)
            priority = self._priority(item, score, intent)
            if kind == "site_homepage":
                continue
            if kind == "article" and score >= self.MIN_SCORE:
                articles.append(self._task(item, "article_mode", priority))
            elif kind == "category" and score >= self.MIN_SCORE:
                categories.append(self._task(item, "article_mode", priority * 0.7))
            elif kind == "unknown" and score >= self.MIN_SCORE + 10:
                unknowns.append(self._task(item, "article_mode", priority * 0.5))

        articles.sort(key=lambda task: task.priority, reverse=True)
        categories.sort(key=lambda task: task.priority, reverse=True)
        unknowns.sort(key=lambda task: task.priority, reverse=True)

        tasks = list(articles)
        if not tasks:
            tasks.extend(categories)
        if not tasks:
            tasks.extend(unknowns)

        seen = {_normalize_url(task.url) for task in tasks}
        for url in official_urls or []:
            if url and _normalize_url(url) not in seen:
                tasks.append(CrawlTask(
                    url=url,
                    mode="site_mode",
                    priority=50.0,
                    reason="官方来源站点头，用于发现相关页面",
                ))
                seen.add(_normalize_url(url))

        if max_tasks > len(tasks):
            chosen = {_normalize_url(task.url) for task in tasks}
            for item in sorted(
                candidates,
                key=lambda value: float(
                    value.get("_candidate_score") or value.get("_snippet_score") or 0
                ),
                reverse=True,
            ):
                url = str(item.get("url") or "")
                key = _normalize_url(url)
                if not url or key in chosen:
                    continue
                if item.get("_url_kind") == "site_homepage":
                    continue
                if item.get("_context_match") is False:
                    continue
                score = float(item.get("_candidate_score") or item.get("_snippet_score") or 0)
                if score < 10:
                    continue
                tasks.append(
                    self._task(item, "article_mode", self._priority(item, score, intent))
                )
                chosen.add(key)
                if len(tasks) >= max_tasks:
                    break

        return tasks[:max(0, max_tasks)]

    @staticmethod
    def _priority(item: dict, score: float, intent: str) -> float:
        priority = score
        content_type = item.get("_content_type", "")
        if item.get("_official"):
            priority += 15.0
        if intent == "official" and content_type in ("version", "character", "news"):
            priority += 15.0
        elif intent == "guide" and content_type in ("guide", "forum", "wiki"):
            priority += 15.0
        elif content_type in ("article", "news", "version", "character"):
            priority += 5.0
        return priority

    @staticmethod
    def _intent(query_heads: list[str] | None) -> str:
        text = " ".join(query_heads or []).lower()
        if any(keyword in text for keyword in ("版本", "更新", "角色", "公告", "新闻", "新")):
            return "official"
        if any(keyword in text for keyword in ("配队", "攻略", "推荐", "队伍", "阵容", "build", "guide")):
            return "guide"
        return "mixed"

    @staticmethod
    def _intent_from_facts(fact_types: list[str]) -> str:
        if any(
            fact_type in ("version", "patch_notes", "character_changes")
            for fact_type in fact_types
        ):
            return "official"
        if any(
            fact_type in ("team_meta", "guide")
            for fact_type in fact_types
        ):
            return "guide"
        return "mixed"

    @staticmethod
    def _task(item: dict, mode: str, priority: float) -> CrawlTask:
        return CrawlTask(
            url=str(item.get("url", "") or ""),
            mode=mode,
            priority=round(priority, 2),
            reason=str(item.get("_classify_reason") or item.get("_snippet_note") or mode),
        )


def _normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        query = "&".join(sorted(parts.query.split("&"))) if parts.query else ""
        return urlunsplit((parts.scheme.lower(), host, (parts.path or "").rstrip("/"), query, ""))
    except ValueError:
        return (url or "").strip().lower()
