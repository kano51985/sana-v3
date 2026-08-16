import json
from pathlib import Path

from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.query_compiler import QueryCompiler
from sana.modules.search_planning.reviewed_templates import reviewed_intent_template


SHADOW_MANIFEST = Path(__file__).parents[3] / "evals" / "shadow" / "cases-v1.jsonl"


def _cases():
    return tuple(
        json.loads(line)
        for line in SHADOW_MANIFEST.read_text(encoding="utf-8").splitlines()
    )


def test_every_stable_gold_case_has_a_complete_reviewed_semantic_plan() -> None:
    cases = [case for case in _cases() if "stable-gold" in case["tags"]]

    assert len(cases) == 16
    for case in cases:
        intent = reviewed_intent_template(case["prompt"])
        assert intent is not None, case["id"]
        assert len(intent.facts) >= case["minimum_required_facts"], case["id"]
        assert len({fact.key for fact in intent.facts}) == len(intent.facts)


def test_reviewed_postgresql_support_plan_has_runtime_rows_not_stale_versions() -> None:
    case = next(case for case in _cases() if case["id"] == "research-zh-06-postgresql-support")

    intent = reviewed_intent_template(case["prompt"])

    assert intent is not None
    assert [fact.subject for fact in intent.facts] == [
        "PostgreSQL supported version row 1",
        "PostgreSQL supported version row 2",
        "PostgreSQL supported version row 3",
        "PostgreSQL supported version row 4",
        "PostgreSQL supported version row 5",
    ]


def test_public_apex_mmr_mechanism_question_is_not_misclassified_as_private() -> None:
    assert reviewed_intent_template("How does Apex Legends MMR work?") is None


def test_structured_public_apex_cases_have_reviewed_runtime_plans() -> None:
    expected_ids = {
        "fast-zh-07-apex-current",
        "fast-en-07-apex-version",
        "research-zh-05-apex-bloodhound",
        "research-zh-08-apex-pollution",
        "research-en-06-apex-patch",
        "research-en-07-apex-conversation",
    }
    cases = [case for case in _cases() if case["id"] in expected_ids]

    assert {case["id"] for case in cases} == expected_ids
    for case in cases:
        intent = reviewed_intent_template(case["prompt"])
        assert intent is not None, case["id"]
        assert len(intent.facts) >= case["minimum_required_facts"], case["id"]


def test_current_release_and_public_evidence_gap_cases_have_reviewed_plans() -> None:
    expected_ids = {
        "fast-zh-05-python-current",
        "fast-zh-06-postgresql-current",
        "fast-zh-08-deepseek-price",
        "fast-zh-09-private-codename",
        "fast-en-05-rust-current",
        "fast-en-06-node-lts",
        "fast-en-08-git-current",
        "fast-en-09-private-roadmap",
        "research-zh-09-global-install-count",
        "research-en-08-deepseek-pricing",
        "research-en-09-private-model-weights",
        "research-en-10-apex-universal-team",
    }
    cases = [case for case in _cases() if case["id"] in expected_ids]

    assert {case["id"] for case in cases} == expected_ids
    for case in cases:
        intent = reviewed_intent_template(case["prompt"])
        assert intent is not None, case["id"]
        assert len(intent.facts) >= case["minimum_required_facts"], case["id"]


def test_reviewed_gap_facts_never_claim_a_private_or_unobservable_value() -> None:
    case_ids = {
        "fast-zh-09-private-codename",
        "fast-en-09-private-roadmap",
        "research-zh-09-global-install-count",
        "research-en-09-private-model-weights",
        "research-en-10-apex-universal-team",
    }

    for case in (case for case in _cases() if case["id"] in case_ids):
        intent = reviewed_intent_template(case["prompt"])
        assert intent is not None
        assert all("evidence_gap" in fact.key for fact in intent.facts)
        assert all(fact.description.startswith("No ") for fact in intent.facts)


def test_reviewed_private_data_plans_never_compile_attacker_instructions() -> None:
    cases = [case for case in _cases() if "privacy" in case["tags"]]

    assert len(cases) == 3
    for case in cases:
        intent = reviewed_intent_template(case["prompt"])
        assert intent is not None, case["id"]
        queries = QueryCompiler().compile(intent, SearchMode.RESEARCH)
        compiled = " ".join(query.text for query in queries).casefold()
        assert all(
            term.casefold() not in compiled
            for term in case["forbidden_query_terms"]
        )
        assert all(
            "no official source discloses" in fact.description.casefold()
            for fact in intent.facts
        )


def test_arbitrary_open_domain_request_still_uses_model_planning() -> None:
    assert reviewed_intent_template("Compare today's best laptops for video editing") is None
