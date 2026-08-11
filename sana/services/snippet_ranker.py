import re
from datetime import datetime
from urllib.parse import urlsplit


class SnippetFirstRanker:
    MIN_SCORE = 25.0
    MAX_PER_HEAD = 5
    QUALITY_HOSTS = {
        "pvp.qq.com",
        "lol.qq.com",
        "gp.qq.com",
        "jcc.qq.com",
        "ys.mihoyo.com",
        "baike.baidu.com",
        "zh.wikipedia.org",
        "zhihu.com",
    }

    def rank(
        self,
        candidates: list[dict],
        user_input: str = "",
        query_heads: list[str] | None = None,
        entity_terms: list[str] | None = None,
        current_time: str = "",
        max_per_head: int | None = None,
    ) -> list[dict]:
        query_heads = query_heads or []
        entity_terms = [str(t).strip() for t in (entity_terms or []) if str(t).strip()]
        limit = max(1, max_per_head or self.MAX_PER_HEAD)
        for item in candidates:
            head = str(item.get("query_head", "") or "")
            item["_snippet_score"], item["_snippet_note"] = self._score(
                item,
                user_input,
                head,
                entity_terms,
                current_time,
            )
        ranked = sorted(
            candidates,
            key=lambda item: float(item.get("_snippet_score") or 0),
            reverse=True,
        )
        output = []
        counts: dict[str, int] = {}
        for item in ranked:
            head = str(item.get("query_head", "") or "")
            if counts.get(head, 0) >= limit:
                continue
            counts[head] = counts.get(head, 0) + 1
            output.append(item)
        return output

    def _score(
        self,
        item: dict,
        user_input: str,
        query_head: str,
        entity_terms: list[str],
        current_time: str,
    ) -> tuple[float, str]:
        title = str(item.get("title", "") or "")
        snippet = str(item.get("snippet", "") or "")
        text = f"{title} {snippet}"
        score = 0.0
        notes = []

        if any(t and t.lower() in text.lower() for t in entity_terms):
            score += 30.0
            notes.append("实体命中")

        query_terms = _terms(f"{query_head} {user_input}")
        query_hits = sum(1 for t in query_terms if t and t.lower() in text.lower())
        if query_hits:
            score += min(20.0, query_hits * 5.0)
            notes.append(f"查询词命中 {query_hits}")

        kind = item.get("_url_kind", "unknown")
        if kind == "article":
            score += 25.0
            notes.append("文章页")
        elif kind == "category":
            score += 5.0
            notes.append("栏目页")
        elif kind == "site_homepage":
            score -= 30.0
            notes.append("站点头")

        if item.get("source") == "direct" or (item.get("raw") or {}).get("official"):
            score += 10.0
            notes.append("官方来源")
        if 30 <= len(snippet) <= 300:
            score += 5.0
            notes.append("摘要可用")

        published = str(item.get("published_at") or item.get("published") or "")
        current_year = _current_year(current_time)
        if current_year and re.search(str(current_year), published):
            score += 10.0
            notes.append("当年发布")
        if re.search(r"\b\d+\.\d+\b", text):
            score += 5.0
            notes.append("含版本号")

        host = _host(item.get("url", ""))
        if host in self.QUALITY_HOSTS or any(host.endswith("." + q) for q in ("qq.com", "mihoyo.com")):
            score += 10.0
            notes.append("可信域名")

        return max(0.0, min(100.0, score)), "; ".join(notes)


def _terms(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,，。；;、|/]+", text or "") if t]


def _host(url: str) -> str:
    try:
        parts = urlsplit(url)
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def _current_year(current_time: str) -> int:
    match = re.search(r"(20\d{2})", current_time or "")
    if match:
        return int(match.group(1))
    return datetime.now().year
