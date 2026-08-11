from dataclasses import dataclass, asdict, field
import json
import os


@dataclass
class WebToolConfig:
    enabled: bool = True
    autonomy_level: int = 2
    max_query_heads: int = 3
    results_per_head: int = 3
    max_injected_results: int = 8
    timeout_seconds: float = 2.5
    total_timeout_seconds: float = 8.0
    provider_timeout_seconds: float = 8.0
    rerank_timeout_seconds: float = 8.0
    web_total_timeout_seconds: float = 40.0
    allow_bing: bool = True
    allow_baidu: bool = True
    allow_direct: bool = True
    allow_bing_rss: bool = True
    allow_duckduckgo: bool = True
    allow_searxng: bool = False
    searxng_url: str = ""
    searxng_timeout_seconds: float = 5.0
    allow_katana: bool = True
    katana_bin: str = "katana"
    katana_max_depth: int = 2
    katana_max_pages: int = 20
    katana_timeout_seconds: float = 5.0
    katana_total_timeout_seconds: float = 20.0
    katana_concurrency: int = 3
    katana_allowed_domains: list[str] = field(default_factory=list)
    mood_influence: str = "strong"


class WebToolConfigStore:
    def __init__(self, file_path: str = "user_profile.json"):
        self.file_path = file_path

    def load(self) -> WebToolConfig:
        data = self._read_json()
        return self.from_dict(data.get("web_tool", {}))

    def save(self, config: WebToolConfig) -> None:
        data = self._read_json()
        data["web_tool"] = asdict(config)
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.file_path)

    @staticmethod
    def from_dict(raw: dict) -> WebToolConfig:
        return WebToolConfig(
            enabled=_as_bool(raw.get("enabled"), True),
            autonomy_level=_as_int(raw.get("autonomy_level"), 2, 0, 4),
            max_query_heads=_as_int(raw.get("max_query_heads"), 3, 1, 5),
            results_per_head=_as_int(raw.get("results_per_head"), 3, 1, 5),
            max_injected_results=_as_int(raw.get("max_injected_results"), 8, 1, 10),
            timeout_seconds=_as_float(raw.get("timeout_seconds"), 2.5, 0.5, 10.0),
            total_timeout_seconds=_as_float(raw.get("total_timeout_seconds"), 8.0, 1.0, 20.0),
            provider_timeout_seconds=_as_float(
                raw.get("provider_timeout_seconds"),
                8.0,
                1.0,
                30.0,
            ),
            rerank_timeout_seconds=_as_float(
                raw.get("rerank_timeout_seconds"),
                8.0,
                1.0,
                30.0,
            ),
            web_total_timeout_seconds=_as_float(
                raw.get("web_total_timeout_seconds"),
                40.0,
                10.0,
                120.0,
            ),
            allow_bing=_as_bool(raw.get("allow_bing"), True),
            allow_baidu=_as_bool(raw.get("allow_baidu"), True),
            allow_direct=_as_bool(raw.get("allow_direct"), True),
            allow_bing_rss=_as_bool(raw.get("allow_bing_rss"), True),
            allow_duckduckgo=_as_bool(raw.get("allow_duckduckgo"), True),
            allow_searxng=_as_bool(raw.get("allow_searxng"), False),
            searxng_url=_as_str(raw.get("searxng_url"), ""),
            searxng_timeout_seconds=_as_float(raw.get("searxng_timeout_seconds"), 5.0, 1.0, 30.0),
            allow_katana=_as_bool(raw.get("allow_katana"), True),
            katana_bin=_as_str(raw.get("katana_bin"), "katana"),
            katana_max_depth=_as_int(raw.get("katana_max_depth"), 2, 1, 10),
            katana_max_pages=_as_int(raw.get("katana_max_pages"), 20, 1, 200),
            katana_timeout_seconds=_as_float(raw.get("katana_timeout_seconds"), 5.0, 1.0, 30.0),
            katana_total_timeout_seconds=_as_float(
                raw.get("katana_total_timeout_seconds"),
                20.0,
                5.0,
                60.0,
            ),
            katana_concurrency=_as_int(raw.get("katana_concurrency"), 3, 1, 20),
            katana_allowed_domains=_as_str_list(raw.get("katana_allowed_domains")),
            mood_influence=raw.get("mood_influence", "strong"),
        )

    def _read_json(self) -> dict:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        return default
    return default


def _as_int(value, default, low, high):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if result < low or result > high:
        return default
    return result


def _as_float(value, default, low, high):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result < low or result > high:
        return default
    return result


def _as_str(value, default):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _as_str_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
    return []
