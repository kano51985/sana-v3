"""Deterministically bound grounded candidates before model verification."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import UUID, uuid5

from sana.modules.content.domain import DocumentChunk, DocumentVersion
from sana.modules.evidence.domain import SourceAuthority
from sana.modules.evidence.source_authority import SourceAuthorityPolicy
from sana.modules.search_planning.domain import FactRequirement


_TERM = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)
_GENERIC_ANCHORS = frozenset(
    {
        "answer",
        "background",
        "code",
        "composition",
        "current",
        "description",
        "detail",
        "evidence",
        "exact",
        "fact",
        "gap",
        "information",
        "object",
        "official",
        "overview",
        "parameter",
        "phrase",
        "private",
        "purpose",
        "recent",
        "reason",
        "result",
        "source",
        "status",
        "type",
    }
)


@dataclass(frozen=True, slots=True)
class CandidateDocument:
    document_id: UUID
    version: DocumentVersion
    chunks: tuple[tuple[UUID, DocumentChunk], ...]
    url: str
    title: str
    mapped_fact_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not self.mapped_fact_ids:
            raise ValueError("Candidate document must remain bound to at least one fact")
        object.__setattr__(
            self,
            "mapped_fact_ids",
            tuple(dict.fromkeys(self.mapped_fact_ids)),
        )


@dataclass(frozen=True, slots=True)
class SelectedCandidate:
    id: UUID
    fact_id: UUID
    fact: FactRequirement
    document_id: UUID
    version: DocumentVersion
    chunk_id: UUID
    chunk: DocumentChunk
    url: str
    title: str
    source_identity: str
    source_authority: SourceAuthority
    quote: str
    score: float


class CandidateSelector:
    def __init__(
        self,
        authority: SourceAuthorityPolicy | None = None,
        *,
        max_per_fact: int = 3,
        max_total: int = 8,
        max_quote_chars: int = 600,
    ) -> None:
        if min(max_per_fact, max_total, max_quote_chars) < 1:
            raise ValueError("Candidate limits must be positive")
        self._authority = authority or SourceAuthorityPolicy()
        self._per_fact = max_per_fact
        self._total = max_total
        self._quote_chars = max_quote_chars

    @staticmethod
    def _tokens(*values: str) -> frozenset[str]:
        normalized = " ".join(values).replace("_", " ").replace("-", " ")
        tokens = []
        for value in _TERM.findall(normalized.casefold()):
            if len(value) <= 1:
                continue
            if (
                value.isascii()
                and len(value) > 4
                and value.endswith("s")
                and not value.endswith(("is", "ss", "us"))
            ):
                value = value[:-1]
            tokens.append(value)
        return frozenset(tokens)

    @classmethod
    def _term_sets(
        cls,
        entity: str,
        fact: FactRequirement,
    ) -> tuple[frozenset[str], frozenset[str]]:
        entity_terms = cls._tokens(entity, fact.fact_type.value)
        context = cls._tokens(
            entity,
            fact.subject,
            fact.description,
            fact.fact_type.value,
        )
        anchors = set(cls._tokens(fact.key))
        anchors.difference_update(entity_terms)
        anchors.difference_update(_GENERIC_ANCHORS)
        if fact.subject.casefold() != entity.casefold():
            anchors.update(cls._tokens(fact.subject) - cls._tokens(entity))
        anchors.update(
            value
            for value in cls._tokens(fact.description)
            if value.isdecimal()
        )
        return context, frozenset(anchors)

    def _quote_window(
        self,
        text: str,
        terms: frozenset[str],
        anchors: frozenset[str],
    ) -> str:
        if len(text) <= self._quote_chars:
            return text
        folded = text.casefold()
        positions: list[int] = []
        position_terms = anchors or terms
        for term in position_terms:
            offset = 0
            while True:
                position = folded.find(term, offset)
                if position < 0:
                    break
                positions.append(position)
                offset = position + max(1, len(term))
        if not positions:
            return text[: self._quote_chars]
        maximum_start = len(text) - self._quote_chars
        best_start = 0
        best_score = (-1, -1)
        for position in positions:
            start = min(maximum_start, max(0, position - self._quote_chars // 3))
            window = folded[start : start + self._quote_chars]
            anchor_matches = sum(term in window for term in anchors)
            anchor_density = sum(window.count(term) for term in anchors)
            context_matches = sum(term in window for term in terms)
            context_density = sum(window.count(term) for term in terms)
            score = (
                anchor_matches,
                anchor_density,
                context_matches,
                context_density,
            )
            if score > best_score:
                best_start = start
                best_score = score
        return text[best_start : best_start + self._quote_chars]

    def select(
        self,
        *,
        run_id: UUID,
        entity: str,
        facts: dict[UUID, FactRequirement],
        documents: tuple[CandidateDocument, ...],
    ) -> tuple[SelectedCandidate, ...]:
        by_fact: dict[UUID, list[SelectedCandidate]] = {key: [] for key in facts}
        for document in documents:
            source_identity, authority = self._authority.classify(
                document.url,
                entity=entity,
            )
            for mapped_fact_id in document.mapped_fact_ids:
                fact = facts.get(mapped_fact_id)
                if fact is None:
                    continue
                terms, anchors = self._term_sets(entity, fact)
                for chunk_id, chunk in document.chunks:
                    folded = chunk.text.casefold()
                    matched = {term for term in terms if term in folded}
                    if not matched:
                        continue
                    matched_anchors = {term for term in anchors if term in folded}
                    if anchors and not matched_anchors:
                        continue
                    context_score = len(matched) / max(1, len(terms))
                    score = (
                        0.7 * len(matched_anchors) / len(anchors)
                        + 0.3 * context_score
                        if anchors
                        else context_score
                    )
                    quote = self._quote_window(chunk.text, terms, anchors)
                    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
                    by_fact[mapped_fact_id].append(
                        SelectedCandidate(
                            id=uuid5(
                                run_id,
                                f"candidate:{mapped_fact_id}:{chunk_id}:{quote_hash}",
                            ),
                            fact_id=mapped_fact_id,
                            fact=fact,
                            document_id=document.document_id,
                            version=document.version,
                            chunk_id=chunk_id,
                            chunk=chunk,
                            url=document.url,
                            title=document.title,
                            source_identity=source_identity,
                            source_authority=authority,
                            quote=quote,
                            score=score,
                        )
                    )

        ranked_by_fact: dict[UUID, list[SelectedCandidate]] = {}
        for fact_id in facts:
            ranked = sorted(
                by_fact[fact_id],
                key=lambda item: (
                    0
                    if item.source_authority is SourceAuthority.OFFICIAL
                    else 1,
                    -item.score,
                    item.source_identity,
                    item.chunk.ordinal,
                ),
            )
            diverse: list[SelectedCandidate] = []
            deferred: list[SelectedCandidate] = []
            seen_sources: set[str] = set()
            for item in ranked:
                if item.source_identity in seen_sources:
                    deferred.append(item)
                else:
                    diverse.append(item)
                    seen_sources.add(item.source_identity)
            ranked_by_fact[fact_id] = (diverse + deferred)[: self._per_fact]

        selected: list[SelectedCandidate] = []
        for rank in range(self._per_fact):
            for fact_id in facts:
                ranked = ranked_by_fact[fact_id]
                if rank < len(ranked):
                    selected.append(ranked[rank])
                    if len(selected) == self._total:
                        return tuple(selected)
        return tuple(selected)


__all__ = ["CandidateDocument", "CandidateSelector", "SelectedCandidate"]
