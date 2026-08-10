import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit


class RetrievalScorer:
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

    def merge(
        self,
        results: list[dict],
        max_results: int = 8,
        canonical_terms: list[str] | None = None,
        current_time: str = "",
    ) -> list[dict]:
        canonical_terms = [t for t in (canonical_terms or []) if t]
        best_by_key: dict[str, dict] = {}
        for index, item in enumerate(results):
            freshness_score, freshness_note = self._freshness(item, current_time)
            item["_freshness_score"] = freshness_score
            item["_freshness_note"] = freshness_note
            item["_score"] = self._score(item, index, canonical_terms) + freshness_score
            key = normalize_url(item.get("url", "")) or item.get("title", "").strip().lower()
            if not key:
                continue
            if key not in best_by_key or item["_score"] > best_by_key[key]["_score"]:
                best_by_key[key] = item

        ranked = sorted(best_by_key.values(), key=lambda x: x["_score"], reverse=True)
        output = []
        for i, item in enumerate(ranked[:max_results]):
            clean = {k: v for k, v in item.items() if not k.startswith("_")}
            clean["rank"] = i + 1
            clean["score"] = round(item["_score"], 2)
            clean["freshness_score"] = round(item["_freshness_score"], 2)
            clean["freshness_note"] = item["_freshness_note"]
            output.append(clean)
        return output

    def _score(self, item: dict, position: int, canonical_terms: list[str]) -> float:
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        text = f"{title} {snippet}"
        query_terms = _terms(item.get("query_head", ""))
        score = 0.0

        if any(term and term in text for term in canonical_terms):
            score += 30.0
        if any(term and term in text for term in query_terms):
            score += 20.0

        host = _host(item.get("url", ""))
        if host in self.QUALITY_HOSTS or any(host.endswith("." + q) for q in ("qq.com", "mihoyo.com")):
            score += 15.0
        if 20 <= len(snippet) <= 150:
            score += 5.0
        score -= position * 0.5
        return max(0.0, score)

    def _freshness(self, item: dict, current_time: str) -> tuple[float, str]:
        text = " ".join([
            item.get("title", ""),
            item.get("snippet", ""),
            item.get("url", ""),
        ])
        current_year = self._current_year(current_time)
        years = set()
        for match in re.finditer(r"(?:^|[\s/年.])(20\d{2})(?:[\s/年.]|$)", text):
            years.add(int(match.group(1)))

        score = 0.0
        notes = []
        for year in sorted(years):
            if year == current_year:
                score += 8.0
                notes.append("当前年份")
            elif year > current_year:
                score += 4.0
                notes.append("较新年份")
            else:
                score -= (current_year - year) * 4.0
                notes.append(f"旧年份 {year}")
        if any(k in text for k in ("今日", "最新", "更新")):
            score += 3.0
            notes.append("时效词")
        if re.search(r"\b\d+\.\d+\b", text):
            score += 2.0
            notes.append("含版本号")
        return max(-10.0, min(15.0, score)), "; ".join(notes)

    @staticmethod
    def _current_year(current_time: str) -> int:
        match = re.search(r"(20\d{2})", current_time or "")
        if match:
            return int(match.group(1))
        return datetime.now().year


def _terms(query: str) -> list[str]:
    return [t for t in re.split(r"[\s,，。；;、|/]+", query or "") if t]


def _host(url: str) -> str:
    try:
        parts = urlsplit(url)
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), "", ""))
    except ValueError:
        return (url or "").strip().lower()
