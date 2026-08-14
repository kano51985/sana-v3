import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from sana.modules.content.chunker import DocumentChunker
from sana.modules.content.domain import FetchArtifact, FetchStatus
from sana.modules.content.extractor import ContentExtractor, DocumentVersionBuilder
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import DeterministicIdFactory


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def successful_html(body: bytes) -> FetchArtifact:
    return FetchArtifact(
        request_url="https://example.com/start",
        final_url="https://example.com/article",
        status=FetchStatus.SUCCEEDED,
        http_status=200,
        media_type="text/html",
        body=body,
        content_hash=hashlib.sha256(body).hexdigest(),
        fetched_at=NOW,
    )


def test_fetched_html_creates_immutable_version_and_exact_chunks() -> None:
    artifact = successful_html(
        b"<html><head><title>Evidence</title><script>ignore()</script></head>"
        b"<body><nav>menu</nav><main>" + (b"verified fact. " * 30) + b"</main></body></html>"
    )
    content = ContentExtractor().extract(artifact)
    document, version = DocumentVersionBuilder(
        DeterministicIdFactory("document")
    ).build(uuid4(), content)
    chunks = DocumentChunker(max_characters=120, overlap=20).chunk(version.text)

    assert content.title == "Evidence"
    assert "ignore" not in content.text
    assert "menu" not in content.text
    assert document.canonical_url == artifact.final_url
    assert version.document_id == document.id
    assert version.content_hash == hashlib.sha256(version.text.encode()).hexdigest()
    assert len(chunks) > 1
    for ordinal, chunk in enumerate(chunks):
        assert chunk.ordinal == ordinal
        assert chunk.text == version.text[chunk.start_offset : chunk.end_offset]
        assert chunk.end_offset - chunk.start_offset <= 120
    for previous, current in zip(chunks, chunks[1:]):
        assert current.start_offset <= previous.end_offset
        assert current.start_offset > previous.start_offset


def test_search_snippet_cannot_be_promoted_to_document_content() -> None:
    with pytest.raises(TypeError, match="FetchArtifact"):
        ContentExtractor().extract({"snippet": "unverified search text"})  # type: ignore[arg-type]


def test_failed_fetch_cannot_create_extracted_content() -> None:
    error = TypedError(
        ErrorCategory.TRANSIENT,
        "fetch_failed",
        "network unavailable",
    )
    artifact = FetchArtifact(
        request_url="https://example.com",
        final_url="https://example.com",
        status=FetchStatus.FAILED,
        http_status=None,
        media_type=None,
        body=b"",
        content_hash=None,
        fetched_at=NOW,
        error=error,
    )

    with pytest.raises(TypedError) as captured:
        ContentExtractor().extract(artifact)

    assert captured.value.code == "fetch_not_successful"


def test_fetch_artifact_rejects_forged_success_hash() -> None:
    with pytest.raises(ValueError, match="invalid content state"):
        FetchArtifact(
            request_url="https://example.com",
            final_url="https://example.com",
            status=FetchStatus.SUCCEEDED,
            http_status=200,
            media_type="text/plain",
            body=b"real body",
            content_hash="forged",
            fetched_at=NOW,
        )
