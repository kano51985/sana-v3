from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.config import registry

class LLMCallNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        backend = registry.get_backend("chat")
        cfg = registry.get_config("chat")
        print(f"[LLM] Calling {cfg.backend_name}/{cfg.model_id} ...")
        try:
            resp = backend.chat(cfg.model_id, [
                {"role": "system", "content": ctx.system_prompt},
                {"role": "user", "content": ctx.augmented_input}
            ], system_prompt=ctx.system_prompt, timeout=10)
            ctx.llm_raw_response = resp.content
            print(f"[LLM] OK ({len(resp.content)} chars)")
            return NodeResult(next="tool_intercept", context=ctx)
        except Exception as e:
            print(f"[LLM] FAIL: {e}")
            ctx.llm_raw_response = f"<chat>[LLM Error: {e}]</chat>"
            return NodeResult(next="response_parser", context=ctx)
