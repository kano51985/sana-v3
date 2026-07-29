import re
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context

class ResponseParserNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        raw = ctx.llm_raw_response or ""
        if not raw.strip():
            ctx.chat = "[Empty response]"
            ctx.thinking = ""
            print("[Parser] Empty response")
            return NodeResult(next="memory_update", context=ctx)
        tm = re.findall(r"<thinking>(.*?)</thinking>", raw, re.DOTALL)
        cm = re.findall(r"<chat>(.*?)</chat>", raw, re.DOTALL)
        ctx.thinking = "\n".join(t.strip() for t in tm) if tm else ""
        ctx.chat = " ".join(c.strip() for c in cm) if cm else raw.split("\n")[-1].strip()
        print(f"[Parser] chat={ctx.chat[:60]}...")
        return NodeResult(next="memory_update", context=ctx)
