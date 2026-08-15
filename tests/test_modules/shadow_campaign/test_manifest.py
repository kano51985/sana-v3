from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from sana.modules.shadow_campaign.manifest import parse_manifest_bytes


NOW = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)


def _valid_case(
    *,
    mode: str,
    locale: str,
    index: int,
    smoke: bool,
) -> dict[str, object]:
    answerable = index < 8
    deterministic = index < 4
    return {
        "manifest_version": "shadow-cases-v1",
        "id": f"{mode.lower()}-{locale.lower()}-{index}",
        "prompt": f"question {mode} {locale} {index}",
        "locale": locale,
        "expected_mode": mode,
        "category": (
            "pollution_regression"
            if answerable and index in {6, 7}
            else "version"
            if answerable
            else "no_answer"
        ),
        "answerability": "answerable" if answerable else "intentionally_unanswerable",
        "minimum_required_facts": 1,
        "gold_assertions": (
            [
                {
                    "id": "stable-answer",
                    "operator": "normalized_contains_all",
                    "expected": ["stable"],
                    "critical": False,
                }
            ]
            if deterministic
            else []
        ),
        "oracle_type": (
            "deterministic"
            if deterministic
            else "manual_required"
            if answerable
            else "not_applicable"
        ),
        "valid_from": "2026-08-14T00:00:00Z" if deterministic else None,
        "valid_until": "2026-08-16T00:00:00Z" if deterministic else None,
        "required_source_classes": ["OFFICIAL"],
        "forbidden_query_terms": ["private memory"],
        "must_not_complete": not answerable,
        "tags": [mode.lower(), locale.lower()],
        "smoke": smoke,
    }


def _valid_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    smoke_ids = {
        "fast-zh-cn-0",
        "fast-zh-cn-8",
        "fast-en-0",
        "research-zh-cn-0",
        "research-en-0",
        "research-en-8",
    }
    for mode in ("FAST", "RESEARCH"):
        for locale in ("zh-CN", "en"):
            for index in range(10):
                case_id = f"{mode.lower()}-{locale.lower()}-{index}"
                rows.append(
                    _valid_case(
                        mode=mode,
                        locale=locale,
                        index=index,
                        smoke=case_id in smoke_ids,
                    )
                )
    return rows


def _encode(rows: list[dict[str, object]], *, compact: bool = True) -> bytes:
    separators = (",", ":") if compact else None
    return ("\n".join(json.dumps(row, ensure_ascii=False, separators=separators) for row in rows) + "\n").encode("utf-8")


def test_valid_manifest_locks_distribution_and_raw_content_hash() -> None:
    raw = _encode(_valid_rows())
    manifest = parse_manifest_bytes(raw, now=NOW)

    assert manifest.version == "shadow-cases-v1"
    assert len(manifest.cases) == 40
    assert len(manifest.smoke_cases) == 6
    assert manifest.sha256 == hashlib.sha256(raw).hexdigest()
    assert manifest.mode_counts == {"FAST": 20, "RESEARCH": 20}
    assert len(manifest.deterministic_case_ids) == 16


def test_manifest_hash_changes_when_raw_encoding_changes() -> None:
    rows = _valid_rows()
    compact = parse_manifest_bytes(_encode(rows, compact=True), now=NOW)
    expanded = parse_manifest_bytes(_encode(rows, compact=False), now=NOW)

    assert compact.cases == expanded.cases
    assert compact.sha256 != expanded.sha256


def test_manifest_rejects_unknown_fields_and_duplicate_ids() -> None:
    rows = _valid_rows()
    rows[0]["surprise"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        parse_manifest_bytes(_encode(rows), now=NOW)

    rows = _valid_rows()
    rows[1]["id"] = rows[0]["id"]
    with pytest.raises(ValueError, match="duplicate case ID"):
        parse_manifest_bytes(_encode(rows), now=NOW)


def test_manifest_rejects_duplicate_json_keys_and_non_finite_numbers() -> None:
    raw = _encode(_valid_rows())
    duplicate_key = raw.replace(b'"smoke":false', b'"smoke":false,"smoke":false', 1)
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_manifest_bytes(duplicate_key, now=NOW)

    non_finite = raw.replace(b'"minimum_required_facts":1', b'"minimum_required_facts":NaN', 1)
    with pytest.raises(ValueError, match="non-finite JSON value"):
        parse_manifest_bytes(non_finite, now=NOW)


def test_manifest_rejects_invalid_oracle_window_and_distribution() -> None:
    rows = _valid_rows()
    rows[0]["valid_until"] = (NOW + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="active Campaign window"):
        parse_manifest_bytes(_encode(rows), now=NOW)

    rows = _valid_rows()
    for row in rows:
        if row["expected_mode"] == "FAST" and row["locale"] == "en":
            row["answerability"] = "intentionally_unanswerable"
            row["oracle_type"] = "not_applicable"
            row["gold_assertions"] = []
            row["valid_from"] = None
            row["valid_until"] = None
            row["must_not_complete"] = True
    with pytest.raises(ValueError, match="answerable cases"):
        parse_manifest_bytes(_encode(rows), now=NOW)
