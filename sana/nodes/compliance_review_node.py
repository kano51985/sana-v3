import re
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.config import AGENT_NAME


class ComplianceReviewNode(PipelineNode):
    """简化版：只做重复回复检测，不干预内容"""

    def process(self, ctx: Context) -> NodeResult:
        raw = ctx.llm_raw_response or ""
        if not raw.strip():
            return NodeResult(next="response_parser", context=ctx)

        # 检测是否与上一条助理回复高度重复
        wm = ctx.working_memory
        last_assistant = ""
        if wm and len(wm) >= 2:
            for m in reversed(wm):
                if m.get("role") == AGENT_NAME:
                    last_assistant = m.get("content", "")
                    break
        if last_assistant:
            current_chat = self._extract_tag(raw, "chat") or raw
            sim = self._text_similarity(current_chat, last_assistant)
            if sim > 0.85:
                print(f"[合规审查] 回复与上一条高度重复 ({sim:.1%})，跳过")
                return NodeResult(next="response_parser", context=ctx)

        return NodeResult(next="response_parser", context=ctx)

    def _extract_tag(self, text, tag):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def _text_similarity(self, a, b):
        if not a or not b:
            return 0.0
        set_a, set_b = set(a), set(b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)
