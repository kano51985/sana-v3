import re

from sana.models.search_context import EntityContext
from sana.services.entity_resolver import EntityResolution
from sana.services.llm_entity_context_extractor import LLMEntityContextExtractor


_STOPWORDS = {
    "当前", "版本", "更新", "2026", "年", "月", "这个", "那个",
    "什么", "怎么", "改成", "什么样", "啦", "听说", "很大",
    "问题", "推荐", "最强", "情况", "了吗", "吗", "了", "的",
    "可以", "告诉", "现在", "最近", "有关", "相关", "关于",
    "sana", "我听说", "你知道", "具体", "改动了什么", "了吗",
}

_GAME_MARKERS = (
    "apex", "genshin", "原神", "王者", "lol", "league", "游戏",
    "英雄", "角色", "赛季", "猎犬", "配队", "攻略", "mihoyo", "ea.com",
)

_PRODUCT_MARKERS = (
    "tool", "工具", "软件", "software", "docs", "documentation",
    "security", "安全", "kali", "specterops",
)


class EntityContextBuilder:
    def __init__(self, llm_extractor: LLMEntityContextExtractor | None = None):
        self.llm_extractor = llm_extractor

    def build(
        self,
        user_input: str,
        resolution: EntityResolution,
        heads: list[str] | None = None,
    ) -> EntityContext:
        canonical = str(resolution.canonical or "").strip()
        aliases = [str(alias).strip() for alias in (resolution.aliases or []) if str(alias).strip()]
        raw_text = f"{user_input or ''} {' '.join(heads or [])}"
        llm_result = (
            self.llm_extractor.extract(user_input, resolution, heads or [])
            if self.llm_extractor is not None
            else None
        )
        if llm_result:
            context_terms = list(
                dict.fromkeys(aliases + llm_result.get("context_terms", []))
            )[:10]
            domain = llm_result.get("domain", "general")
            entity_kind = llm_result.get("entity_kind", "general")
            ambiguous = bool(llm_result.get("ambiguous", False))
            evidence = str(llm_result.get("evidence") or resolution.evidence or "")
            context_source = "llm"
        else:
            context_terms = _context_terms(canonical, aliases, raw_text)
            text_lower = raw_text.lower()
            domain = _domain(text_lower)
            entity_kind = _entity_kind(text_lower, canonical, aliases)
            ambiguous = _ambiguous(user_input or "", text_lower)
            evidence = str(resolution.evidence or "")
            context_source = "rules"
        return EntityContext(
            canonical=canonical,
            aliases=aliases,
            domain=domain,
            entity_kind=entity_kind,
            context_terms=context_terms,
            ambiguous=ambiguous,
            evidence=evidence,
            context_source=context_source,
        )


def _context_terms(canonical: str, aliases: list[str], text: str) -> list[str]:
    terms = list(aliases)
    excluded = {canonical.lower(), *(alias.lower() for alias in aliases)}
    for part in re.split(r"[\s,，。；;、/]+", text or ""):
        part = part.strip().lower()
        if (
            part
            and len(part) > 1
            and len(part) <= 16
            and part not in excluded
            and part not in _STOPWORDS
        ):
            terms.append(part)
    return list(dict.fromkeys(terms))[:10]


def _domain(text_lower: str) -> str:
    if any(marker in text_lower for marker in _GAME_MARKERS):
        return "game"
    if any(marker in text_lower for marker in _PRODUCT_MARKERS):
        return "product"
    return "general"


def _entity_kind(text_lower: str, canonical: str, aliases: list[str]) -> str:
    combined = " ".join([canonical, *aliases, text_lower]).lower()
    if any(marker in combined for marker in ("角色", "英雄", "character", "猎犬", "狗子", "hound")):
        return "character"
    if any(marker in text_lower for marker in ("游戏", "game")):
        return "game"
    if any(marker in text_lower for marker in ("人物", "person")):
        return "person"
    return "general"


def _ambiguous(user_input: str, full_text_lower: str) -> bool:
    user_has_game = any(marker in user_input.lower() for marker in _GAME_MARKERS)
    full_has_game = any(marker in full_text_lower for marker in _GAME_MARKERS)
    return bool(not user_has_game and full_has_game)
