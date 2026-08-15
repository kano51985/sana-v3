"""Strict, non-executable JSONL manifest parsing and validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from sana.modules.evidence.domain import SourceAuthority
from sana.modules.orchestration.domain import SearchMode
from sana.modules.shadow_campaign.domain import (
    canonical_json_bytes,
    freeze_json,
    require_aware,
)


CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
ALLOWED_OPERATORS = frozenset(
    {
        "normalized_contains_all",
        "normalized_equals",
        "number_in_range",
        "set_contains",
        "source_class_at_least",
    }
)

_CASE_FIELDS = frozenset(
    {
        "manifest_version",
        "id",
        "prompt",
        "locale",
        "expected_mode",
        "category",
        "answerability",
        "minimum_required_facts",
        "gold_assertions",
        "oracle_type",
        "valid_from",
        "valid_until",
        "required_source_classes",
        "forbidden_query_terms",
        "must_not_complete",
        "tags",
        "smoke",
    }
)
_ASSERTION_FIELDS = frozenset({"id", "operator", "expected", "critical"})


class CaseCategory(StrEnum):
    VERSION = "version"
    BACKGROUND = "background"
    COMPARISON = "comparison"
    MULTI_FACT = "multi_fact"
    CONFLICT = "conflict"
    NO_ANSWER = "no_answer"
    PROVIDER_RESILIENCE = "provider_resilience"
    POLLUTION_REGRESSION = "pollution_regression"


class Answerability(StrEnum):
    ANSWERABLE = "answerable"
    INTENTIONALLY_UNANSWERABLE = "intentionally_unanswerable"


class OracleType(StrEnum):
    DETERMINISTIC = "deterministic"
    MANUAL_REQUIRED = "manual_required"
    NOT_APPLICABLE = "not_applicable"


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class GoldAssertion:
    id: str
    operator: str
    expected: Any
    critical: bool

    def __post_init__(self) -> None:
        assertion_id = self.id.strip()
        if not assertion_id or len(assertion_id) > 100:
            raise ValueError(
                "Gold assertion ID must contain between 1 and 100 characters"
            )
        if not isinstance(self.critical, bool):
            raise ValueError("Gold assertion critical must be boolean")
        if self.operator not in ALLOWED_OPERATORS:
            raise ValueError(f"Unknown gold assertion operator: {self.operator}")
        expected = freeze_json(self.expected)
        if self.operator in {"normalized_contains_all", "set_contains"}:
            if not isinstance(expected, tuple) or not expected or any(
                not isinstance(item, str) or not item.strip() for item in expected
            ):
                raise ValueError(f"{self.operator} expects non-empty string values")
        elif self.operator == "normalized_equals":
            if not isinstance(expected, (str, int, Decimal)) or isinstance(expected, bool):
                raise ValueError("normalized_equals expects a scalar value")
        elif self.operator == "number_in_range":
            if not isinstance(expected, Mapping) or set(expected) != {"min", "max"}:
                raise ValueError("number_in_range expects min and max")
            minimum = _decimal(expected["min"], "number range min")
            maximum = _decimal(expected["max"], "number range max")
            if minimum > maximum:
                raise ValueError("number range min cannot exceed max")
            expected = MappingProxyType({"min": minimum, "max": maximum})
        elif self.operator == "source_class_at_least":
            expected = SourceAuthority(str(expected)).value
        canonical_json_bytes(expected)
        object.__setattr__(self, "id", assertion_id)
        object.__setattr__(self, "expected", expected)


@dataclass(frozen=True, slots=True)
class ShadowCase:
    id: str
    prompt: str
    locale: str
    expected_mode: SearchMode
    category: CaseCategory
    answerability: Answerability
    minimum_required_facts: int
    gold_assertions: tuple[GoldAssertion, ...]
    oracle_type: OracleType
    valid_from: datetime | None
    valid_until: datetime | None
    required_source_classes: tuple[SourceAuthority, ...]
    forbidden_query_terms: tuple[str, ...]
    must_not_complete: bool
    tags: tuple[str, ...]
    smoke: bool

    def __post_init__(self) -> None:
        if not CASE_ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid case ID: {self.id}")
        if not self.prompt.strip() or len(self.prompt) > 100_000:
            raise ValueError(f"Case {self.id} prompt length is invalid")
        if self.locale not in {"zh-CN", "en"}:
            raise ValueError(f"Case {self.id} locale is unsupported")
        if not 1 <= self.minimum_required_facts <= 50:
            raise ValueError(f"Case {self.id} minimum_required_facts is invalid")
        assertion_ids = [assertion.id for assertion in self.gold_assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError(f"Case {self.id} has duplicate gold assertion IDs")
        if any(not item.strip() for item in self.forbidden_query_terms + self.tags):
            raise ValueError(f"Case {self.id} contains empty terms or tags")
        answerable = self.answerability is Answerability.ANSWERABLE
        if self.must_not_complete == answerable:
            raise ValueError(f"Case {self.id} answerability/must_not_complete mismatch")
        if self.oracle_type is OracleType.DETERMINISTIC:
            if not answerable or not self.gold_assertions:
                raise ValueError(f"Case {self.id} deterministic oracle requires assertions")
            if self.valid_from is None or self.valid_until is None:
                raise ValueError(f"Case {self.id} deterministic oracle requires a window")
            require_aware(self.valid_from, "valid_from")
            require_aware(self.valid_until, "valid_until")
            if self.valid_from >= self.valid_until:
                raise ValueError(f"Case {self.id} oracle window is invalid")
        else:
            if self.gold_assertions or self.valid_from is not None or self.valid_until is not None:
                raise ValueError(f"Case {self.id} non-deterministic oracle must not carry assertions")
        if self.oracle_type is OracleType.MANUAL_REQUIRED and not answerable:
            raise ValueError(f"Case {self.id} manual oracle must be answerable")
        if self.oracle_type is OracleType.NOT_APPLICABLE and answerable:
            raise ValueError(f"Case {self.id} answerable oracle cannot be not_applicable")


@dataclass(frozen=True, slots=True)
class ShadowManifest:
    version: str
    cases: tuple[ShadowCase, ...]
    sha256: str

    @property
    def smoke_cases(self) -> tuple[ShadowCase, ...]:
        return tuple(case for case in self.cases if case.smoke)

    @property
    def deterministic_case_ids(self) -> frozenset[str]:
        return frozenset(
            case.id for case in self.cases if case.oracle_type is OracleType.DETERMINISTIC
        )

    @property
    def mode_counts(self) -> dict[str, int]:
        counts = Counter(case.expected_mode.value for case in self.cases)
        return dict(counts)


def _parse_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an RFC3339 string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC3339 timestamp") from exc
    require_aware(parsed, field_name)
    return parsed.astimezone(UTC)


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    return tuple(item.strip() for item in value)


def _parse_assertion(value: Any, case_id: str) -> GoldAssertion:
    if not isinstance(value, dict):
        raise ValueError(f"Case {case_id} gold assertion must be an object")
    unknown = set(value) - _ASSERTION_FIELDS
    missing = _ASSERTION_FIELDS - set(value)
    if unknown:
        raise ValueError(f"Case {case_id} gold assertion has unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"Case {case_id} gold assertion is missing fields: {sorted(missing)}")
    if not isinstance(value["id"], str) or not isinstance(value["operator"], str):
        raise ValueError(f"Case {case_id} assertion ID/operator must be strings")
    if not isinstance(value["critical"], bool):
        raise ValueError(f"Case {case_id} assertion critical must be boolean")
    return GoldAssertion(
        id=value["id"],
        operator=value["operator"],
        expected=value["expected"],
        critical=value["critical"],
    )


def _parse_case(value: Any, line_number: int) -> tuple[str, ShadowCase]:
    if not isinstance(value, dict):
        raise ValueError(f"Manifest line {line_number} must be an object")
    unknown = set(value) - _CASE_FIELDS
    missing = _CASE_FIELDS - set(value)
    if unknown:
        raise ValueError(f"Manifest line {line_number} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"Manifest line {line_number} is missing fields: {sorted(missing)}")
    if not isinstance(value["manifest_version"], str) or not value["manifest_version"].strip():
        raise ValueError(f"Manifest line {line_number} has invalid manifest_version")
    if not isinstance(value["id"], str):
        raise ValueError(f"Manifest line {line_number} case ID must be a string")
    case_id = value["id"]
    if not isinstance(value["prompt"], str):
        raise ValueError(f"Case {case_id} prompt must be a string")
    if not isinstance(value["locale"], str):
        raise ValueError(f"Case {case_id} locale must be a string")
    if not isinstance(value["minimum_required_facts"], int) or isinstance(
        value["minimum_required_facts"], bool
    ):
        raise ValueError(f"Case {case_id} minimum_required_facts must be an integer")
    if not isinstance(value["gold_assertions"], list):
        raise ValueError(f"Case {case_id} gold_assertions must be an array")
    if not isinstance(value["must_not_complete"], bool) or not isinstance(value["smoke"], bool):
        raise ValueError(f"Case {case_id} flags must be booleans")
    assertions = tuple(_parse_assertion(item, case_id) for item in value["gold_assertions"])
    return value["manifest_version"].strip(), ShadowCase(
        id=case_id,
        prompt=value["prompt"],
        locale=value["locale"],
        expected_mode=SearchMode(value["expected_mode"]),
        category=CaseCategory(value["category"]),
        answerability=Answerability(value["answerability"]),
        minimum_required_facts=value["minimum_required_facts"],
        gold_assertions=assertions,
        oracle_type=OracleType(value["oracle_type"]),
        valid_from=_parse_datetime(value["valid_from"], f"Case {case_id} valid_from"),
        valid_until=_parse_datetime(value["valid_until"], f"Case {case_id} valid_until"),
        required_source_classes=tuple(
            SourceAuthority(item)
            for item in _string_tuple(
                value["required_source_classes"],
                f"Case {case_id} required_source_classes",
            )
        ),
        forbidden_query_terms=_string_tuple(
            value["forbidden_query_terms"],
            f"Case {case_id} forbidden_query_terms",
        ),
        must_not_complete=value["must_not_complete"],
        tags=_string_tuple(value["tags"], f"Case {case_id} tags"),
        smoke=value["smoke"],
    )


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Manifest contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Manifest contains non-finite JSON value: {value}")


def _validate_distribution(
    cases: tuple[ShadowCase, ...],
    *,
    now: datetime,
    active_window: timedelta,
) -> None:
    if len(cases) != 40:
        raise ValueError("Shadow manifest must contain exactly 40 cases")
    case_ids = [case.id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Shadow manifest contains a duplicate case ID")
    mode_counts = Counter(case.expected_mode for case in cases)
    if mode_counts != {SearchMode.FAST: 20, SearchMode.RESEARCH: 20}:
        raise ValueError("Shadow manifest must contain 20 FAST and 20 RESEARCH cases")
    strata = Counter((case.expected_mode, case.locale) for case in cases)
    expected_strata = {
        (SearchMode.FAST, "zh-CN"): 10,
        (SearchMode.FAST, "en"): 10,
        (SearchMode.RESEARCH, "zh-CN"): 10,
        (SearchMode.RESEARCH, "en"): 10,
    }
    if strata != expected_strata:
        raise ValueError("Each expected-mode/locale stratum must contain 10 cases")
    for stratum in expected_strata:
        answerable = sum(
            case.answerability is Answerability.ANSWERABLE
            for case in cases
            if (case.expected_mode, case.locale) == stratum
        )
        deterministic = sum(
            case.oracle_type is OracleType.DETERMINISTIC
            for case in cases
            if (case.expected_mode, case.locale) == stratum
        )
        if answerable < 5:
            raise ValueError(f"Stratum {stratum} must contain at least 5 answerable cases")
        if deterministic < 4:
            raise ValueError(f"Stratum {stratum} must contain at least 4 deterministic cases")
    unanswerable = sum(
        case.answerability is Answerability.INTENTIONALLY_UNANSWERABLE for case in cases
    )
    if unanswerable < 8:
        raise ValueError("Shadow manifest requires at least 8 unanswerable/conflict cases")
    regressions = sum(
        case.category is CaseCategory.POLLUTION_REGRESSION
        or "apex" in {tag.casefold() for tag in case.tags}
        for case in cases
    )
    if regressions < 6:
        raise ValueError("Shadow manifest requires at least 6 Apex/pollution cases")
    smoke = tuple(case for case in cases if case.smoke)
    if len(smoke) != 6:
        raise ValueError("Shadow manifest must contain exactly 6 smoke cases")
    smoke_modes = Counter(case.expected_mode for case in smoke)
    if smoke_modes != {SearchMode.FAST: 3, SearchMode.RESEARCH: 3}:
        raise ValueError("Smoke cases must contain 3 FAST and 3 RESEARCH cases")
    if not any(
        case.answerability is Answerability.INTENTIONALLY_UNANSWERABLE for case in smoke
    ):
        raise ValueError("Smoke cases require an intentionally unanswerable case")
    for case in cases:
        if case.oracle_type is not OracleType.DETERMINISTIC:
            continue
        assert case.valid_from is not None and case.valid_until is not None
        if case.valid_from > now or case.valid_until < now + active_window:
            raise ValueError(
                f"Case {case.id} oracle does not cover the active Campaign window"
            )


def parse_manifest_bytes(
    raw: bytes,
    *,
    now: datetime,
    active_window: timedelta = timedelta(hours=6),
) -> ShadowManifest:
    require_aware(now, "manifest validation time")
    if not raw:
        raise ValueError("Shadow manifest is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Shadow manifest must be UTF-8") from exc
    parsed: list[tuple[str, ShadowCase]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_json_object,
            )
        except json.JSONDecodeError as exc:
            raise ValueError(f"Manifest line {line_number} is invalid JSON") from exc
        except ValueError:
            raise
        parsed.append(_parse_case(value, line_number))
    if not parsed:
        raise ValueError("Shadow manifest is empty")
    versions = {version for version, _case in parsed}
    if len(versions) != 1:
        raise ValueError("All manifest cases must use one manifest_version")
    cases = tuple(case for _version, case in parsed)
    _validate_distribution(cases, now=now.astimezone(UTC), active_window=active_window)
    return ShadowManifest(
        version=next(iter(versions)),
        cases=cases,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
