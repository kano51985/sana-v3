import re

from sana.models.search_context import EntityContext, FactType, SearchIntent


_FACT_RULES = [
    (
        FactType.CHARACTER_CHANGES,
        r"(角色|英雄|character).{0,12}(改动|调整|重做|平衡|变化|change|balance|rework)"
        r"|(改动|调整|重做|变化|改).{0,12}(角色|英雄|character)",
    ),
    (
        FactType.VERSION,
        r"(当前版本|版本号|什么版本|版本更新|version)",
    ),
    (
        FactType.PATCH_NOTES,
        r"(patch|补丁|更新公告|改动|平衡|调整)",
    ),
    (
        FactType.TEAM_META,
        r"(配队|阵容|队伍|meta|最强组合|最强配队|team)",
    ),
    (
        FactType.GUIDE,
        r"(攻略|指南|guide|build)",
    ),
    (
        FactType.NEWS,
        r"(新闻|资讯|公告|news)",
    ),
]

_PAGE_TYPES = {
    FactType.VERSION: ["news", "version"],
    FactType.PATCH_NOTES: ["patch_notes", "news", "character"],
    FactType.CHARACTER_CHANGES: ["character", "patch_notes", "news"],
    FactType.TEAM_META: ["guide", "wiki", "forum"],
    FactType.GUIDE: ["guide", "wiki"],
    FactType.NEWS: ["news", "article"],
    FactType.GENERAL: [],
}


class FactIntentClassifier:
    def classify(
        self,
        user_input: str,
        entity_context: EntityContext | None = None,
    ) -> SearchIntent:
        text = (user_input or "").lower()
        fact_types = []
        for fact_type, pattern in _FACT_RULES:
            if re.search(pattern, text):
                if fact_type not in fact_types:
                    fact_types.append(fact_type)
        if not fact_types:
            fact_types.append(FactType.GENERAL)

        required_page_types = []
        for fact_type in fact_types:
            for page_type in _PAGE_TYPES.get(fact_type, []):
                if page_type not in required_page_types:
                    required_page_types.append(page_type)

        answer_strategy = "extract_facts"
        if fact_types == [FactType.NEWS] or fact_types == [FactType.GENERAL]:
            answer_strategy = "summarize"
        return SearchIntent(
            fact_types=fact_types,
            required_page_types=required_page_types,
            answer_strategy=answer_strategy,
        )
