from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.config import AGENT_NAME, USER_NAME

class MemoryUpdateNode(PipelineNode):
    def __init__(self, working_memory: list, chat_buffer: list):
        self.working_memory = working_memory
        self.chat_buffer = chat_buffer
        self.max_wm = 20  # 滑动窗口上限：10 轮对话 = 20 条
    def process(self, ctx: Context) -> NodeResult:
        self.working_memory.append({"role": USER_NAME, "content": ctx.user_input})
        self.working_memory.append({"role": AGENT_NAME, "content": ctx.chat})
        if ctx.tool_trace.get("triggered"):
            self.working_memory.append({"role": "tool", "content": _tool_note(ctx.tool_trace)})
        self.chat_buffer.append({"role": USER_NAME, "content": ctx.user_input})
        self.chat_buffer.append({"role": AGENT_NAME, "content": ctx.chat})
        # 滑动窗口：超出上限时移除最早的一对 user+assistant
        if len(self.working_memory) > self.max_wm:
            excess = len(self.working_memory) - self.max_wm
            if excess % 2 != 0:
                excess += 1  # 保持成对删除
            self.working_memory = self.working_memory[excess:]
        ctx.working_memory = self.working_memory
        ctx.chat_buffer = self.chat_buffer
        return NodeResult(next="consolidation", context=ctx)


def _tool_note(trace: dict) -> str:
    tool = trace.get("tool", "tool")
    status = trace.get("status", "")
    if tool == "web":
        query = " | ".join(trace.get("query_heads", []))
        count = trace.get("results_count", 0)
        error = trace.get("error", "")
        note = f"[Web] {status}: {query}; 结果 {count} 条"
        if error:
            note += f"; 错误 {error}"
        return note
    return f"[Tool] {tool} {status}"
