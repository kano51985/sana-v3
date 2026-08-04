from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.emotional_directive import EmotionalDirective

class DirectiveNode(PipelineNode):
    def __init__(self, alma, directive: EmotionalDirective):
        self.alma = alma
        self.directive = directive

    def process(self, ctx: Context) -> NodeResult:
        emotion_label = self.alma.current_transient_emotion
        if "+" in emotion_label:
            emotion_label = emotion_label.split("+")[0]
        intensity = self.alma.emotion_intensity
        repeat = ctx.perception_data.get("user_repeat_count", 1)
        behavior = ctx.perception_data.get("user_behavior_type", "normal")
        ocean = self.alma.ocean
        pad = self.alma.current_mood
        ctx.emotional_directive = self.directive.generate(
            emotion_label=emotion_label,
            intensity=intensity,
            repeat_count=repeat,
            user_behavior=behavior,
            ocean=ocean,
            pad=pad,
        )
        # Log generated directive (first line only)
        summary = ctx.emotional_directive.split(chr(10))[0][:60] if ctx.emotional_directive else "(empty)"
        print(f"[指令] {self.alma.current_transient_emotion} {summary}")
        return NodeResult(next="persona_selection", context=ctx)
