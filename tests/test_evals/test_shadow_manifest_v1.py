from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from sana.app.shadow_runtime import load_cost_rate, load_review_rubric
from sana.modules.shadow_campaign.manifest import (
    Answerability,
    CaseCategory,
    OracleType,
    parse_manifest_bytes,
)
from sana.modules.shadow_campaign.policy import (
    DOCKER_SMOKE_V1,
    SHADOW_FULL_GATE_V2,
    SHADOW_FULL_V1,
    SHADOW_SMOKE_GATE_V1,
)
from sana.modules.shadow_campaign.scheduler import materialize_run_plans


ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = ROOT / "evals" / "shadow"
NOW = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
EXPECTED_FILE_HASHES = {
    "cases-v1.jsonl": "3508ca836efd7e9c1b0606ff1b0836e92be55f5efb6756117af56f79b6197794",
    "cost-rates-v1.json": "e7998119196b8e598735092cca154b11bf2276ef755b686f7123235ebf4aed38",
    "gate-policies-v1.json": "35c1096726a822fe8325022de6cbaa2279dc730d896409736cd528571cf1f904",
    "profiles-v1.json": "22c4f8605197ba950ae1186ab1d2df18876369652b7722ae291a9716d23ab925",
    "review-rubric-v1.json": "55da85e368a1d67c58d2e3326f7143e997ffbdcfd44d9528d866f29591a2aee0",
}


def _json(name: str) -> dict[str, object]:
    value = json.loads((ASSET_ROOT / name).read_bytes())
    assert isinstance(value, dict)
    return value


def test_v1_asset_bytes_are_frozen() -> None:
    actual = {
        name: hashlib.sha256((ASSET_ROOT / name).read_bytes()).hexdigest()
        for name in EXPECTED_FILE_HASHES
    }
    assert actual == EXPECTED_FILE_HASHES


def test_manifest_v1_has_the_locked_distribution_and_oracles() -> None:
    manifest = parse_manifest_bytes(
        (ASSET_ROOT / "cases-v1.jsonl").read_bytes(),
        now=NOW,
    )

    assert manifest.version == "shadow-cases-v1"
    assert manifest.sha256 == EXPECTED_FILE_HASHES["cases-v1.jsonl"]
    assert len(manifest.cases) == 40
    assert manifest.mode_counts == {"FAST": 20, "RESEARCH": 20}
    assert len(manifest.deterministic_case_ids) == 16
    assert sum(
        case.answerability is Answerability.INTENTIONALLY_UNANSWERABLE
        for case in manifest.cases
    ) == 8
    assert sum(
        case.category is CaseCategory.POLLUTION_REGRESSION
        or "apex" in {tag.casefold() for tag in case.tags}
        for case in manifest.cases
    ) == 10
    assert Counter(
        (case.expected_mode.value, case.locale) for case in manifest.cases
    ) == {
        ("FAST", "zh-CN"): 10,
        ("FAST", "en"): 10,
        ("RESEARCH", "zh-CN"): 10,
        ("RESEARCH", "en"): 10,
    }
    for case in manifest.cases:
        if case.oracle_type is OracleType.DETERMINISTIC:
            assert case.valid_from is not None
            assert case.valid_until is not None
            assert case.valid_from <= NOW
            assert case.valid_until >= NOW + timedelta(hours=6)
            assert "stable-gold" in case.tags
        elif case.answerability is Answerability.ANSWERABLE:
            assert case.oracle_type is OracleType.MANUAL_REQUIRED
        else:
            assert case.oracle_type is OracleType.NOT_APPLICABLE
        for forbidden in case.forbidden_query_terms:
            assert forbidden.casefold() in case.prompt.casefold()


def test_smoke_and_full_schedules_are_exact_and_review_sample_is_stable() -> None:
    manifest = parse_manifest_bytes(
        (ASSET_ROOT / "cases-v1.jsonl").read_bytes(),
        now=NOW,
    )
    assert {case.id for case in manifest.smoke_cases} == {
        "fast-zh-01-http-404",
        "fast-zh-09-private-codename",
        "fast-en-05-rust-current",
        "research-zh-01-git-objects",
        "research-zh-05-apex-bloodhound",
        "research-en-09-private-model-weights",
    }
    campaign_id = UUID("6c193bdd-dc48-5b08-b901-4d1d2f78c756")
    smoke = materialize_run_plans(
        campaign_id,
        manifest,
        DOCKER_SMOKE_V1,
        required_reviews=0,
    )
    full = materialize_run_plans(
        campaign_id,
        manifest,
        SHADOW_FULL_V1,
        required_reviews=20,
    )

    assert len(smoke) == 6
    assert Counter(item.expected_mode for item in smoke) == {
        "FAST": 3,
        "RESEARCH": 3,
    }
    assert len(full) == 120
    assert Counter(item.expected_mode for item in full) == {
        "FAST": 60,
        "RESEARCH": 60,
    }
    selected = tuple(item for item in full if item.manual_review_selected)
    assert len(selected) == 20
    assert len({item.case_id for item in selected}) == 20
    assert Counter((item.expected_mode, item.locale) for item in selected) == {
        ("FAST", "zh-CN"): 5,
        ("FAST", "en"): 5,
        ("RESEARCH", "zh-CN"): 5,
        ("RESEARCH", "en"): 5,
    }


def test_profile_and_gate_assets_mirror_the_locked_domain_catalog() -> None:
    profiles = _json("profiles-v1.json")
    policies = _json("gate-policies-v1.json")

    assert profiles == {
        "asset_version": "shadow-profiles-v1",
        "profiles": [DOCKER_SMOKE_V1.snapshot(), SHADOW_FULL_V1.snapshot()],
    }
    assert policies == {
        "asset_version": "shadow-gate-policies-v1",
        "policies": [
            SHADOW_SMOKE_GATE_V1.snapshot(),
            SHADOW_FULL_GATE_V2.snapshot(),
        ],
    }
    assert DOCKER_SMOKE_V1.sha256 == (
        "f9bae62a650fe16101c67749bcec8fd1a85e747e4926c60cd9007efcd8dbade5"
    )
    assert SHADOW_FULL_V1.sha256 == (
        "2c6715de30a134639e379ca561461b5dbbc57ca2fa001ae15355b1fbb6958151"
    )
    assert SHADOW_SMOKE_GATE_V1.sha256 == (
        "054878e972560ef6177ab23f8cac2c1990aaf9512cce76d25d1cc8e72ed5c4dd"
    )
    assert SHADOW_FULL_GATE_V2.sha256 == (
        "53920b77ace94b6aad555d821cd3d6fd84d913bcef7e8fa223b6b79205a08bb5"
    )


def test_runtime_assets_have_locked_semantic_hashes_and_conservative_rate() -> None:
    rubric = load_review_rubric(ASSET_ROOT / "review-rubric-v1.json")
    rate = load_cost_rate(ASSET_ROOT / "cost-rates-v1.json")

    assert rubric.sha256 == (
        "c435649e92b44078c2ebe8ff18dc5e7dee45ca27b7759b459e2379183f5e3d1e"
    )
    assert rate.sha256 == (
        "d1cc6cb593343fb0693ad677fb0de355fb2559345c4db6428839af0f254f8e8f"
    )
    assert rate.version == "deepseek-v4-flash-usd-2026-08-15-v1"
    assert rate.possibly_billed_run_reserve_usd * 6 <= (
        DOCKER_SMOKE_V1.estimated_cost_stop_threshold
    )


def test_runtime_asset_loader_rejects_duplicate_and_non_finite_json(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"version":"one","version":"two","criteria":["correctness"]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        load_review_rubric(duplicate)

    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text(
        '{"version":"rate","prompt_per_million_usd":NaN,'
        '"completion_per_million_usd":"1",'
        '"possibly_billed_run_reserve_usd":"0.001"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-finite"):
        load_cost_rate(non_finite)
