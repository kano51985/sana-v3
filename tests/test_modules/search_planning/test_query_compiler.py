import json
from pathlib import Path

import pytest

from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.domain import (
    Consequence,
    FactRequirement,
    FactType,
    Freshness,
    NormalizedIntent,
)
from sana.modules.search_planning.query_compiler import QueryCompiler


FIXTURE = Path(__file__).parents[2] / "fixtures" / "evals" / "apex_multi_fact.json"


def load_intent():
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))
    facts = tuple(
        FactRequirement(
            key=fact["key"],
            fact_type=FactType(fact["fact_type"]),
            description=fact["description"],
            subject=fact["subject"],
            freshness=Freshness(fact["freshness"]),
            consequence=Consequence.LOW,
            preferred_source_kinds=tuple(fact["preferred_source_kinds"]),
        )
        for fact in case["facts"]
    )
    return case, NormalizedIntent(
        entity=case["entity"],
        aliases=tuple(case["aliases"]),
        locale=case["locale"],
        facts=facts,
    )


def test_query_compiler_cannot_append_conversational_fragments() -> None:
    case, intent = load_intent()

    queries = QueryCompiler().compile(intent, SearchMode.RESEARCH)

    assert len(queries) == 8
    assert {query.fact_key for query in queries} == {fact.key for fact in intent.facts}
    for query in queries:
        assert query.text.startswith("Apex Legends")
        assert len(query.text) <= 64
        assert all(
            fragment.casefold() not in query.text.casefold()
            for fragment in case["forbidden_query_fragments"]
        )


def test_fast_query_limit_and_per_fact_limit_are_enforced() -> None:
    _, intent = load_intent()
    queries = QueryCompiler().compile(intent, SearchMode.FAST)

    assert len(queries) == 4
    counts = {
        fact.key: sum(query.fact_key == fact.key for query in queries)
        for fact in intent.facts
    }
    assert all(count == 1 for count in counts.values())


def test_fast_version_query_prefers_official_stable_release_language() -> None:
    intent = NormalizedIntent(
        entity="Python",
        aliases=(),
        locale="en",
        facts=(
            FactRequirement(
                key="stable-version",
                fact_type=FactType.VERSION,
                description="current stable version",
                subject="Python",
                freshness=Freshness.CURRENT,
                consequence=Consequence.LOW,
            ),
        ),
    )

    query = QueryCompiler().compile(intent, SearchMode.FAST)[0]

    assert query.text == "Python latest stable version official"
    assert "season" not in query.text


def test_existing_signatures_are_deduplicated_during_expansion() -> None:
    _, intent = load_intent()
    compiler = QueryCompiler()
    initial = compiler.compile(intent, SearchMode.RESEARCH)
    expanded = compiler.compile(
        intent,
        SearchMode.RESEARCH,
        plan_revision=2,
        expansion=True,
        existing_signatures=frozenset(query.signature for query in initial),
    )

    assert not (
        {query.signature for query in initial}
        & {query.signature for query in expanded}
    )
    assert len(initial) + len(expanded) == 12


def test_conversational_subject_is_rejected_before_query_compilation() -> None:
    _, intent = load_intent()
    with pytest.raises(ValueError, match="conversational"):
        FactRequirement(
            key="polluted",
            fact_type=FactType.VERSION,
            description="version",
            subject="sana 我好久没碰apex啦",
        )
