import re
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.nodes.pause_parser import strip_pause_tags

class ResponseParserNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        raw = ctx.llm_raw_response or ""
        if not raw.strip():
            ctx.chat_raw = ""
            ctx.chat = "[Empty response]"
            ctx.thinking = ""
            print("[Parser] Empty response")
            return NodeResult(next="sentence_segment", context=ctx)
        tm = self._extract_tag(raw, "thinking")
        cm = self._extract_tag(raw, "chat")
        ctx.thinking = "\n".join(t.strip() for t in tm) if tm else ""
        chat_parts = [self._clean_tag_fragment(c.strip()) for c in cm if c.strip()]
        ctx.chat_raw = " ".join(chat_parts) if chat_parts else self._fallback_chat(raw)
        ctx.chat = strip_pause_tags(ctx.chat_raw)
        print(f"[Parser] chat={ctx.chat[:60]}...")
        return NodeResult(next="sentence_segment", context=ctx)

    @staticmethod
    def _extract_tag(raw: str, tag: str):
        if tag == "chat":
            pattern = r"<chat>(.*?)(?:</chat>|</thinking>|\Z)"
        else:
            pattern = r"<thinking>(.*?)</thinking>"
        return re.findall(pattern, raw, re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _clean_tag_fragment(text: str):
        return re.sub(r"</?(?:thinking|chat)>\s*$", "", text, flags=re.IGNORECASE).strip()

    @staticmethod
    def _fallback_chat(raw: str):
        cleaned = re.sub(r"</?(?:thinking|chat)>", "", raw, flags=re.IGNORECASE)
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        return lines[-1] if lines else ""
