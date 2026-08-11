import re
from urllib.parse import parse_qs, urlsplit

from sana.models.search import CandidateClassification, SearchResult


class CandidateClassifier:
    def classify_many(
        self,
        candidates: list,
        entity_terms: list[str] | None = None,
        question: str = "",
    ) -> list[dict]:
        classified = []
        for item in candidates:
            data = item.to_dict() if isinstance(item, SearchResult) else dict(item)
            verdict = self.classify(data, entity_terms, question)
            data["_url_kind"] = verdict.url_kind
            data["_relevance"] = verdict.relevance
            data["_entity_match"] = verdict.entity_match
            data["_classify_reason"] = verdict.reason
            classified.append(data)
        return classified

    def classify(
        self,
        item: dict,
        entity_terms: list[str] | None = None,
        question: str = "",
    ) -> CandidateClassification:
        url = str(item.get("url", "") or "")
        title = str(item.get("title", "") or "")
        snippet = str(item.get("snippet", "") or "")
        text = f"{title} {snippet}"
        entity_terms = [str(t).strip() for t in (entity_terms or []) if str(t).strip()]
        entity_match = any(t and _contains(text, t) for t in entity_terms)

        url_kind = self._classify_url(url, text)
        fact_signal = bool(re.search(
            r"(20\d{2}[-年/]\d{1,2}|v?\d+\.\d+|更新|版本|角色|英雄|配队|攻略|资讯|公告)",
            text,
            re.IGNORECASE,
        ))
        if url_kind == "unknown" and entity_match and fact_signal:
            url_kind = "article"

        relevance = 0.35 if entity_match else 0.0
        if url_kind == "article":
            relevance += 0.35
        elif url_kind == "category":
            relevance -= 0.05
        elif url_kind == "site_homepage":
            relevance -= 0.20

        question_terms = _terms(question)
        if any(t and _contains(text, t) for t in question_terms):
            relevance += 0.15
        relevance = max(0.0, min(1.0, relevance))

        reasons = []
        if entity_match:
            reasons.append("标题/摘要包含解析实体")
        if url_kind == "article":
            reasons.append("URL 或内容符合文章页特征")
        elif url_kind == "category":
            reasons.append("URL 更像栏目/分类页")
        elif url_kind == "site_homepage":
            reasons.append("URL 是站点头/首页")
        if not reasons:
            reasons.append("未识别为文章页")
        return CandidateClassification(
            url_kind=url_kind,
            relevance=round(relevance, 2),
            entity_match=entity_match,
            reason="; ".join(reasons),
        )

    @staticmethod
    def _classify_url(url: str, text: str) -> str:
        try:
            parts = urlsplit(url)
        except ValueError:
            return "unknown"
        path = parts.path or ""
        segments = [s for s in path.split("/") if s]
        depth = len(segments)
        basename = segments[-1].lower() if segments else ""
        query = parse_qs(parts.query or "")

        if not segments:
            return "site_homepage"
        if basename in ("", "index.html", "index.htm", "index.php", "default.html", "home"):
            return "site_homepage"
        if depth <= 1 and (
            "首页" in text
            or "官网" in text
            or "主页" in text
            or basename in ("news.shtml", "news.html", "list.html", "category.html")
        ):
            return "site_homepage"

        article_query_keys = {
            "id",
            "article",
            "newsid",
            "aid",
            "docid",
            "itemid",
            "sku",
            "postid",
        }
        if any(key in query for key in article_query_keys):
            return "article"

        first = segments[0].lower()
        category_first = first in {
            "news",
            "list",
            "category",
            "categories",
            "archives",
            "column",
            "topic",
            "tag",
            "search",
            "download",
            "library",
            "heroes",
            "hero",
        }
        if category_first and depth <= 1:
            return "category"
        if basename.endswith((".shtml", ".html", ".htm")) and depth <= 2 and not article_query_keys:
            return "category"

        article_segments = {
            "article",
            "articles",
            "post",
            "posts",
            "detail",
            "details",
            "content",
            "a",
            "p",
            "info",
            "docs",
            "wiki",
            "game",
            "news",
            "update",
            "version",
            "character",
            "team",
        }
        if depth >= 2 and any(seg.lower() in article_segments for seg in segments):
            return "article"
        if re.search(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{8})", path):
            return "article"
        return "unknown"


def _terms(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,，。；;、|/]+", text or "") if t]


def _contains(text: str, term: str) -> bool:
    return term.lower() in (text or "").lower()
