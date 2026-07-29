from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.profile_manager import ProfileManager

class ProfileLoadNode(PipelineNode):
    def __init__(self, profile_mgr: ProfileManager):
        self.profile_mgr = profile_mgr
    def process(self, ctx: Context) -> NodeResult:
        ctx.current_profile = self.profile_mgr.load_profile()
        return NodeResult(next="working_memory", context=ctx)
