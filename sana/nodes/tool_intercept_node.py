import re
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.mongo_client import RawMemoryDB
from sana.services.tool_intent_detector import ToolIntentDetector
from sana.services.tool_registry import ToolRegistry

class ToolInterceptNode(PipelineNode):
    def __init__(
        self,
        raw_db: RawMemoryDB,
        intent_detector: ToolIntentDetector | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self.raw_db = raw_db
        self.intent_detector = intent_detector
        self.tool_registry = tool_registry or ToolRegistry()
    def process(self, ctx: Context) -> NodeResult:
        existing_trace = ctx.tool_trace or {}
        if existing_trace.get("tool") == "web" and existing_trace.get("status") != "pending":
            print("[工具拦截] web 调用已处理，跳过重复执行")
            return NodeResult(next="format_check", context=ctx)

        pat = "<invoke_memory>(batch_[a-zA-Z0-9_]+)</invoke_memory>"
        m = re.search(pat, ctx.llm_raw_response)
        if m:
            print(f"[工具拦截] 检测到 memory 调用: {m.group(1)}")
            ctx.tool_triggered = True
            ctx.tool_target_batch = m.group(1)
            ctx.tool_trace = {
                "triggered": True,
                "tool": "memory",
                "status": "executed",
                "error": "",
            }
            return NodeResult(next="deep_dive", context=ctx)
        web_pat = re.compile(
            r'<invoke_web(?:\s+query=["\'](?P<query>[^"\']+)["\'])?\s*/?>',
            re.IGNORECASE,
        )
        web = web_pat.search(ctx.llm_raw_response or "")
        if web:
            query = web.group("query") or ctx.user_input[:120]
            print(f"[工具拦截] 检测到 web 调用: {query}")
            ctx.tool_triggered = True
            ctx.tool_target_web = query
            ctx.tool_trace = {
                "triggered": True,
                "tool": "web",
                "status": "pending",
                "error": "",
            }
            return NodeResult(next="web_search", context=ctx)
        if ctx.web_tool_enabled and self.intent_detector is not None:
            intent = self.intent_detector.detect(
                ctx.user_input,
                ctx.perception_data,
                ctx.llm_raw_response,
            )
            if intent.needs_tool and intent.tool_name == "web" and self.tool_registry.get("web"):
                query = intent.query or ctx.user_input[:120]
                print(f"[工具拦截] 通用意图判断需要联网，自动触发: {query}")
                ctx.tool_triggered = True
                ctx.web_should_query = True
                ctx.web_suggested_query = query
                ctx.tool_target_web = query
                ctx.tool_trace = {
                    "triggered": True,
                    "tool": "web",
                    "status": "auto_triggered",
                    "error": "",
                }
                return NodeResult(next="web_search", context=ctx)
        print(f"[工具拦截] 未检测到工具调用")
        return NodeResult(next="format_check", context=ctx)
