import re
from urllib.parse import urlsplit


OFFICIAL_HINTS = ("官网", "官方", "official", "公告", "新闻中心")
VERSION_HINTS = ("版本", "更新", "公告", "ver.", "version")
CHARACTER_HINTS = ("角色", "新角色", "character")
GUIDE_HINTS = ("配队", "攻略", "阵容", "队伍", "推荐", "build", "guide")
FORUM_HINTS = ("forum", "bbs", "thread", "topic", "社区")
WIKI_HINTS = ("wiki", "fandom", "百科")
NEWS_HINTS = ("news", "article", "post", "公告", "新闻", "资讯")


class CandidateScorer:
    """Lightweight hybrid scorer used before crawling and LLM reranking."""

    def rank(
        self,
        candidates: list,
        user_input: str = "",
        query_heads: list[str] | None = None,
        entity_terms: list[str] | None = None,
        current_time: str = "",
        official_domains: set[str] | None = None,
        context_terms: list[str] | None = None,
        required_page_types: list[str] | None = None,
    ) -> list[dict]:
        query_heads = query_heads or []
        entity_terms = [str(t).strip() for t in (entity_terms or []) if str(t).strip()]
        official_domains = {str(d).lower() for d in (official_domains or [])}

        for item in candidates:
            self._score(
                item,
                user_input=user_input,
                query_heads=query_heads,
                entity_terms=entity_terms,
                current_time=current_time,
                official_domains=official_domains,
                context_terms=context_terms or [],
                required_page_types=required_page_types or [],
            )
        return sorted(
            candidates,
            key=lambda item: float(item.get("_candidate_score") or 0),
            reverse=True,
        )

    def _score(
        self,
        item: dict,
        user_input: str,
        query_heads: list[str],
        entity_terms: list[str],
        current_time: str,
        official_domains: set[str],
        context_terms: list[str],
        required_page_types: list[str],
    ) -> None:
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")
        snippet = str(item.get("snippet") or "")
        text = f"{title} {snippet}"
        text_lower = text.lower()
        host = _host(url)

        entity_match = bool(item.get("_entity_match"))
        if not entity_match:
            entity_match = any(term and term.lower() in text_lower for term in entity_terms)

        query_terms = _terms(" ".join([user_input, *query_heads]))
        query_hits = sum(1 for term in query_terms if term and term.lower() in text_lower)
        context_match = any(
            term and term.lower() in text_lower for term in context_terms
        )

        content_type = self._content_type(url, text, item.get("_url_kind", "unknown"))
        official = host in official_domains or self._looks_official(text_lower, entity_match, query_hits)
        score = 0.0
        notes = []

        if entity_match:
            score += 25.0
            notes.append("实体命中")
        if query_hits:
            score += min(20.0, query_hits * 5.0)
            notes.append(f"query 命中 {query_hits}")
        if context_terms and not context_match:
            score -= 40.0
            notes.append("缺少上下文实体")
        if required_page_types and content_type not in required_page_types and content_type != "unknown":
            score -= 15.0
            notes.append("页面类型与事实意图不匹配")
        if official:
            score += 20.0
            notes.append("官方/可信源")
        if host in official_domains:
            score += 10.0
            notes.append("已识别官方域名")

        if content_type == "version":
            score += 18.0
            notes.append("版本页")
        elif content_type == "guide":
            score += 18.0
            notes.append("攻略/配队")
        elif content_type == "character":
            score += 15.0
            notes.append("角色页")
        elif content_type == "news":
            score += 12.0
            notes.append("新闻/公告")
        elif content_type == "wiki":
            score += 10.0
            notes.append("Wiki")
        elif content_type == "forum":
            score += 6.0
            notes.append("论坛")
        elif content_type == "homepage":
            score -= 15.0
            notes.append("首页")

        if 30 <= len(snippet) <= 300:
            score += 5.0
            notes.append("摘要可用")
        if re.search(r"(20\d{2}[-/年]\d{1,2}|v?\d+\.\d+)", text, re.IGNORECASE):
            score += 5.0
            notes.append("含时间/版本号")
        if _has_current_year(text, current_time):
            score += 8.0
            notes.append("当年发布")

        score = max(0.0, min(100.0, score))
        item["_candidate_score"] = round(score, 2)
        item["_snippet_score"] = round(score, 2)
        item["_snippet_note"] = "; ".join(notes)
        item["_content_type"] = content_type
        item["_official"] = official
        item["_official_domain"] = host if official else ""
        item["_context_match"] = context_match

    @staticmethod
    def _content_type(url: str, text: str, url_kind: str) -> str:
        text_lower = text.lower()
        try:
            parts = urlsplit(url)
        except ValueError:
            return "unknown"
        path = parts.path or ""
        host = (parts.hostname or "").lower()

        if not path or path == "/":
            return "homepage"
        if host and any(term in host for term in ("forum", "bbs")):
            return "forum"
        if any(term in host for term in ("wiki", "fandom")):
            return "wiki"
        if any(term in path.lower() for term in ("thread", "topic", "forum", "bbs")):
            return "forum"
        if any(term in path.lower() for term in ("wiki", "fandom")):
            return "wiki"
        if any(term in text_lower for term in GUIDE_HINTS):
            return "guide"
        if any(term in text_lower for term in VERSION_HINTS):
            return "version"
        if any(term in text_lower for term in NEWS_HINTS):
            return "news"
        if any(term in text_lower for term in CHARACTER_HINTS):
            return "character"
        if url_kind == "article":
            return "article"
        if url_kind == "site_homepage":
            return "homepage"
        return "unknown"

    @staticmethod
    def _looks_official(text_lower: str, entity_match: bool, query_hits: int) -> bool:
        if not entity_match and query_hits <= 0:
            return False
        return any(hint in text_lower for hint in OFFICIAL_HINTS)


def _terms(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,，。；;、/]+", text or "") if t]


def _host(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def _has_current_year(text: str, current_time: str) -> bool:
    match = re.search(r"(20\d{2})", current_time or "")
    if not match:
        return False
    return match.group(1) in text
