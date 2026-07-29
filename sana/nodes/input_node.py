from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context

class InputNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        return NodeResult(next="perception", context=ctx)
