from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context

class WorkingMemoryNode(PipelineNode):
    def __init__(self, working_memory: list):
        self.working_memory = working_memory
    def process(self, ctx: Context) -> NodeResult:
        ctx.working_memory = self.working_memory
        return NodeResult(next="prompt_builder", context=ctx)
