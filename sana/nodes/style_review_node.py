from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context


class StyleReviewNode(PipelineNode):
    """生成后风格审查闸门

    当前阶段只做 pass-through：格式校验通过后，把回复交给 compliance_review。
    后续接入 LLM 风格审查时，应在这里检查人设与当前情绪状态的自洽性。
    """

    def process(self, ctx: Context) -> NodeResult:
        return NodeResult(next="compliance_review", context=ctx)
