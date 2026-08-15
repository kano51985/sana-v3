from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from sana.modules.content.domain import DocumentChunk, DocumentVersion
from sana.modules.evidence.candidate_selector import CandidateDocument, CandidateSelector
from sana.modules.evidence.domain import SourceAuthority
from sana.modules.evidence.source_authority import SourceAuthorityPolicy, registrable_domain
from sana.modules.search_planning.domain import FactRequirement, FactType


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


def document(url: str, fact_id, text: str) -> CandidateDocument:
    tenant_id, document_id = uuid4(), uuid4()
    version = DocumentVersion(
        uuid4(),
        tenant_id,
        document_id,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
        "text/plain",
        "en",
        NOW,
    )
    chunk = DocumentChunk(
        0,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        10,
        0,
        len(text),
    )
    return CandidateDocument(
        document_id,
        version,
        ((uuid4(), chunk),),
        url,
        "title",
        (fact_id,),
    )


def test_registrable_domain_uses_offline_psl_not_raw_hostname() -> None:
    assert registrable_domain("https://news.example.co.uk/path") == "example.co.uk"
    assert registrable_domain("https://a.b.blogspot.com/post") == "b.blogspot.com"


def test_authority_is_entity_specific_and_not_model_controlled() -> None:
    policy = SourceAuthorityPolicy()

    assert policy.classify("https://www.ea.com/games/apex", entity="Apex Legends") == (
        "ea.com",
        SourceAuthority.OFFICIAL,
    )
    assert policy.classify("https://www.ea.com/news", entity="Unrelated Product")[1] is SourceAuthority.INDEPENDENT
    assert policy.classify(
        "https://git-scm.com/book/en/v2/Git-Internals-Git-Objects",
        entity="Git object model",
    )[1] is SourceAuthority.OFFICIAL
    assert policy.classify(
        "https://openai.com/",
        entity="next unreleased OpenAI model",
    )[1] is SourceAuthority.OFFICIAL


def test_selector_bounds_candidates_and_prefers_distinct_publishers() -> None:
    fact_id = uuid4()
    fact = FactRequirement(
        "version",
        FactType.VERSION,
        "Apex Legends current version",
        "Apex Legends",
    )
    documents = tuple(
        document(url, fact_id, "Apex Legends current version season update " + "x" * 800)
        for url in (
            "https://one.example.com/a",
            "https://two.example.com/b",
            "https://another.other.example.net/c",
            "https://three.example.org/d",
        )
    )

    selected = CandidateSelector(max_per_fact=3, max_total=3).select(
        run_id=uuid4(),
        entity="Apex Legends",
        facts={fact_id: fact},
        documents=documents,
    )

    assert len(selected) == 3
    assert len({item.source_identity for item in selected}) == 3
    assert all(len(item.quote) <= 600 for item in selected)


def test_selector_centers_quote_on_relevant_term_cluster() -> None:
    fact_id = uuid4()
    fact = FactRequirement(
        "version",
        FactType.VERSION,
        "Python current stable version",
        "Python",
    )
    text = (
        "Python general navigation "
        + "x" * 1_000
        + " Python 3.14.2 is the latest stable version available for download."
        + " y" * 400
    )

    selected = CandidateSelector(max_per_fact=1, max_total=1).select(
        run_id=uuid4(),
        entity="Python",
        facts={fact_id: fact},
        documents=(document("https://www.python.org/downloads/", fact_id, text),),
    )

    assert len(selected) == 1
    assert "Python 3.14.2 is the latest stable version" in selected[0].quote
    assert selected[0].quote in text
    assert len(selected[0].quote) <= 600


def test_one_fetched_document_can_ground_multiple_planned_facts() -> None:
    first, second = uuid4(), uuid4()
    facts = {
        first: FactRequirement("blob", FactType.BACKGROUND, "Git blob object", "Git"),
        second: FactRequirement("tree", FactType.BACKGROUND, "Git tree object", "Git"),
    }
    candidate_document = document(
        "https://git-scm.com/book/en/v2/Git-Internals-Git-Objects",
        first,
        "Git blob object stores content. Git tree object stores directory entries.",
    )
    candidate_document = CandidateDocument(
        candidate_document.document_id,
        candidate_document.version,
        candidate_document.chunks,
        candidate_document.url,
        candidate_document.title,
        (first, second),
    )

    selected = CandidateSelector(max_per_fact=1, max_total=2).select(
        run_id=uuid4(),
        entity="Git",
        facts=facts,
        documents=(candidate_document,),
    )

    assert {item.fact_id for item in selected} == {first, second}
