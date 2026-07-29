from sana.core.context import Context
from sana.core.node import PipelineNode, NodeResult

class PipelineResult:
    def __init__(self, success: bool = True, context: Context = None, error: str = ""):
        self.success = success
        self.context = context
        self.error = error

class PipelineEngine:
    def __init__(self):
        self._nodes: dict[str, PipelineNode] = {}
        self._start_node: str = ""
        self._max_steps = 50

    def register(self, node_id: str, node: PipelineNode) -> "PipelineEngine":
        self._nodes[node_id] = node
        return self

    def start_at(self, node_id: str) -> "PipelineEngine":
        self._start_node = node_id
        return self

    def run(self, ctx: Context) -> PipelineResult:
        current = self._start_node
        for step in range(self._max_steps):
            node = self._nodes.get(current)
            if not node:
                return PipelineResult(False, ctx, f"Node {current!r} not found")
            result = node.process(ctx)
            ctx = result.context or ctx
            if result.fallback:
                current = result.fallback
            elif result.next:
                current = result.next
            else:
                break
        return PipelineResult(True, ctx)
