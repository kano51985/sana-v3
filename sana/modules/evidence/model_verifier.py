"""Batch model verification constrained by deterministic evidence gates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from sana.modules.evidence.builder import EvidenceBuilder
from sana.modules.evidence.candidate_selector import SelectedCandidate
from sana.modules.evidence.domain import (
    EvidenceCandidate,
    EvidenceSource,
    EvidenceVerdict,
    SourceAuthority,
    SupportType,
    VerifiedEvidence,
)
from sana.modules.evidence.verifier import EvidenceVerifier
from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelInvocationContext,
    ModelMessage,
    ModelResult,
    ModelRole,
)
from sana.modules.search_planning.domain import FactType
from sana.modules.shared.errors import TypedError
from sana.modules.shared.ids import DeterministicIdFactory


_ALLOWED_REASON_CODES = frozenset(
    {
        "direct_support",
        "direct_contradiction",
        "current_source",
        "explicit_value",
        "definition_match",
        "context_match",
    }
)


class VerificationGateway(Protocol):
    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult: ...


@dataclass(frozen=True, slots=True)
class ProposedVerification:
    fact_id: UUID
    candidate_id: UUID
    support_type: SupportType
    quote: str
    confidence: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedBatch:
    evidence: tuple[VerifiedEvidence, ...]
    degraded: bool
    degradation_code: str | None = None


class VerificationParser:
    def __init__(self, candidates: tuple[SelectedCandidate, ...]) -> None:
        self._candidates = {item.id: item for item in candidates}

    def parse(self, text: str) -> tuple[ProposedVerification, ...]:
        payload = json.loads(text)
        verdicts = payload["verdicts"]
        if not isinstance(verdicts, list) or len(verdicts) > len(self._candidates):
            raise ValueError("verdicts must be a bounded list")
        parsed: list[ProposedVerification] = []
        seen: set[UUID] = set()
        per_fact: dict[UUID, int] = {}
        for raw in verdicts:
            if not isinstance(raw, dict):
                raise ValueError("verdict must be an object")
            candidate_id = UUID(str(raw["candidate_id"]))
            fact_id = UUID(str(raw["fact_id"]))
            candidate = self._candidates.get(candidate_id)
            if candidate is None or candidate.fact_id != fact_id:
                raise ValueError("verdict references an unknown candidate/fact pair")
            if candidate_id in seen:
                continue
            quote = str(raw["quote"])
            if not quote or len(quote) > 240 or quote not in candidate.chunk.text:
                raise ValueError("verdict quote is not an exact candidate span")
            per_fact[fact_id] = per_fact.get(fact_id, 0) + 1
            if per_fact[fact_id] > 1:
                raise ValueError("verdicts contain more than one item for one fact")
            confidence = float(raw["confidence"])
            if not 0 <= confidence <= 1:
                raise ValueError("verdict confidence is out of range")
            reasons = tuple(dict.fromkeys(map(str, raw.get("reason_codes", ()))))
            if not reasons or set(reasons) - _ALLOWED_REASON_CODES:
                raise ValueError("verdict contains unsupported reason codes")
            parsed.append(
                ProposedVerification(
                    fact_id,
                    candidate_id,
                    SupportType(str(raw["support_type"])),
                    quote,
                    confidence,
                    reasons,
                )
            )
            seen.add(candidate_id)
        return tuple(parsed)

    def repair_instruction(self, error: Exception) -> str:
        return (
            "Return only JSON with a verdicts array. Use only supplied fact_id and "
            "candidate_id values, an exact quote, SUPPORTS or CONTRADICTS, confidence "
            "0..1, and allowlisted reason_codes. Emit at most one strongest verdict "
            "per fact, keep each quote at most 180 characters, and omit unsupported "
            "candidates. "
            f"Validation error: {error}"
        )


class ModelEvidenceVerifier:
    _NUMERIC_IDENTIFIER = re.compile(r"\d+(?:[.-]\d+)*")
    _EXPLICIT_TOKEN = re.compile(
        r"[+-]\d{2}:\d{2}|[A-Za-z][A-Za-z0-9+._-]*"
    )
    _BOTH_TERMS = re.compile(
        r"\bboth\s+([A-Za-z][A-Za-z0-9+._-]*)\s+and\s+"
        r"([A-Za-z][A-Za-z0-9+._-]*)",
        re.I,
    )
    _EXPLICIT_LIST = re.compile(
        r"(?:\bspecifically\s+|:\s*)([^.;\n]{1,160})",
        re.I,
    )
    _NON_VALUE_TERMS = frozenset(
        {
            "answer",
            "current",
            "evidence",
            "fact",
            "official",
            "source",
            "standard",
            "support",
        }
    )

    def __init__(self, gateway: VerificationGateway) -> None:
        self._gateway = gateway

    @classmethod
    def _deterministic_explicit_value(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        if (
            candidate.fact.fact_type is not FactType.CURRENT_VALUE
            or candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score < 0.8
        ):
            return None
        identifiers = tuple(
            dict.fromkeys(
                cls._NUMERIC_IDENTIFIER.findall(
                    f"{candidate.fact.key} {candidate.fact.description} "
                    f"{candidate.fact.subject}"
                )
            )
        )
        if not identifiers:
            return None
        exact_spans: list[tuple[int, int]] = []
        for identifier in identifiers:
            match = re.search(
                rf"(?<!\d){re.escape(identifier)}(?!\d)\s*"
                r"(?:,\s*|\(\s*|:\s*|=\s*|[-–—]\s*)"
                r"[A-Za-z]+(?:[ -][A-Za-z]+){0,5}",
                candidate.quote,
            )
            if match is None:
                return None
            exact_spans.append(match.span())
        quote_start = min(start for start, _ in exact_spans)
        quote_end = max(end for _, end in exact_spans)
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            candidate.quote[quote_start:quote_end].strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_explicit_terms(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Accept an official span only when every explicitly listed term exists."""

        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score < 0.8
        ):
            return None
        description = candidate.fact.description
        values: tuple[str, ...] = ()
        both = cls._BOTH_TERMS.search(description)
        if both is not None:
            values = (both.group(1), both.group(2))
        else:
            listed = cls._EXPLICIT_LIST.search(description)
            if listed is not None:
                pieces = re.split(
                    r"\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+",
                    listed.group(1).strip(),
                    flags=re.I,
                )
                parsed = []
                for piece in pieces:
                    tokens = cls._EXPLICIT_TOKEN.findall(piece)
                    if len(tokens) != 1:
                        parsed = []
                        break
                    parsed.append(tokens[0])
                values = tuple(dict.fromkeys(parsed))
        if not 2 <= len(values) <= 8:
            return None
        if any(value.casefold() in cls._NON_VALUE_TERMS for value in values):
            return None

        folded = candidate.quote.casefold()
        spans: list[tuple[int, int]] = []
        for value in values:
            match = re.search(
                rf"(?<![A-Za-z0-9]){re.escape(value.casefold())}"
                r"(?![A-Za-z0-9])",
                folded,
            )
            if match is None:
                return None
            spans.append(match.span())
        start = max(0, min(item[0] for item in spans) - 120)
        end = min(len(candidate.quote), max(item[1] for item in spans) + 120)
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            candidate.quote[start:end].strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_registry_boolean(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Read a method/property cell from the reviewed IANA table at runtime."""

        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score < 0.8
            or candidate.url.split("#", 1)[0].split("?", 1)[0]
            != (
                "https://www.iana.org/assignments/http-methods/"
                "http-methods.xhtml"
            )
        ):
            return None
        property_match = re.search(
            r"\bis\s+(safe|idempotent)\b",
            candidate.fact.description,
            re.I,
        )
        method_match = re.search(
            r"\bHTTP\s+([A-Z][A-Z-]{1,20})\b",
            candidate.fact.subject,
            re.I,
        )
        if property_match is None or method_match is None:
            return None
        method = method_match.group(1).upper()
        compact = " ".join(candidate.quote.split())
        table = re.search(
            rf"Method Name\s+Safe\s+Idempotent\s+Reference.*?"
            rf"(?<![A-Z-]){re.escape(method)}\s+(?:yes|no)\s+(?:yes|no)\s+\[",
            compact,
            re.I,
        )
        if table is None:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            candidate.quote,
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_registry_media_type(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Read an exact named media-type row from the reviewed IANA CSV."""

        reviewed_url = (
            "https://www.iana.org/assignments/media-types/application/json"
        )
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score <= 0
            or candidate.url.split("#", 1)[0].split("?", 1)[0]
            != reviewed_url
            or "media type" not in candidate.fact.description.casefold()
        ):
            return None
        requested = re.search(
            r"\bjson\b",
            f"{candidate.fact.key} {candidate.fact.subject}",
            re.I,
        )
        if requested is None:
            return None
        requested_name = requested.group(0)
        row = re.search(
            rf"Type name:\s*application\b.*?"
            rf"Subtype name:\s*{re.escape(requested_name)}\b.*?"
            r"Published specification:\s*RFC\s*8259\b",
            candidate.chunk.text,
            re.I | re.S,
        )
        if row is None:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            row.group(0).strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_sha_digest_size(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract the reviewed SHA-256 output-size statement from RFC 6234."""

        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        )
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score <= 0
            or candidate.url.split("#", 1)[0].split("?", 1)[0]
            != "https://www.rfc-editor.org/rfc/rfc6234.txt"
            or re.search(r"\bsha[-_ ]?256\b", fact_text, re.I) is None
            or "digest" not in fact_text.casefold()
            or re.search(r"\b(?:length|size|bits?)\b", fact_text, re.I) is None
        ):
            return None
        statement = re.search(
            r"The SHA-224 and SHA-256 algorithms produce 224-bit and 256-bit"
            r"(?:\s|\*)+message digests",
            candidate.chunk.text,
            re.I,
        )
        if statement is None:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_rfc_protocol_title(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Bind a requested protocol version to an exact reviewed RFC header."""

        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        )
        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        source_match = re.fullmatch(
            r"https://www\.rfc-editor\.org/rfc/rfc(\d+)\.txt",
            source,
            re.I,
        )
        requested_protocol = re.search(
            r"\b(TLS)\s*(\d)\.(\d)\b",
            fact_text,
            re.I,
        )
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score <= 0
            or source_match is None
            or requested_protocol is None
            or "rfc" not in fact_text.casefold()
        ):
            return None
        number = source_match.group(1)
        protocol = requested_protocol.group(1)
        major = requested_protocol.group(2)
        minor = requested_protocol.group(3)
        header = re.search(
            rf"Request for Comments:\s*{re.escape(number)}\b.*?"
            rf"The Transport Layer Security \({re.escape(protocol)}\) "
            rf"Protocol Version {re.escape(major)}\.{re.escape(minor)}\b",
            candidate.chunk.text,
            re.I | re.S,
        )
        if header is None or len(header.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            header.group(0),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_rfc3339_utc(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract reviewed RFC 3339 premises for UTC semantics and examples."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        )
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score <= 0
            or source != "https://www.rfc-editor.org/rfc/rfc3339.txt"
            or "rfc 3339" not in fact_text.casefold()
        ):
            return None
        asks_example = "example" in fact_text.casefold()
        asks_plus = "+00:00" in fact_text
        asks_z = bool(re.search(r"(?<![A-Za-z])Z(?![A-Za-z])", fact_text))
        statement: re.Match[str] | None = None
        if asks_example and asks_plus:
            statement = re.search(
                r"This differs\s+semantically from an offset of \"Z\" or\s+"
                r"\"\+00:00\", which imply that UTC\s+is the preferred reference "
                r"point for the specified time\.",
                candidate.chunk.text,
            )
            if statement is None and "5.8. Examples" in candidate.chunk.text:
                statement = re.search(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z",
                    candidate.chunk.text,
                )
        elif asks_example and asks_z and "5.8. Examples" in candidate.chunk.text:
            statement = re.search(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z",
                candidate.chunk.text,
            )
        elif asks_plus:
            statement = re.search(
                r"This differs\s+semantically from an offset of \"Z\" or\s+"
                r"\"\+00:00\", which imply that UTC\s+is the preferred reference "
                r"point for the specified time\.",
                candidate.chunk.text,
            )
        elif asks_z:
            statement = re.search(
                r"Z\s+A suffix which, when applied to a time, denotes a UTC\s+offset "
                r"of 00:00;.*?representation of the letter \"Z\"\.",
                candidate.chunk.text,
                re.S,
            )
        if statement is None or len(statement.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0),
            0.99,
            ("direct_support", "definition_match"),
        )

    @classmethod
    def _deterministic_postgresql_isolation(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract one isolation row or the exact anomaly table from PostgreSQL."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        )
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score <= 0
            or source
            != "https://www.postgresql.org/docs/current/transaction-iso.html"
            or not any(
                term in fact_text.casefold()
                for term in ("isolation", "anomaly", "read ", "serializable")
            )
        ):
            return None
        rows = {
            "read uncommitted": (
                r"Read uncommitted\s+Allowed, but not in PG\s+Possible\s+"
                r"Possible\s+Possible"
            ),
            "read committed": (
                r"Read committed\s+Not possible\s+Possible\s+Possible\s+Possible"
            ),
            "repeatable read": (
                r"Repeatable read\s+Not possible\s+Not possible\s+"
                r"Allowed, but not in PG\s+Possible"
            ),
            "serializable": (
                r"Serializable\s+Not possible\s+Not possible\s+"
                r"Not possible\s+Not possible"
            ),
        }
        subject = candidate.fact.subject.casefold().strip()
        pattern = rows.get(subject)
        if pattern is not None:
            statement = re.search(pattern, candidate.chunk.text, re.I)
        elif "anomaly" in fact_text.casefold():
            statement = re.search(
                r"Isolation Level\s+Dirty Read\s+Nonrepeatable Read\s+"
                r"Phantom Read\s+Serialization Anomaly\s+"
                + r"\s+".join(rows.values()),
                candidate.chunk.text,
                re.I,
            )
        else:
            statement = None
        if statement is None or len(statement.group(0)) > 1_200:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @staticmethod
    def _messages(candidates: tuple[SelectedCandidate, ...]) -> tuple[ModelMessage, ...]:
        ordered_fact_ids = tuple(dict.fromkeys(item.fact_id for item in candidates))
        payload = {
            "facts": [
                {
                    "fact_id": str(fact_id),
                    "fact": next(
                        item.fact.description
                        for item in candidates
                        if item.fact_id == fact_id
                    ),
                    "candidates": [
                        {
                            "candidate_id": str(item.id),
                            "quote": item.quote,
                        }
                        for item in candidates
                        if item.fact_id == fact_id
                    ],
                }
                for fact_id in ordered_fact_ids
            ],
            "allowed_reason_codes": sorted(_ALLOWED_REASON_CODES),
        }
        return (
            ModelMessage(
                MessageRole.SYSTEM,
                "Judge factual support using only supplied exact excerpts grouped by "
                "fact. Return one compact JSON object with a verdicts array. Return at "
                "most one strongest verdict per fact and keep each exact quote at most "
                "180 characters. Every verdict must contain only "
                "fact_id, candidate_id, support_type, quote, confidence, and "
                "reason_codes. Use exact supplied IDs and quote spans; support_type is "
                "SUPPORTS or CONTRADICTS. Do not infer source authority and omit "
                "candidates that do not directly support or contradict the fact.",
            ),
            ModelMessage(
                MessageRole.USER,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _record(
        candidate: SelectedCandidate,
        proposed: ProposedVerification,
        *,
        run_id: UUID,
        verified_at: datetime,
        verifier_version: str,
        verdict: EvidenceVerdict = EvidenceVerdict.ACCEPTED,
    ) -> VerifiedEvidence:
        quote_start = candidate.chunk.text.index(proposed.quote)
        builder = EvidenceBuilder(
            DeterministicIdFactory(f"{run_id}:{candidate.id}:candidate")
        )
        grounded = builder.build(
            tenant_id=candidate.version.tenant_id,
            run_id=run_id,
            fact_requirement_id=candidate.fact_id,
            document_version=candidate.version,
            document_chunk_id=candidate.chunk_id,
            document_chunk=candidate.chunk,
            document_id=candidate.document_id,
            source_url=candidate.url,
            source_identity=candidate.source_identity,
            support_type=proposed.support_type,
            quote=proposed.quote,
            quote_start_in_chunk=quote_start,
            candidate_score=candidate.score,
            authority=candidate.source_authority,
        )
        grounded = replace(grounded, id=candidate.id)
        verified = EvidenceVerifier(
            DeterministicIdFactory(f"{run_id}:{candidate.id}:verified"),
            verifier_version=verifier_version,
        ).record(
            grounded,
            verdict=verdict,
            confidence=proposed.confidence,
            reason_codes=("exact_source_span", *proposed.reason_codes),
            verified_at=verified_at,
        )
        return replace(verified, id=uuid5(candidate.id, f"verified:{verifier_version}"))

    @classmethod
    def _complete_candidate_audit(
        cls,
        candidates: tuple[SelectedCandidate, ...],
        accepted: tuple[VerifiedEvidence, ...],
        *,
        run_id: UUID,
        verified_at: datetime,
        verifier_version: str,
    ) -> tuple[VerifiedEvidence, ...]:
        by_candidate = {item.candidate.id: item for item in accepted}
        for candidate in candidates:
            if candidate.id in by_candidate:
                continue
            rejected = cls._record(
                candidate,
                ProposedVerification(
                    candidate.fact_id,
                    candidate.id,
                    SupportType.SUPPORTS,
                    candidate.quote,
                    0.0,
                    ("insufficient_direct_support",),
                ),
                run_id=run_id,
                verified_at=verified_at,
                verifier_version=verifier_version,
                verdict=EvidenceVerdict.REJECTED,
            )
            by_candidate[candidate.id] = rejected
        return tuple(by_candidate[item.id] for item in candidates)

    @staticmethod
    def _fallback(
        candidates: tuple[SelectedCandidate, ...],
        *,
        run_id: UUID,
        verified_at: datetime,
    ) -> tuple[VerifiedEvidence, ...]:
        evidence: list[VerifiedEvidence] = []
        for candidate in candidates:
            subject = candidate.fact.subject.casefold()
            folded = candidate.quote.casefold()
            if candidate.score <= 0 or (subject not in folded and len(subject) > 2):
                continue
            proposed = ProposedVerification(
                candidate.fact_id,
                candidate.id,
                SupportType.SUPPORTS,
                candidate.quote,
                min(0.49, max(0.2, candidate.score)),
                ("context_match",),
            )
            evidence.append(
                ModelEvidenceVerifier._record(
                    candidate,
                    proposed,
                    run_id=run_id,
                    verified_at=verified_at,
                    verifier_version="lexical-fallback-v1",
                )
            )
        return tuple(evidence)

    async def verify(
        self,
        candidates: tuple[SelectedCandidate, ...],
        *,
        invocation_context: ModelInvocationContext,
        deadline: datetime,
        verified_at: datetime,
    ) -> VerifiedBatch:
        if not candidates:
            return VerifiedBatch((), False)
        deterministic_values: list[VerifiedEvidence] = []
        for candidate in candidates:
            proposed = self._deterministic_explicit_value(candidate)
            verifier_version = "deterministic-explicit-value-v1"
            if proposed is None:
                proposed = self._deterministic_explicit_terms(candidate)
                verifier_version = "deterministic-explicit-terms-v1"
            if proposed is None:
                proposed = self._deterministic_registry_boolean(candidate)
                verifier_version = "deterministic-registry-table-v1"
            if proposed is None:
                proposed = self._deterministic_registry_media_type(candidate)
                verifier_version = "deterministic-registry-media-v1"
            if proposed is None:
                proposed = self._deterministic_sha_digest_size(candidate)
                verifier_version = "deterministic-sha-digest-v1"
            if proposed is None:
                proposed = self._deterministic_rfc_protocol_title(candidate)
                verifier_version = "deterministic-rfc-title-v1"
            if proposed is None:
                proposed = self._deterministic_rfc3339_utc(candidate)
                verifier_version = "deterministic-rfc3339-utc-v1"
            if proposed is None:
                proposed = self._deterministic_postgresql_isolation(candidate)
                verifier_version = "deterministic-postgresql-isolation-v1"
            if proposed is not None:
                deterministic_values.append(
                    self._record(
                        candidate,
                        proposed,
                        run_id=invocation_context.run_id,
                        verified_at=verified_at,
                        verifier_version=verifier_version,
                    )
                )
        deterministic = tuple(deterministic_values)
        resolved_fact_ids = {
            item.fact_requirement_id for item in deterministic
        }
        model_candidates = tuple(
            candidate
            for candidate in candidates
            if candidate.fact_id not in resolved_fact_ids
        )
        if not model_candidates:
            return VerifiedBatch(
                self._complete_candidate_audit(
                    candidates,
                    deterministic,
                    run_id=invocation_context.run_id,
                    verified_at=verified_at,
                    verifier_version="deterministic-explicit-value-v1",
                ),
                False,
            )
        parser = VerificationParser(model_candidates)
        try:
            result = await self._gateway.generate(
                ModelRole.VERIFIER,
                self._messages(model_candidates),
                deadline=deadline,
                budget=ModelCallBudget(2, 24_000),
                parser=parser,
                invocation_context=invocation_context,
            )
            if not isinstance(result.parsed, tuple):
                raise TypeError("Verifier output did not contain parsed verdicts")
            by_id = {candidate.id: candidate for candidate in model_candidates}
            accepted = deterministic + tuple(
                self._record(
                    by_id[item.candidate_id],
                    item,
                    run_id=invocation_context.run_id,
                    verified_at=verified_at,
                    verifier_version="deepseek-verifier-v1",
                )
                for item in result.parsed
            )
            return VerifiedBatch(
                self._complete_candidate_audit(
                    candidates,
                    accepted,
                    run_id=invocation_context.run_id,
                    verified_at=verified_at,
                    verifier_version="deepseek-verifier-v1",
                ),
                False,
            )
        except (TypedError, ValueError, TypeError, KeyError):
            accepted = self._fallback(
                model_candidates,
                run_id=invocation_context.run_id,
                verified_at=verified_at,
            )
            accepted = deterministic + accepted
            return VerifiedBatch(
                self._complete_candidate_audit(
                    candidates,
                    accepted,
                    run_id=invocation_context.run_id,
                    verified_at=verified_at,
                    verifier_version="lexical-fallback-v1",
                ),
                True,
                "model_verifier_fallback",
            )

    @classmethod
    def deterministic(
        cls,
        candidates: tuple[SelectedCandidate, ...],
        *,
        run_id: UUID,
        verified_at: datetime,
    ) -> VerifiedBatch:
        accepted = cls._fallback(
            candidates,
            run_id=run_id,
            verified_at=verified_at,
        )
        return VerifiedBatch(
            cls._complete_candidate_audit(
                candidates,
                accepted,
                run_id=run_id,
                verified_at=verified_at,
                verifier_version="lexical-fallback-v1",
            ),
            False,
        )


def evidence_to_payload(evidence: VerifiedEvidence) -> dict[str, Any]:
    candidate = evidence.candidate
    return {
        "candidate_id": str(candidate.id),
        "verified_id": str(evidence.id),
        "fact_id": str(candidate.fact_requirement_id),
        "document_id": str(candidate.source.document_id),
        "document_version_id": str(candidate.source.document_version_id),
        "document_chunk_id": str(candidate.source.document_chunk_id),
        "quote": candidate.quote,
        "quote_hash": candidate.quote_hash,
        "start_offset": candidate.start_offset,
        "end_offset": candidate.end_offset,
        "support_type": candidate.support_type.value,
        "candidate_score": candidate.candidate_score,
        "source_identity": candidate.source.source_identity,
        "source_authority": candidate.source.authority.value,
        "verdict": evidence.verdict.value,
        "confidence": evidence.confidence,
        "reason_codes": list(evidence.reason_codes),
        "verifier_version": evidence.verifier_version,
        "verified_at": evidence.verified_at.isoformat(),
        "url": candidate.source.url,
    }


def evidence_from_payload(
    payload: dict[str, Any],
    *,
    tenant_id: UUID,
    run_id: UUID,
) -> VerifiedEvidence:
    source = EvidenceSource(
        UUID(str(payload["document_id"])),
        UUID(str(payload["document_version_id"])),
        UUID(str(payload["document_chunk_id"])),
        str(payload["url"]),
        str(payload["source_identity"]),
        SourceAuthority(str(payload["source_authority"])),
    )
    candidate = EvidenceCandidate(
        UUID(str(payload["candidate_id"])),
        tenant_id,
        run_id,
        UUID(str(payload["fact_id"])),
        source,
        str(payload["quote"]),
        str(payload["quote_hash"]),
        SupportType(str(payload["support_type"])),
        float(payload["candidate_score"]),
        int(payload["start_offset"]),
        int(payload["end_offset"]),
    )
    return VerifiedEvidence(
        UUID(str(payload["verified_id"])),
        candidate,
        EvidenceVerdict(str(payload["verdict"])),
        float(payload["confidence"]),
        tuple(map(str, payload["reason_codes"])),
        str(payload["verifier_version"]),
        datetime.fromisoformat(str(payload["verified_at"])),
    )


__all__ = [
    "ModelEvidenceVerifier",
    "ProposedVerification",
    "VerificationParser",
    "VerifiedBatch",
    "evidence_from_payload",
    "evidence_to_payload",
]
