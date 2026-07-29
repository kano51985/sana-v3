import json
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.prompts.system import SANA_SYSTEM_PROMPT

class PromptBuilderNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        prof_str = json.dumps(ctx.current_profile, ensure_ascii=False, indent=2)
        wm = "\n".join([f"[{m['role']}]: {m['content']}" for m in ctx.working_memory])
        ctx.augmented_input = (
            "[Profile]\n" + prof_str +
            "\n\n[Context]\n" + wm +
            "\n\n[Recall]\n" + ctx.recalled_context +
            "\n\n[ALMA]\n" + ctx.alma_override +
            "\n\n[User]: " + ctx.user_input
        )
        ctx.system_prompt = SANA_SYSTEM_PROMPT
        return NodeResult(next="llm_call", context=ctx)
