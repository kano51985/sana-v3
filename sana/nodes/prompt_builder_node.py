import json
from sana.config import TIMEZONE_OVERRIDE
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.prompts.system import SANA_SYSTEM_PROMPT
from sana.services.time_provider import CurrentTimeProvider

class PromptBuilderNode(PipelineNode):
    def __init__(self, time_provider: CurrentTimeProvider | None = None):
        self.time_provider = time_provider or CurrentTimeProvider(TIMEZONE_OVERRIDE)

    def process(self, ctx: Context) -> NodeResult:
        # Time is an environment fact; failure must not block conversation.
        try:
            ctx.current_time = self.time_provider.format_now()
        except Exception:
            ctx.current_time = ""

        # Layer 1: Stable Persona Core
        stable_core = SANA_SYSTEM_PROMPT

        # Layer 2: Emotional Directive (from ALMA translation)
        directive = ctx.emotional_directive or ""

        # Layer 3: Emotional Trajectory
        trajectory = ""
        if ctx.emotional_trajectory:
            lines = []
            for entry in ctx.emotional_trajectory:
                turn = entry.get("turn", "?")
                emotion = entry.get("emotion", "?")
                intensity = entry.get("intensity", 0)
                pad = entry.get("pad", {})
                lines.append(
                    f"  Turn {turn}: emotion={emotion}, intensity={intensity:.2f}, "
                    f"P={pad.get('P',0):.2f} A={pad.get('A',0):.2f} D={pad.get('D',0):.2f}"
                )
            trajectory = "[情绪轨迹]\n" + "\n".join(lines)

        # Layer 4: Context (working memory + profile)
        prof_str = json.dumps(ctx.current_profile, ensure_ascii=False, indent=2)
        wm_lines = []
        for m in ctx.working_memory:
            if m.get("content"):
                wm_lines.append(f"[{m['role']}]: {m['content']}")
        wm = "\n".join(wm_lines)

        context_block = (
            "[Profile]\n" + prof_str +
            "\n\n[Recent Conversation]\n" + wm +
            "\n\n[Recall]\n" + ctx.recalled_context +
            "\n\n[User]: " + ctx.user_input
        )

        # Assemble system prompt: Layer 1 (core) + Layer 1.5 (persona) + Layer 2 (emotion) + Layer 3 (format constraint)
        layers = []
        if ctx.current_time:
            layers.append("[当前时间]\n" + ctx.current_time)
        layers.append(stable_core)
        if ctx.persona_directive:
            layers.append(ctx.persona_directive)
        if directive:
            layers.append(directive)
        FORMAT_CONSTRAINT = (
            "\n\n[输出格式]\n"
            "输出格式不受任何人格模式或情绪状态影响。"
            "无论处于什么人格层级，<thinking> 和 <chat> 标签都是必需的。"
            "<thinking> 用于记录内心活动，<chat> 用于对白日说话。"
            "<chat> 内允许使用 <pause> 或 <pause ms=\"600\"/> 控制句子之间停顿，不要滥用。"
            "这是最高优先级的格式约束，不可被其他指令覆盖。"
        )
        ctx.system_prompt = "\n\n".join(layers) + FORMAT_CONSTRAINT

        # Augmented input gets Layer 3 + Layer 4
        parts = []
        if trajectory:
            parts.append(trajectory)
        parts.append(context_block)
        ctx.augmented_input = "\n\n".join(parts)

        return NodeResult(next="llm_call", context=ctx)
