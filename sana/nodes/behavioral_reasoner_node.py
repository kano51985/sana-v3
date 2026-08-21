from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.behavioral_reasoner import BehavioralReasoner


class BehavioralReasonerNode(PipelineNode):
    def __init__(self, reasoner: BehavioralReasoner, alma=None):
        self.reasoner = reasoner
        self.alma = alma

    def process(self, ctx: Context) -> NodeResult:
        ocean = (self.alma.ocean if self.alma else ctx.current_profile.get("ocean", {}))
        ctx.behavioral_insight = self.reasoner.analyze(
            perception_data=ctx.perception_data,
            working_memory=ctx.working_memory,
            ocean=ocean,
        )
        if ctx.behavioral_insight["patterns"]:
            for p in ctx.behavioral_insight["patterns"]:
                print(
                    f"[行为推理] 模式={p['type']}, "
                    f"置信度={p['confidence']}, {p['detail']}"
                )
            if ctx.behavioral_insight["emotion_additions"]:
                print(
                    "[行为推理] 情绪加成: "
                    f"{ctx.behavioral_insight['emotion_additions']}, 强度: "
                    f"{ctx.behavioral_insight['suggested_intensity']}"
                )
        return NodeResult(next="alma", context=ctx)
