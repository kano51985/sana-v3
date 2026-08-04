import re
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.mongo_client import RawMemoryDB

class ToolInterceptNode(PipelineNode):
    def __init__(self, raw_db: RawMemoryDB):
        self.raw_db = raw_db
    def process(self, ctx: Context) -> NodeResult:
        pat = "<invoke_memory>(batch_[a-zA-Z0-9_]+)</invoke_memory>"
        m = re.search(pat, ctx.llm_raw_response)
        if m:
            print(f"[工具拦截] 检测到 memory 调用: {m.group(1)}")
            ctx.tool_triggered = True
            ctx.tool_target_batch = m.group(1)
            return NodeResult(next="deep_dive", context=ctx)
        print(f"[工具拦截] 未检测到工具调用")
        return NodeResult(next="format_check", context=ctx)
