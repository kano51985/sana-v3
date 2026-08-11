from dataclasses import asdict, dataclass, field


@dataclass
class SearchResult:
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""
    published_at: str = ""
    fetched_at: str = ""
    query_head: str = ""
    url_kind: str = "unknown"
    entity_mentions: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CrawlTask:
    url: str = ""
    mode: str = "article_mode"
    priority: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContentDocument:
    title: str = ""
    url: str = ""
    text: str = ""
    published_at: str = ""
    version: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CandidateClassification:
    url_kind: str = "unknown"
    relevance: float = 0.0
    entity_match: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
