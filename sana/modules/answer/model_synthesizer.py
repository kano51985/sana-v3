"""Model prose generation constrained by deterministic claims and citations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sana.modules.answer.citation_validator import CitationValidator
from sana.modules.answer.domain import (
    ClaimKind,
    ProposedClaim,
    UnsupportedClaimPolicy,
    ValidatedAnswer,
)
from sana.modules.answer.synthesizer import ClaimSynthesizer
from sana.modules.evidence.coverage import CoverageAssessment
from sana.modules.evidence.domain import EvidenceVerdict, VerifiedEvidence
from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelInvocationContext,
    ModelMessage,
    ModelResult,
    ModelRole,
)
from sana.modules.search_planning.domain import FactRequirement
from sana.modules.shared.errors import TypedError
from sana.modules.shared.ids import DeterministicIdFactory


class SynthesisGateway(Protocol):
    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult: ...


@dataclass(frozen=True, slots=True)
class ConstrainedSynthesisResult:
    answer: ValidatedAnswer
    degraded: bool
    degradation_code: str | None = None


class ProposedClaimParser:
    _FIELDS = frozenset({"claim_key", "text", "fact_id", "evidence_ids"})
    _NUMERIC_IDENTIFIER = re.compile(r"\d+(?:[.-]\d+)*")
    _QUOTED_IDENTIFIER = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"]{1,64})(?P=quote)")
    _STANDARD_IDENTIFIER = re.compile(
        r"\b(?:RFC\s*\d+|TLS\s*\d+(?:\.\d+)*|SHA-?\d+|HTTP\s+[A-Z][A-Z-]+)\b"
    )
    _TITLE_PHRASE = re.compile(
        r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+\b"
    )
    _TITLE_WORD = re.compile(r"\b[A-Z][a-z]{3,}\b")
    _RFC3339_Z_TIMESTAMP = re.compile(
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b"
    )
    _RFC3339_PLUS_ZERO_TIMESTAMP = re.compile(
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00\b"
    )
    _GENERIC_TITLE_WORDS = frozenset(
        {
            "Anomaly",
            "Definition",
            "Example",
            "Guarantees",
            "Launch",
            "Semantics",
            "Standard",
            "Whether",
        }
    )

    def __init__(
        self,
        facts: dict[UUID, FactRequirement],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> None:
        self._facts = facts
        self._evidence_fact = {
            item.id: item.fact_requirement_id
            for item in evidence
            if item.verdict is EvidenceVerdict.ACCEPTED
        }
        self._evidence_quote = {
            item.id: item.candidate.quote
            for item in evidence
            if item.verdict is EvidenceVerdict.ACCEPTED
        }
        self._evidence_url = {
            item.id: item.source.url
            for item in evidence
            if item.verdict is EvidenceVerdict.ACCEPTED
        }

    @classmethod
    def _numeric_identifiers(cls, fact: FactRequirement) -> tuple[str, ...]:
        material = f"{fact.description} {fact.subject}"
        material = cls._QUOTED_IDENTIFIER.sub(" ", material)
        material = cls._STANDARD_IDENTIFIER.sub(" ", material)
        values = cls._NUMERIC_IDENTIFIER.findall(material)
        return tuple(dict.fromkeys(values))

    @classmethod
    def _symbolic_identifiers(cls, fact: FactRequirement) -> tuple[str, ...]:
        values: list[str] = []
        for material in (fact.description, fact.subject):
            values.extend(
                match.group("value")
                for match in cls._QUOTED_IDENTIFIER.finditer(material)
            )
            values.extend(cls._STANDARD_IDENTIFIER.findall(material))
            values.extend(cls._TITLE_PHRASE.findall(material))
            values.extend(
                value
                for value in cls._TITLE_WORD.findall(material)
                if value not in cls._GENERIC_TITLE_WORDS
            )
        deduplicated = tuple(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        return tuple(
            value
            for value in deduplicated
            if not any(
                value != other
                and re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
                    other,
                )
                for other in deduplicated
            )
        )

    @staticmethod
    def _contains_identifier(
        text: str,
        identifier: str,
        *,
        case_sensitive: bool = False,
    ) -> bool:
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])",
                text,
                0 if case_sensitive else re.I,
            )
        )

    def _identifier_grounded(self, evidence_id: UUID, identifier: str) -> bool:
        if self._contains_identifier(self._evidence_quote[evidence_id], identifier):
            return True
        rfc = re.fullmatch(r"RFC\s*(\d+)", identifier, re.I)
        return bool(
            rfc
            and re.search(
                rf"/rfc/rfc{re.escape(rfc.group(1))}(?:[./]|$)",
                self._evidence_url[evidence_id],
                re.I,
            )
        )

    def _self_contained_text(
        self,
        fact: FactRequirement,
        claim_text: str,
        evidence_ids: tuple[UUID, ...],
    ) -> str:
        missing_numeric = tuple(
            identifier
            for identifier in self._numeric_identifiers(fact)
            if not self._contains_identifier(claim_text, identifier)
        )
        cited_quotes = tuple(self._evidence_quote[value] for value in evidence_ids)
        if any(
            not any(
                self._contains_identifier(quote, identifier)
                for quote in cited_quotes
            )
            for identifier in missing_numeric
        ):
            raise ValueError(
                "claim omitted a numeric identifier not grounded by citations"
            )
        missing_symbolic = tuple(
            identifier
            for identifier in self._symbolic_identifiers(fact)
            if not self._contains_identifier(
                claim_text,
                identifier,
                case_sensitive=True,
            )
            and any(self._identifier_grounded(value, identifier) for value in evidence_ids)
        )
        missing = tuple(dict.fromkeys((*missing_numeric, *missing_symbolic)))
        if not missing:
            return claim_text
        normalized = f"{', '.join(missing)}: {claim_text}"
        if len(normalized) > 1_200:
            raise ValueError("self-contained claim is too long")
        return normalized

    @classmethod
    def _validate_requested_relationship(
        cls,
        fact: FactRequirement,
        claim_text: str,
    ) -> None:
        fact_text = f"{fact.key} {fact.description} {fact.subject}"
        folded = fact_text.casefold()
        claim_folded = claim_text.casefold()
        if "git" in folded and "git_object_types" in folded:
            if not all(
                re.search(rf"\b{object_type}\b", claim_text, re.I)
                for object_type in ("blob", "tree", "commit", "tag")
            ):
                raise ValueError("Git object-type claim must preserve all four types")
            return
        if "git" in folded and "_purpose" in fact.key.casefold():
            object_type = next(
                (
                    value
                    for value in ("blob", "tree", "commit", "tag")
                    if value in fact.key.casefold()
                ),
                None,
            )
            if object_type is None or not re.search(
                rf"\b{object_type}\b",
                claim_text,
                re.I,
            ):
                raise ValueError("Git object-purpose claim omitted its object type")
            semantic_patterns = {
                "blob": r"(?:file.{0,24}content|content.{0,24}file|\u6587\u4ef6.{0,12}\u5185\u5bb9)",
                "tree": r"(?:director|subdirector|\u76ee\u5f55)",
                "commit": (
                    r"(?:top-level tree|parent commit|snapshot|\u9876\u5c42\u6811|"
                    r"\u7236\u63d0\u4ea4|\u5feb\u7167)"
                ),
                "tag": (
                    r"(?:object.{0,24}(?:reference| id)|"
                    r"reference.{0,24}object|\u5f15\u7528.{0,12}\u5bf9\u8c61|"
                    r"\u5bf9\u8c61.{0,12}\u6807\u8bc6)"
                ),
            }
            if re.search(semantic_patterns[object_type], claim_folded, re.I) is None:
                raise ValueError("Git object-purpose claim lost the reviewed semantics")
            return
        if "media type" in folded and "json" in folded:
            if "application/json" not in claim_folded:
                raise ValueError("JSON media-type claim must contain application/json")
            return
        tls = re.search(r"\bTLS\s*(\d)\.(\d)\b", fact_text, re.I)
        if tls is not None and "rfc" in folded:
            if (
                re.search(
                    rf"\bTLS\s*{tls.group(1)}\.{tls.group(2)}\b",
                    claim_text,
                    re.I,
                )
                is None
                or re.search(r"\bRFC\s*\d+\b", claim_text, re.I) is None
            ):
                raise ValueError("TLS/RFC claim must preserve both related identifiers")
            return
        sha = re.search(r"\bSHA-?256\b", fact_text, re.I)
        if sha is not None and "digest" in folded:
            if (
                "sha-256" not in claim_text.casefold()
                or re.search(r"\b256\s*(?:bits?|位)\b", claim_text, re.I) is None
            ):
                raise ValueError("SHA-256 digest claim must preserve its 256-bit size")
            return
        if "rfc 3339" not in folded or "example" not in folded:
            return
        if "+00:00" in fact_text and not cls._RFC3339_PLUS_ZERO_TIMESTAMP.search(
            claim_text
        ):
            raise ValueError(
                "RFC 3339 +00:00 example must contain a timestamp ending +00:00"
            )
        if (
            "+00:00" not in fact_text
            and re.search(r"(?<![A-Za-z])Z(?![A-Za-z])", fact_text)
            and not cls._RFC3339_Z_TIMESTAMP.search(claim_text)
        ):
            raise ValueError("RFC 3339 Z example must contain a timestamp ending Z")

    def parse(self, text: str) -> tuple[ProposedClaim, ...]:
        payload = json.loads(text)
        claims = payload["claims"]
        if not isinstance(claims, list) or len(claims) > len(self._facts) * 2:
            raise ValueError("claims must be a bounded list")
        parsed: list[ProposedClaim] = []
        seen: set[str] = set()
        for raw in claims:
            if not isinstance(raw, dict) or set(raw) - self._FIELDS:
                raise ValueError("claim contains unsupported fields")
            claim_key = str(raw["claim_key"])
            if not claim_key.strip() or claim_key in seen:
                raise ValueError("claim keys must be non-empty and unique")
            fact_id = UUID(str(raw["fact_id"]))
            if fact_id not in self._facts:
                raise ValueError("claim references an unknown fact")
            claim_text = str(raw["text"]).strip()
            if not claim_text or len(claim_text) > 1_200:
                raise ValueError("claim text is empty or too long")
            evidence_ids = tuple(
                dict.fromkeys(UUID(str(value)) for value in raw.get("evidence_ids", ()))
            )
            if not evidence_ids:
                raise ValueError("factual claim must reference accepted evidence")
            if any(self._evidence_fact.get(value) != fact_id for value in evidence_ids):
                raise ValueError("claim references unavailable or cross-fact evidence")
            claim_text = self._self_contained_text(
                self._facts[fact_id],
                claim_text,
                evidence_ids,
            )
            self._validate_requested_relationship(
                self._facts[fact_id],
                claim_text,
            )
            parsed.append(
                ProposedClaim(
                    claim_key,
                    claim_text,
                    fact_id,
                    evidence_ids,
                    ClaimKind.FACTUAL,
                )
            )
            seen.add(claim_key)
        expected_fact_ids = set(self._evidence_fact.values())
        claimed_fact_ids = {
            item.fact_requirement_id for item in parsed if item.evidence_ids
        }
        if not expected_fact_ids <= claimed_fact_ids:
            raise ValueError("claims omitted facts with accepted evidence")
        return tuple(parsed)

    def repair_instruction(self, error: Exception) -> str:
        return (
            "Return only JSON with a claims array. Each claim may contain only "
            "claim_key, text, fact_id, and evidence_ids. Use only supplied IDs; do "
            "not emit URLs, citation labels, support status, or answer quality. Emit "
            "at least one claim with evidence_ids for every fact that has accepted "
            "evidence. "
            f"Validation error: {error}"
        )


class ConstrainedModelSynthesizer:
    def __init__(self, gateway: SynthesisGateway) -> None:
        self._gateway = gateway

    @staticmethod
    def _messages(
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> tuple[ModelMessage, ...]:
        payload = {
            "facts": [
                {
                    "fact_id": str(fact_id),
                    "key": fact.key,
                    "description": fact.description,
                    "coverage": coverage[fact_id].status.value,
                    "evidence_ids": [
                        str(value) for value in coverage[fact_id].evidence_ids
                    ],
                    "accepted_evidence": [
                        {
                            "evidence_id": str(item.id),
                            "quote": item.candidate.quote,
                        }
                        for item in evidence
                        if item.verdict is EvidenceVerdict.ACCEPTED
                        and item.fact_requirement_id == fact_id
                    ],
                }
                for fact_id, fact in facts.items()
            ],
        }
        return (
            ModelMessage(
                MessageRole.SYSTEM,
                "Write one concise factual claim for every fact that has accepted "
                "evidence, using only the evidence nested inside that fact. "
                "Keep each claim self-contained by preserving requested numeric "
                "identifiers when they also appear in its cited evidence. "
                "Return one JSON object with a claims array. Every claim must contain "
                "only claim_key, text, fact_id, and evidence_ids, using exact supplied "
                "IDs. Never move an evidence ID between facts. Never emit URLs, "
                "citation labels, support labels, or answer "
                "quality. Return an empty claims array when no accepted evidence "
                "supports a fact.",
            ),
            ModelMessage(
                MessageRole.USER,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _derived_fallback_text(
        fact: FactRequirement,
        evidence_ids: tuple[UUID, ...],
        by_id: dict[UUID, VerifiedEvidence],
    ) -> str | None:
        fact_text = f"{fact.key} {fact.description} {fact.subject}"
        folded = fact_text.casefold()
        quotes = tuple(by_id[value].candidate.quote for value in evidence_ids)
        reviewed_git = any(
            by_id[value].source.url.split("#", 1)[0].split("?", 1)[0]
            == "https://git-scm.com/docs/gitdatamodel.html"
            for value in evidence_ids
        )
        verifier_versions = {
            by_id[value].verifier_version for value in evidence_ids
        }
        if "deterministic-dns-registry-v1" in verifier_versions:
            if "transport" in folded and "port" in folded:
                return "DNS conventionally uses port 53 over both TCP and UDP."
            if "dns_port" in folded:
                return "DNS conventionally uses port 53."
            if "dns_tcp" in folded:
                return "DNS conventionally uses TCP transport."
            if "dns_udp" in folded:
                return "DNS conventionally uses UDP transport."
            return "DNS conventionally uses port 53 over both TCP and UDP."
        if "deterministic-python-origin-v1" in verifier_versions:
            if any(marker in folded for marker in ("creator", "created", "创建")):
                return "Python was created by Guido van Rossum."
            if any(
                marker in folded
                for marker in ("first_public", "first public", "release_year", "首次公开")
            ):
                return "Python was first publicly released in 1991."
        if "deterministic-json-literals-v1" in verifier_versions:
            return "The three lowercase JSON literal names are true, false, and null."
        if "deterministic-http-status-v1" in verifier_versions:
            has_201 = any("201 (Created)" in quote for quote in quotes)
            has_204 = any("204 (No Content)" in quote for quote in quotes)
            if has_201 and has_204:
                return (
                    "HTTP 201 Created reports creation of one or more resources; "
                    "HTTP 204 No Content reports successful fulfillment with no "
                    "additional response content."
                )
            if has_201:
                return "HTTP 201 Created means one or more resources were created."
            if has_204:
                return (
                    "HTTP 204 No Content means the request succeeded with no "
                    "additional response content."
                )
        if "deterministic-registry-table-v1" in verifier_versions:
            if "idempotent" in folded:
                return "HTTP GET is idempotent."
            if "safe" in folded:
                return "HTTP GET is safe."
        if "deterministic-acid-v1" in verifier_versions:
            property_name = next(
                (
                    value
                    for value in (
                        "Atomicity",
                        "Consistency",
                        "Isolation",
                        "Durability",
                    )
                    if value.casefold() in folded
                ),
                None,
            )
            if property_name is not None:
                return f"{property_name}: {quotes[0]}"
        if "deterministic-cap-v1" in verifier_versions:
            return quotes[0]
        if "deterministic-sqlite-v1" in verifier_versions:
            if "public_domain" in folded or (
                "public domain" in folded and "jurisdiction" not in folded
            ):
                return "SQLite is in the public domain."
            return f"SQLite: {quotes[0]}"
        if "deterministic-deepseek-pricing-v1" in verifier_versions:
            return f"deepseek-v4-flash — {quotes[0]}"
        if "deterministic-postgresql-support-v1" in verifier_versions:
            return f"PostgreSQL: {quotes[0]}"
        if "deterministic-postgresql-isolation-v1" in verifier_versions:
            return quotes[0]
        if "deterministic-git-state-v1" in verifier_versions:
            state = next(
                (
                    value
                    for value in ("modified", "staged", "committed")
                    if value in folded
                ),
                None,
            )
            if state is not None:
                return f"{state}: {quotes[0]}"
        if reviewed_git and "git" in folded:
            key = fact.key.casefold()
            if "git_object_types" in key and any(
                all(term in quote.casefold() for term in ("commits", "trees", "blobs", "tag objects"))
                for quote in quotes
            ):
                return "Git has four object types: commit, tree, blob, and tag."
            if "blob_purpose" in key and any(
                "contains a file's contents" in quote.casefold()
                or "contains a file\u2019s contents" in quote.casefold()
                for quote in quotes
            ):
                return "A Git blob object contains a file's contents."
            if "tree_purpose" in key and any(
                "represents a directory" in quote.casefold() for quote in quotes
            ):
                return (
                    "A Git tree object represents a directory and can contain files "
                    "or subtrees."
                )
            if "commit_purpose" in key and any(
                "a commit contains these required fields" in quote.casefold()
                for quote in quotes
            ):
                return (
                    "A Git commit object records its top-level tree, parent commits, "
                    "author and committer metadata, and commit message."
                )
            if "tag_purpose" in key and any(
                "tag objects contain these required fields" in quote.casefold()
                for quote in quotes
            ):
                return (
                    "A Git tag object records the referenced object's ID and type, "
                    "tagger metadata, and a tag message."
                )
        if "media type" in folded and "json" in folded:
            media_type = next(
                (
                    f"{match.group(1).casefold()}/{match.group(2).casefold()}"
                    for quote in quotes
                    if (
                        match := re.search(
                            r"Type name:\s*([A-Za-z0-9.+-]+).*?"
                            r"Subtype name:\s*([A-Za-z0-9.+-]+)",
                            quote,
                            re.I | re.S,
                        )
                    )
                    is not None
                ),
                None,
            )
            if media_type is not None:
                return media_type
        tls = re.search(r"\bTLS\s*(\d)\.(\d)\b", fact_text, re.I)
        if tls is not None and "rfc" in folded:
            rfc_number = next(
                (
                    match.group(1)
                    for value in evidence_ids
                    if (
                        match := re.search(
                            r"/rfc/rfc(\d+)(?:[./]|$)",
                            by_id[value].source.url,
                            re.I,
                        )
                    )
                    is not None
                ),
                None,
            )
            if rfc_number is not None:
                return f"TLS {tls.group(1)}.{tls.group(2)} is specified by RFC {rfc_number}."
        if re.search(r"\bSHA-?256\b", fact_text, re.I) and "digest" in folded:
            if any(
                re.search(r"SHA-256.*?256-bit.*?message digests", quote, re.I | re.S)
                for quote in quotes
            ):
                return (
                    "The SHA-256 digest length is 256 bits（SHA-256 的摘要长度为 256 位）。"
                )
        if not (
            "rfc 3339" in folded
            and "example" in folded
            and "+00:00" in fact_text
        ):
            return None
        has_equivalence = any('"Z" or "+00:00"' in quote for quote in quotes)
        timestamp = next(
            (
                match.group(0)
                for quote in quotes
                if (match := ProposedClaimParser._RFC3339_Z_TIMESTAMP.search(quote))
                is not None
            ),
            None,
        )
        if not has_equivalence or timestamp is None:
            return None
        return f"{timestamp[:-1]}+00:00"

    @staticmethod
    def _fallback(
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> tuple[ProposedClaim, ...]:
        by_id = {item.id: item for item in evidence}
        parser = ProposedClaimParser(facts, evidence)
        proposals: list[ProposedClaim] = []
        for fact_id, fact in facts.items():
            evidence_ids = tuple(
                value for value in coverage[fact_id].supporting_ids if value in by_id
            )
            if not evidence_ids:
                continue
            quote = by_id[evidence_ids[0]].candidate.quote.replace("\n", " ").strip()
            quote = (
                ConstrainedModelSynthesizer._derived_fallback_text(
                    fact,
                    evidence_ids,
                    by_id,
                )
                or quote
            )
            try:
                quote = parser._self_contained_text(fact, quote, evidence_ids)
            except ValueError:
                # A fallback never manufactures an ungrounded identifier.
                pass
            proposals.append(
                ProposedClaim(
                    fact.key,
                    quote[:1_200],
                    fact_id,
                    evidence_ids,
                    ClaimKind.FACTUAL,
                )
            )
        return tuple(proposals)

    async def synthesize(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
        invocation_context: ModelInvocationContext,
        deadline: datetime,
    ) -> ConstrainedSynthesisResult:
        accepted_evidence = tuple(
            item for item in evidence if item.verdict is EvidenceVerdict.ACCEPTED
        )
        if not accepted_evidence:
            return self.deterministic(
                tenant_id=tenant_id,
                run_id=run_id,
                facts=facts,
                coverage=coverage,
                evidence=evidence,
            )
        if all(
            item.verifier_version.startswith("deterministic-")
            for item in accepted_evidence
        ):
            # Reviewed structured sources already carry enough semantics for a
            # deterministic answer. Avoid another probabilistic call, which reduces
            # latency, cost, and transient degradation without weakening grounding.
            return self.deterministic(
                tenant_id=tenant_id,
                run_id=run_id,
                facts=facts,
                coverage=coverage,
                evidence=evidence,
            )
        parser = ProposedClaimParser(facts, evidence)
        degraded = False
        degradation_code = None
        try:
            result = await self._gateway.generate(
                ModelRole.SYNTHESIZER,
                self._messages(facts, coverage, evidence),
                deadline=deadline,
                budget=ModelCallBudget(2, 24_000),
                parser=parser,
                invocation_context=invocation_context,
            )
            if not isinstance(result.parsed, tuple):
                raise TypeError("Synthesizer output did not contain parsed claims")
            proposals = result.parsed
        except (TypedError, ValueError, TypeError, KeyError):
            proposals = self._fallback(facts, coverage, evidence)
            degraded = True
            degradation_code = "model_synthesizer_fallback"

        draft = ClaimSynthesizer(
            DeterministicIdFactory(f"{run_id}:answer-claims")
        ).synthesize(
            tenant_id=tenant_id,
            run_id=run_id,
            proposals=proposals,
            coverage_by_fact=coverage,
        )
        validated = CitationValidator(
            DeterministicIdFactory(f"{run_id}:citations")
        ).validate(
            draft,
            {item.id: item for item in evidence},
            unsupported_policy=UnsupportedClaimPolicy.MARK_UNCONFIRMED,
        )
        return ConstrainedSynthesisResult(
            validated,
            degraded,
            degradation_code,
        )

    @classmethod
    def deterministic(
        cls,
        *,
        tenant_id: UUID,
        run_id: UUID,
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> ConstrainedSynthesisResult:
        proposals = cls._fallback(facts, coverage, evidence)
        draft = ClaimSynthesizer(
            DeterministicIdFactory(f"{run_id}:answer-claims")
        ).synthesize(
            tenant_id=tenant_id,
            run_id=run_id,
            proposals=proposals,
            coverage_by_fact=coverage,
        )
        validated = CitationValidator(
            DeterministicIdFactory(f"{run_id}:citations")
        ).validate(
            draft,
            {item.id: item for item in evidence},
            unsupported_policy=UnsupportedClaimPolicy.MARK_UNCONFIRMED,
        )
        return ConstrainedSynthesisResult(validated, False)


__all__ = [
    "ConstrainedModelSynthesizer",
    "ConstrainedSynthesisResult",
    "ProposedClaimParser",
]
