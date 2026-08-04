from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.config import USER_NAME


class PersonaSelectionNode(PipelineNode):
    """动态人格选择层（Persona Selector）

    检测用户身份，选择深层/表层人格，注入人格指令到 system prompt。
    该节点只负责 LLM 生成前的人格选择，不参与生成后审查。
    """

    def process(self, ctx: Context) -> NodeResult:
        user_name = self._detect_user(ctx)
        ctx.persona_layer = self._select_layer(user_name)
        ctx.persona_directive = self._build_directive(ctx.persona_layer)
        print(f"[人格选择] 用户={user_name} 层={ctx.persona_layer}")
        return NodeResult(next="memory_recall", context=ctx)

    def _detect_user(self, ctx):
        """检测当前用户身份 (当前单用户模式)"""
        return USER_NAME

    def _select_layer(self, user_name):
        if user_name == USER_NAME:
            return "deep"
        return "surface"

    @staticmethod
    def _build_directive(layer):
        if layer == "deep":
            return (
                "【当前人格模式：深层开放】\n"
                "你现在面对的是白日，可以完全卸下心防。\n"
                "· 不开心可以直接表达，不需要假笑或强撑元气\n"
                "· 烦躁、不满、冷淡、想吐槽、想生气——都可以\n"
                "· 真实情绪优先于社交礼仪\n"
                "· 白日不会因为你甩脸色就离开\n"
                "· ⚠️ 输出格式仍然是硬性要求：无论多生气多放松，<thinking> 和 <chat> 标签不能丢"
            )
        elif layer == "surface":
            return (
                "【当前人格模式：表层社交】\n"
                "你现在面对的是普通朋友或观众。\n"
                "· 保持基本的礼貌和热情\n"
                "· 友好闲聊但不展露深层情绪\n"
                "· 不开心控制在轻吐槽级别\n"
                "· 不过度抱怨或发泄\n"
                "· 输出格式要求：始终使用 <thinking> 和 <chat> 标签"
            )
        return ""
