"""Stable character-window chunking with exact source offsets."""

from __future__ import annotations

import hashlib
import re

from sana.modules.content.domain import DocumentChunk


class DocumentChunker:
    def __init__(self, *, max_characters: int = 1_800, overlap: int = 200) -> None:
        if max_characters < 100 or overlap < 0 or overlap >= max_characters:
            raise ValueError("Chunk size or overlap is invalid")
        self._max = max_characters
        self._overlap = overlap

    @staticmethod
    def _token_count(text: str) -> int:
        return len(re.findall(r"[\w]+|[\u3400-\u9fff]", text, flags=re.UNICODE))

    def chunk(self, text: str) -> tuple[DocumentChunk, ...]:
        if not text:
            return ()
        chunks = []
        start = 0
        ordinal = 0
        while start < len(text):
            hard_end = min(len(text), start + self._max)
            end = hard_end
            if hard_end < len(text):
                boundary = max(
                    text.rfind("\n", start + self._max // 2, hard_end),
                    text.rfind("。", start + self._max // 2, hard_end),
                    text.rfind(". ", start + self._max // 2, hard_end),
                )
                if boundary > start:
                    end = boundary + 1
            chunk_text = text[start:end]
            chunks.append(
                DocumentChunk(
                    ordinal=ordinal,
                    text=chunk_text,
                    text_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                    token_count=self._token_count(chunk_text),
                    start_offset=start,
                    end_offset=end,
                )
            )
            if end == len(text):
                break
            start = max(start + 1, end - self._overlap)
            ordinal += 1
        return tuple(chunks)
