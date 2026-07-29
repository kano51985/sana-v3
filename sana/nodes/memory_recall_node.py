from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.memory_service import MemoryManager

class MemoryRecallNode(PipelineNode):
    def __init__(self, memory: MemoryManager):
        self.memory = memory
    def process(self, ctx: Context) -> NodeResult:
        entities = ctx.perception_data.get("entities", [])
        query = " ".join(entities) + " " + ctx.user_input
        ctx.recalled_context = self.memory.recall(query)
        return NodeResult(next="profile_load", context=ctx)
