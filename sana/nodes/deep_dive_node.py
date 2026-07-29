from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.config import registry

class DeepDiveNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        if ctx.tool_triggered and ctx.tool_target_batch:
            print(f"[深潜] 开始深潜，目标批次: {ctx.tool_target_batch}")
            detail = f"[Details for {ctx.tool_target_batch}]"
            enhanced = ctx.augmented_input + "\n" + detail
            backend = registry.get_backend("chat")
            cfg = registry.get_config("chat")
            try:
                resp = backend.chat(cfg.model_id, [
                    {"role": "system", "content": ctx.system_prompt},
                    {"role": "user", "content": enhanced}
                ], system_prompt=ctx.system_prompt, timeout=30)
                ctx.llm_raw_response = resp.content
                print(f"[深潜] 深潜完成 ({len(resp.content)} 字符)")
            except Exception as e:
                print(f"[深潜] 深潜失败: {e}")
                pass
        return NodeResult(next="format_check", context=ctx)
