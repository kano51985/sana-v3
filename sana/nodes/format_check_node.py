import re
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context


class FormatCheckerNode(PipelineNode):
    """格式校验层：只做标签完整性检查，不做内容或风格审查。
    情绪强烈时（P<0.2 或 intensity>0.6）跳过校验，放行原始回复。
    """

    MAX_RETRIES = 2

    def process(self, ctx: Context) -> NodeResult:
        raw = ctx.llm_raw_response or ""
        if not raw.strip():
            return NodeResult(next="compliance_review", context=ctx)
        if not self._should_check_format(ctx):
            return NodeResult(next="compliance_review", context=ctx)
        has_thinking = bool(re.search(r"<thinking>", raw, re.IGNORECASE))
        has_chat = bool(re.search(r"<chat>", raw, re.IGNORECASE))
        if not has_thinking or not has_chat:
            missing = "thinking" if not has_thinking else "chat"
            return self._reject(ctx, f"回复缺少 <{missing}> 标签，请补充完整")
        return NodeResult(next="compliance_review", context=ctx)

    def _should_check_format(self, ctx):
        if not ctx.emotional_trajectory:
            return True
        last = ctx.emotional_trajectory[-1]
        pad = last.get("pad", {})
        intensity = last.get("intensity", 0)
        if pad.get("P", 0) < -0.2:
            print(f"[格式校验] 跳过: P={pad['P']:.2f} 情绪强烈, 放行")
            return False
        if intensity > 0.6:
            print(f"[格式校验] 跳过: intensity={intensity:.2f} 情绪强烈, 放行")
            return False
        return True

    def _reject(self, ctx, feedback):
        if ctx.review_retry_count >= self.MAX_RETRIES:
            print(f"[格式校验] 重试已达上限({self.MAX_RETRIES})，自动补全标签")
            ctx.llm_raw_response = self._auto_repair(ctx.llm_raw_response or "")
            return NodeResult(next="compliance_review", context=ctx)
        ctx.review_retry_count += 1
        ctx.review_feedback = feedback
        ctx.augmented_input += "\n\n[Format Check]: " + feedback
        print(f"[格式校验] 不通过: {feedback}")
        return NodeResult(fallback="llm_call", context=ctx)

    def _auto_repair(self, raw):
        if not raw.strip():
            raw = "(空回复)"
        has_thinking = bool(re.search(r"<thinking>", raw, re.IGNORECASE))
        has_chat = bool(re.search(r"<chat>", raw, re.IGNORECASE))
        if not has_thinking and not has_chat:
            raw = f"<thinking>\n(自动补全)\n</thinking>\n<chat>\n{raw}\n</chat>"
        elif not has_thinking:
            raw = f"<thinking>\n(自动补全)\n</thinking>\n" + raw
        elif not has_chat:
            raw = raw + f"\n<chat>\n(自动补全)\n</chat>"
        return raw
