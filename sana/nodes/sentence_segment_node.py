import re

from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.nodes.pause_parser import (
    DEFAULT_DELAY,
    MAX_DELAY,
    PAUSE_RE,
    parse_pause_delay,
    strip_pause_tags,
)
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[。！？；\n])|(?<=[~～])|(?<=……)|(?<=\.\.\.)"
)


class SentenceSegmentNode(PipelineNode):
    DEFAULT_DELAY = DEFAULT_DELAY
    SHORT_DELAY = 0.45
    LONG_DELAY = 0.8
    MAX_DELAY = MAX_DELAY
    EMPTY_TEXT = "[Empty response]"

    def process(self, ctx: Context) -> NodeResult:
        raw = ctx.chat_raw or ctx.chat or ""
        ctx.segments = self.build_segments(raw)
        ctx.chat = strip_pause_tags(ctx.chat or raw)
        return NodeResult(next="memory_update", context=ctx)

    @classmethod
    def build_segments(cls, text: str) -> list[dict]:
        tokens = cls._tokenize(text or "")
        segments = []
        pending_delay = None

        for kind, value in tokens:
            if kind == "pause":
                pending_delay = value
                continue

            for piece in cls._split_sentences(value):
                if not piece.strip():
                    continue
                if pending_delay is not None:
                    delay = pending_delay
                    pending_delay = None
                else:
                    delay = cls._default_delay(piece, is_first=not segments)
                segments.append({"text": piece.strip(), "delay": delay})

        if not segments:
            return [{"text": cls.EMPTY_TEXT, "delay": 0.0}]
        return segments

    @classmethod
    def _tokenize(cls, text: str) -> list[tuple[str, object]]:
        tokens = []
        pos = 0
        for match in PAUSE_RE.finditer(text):
            before = text[pos:match.start()]
            if before:
                tokens.append(("text", before))
            tokens.append(("pause", cls._parse_delay(match.group(1))))
            pos = match.end()
        if pos < len(text):
            tokens.append(("text", text[pos:]))
        return tokens

    @classmethod
    def _parse_delay(cls, attrs: str) -> float:
        return parse_pause_delay(attrs)

    @staticmethod
    def _split_sentences(value: str) -> list[str]:
        return [part for part in _SENTENCE_SPLIT_RE.split(value) if part]

    @classmethod
    def _default_delay(cls, text: str, is_first: bool) -> float:
        if is_first:
            return 0.0
        length = len(text.strip())
        if length <= 8:
            return cls.SHORT_DELAY
        if length >= 24:
            return cls.LONG_DELAY
        return cls.DEFAULT_DELAY
