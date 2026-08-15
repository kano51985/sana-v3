"""Deterministic query generation from semantic facts, never raw conversation text."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.domain import (
    FactRequirement,
    FactType,
    Freshness,
    NormalizedIntent,
    QuerySpec,
)
from sana.modules.search_planning.policy import SearchPlanningPolicy


class QueryCompiler:
    _KEY_TOKEN = re.compile(r"[A-Za-z0-9]+")

    def __init__(self, policy: SearchPlanningPolicy | None = None) -> None:
        self.policy = policy or SearchPlanningPolicy()

    @classmethod
    def _semantic_key_terms(
        cls,
        fact: FactRequirement,
        *,
        entity: str,
        keyword_groups: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Keep bounded Fact semantics without copying conversational prose."""

        excluded = {
            value.casefold()
            for value in cls._KEY_TOKEN.findall(
                " ".join((entity, fact.subject, *keyword_groups))
            )
        }
        excluded.update(
            {
                "answer",
                "background",
                "evidence",
                "fact",
                "gap",
                "info",
                "information",
                "overview",
                "profile",
                "purpose",
                "result",
                "source",
                "status",
            }
        )
        terms: list[str] = []
        for raw in cls._KEY_TOKEN.findall(fact.key):
            term = raw.casefold()
            if len(term) < 2 or term in excluded or term in terms:
                continue
            terms.append(term)
            if len(terms) == 4:
                break
        return tuple(terms)

    @staticmethod
    def _keywords(fact: FactRequirement, *, chinese: bool) -> tuple[tuple[str, ...], ...]:
        if chinese:
            mapping = {
                FactType.CHARACTER_CHANGES: (("改动", "官方补丁"), ("平衡性调整", "更新"), ("技能改动",)),
                FactType.VERSION: (("当前稳定版本", "官方"), ("最新版本号",), ("最新更新",)),
                FactType.PATCH_NOTES: (("补丁说明", "官方"), ("更新日志",), ("版本公告",)),
                FactType.TEAM_META: (("当前版本", "配队"), ("阵容", "meta"), ("排位热门阵容",)),
                FactType.CURRENT_VALUE: (("当前",), ("最新",), ("官方数据",)),
                FactType.COMPARISON: (("对比",), ("区别",), ("优缺点",)),
                FactType.BACKGROUND: (("背景",), ("介绍",), ("官方资料",)),
            }
        else:
            mapping = {
                FactType.CHARACTER_CHANGES: (("changes", "official patch"), ("balance update",), ("ability changes",)),
                FactType.VERSION: (("latest stable version", "official"), ("current version number",), ("latest release",)),
                FactType.PATCH_NOTES: (("official patch notes",), ("changelog",), ("release notes",)),
                FactType.TEAM_META: (("current meta", "team composition"), ("best lineup",), ("ranked team picks",)),
                FactType.CURRENT_VALUE: (("current",), ("latest",), ("official data",)),
                FactType.COMPARISON: (("comparison",), ("differences",), ("pros and cons",)),
                FactType.BACKGROUND: (("background",), ("overview",), ("official profile",)),
            }
        return mapping[fact.fact_type]

    def _fit(self, entity: str, components: list[str]) -> str:
        parts = [entity, *components]
        while len(" ".join(parts)) > self.policy.max_query_characters and len(parts) > 2:
            parts.pop()
        query = " ".join(parts)
        if len(query) > self.policy.max_query_characters:
            if len(entity) > self.policy.max_query_characters:
                raise ValueError("Canonical entity exceeds query character limit")
            query = query[: self.policy.max_query_characters].rstrip()
        return query

    @staticmethod
    def signature(query: str, *, fact_key: str | None = None) -> str:
        normalized = unicodedata.normalize("NFKC", query).casefold()
        normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()
        scope = (
            normalized
            if fact_key is None
            else f"{unicodedata.normalize('NFKC', fact_key).casefold()}\0{normalized}"
        )
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()

    @staticmethod
    def _freshness_days(fact: FactRequirement) -> int | None:
        if fact.freshness is Freshness.CURRENT:
            return 45
        if fact.freshness is Freshness.RECENT:
            return 180
        return None

    def compile(
        self,
        intent: NormalizedIntent,
        mode: SearchMode,
        *,
        plan_revision: int = 1,
        expansion: bool = False,
        existing_signatures: frozenset[str] = frozenset(),
    ) -> tuple[QuerySpec, ...]:
        total_limit = self.policy.query_limit(mode, expansion=expansion)
        limit = (
            max(0, total_limit - len(existing_signatures))
            if expansion
            else total_limit
        )
        if limit == 0:
            return ()
        per_fact_limit = (
            self.policy.fast_max_queries_per_fact
            if mode is SearchMode.FAST
            else self.policy.research_max_queries_per_fact
        )
        chinese = intent.locale.lower().startswith("zh")
        candidates: list[tuple[FactRequirement, int, tuple[str, ...]]] = []
        variants = {
            fact.key: self._keywords(fact, chinese=chinese)
            for fact in intent.facts
        }
        for variant_index in range(per_fact_limit):
            for fact in intent.facts:
                fact_variants = variants[fact.key]
                if variant_index < len(fact_variants):
                    candidates.append((fact, variant_index, fact_variants[variant_index]))

        queries: list[QuerySpec] = []
        signatures = set(existing_signatures)
        for fact, variant_index, keywords in candidates:
            subject = fact.subject.strip()
            components = []
            if subject.casefold() != intent.entity.casefold():
                components.append(subject)
            components.extend(
                self._semantic_key_terms(
                    fact,
                    entity=intent.entity,
                    keyword_groups=keywords,
                )
            )
            components.extend(keywords)
            text = self._fit(intent.entity.strip(), components)
            # QuerySpec has exactly one Fact binding. Text-identical queries for
            # different Facts must therefore remain distinct; otherwise the
            # later Fact silently loses discovery and evidence lineage.
            signature = self.signature(text, fact_key=fact.key)
            if signature in signatures:
                continue
            signatures.add(signature)
            queries.append(
                QuerySpec(
                    key=f"q:{plan_revision}:{fact.key}:{variant_index + 1}",
                    fact_key=fact.key,
                    text=text,
                    signature=signature,
                    locale=intent.locale,
                    freshness_days=self._freshness_days(fact),
                    plan_revision=plan_revision,
                    metadata={
                        "fact_type": fact.fact_type.value,
                        "source_kind": (
                            fact.preferred_source_kinds[0]
                            if fact.preferred_source_kinds
                            else "web"
                        ),
                    },
                )
            )
            if len(queries) >= limit:
                break
        return tuple(queries)
