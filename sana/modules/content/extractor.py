"""Deterministic text extraction; snippets are not accepted as fetch artifacts."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from urllib.parse import urlsplit
from uuid import UUID

from sana.modules.content.domain import (
    Document,
    DocumentVersion,
    ExtractedContent,
    FetchArtifact,
    FetchStatus,
)
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import IdFactory


class _VisibleTextParser(HTMLParser):
    _IGNORED = {"script", "style", "noscript", "svg", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED:
            self._ignored_depth += 1
        if lowered == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        if lowered == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if not normalized:
            return
        if self._in_title:
            self.title_parts.append(normalized)
        if self._ignored_depth == 0 and not self._in_title:
            self.text_parts.append(normalized)


class ContentExtractor:
    def extract(self, artifact: FetchArtifact) -> ExtractedContent:
        if not isinstance(artifact, FetchArtifact):
            raise TypeError("Only a FetchArtifact can create extracted content")
        if artifact.status is not FetchStatus.SUCCEEDED:
            raise TypedError(
                ErrorCategory.CONTENT,
                "fetch_not_successful",
                "Only a successful fetch can be extracted",
                retryable=False,
            )
        media_type = (artifact.media_type or "").lower()
        if media_type in {"text/html", "application/xhtml+xml"}:
            parser = _VisibleTextParser()
            parser.feed(artifact.body.decode("utf-8", errors="replace"))
            text = "\n".join(parser.text_parts)
            title = " ".join(parser.title_parts)
        elif media_type.startswith("text/") or media_type == "application/json":
            text = artifact.body.decode("utf-8", errors="replace")
            title = ""
        else:
            raise TypedError(
                ErrorCategory.CONTENT,
                "unsupported_content_type",
                f"No extractor for content type: {media_type}",
                retryable=False,
            )
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if not text:
            raise TypedError(
                ErrorCategory.CONTENT,
                "empty_extracted_content",
                "Fetched response contained no extractable body text",
                retryable=False,
            )
        return ExtractedContent(
            canonical_url=artifact.final_url,
            title=title or (urlsplit(artifact.final_url).hostname or artifact.final_url),
            text=text,
            media_type=media_type,
            language=None,
            source_content_hash=artifact.content_hash or "",
            fetched_at=artifact.fetched_at,
            extraction_metadata={"extractor": "deterministic-v1"},
        )


class DocumentVersionBuilder:
    def __init__(self, id_factory: IdFactory) -> None:
        self._ids = id_factory

    def build(
        self,
        tenant_id: UUID,
        content: ExtractedContent,
    ) -> tuple[Document, DocumentVersion]:
        if not isinstance(content, ExtractedContent):
            raise TypeError("DocumentVersion requires successfully extracted content")
        parsed = urlsplit(content.canonical_url)
        canonical_hash = hashlib.sha256(
            content.canonical_url.encode("utf-8")
        ).hexdigest()
        document = Document(
            id=self._ids.new_uuid(),
            tenant_id=tenant_id,
            canonical_url=content.canonical_url,
            canonical_url_hash=canonical_hash,
            title=content.title,
            source_host=parsed.hostname or "",
        )
        version = DocumentVersion(
            id=self._ids.new_uuid(),
            tenant_id=tenant_id,
            document_id=document.id,
            content_hash=hashlib.sha256(content.text.encode("utf-8")).hexdigest(),
            text=content.text,
            media_type=content.media_type,
            language=content.language,
            fetched_at=content.fetched_at,
        )
        return document, version
