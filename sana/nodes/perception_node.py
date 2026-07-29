from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.perception import PerceptionLayer
from sana.config import USER_NAME

class PerceptionNode(PipelineNode):
    def __init__(self, perception: PerceptionLayer):
        self.perception = perception
    def process(self, ctx: Context) -> NodeResult:
        recent = []
        for m in reversed(ctx.working_memory):
            if m.get("role") == USER_NAME:
                recent.append(m["content"])
            if len(recent) >= 5:
                break
        ctx.perception_data = self.perception.analyze(ctx.user_input, recent)
        return NodeResult(next="alma", context=ctx)
