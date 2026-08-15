from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from sana.modules.content.domain import DocumentChunk, DocumentVersion
from sana.modules.evidence.candidate_selector import CandidateDocument, CandidateSelector
from sana.modules.evidence.domain import SourceAuthority
from sana.modules.evidence.source_authority import SourceAuthorityPolicy, registrable_domain
from sana.modules.search_planning.domain import FactRequirement, FactType, Freshness


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
    assert policy.classify(
        "https://www.kernel.org/pub/software/scm/git/docs/gitglossary.html",
        entity="Git object model",
    )[1] is SourceAuthority.OFFICIAL
    assert policy.classify(
        "https://www.kernel.org/doc/html/latest/",
        entity="Git object model",
    )[1] is SourceAuthority.INDEPENDENT


def test_full_campaign_primary_sources_receive_entity_scoped_authority() -> None:
    policy = SourceAuthorityPolicy()
    official_pairs = (
        ("https://www.rfc-editor.org/rfc/rfc8259.html", "JSON standard"),
        ("https://www.rfc-editor.org/rfc/rfc6234.html", "SHA-256"),
        (
            "https://www.iana.org/assignments/service-names-port-numbers/",
            "DNS",
        ),
        ("https://www.rfc-editor.org/rfc/rfc8446.html", "TLS 1.3"),
        ("https://www.rfc-editor.org/rfc/rfc3339.html", "RFC 3339"),
        (
            "https://www.postgresql.org/docs/current/transaction-iso.html",
            "SQL transaction isolation",
        ),
        ("https://www.postgresql.org/support/versioning/", "PostgreSQL"),
        ("https://www.sqlite.org/copyright.html", "SQLite"),
    )

    for url, entity in official_pairs:
        assert policy.classify(url, entity=entity)[1] is SourceAuthority.OFFICIAL

    assert policy.classify(
        "https://www.ibm.com/think/topics/cap-theorem",
        entity="CAP theorem",
    )[1] is SourceAuthority.INDEPENDENT


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


def test_selector_uses_fact_key_anchor_for_quote_and_chunk_filtering() -> None:
    fact_id = uuid4()
    fact = FactRequirement(
        "http_404_reason_phrase",
        FactType.CURRENT_VALUE,
        "The English reason phrase for HTTP status code 404",
        "HTTP",
    )
    tenant_id, document_id = uuid4(), uuid4()
    chunks = (
        DocumentChunk(
            0,
            "HTTP status code reason phrase standard overview " + "x" * 900,
            hashlib.sha256(
                ("HTTP status code reason phrase standard overview " + "x" * 900).encode()
            ).hexdigest(),
            10,
            0,
            950,
        ),
        DocumentChunk(
            1,
            "HTTP status registry " + "y" * 900 + " 404,Not Found,RFC9110",
            hashlib.sha256(
                ("HTTP status registry " + "y" * 900 + " 404,Not Found,RFC9110").encode()
            ).hexdigest(),
            10,
            950,
            1_900,
        ),
    )
    text = "\n".join(chunk.text for chunk in chunks)
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
    candidate_document = CandidateDocument(
        document_id,
        version,
        tuple((uuid4(), chunk) for chunk in chunks),
        "https://www.iana.org/assignments/http-status-codes/http-status-codes-1.csv",
        "HTTP Status Code Registry",
        (fact_id,),
    )

    selected = CandidateSelector(max_per_fact=1, max_total=1).select(
        run_id=uuid4(),
        entity="HTTP",
        facts={fact_id: fact},
        documents=(candidate_document,),
    )

    assert len(selected) == 1
    assert selected[0].chunk.ordinal == 1
    assert "404,Not Found" in selected[0].quote


def test_global_candidate_cap_round_robins_across_facts() -> None:
    fact_ids = tuple(uuid4() for _ in range(5))
    names = ("alpha", "beta", "gamma", "delta", "epsilon")
    facts = {
        fact_id: FactRequirement(name, FactType.BACKGROUND, name, "Example")
        for fact_id, name in zip(fact_ids, names, strict=True)
    }
    documents = []
    for index, domain in enumerate(("one.example", "two.example", "three.example")):
        value = document(
            f"https://{domain}/{index}",
            fact_ids[0],
            "Example alpha beta gamma delta epsilon",
        )
        documents.append(
            CandidateDocument(
                value.document_id,
                value.version,
                value.chunks,
                value.url,
                value.title,
                fact_ids,
            )
        )

    selected = CandidateSelector(max_per_fact=3, max_total=12).select(
        run_id=uuid4(),
        entity="Example",
        facts=facts,
        documents=tuple(documents),
    )

    assert len(selected) == 12
    assert {item.fact_id for item in selected} == set(fact_ids)


def test_request_word_advice_does_not_filter_relevant_meta_page() -> None:
    fact_id = uuid4()
    fact = FactRequirement(
        "current_meta_legend_advice",
        FactType.TEAM_META,
        "Current community recommendations for legend composition",
        "Apex Legends",
        freshness=Freshness.CURRENT,
    )

    selected = CandidateSelector(max_per_fact=1, max_total=1).select(
        run_id=uuid4(),
        entity="Apex Legends",
        facts={fact_id: fact},
        documents=(
            document(
                "https://apexranked.com/meta",
                fact_id,
                "Apex Legends Season 29 team comps and ranked meta picks",
            ),
        ),
    )

    assert len(selected) == 1


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


def test_current_fact_prefers_leading_fresh_content_over_later_lexical_match() -> None:
    fact_id = uuid4()
    fact = FactRequirement(
        "rust_current_version",
        FactType.VERSION,
        "Rust current stable version",
        "Rust",
        freshness=Freshness.CURRENT,
    )
    tenant_id, document_id = uuid4(), uuid4()
    raw_chunks = (
        "Rust 1.97.1 current release information",
        "Rust old archive current stable version release details",
    )
    chunks = tuple(
        (
            uuid4(),
            DocumentChunk(
                ordinal,
                text,
                hashlib.sha256(text.encode()).hexdigest(),
                10,
                ordinal * 100,
                ordinal * 100 + len(text),
            ),
        )
        for ordinal, text in enumerate(raw_chunks)
    )
    full_text = "\n".join(raw_chunks)
    version = DocumentVersion(
        uuid4(),
        tenant_id,
        document_id,
        hashlib.sha256(full_text.encode()).hexdigest(),
        full_text,
        "text/plain",
        "en",
        NOW,
    )
    candidate_document = CandidateDocument(
        document_id,
        version,
        chunks,
        "https://www.rust-lang.org/tools/install",
        "Rust releases",
        (fact_id,),
    )

    selected = CandidateSelector(max_per_fact=1, max_total=1).select(
        run_id=uuid4(),
        entity="Rust",
        facts={fact_id: fact},
        documents=(candidate_document,),
    )

    assert len(selected) == 1
    assert selected[0].chunk.ordinal == 0
