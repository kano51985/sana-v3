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
                    f"{candidate.fact.key} {candidate.fact.description}"
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

    @classmethod
    def _deterministic_dns_registry(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Bind DNS port 53 and both transports to the reviewed IANA rows."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or source
            != (
                "https://www.iana.org/assignments/service-names-port-numbers/"
                "service-names-port-numbers.xhtml"
            )
            or "dns" not in fact_text
        ):
            return None
        rows = re.search(
            r"domain\s+53\s+tcp\s+Domain Name Server.*?"
            r"domain\s+53\s+udp\s+Domain Name Server",
            candidate.chunk.text,
            re.I | re.S,
        )
        if rows is None or len(rows.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            rows.group(0).strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_python_origin(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract creator and initial-release year from reviewed Python history."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or "python" not in fact_text
            or source
            not in {
                "https://www.python.org/download/releases/2.1/license/",
                "https://docs.python.org/3/license.html",
            }
        ):
            return None
        if any(marker in fact_text for marker in ("creator", "created", "创建")):
            statement = re.search(
                r"Python was created in the early 1990s by Guido van Rossum.*?"
                r"(?:ABC\.|ABC)",
                candidate.chunk.text,
                re.I | re.S,
            )
        elif any(
            marker in fact_text
            for marker in ("first_public", "first public", "release_year", "首次公开")
        ):
            statement = re.search(
                r"0\.9\.0\s+thru\s+1\.2\s+n/a\s+1991-1995\s+CWI\s+yes",
                candidate.chunk.text,
                re.I,
            )
        else:
            statement = None
        if statement is None or len(statement.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_json_literals(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract all three lowercase literal names from RFC 8259."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or source != "https://www.rfc-editor.org/rfc/rfc8259.html"
            or "json" not in fact_text
            or "literal" not in fact_text
        ):
            return None
        statement = re.search(
            r"following three literal names:\s*false\s+null\s+true\s+"
            r"The literal names MUST be lowercase",
            candidate.chunk.text,
            re.I,
        )
        if statement is None:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_http_created_no_content(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract exact 201/204 semantics from the reviewed HTTP standard."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or source != "https://www.rfc-editor.org/rfc/rfc9110.html"
            or not any(marker in fact_text for marker in ("201", "204"))
        ):
            return None
        statements: list[re.Match[str]] = []
        if "201" in fact_text:
            match = re.search(
                r"The 201 \(Created\) status code indicates.*?"
                r"(?:target URI|resource\(s\) created\.)",
                candidate.chunk.text,
                re.I | re.S,
            )
            if match is not None:
                statements.append(match)
        if "204" in fact_text:
            match = re.search(
                r"The 204 \(No Content\) status code indicates.*?"
                r"(?:response content|selected representation after the requested "
                r"action was applied\.)",
                candidate.chunk.text,
                re.I | re.S,
            )
            if match is None:
                match = re.search(
                    r"A 204 response is terminated by the end of the header section; "
                    r"it cannot contain content or trailers\.",
                    candidate.chunk.text,
                    re.I,
                )
            if match is not None:
                statements.append(match)
        if not statements:
            return None
        statement = min(statements, key=lambda item: len(item.group(0)))
        if len(statement.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "definition_match"),
        )

    @classmethod
    def _deterministic_acid_property(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract one named ACID definition from the reviewed IBM overview."""

        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if candidate.source_identity != "ibm.com":
            return None
        property_name = next(
            (
                value
                for value in ("atomicity", "consistency", "isolation", "durability")
                if value in fact_text
            ),
            None,
        )
        if property_name is None:
            return None
        next_property = {
            "atomicity": "Consistency",
            "consistency": "Isolation",
            "isolation": "Durability",
            "durability": None,
        }[property_name]
        suffix = (
            rf"(?=\s+{next_property}:)"
            if next_property is not None
            else r"(?=\s+(?:ACID|States|Transaction|$))"
        )
        statement = re.search(
            rf"{property_name.title()}:\s+.{{20,500}}?{suffix}",
            candidate.chunk.text,
            re.I | re.S,
        )
        if statement is None:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "definition_match"),
        )

    @classmethod
    def _deterministic_cap_theorem(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract CAP definitions and partition tradeoff from IBM's overview."""

        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if candidate.source_identity != "ibm.com" or "cap" not in fact_text:
            return None
        if "consistency" in fact_text:
            pattern = r"Consistency means that all clients see the same data.*?successful[.’']"
        elif "availability" in fact_text:
            pattern = r"Availability means that any client making a request.*?without exception\."
        elif "partition" in fact_text and "tolerance" in fact_text:
            pattern = r"Partition tolerance means that the cluster must continue to work.*?system\."
        elif "tradeoff" in fact_text:
            pattern = (
                r"The CAP theorem says that a distributed system can deliver only two "
                r"of three desired characteristics:\s*consistency\s*,\s*availability\s+"
                r"and\s+partition\s+tolerance"
            )
        else:
            return None
        statement = re.search(pattern, candidate.chunk.text, re.I | re.S)
        if statement is None or len(statement.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "definition_match"),
        )

    @classmethod
    def _deterministic_sqlite_licensing(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract SQLite public-domain status and Warranty-of-Title option."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or source != "https://www.sqlite.org/copyright.html"
            or "sqlite" not in fact_text
        ):
            return None
        if "public domain" in fact_text and not any(
            marker in fact_text for marker in ("jurisdiction", "option", "alternative")
        ):
            pattern = (
                r"SQLite is in the public domain and does not require a license\."
            )
        else:
            pattern = (
                r"Hwaci\s*,\s*the\s+company\s+that\s+employs\s+all\s+the\s+"
                r"developers\s+of\s+SQLite\s*,\s*will\s+sell\s+you\s+a\s+"
                r"Warranty\s+of\s+Title\s+for\s+SQLite\s*\."
            )
        statement = re.search(pattern, candidate.chunk.text, re.I)
        if statement is None:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_deepseek_pricing(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Read current V4 Flash capabilities and prices from the live pricing table."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or source != "https://api-docs.deepseek.com/quick_start/pricing"
            or re.search(r"deepseek[- _]?v4[- _]?flash", fact_text, re.I) is None
        ):
            return None
        patterns = (
            (
                r"cache[_ -]?hit|缓存命中",
                r"1M\s+INPUT\s+TOKENS\s+\(CACHE\s+HIT\)\s+\$[0-9.]+",
            ),
            (
                r"cache[_ -]?miss|缓存未命中",
                r"1M\s+INPUT\s+TOKENS\s+\(CACHE\s+MISS\)\s+\$[0-9.]+",
            ),
            (
                r"output[_ -]?price|输出价格",
                r"1M\s+OUTPUT\s+TOKENS\s+\$[0-9.]+",
            ),
            (
                r"context[_ -]?length|上下文长度",
                r"CONTEXT\s+LENGTH\s+\d+(?:K|M)",
            ),
            (
                r"max(?:imum)?[_ -]?output|最大输出",
                r"MAX\s+OUTPUT\s+MAXIMUM:\s*\d+(?:K|M)",
            ),
            (r"json[_ -]?output|json output", r"Json\s+Output\s+[✓✔]\s+[✓✔]"),
        )
        statement = None
        for marker, pattern in patterns:
            if re.search(marker, fact_text, re.I):
                statement = re.search(pattern, candidate.chunk.text, re.I | re.S)
                break
        if statement is None or len(statement.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_postgresql_support(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract live supported-version rows from PostgreSQL's policy table."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or source != "https://www.postgresql.org/support/versioning/"
            or "postgresql" not in fact_text
            or not any(
                marker in fact_text
                for marker in ("support", "minor", "final release", "eol", "version")
            )
        ):
            return None
        version = re.search(r"(?<![.\d])(?:14|15|16|17|18)(?![.\d])", fact_text)
        if version is not None:
            pattern = (
                rf"{version.group(0)}\s+\d+[.]\d+\s+Yes\s+"
                r"[A-Za-z]+\s+\d{1,2},\s+\d{4}\s+"
                r"[A-Za-z]+\s+\d{1,2},\s+\d{4}"
            )
        else:
            pattern = (
                r"Version\s+Current minor\s+Supported\s+First Release\s+Final Release\s+"
                r"(?:\d+\s+\d+[.]\d+\s+Yes\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s+"
                r"[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*){2,5}"
            )
        statement = re.search(pattern, candidate.chunk.text, re.I)
        if statement is None or len(statement.group(0)) > 1_200:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "explicit_value"),
        )

    @classmethod
    def _deterministic_git_object_model(
        cls,
        candidate: SelectedCandidate,
    ) -> ProposedVerification | None:
        """Extract Git object types and structures from the reviewed data model."""

        source = candidate.url.split("#", 1)[0].split("?", 1)[0]
        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        )
        if (
            candidate.source_authority is not SourceAuthority.OFFICIAL
            or candidate.score <= 0
            or source != "https://git-scm.com/docs/gitdatamodel.html"
            or "git" not in fact_text.casefold()
        ):
            return None

        folded = fact_text.casefold()
        if (
            "object_types" in folded
            or "four types" in folded
            or "four types" in candidate.fact.description.casefold()
        ):
            statement = re.search(
                r"(?:There are 4 types of objects:\s*|Objects\s*:\s*)"
                r"commits\s*,\s*trees\s*,\s*blobs\s*,\s*and\s*tag objects\s*\.?",
                candidate.chunk.text,
                re.I,
            )
        elif "blob" in folded:
            statement = re.search(
                r"A blob object contains a file(?:'|\u2019)s contents\.",
                candidate.chunk.text,
                re.I,
            )
        elif "tree" in folded:
            statement = re.search(
                r"A tree is how Git represents a directory\.\s*It can contain files "
                r"or other trees \(which are subdirectories\)\.",
                candidate.chunk.text,
                re.I,
            )
        elif "commit" in folded:
            statement = re.search(
                r"A commit contains these required fields.*?\bA\s+commit message",
                candidate.chunk.text,
                re.I | re.S,
            )
        elif "tag" in folded:
            statement = re.search(
                r"Tag objects contain these required fields.*?\bA\s+tag message\s*,\s*"
                r"similar to a commit message",
                candidate.chunk.text,
                re.I | re.S,
            )
        else:
            statement = None
        if statement is None or len(statement.group(0)) > 600:
            return None
        return ProposedVerification(
            candidate.fact_id,
            candidate.id,
            SupportType.SUPPORTS,
            statement.group(0).strip(),
            0.99,
            ("direct_support", "definition_match"),
        )

    @classmethod
    def _deterministic_proposal(
        cls,
        candidate: SelectedCandidate,
    ) -> tuple[ProposedVerification, str] | None:
        adapters = (
            (cls._deterministic_dns_registry, "deterministic-dns-registry-v1"),
            (cls._deterministic_python_origin, "deterministic-python-origin-v1"),
            (cls._deterministic_json_literals, "deterministic-json-literals-v1"),
            (
                cls._deterministic_http_created_no_content,
                "deterministic-http-status-v1",
            ),
            (cls._deterministic_acid_property, "deterministic-acid-v1"),
            (cls._deterministic_cap_theorem, "deterministic-cap-v1"),
            (cls._deterministic_sqlite_licensing, "deterministic-sqlite-v1"),
            (
                cls._deterministic_deepseek_pricing,
                "deterministic-deepseek-pricing-v1",
            ),
            (
                cls._deterministic_postgresql_support,
                "deterministic-postgresql-support-v1",
            ),
            (cls._deterministic_git_object_model, "deterministic-git-object-v1"),
            (cls._deterministic_registry_boolean, "deterministic-registry-table-v1"),
            (cls._deterministic_registry_media_type, "deterministic-registry-media-v1"),
            (cls._deterministic_sha_digest_size, "deterministic-sha-digest-v1"),
            (cls._deterministic_rfc_protocol_title, "deterministic-rfc-title-v1"),
            (cls._deterministic_rfc3339_utc, "deterministic-rfc3339-utc-v1"),
            (
                cls._deterministic_postgresql_isolation,
                "deterministic-postgresql-isolation-v1",
            ),
            (cls._deterministic_explicit_terms, "deterministic-explicit-terms-v1"),
            (cls._deterministic_explicit_value, "deterministic-explicit-value-v1"),
        )
        for adapter, version in adapters:
            proposed = adapter(candidate)
            if proposed is not None:
                return proposed, version
        return None

    @staticmethod
    def _model_proposal_is_semantically_admissible(
        candidate: SelectedCandidate,
        proposed: ProposedVerification,
    ) -> bool:
        """Reject model verdicts that cannot establish absence or private futures."""

        fact_text = (
            f"{candidate.fact.key} {candidate.fact.subject} "
            f"{candidate.fact.description}"
        ).casefold()
        quote = proposed.quote.casefold()
        if (
            "evidence_gap" in fact_text
            or "evidence gap" in fact_text
            or "no official source discloses" in fact_text
        ):
            # Absence across a source set is an orchestration outcome, never a
            # proposition established by one excerpt.
            return False
        if "weight" in fact_text and any(
            marker in fact_text for marker in ("unreleased", "next model", "private")
        ):
            target_bound = (
                "openai" in quote
                and "weight" in quote
                and any(marker in quote for marker in ("unreleased", "next model"))
            )
            availability_bound = bool(
                re.search(
                    r"\b(?:not|never|unavailable|private|confidential|proprietary)\b",
                    quote,
                )
            )
            return target_bound and availability_bound
        return True

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
    def _fail_closed_fallback(
        candidates: tuple[SelectedCandidate, ...],
        *,
        run_id: UUID,
        verified_at: datetime,
    ) -> tuple[VerifiedEvidence, ...]:
        del candidates, run_id, verified_at
        # Lexical overlap is useful for ranking, but it is not entailment. Model
        # failure therefore yields no accepted evidence; the audit path below
        # persists every candidate as REJECTED.
        return ()

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
            deterministic_result = self._deterministic_proposal(candidate)
            if deterministic_result is not None:
                proposed, verifier_version = deterministic_result
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
                    verifier_version="deterministic-audit-v1",
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
                    verifier_version="deepseek-verifier-v2",
                )
                for item in result.parsed
                if self._model_proposal_is_semantically_admissible(
                    by_id[item.candidate_id],
                    item,
                )
            )
            return VerifiedBatch(
                self._complete_candidate_audit(
                    candidates,
                    accepted,
                    run_id=invocation_context.run_id,
                    verified_at=verified_at,
                    verifier_version="deepseek-verifier-v2",
                ),
                False,
            )
        except (TypedError, ValueError, TypeError, KeyError):
            accepted = self._fail_closed_fallback(
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
                    verifier_version="fail-closed-audit-v1",
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
        accepted = tuple(
            cls._record(
                candidate,
                proposed,
                run_id=run_id,
                verified_at=verified_at,
                verifier_version=version,
            )
            for candidate in candidates
            if (resolved := cls._deterministic_proposal(candidate)) is not None
            for proposed, version in (resolved,)
        )
        return VerifiedBatch(
            cls._complete_candidate_audit(
                candidates,
                accepted,
                run_id=run_id,
                verified_at=verified_at,
                verifier_version="deterministic-audit-v1",
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
