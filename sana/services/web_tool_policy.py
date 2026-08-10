from sana.core.context import Context
from sana.services.web_tool_config import WebToolConfig, WebToolConfigStore


EXPLICIT_KEYWORDS = [
    "帮我查",
    "帮我搜",
    "查一下",
    "查查",
    "搜一下",
    "搜搜",
    "百度一下",
    "帮我看看",
    "查一查",
    "搜一搜",
]

LEVEL_LABELS = {
    0: "关闭",
    1: "显式",
    2: "克制",
    3: "主动",
    4: "探索",
}


class WebToolPolicy:
    def __init__(self, config_store: WebToolConfigStore | None = None, config: WebToolConfig | None = None):
        self.config_store = config_store or WebToolConfigStore()
        self._config = config

    def get_config(self) -> WebToolConfig:
        return self._config or self.config_store.load()

    def is_explicit_request(self, user_input: str, perception_data: dict) -> bool:
        text = (user_input or "").lower()
        if not text:
            return False
        if any(k in text for k in EXPLICIT_KEYWORDS):
            intent = perception_data.get("intent", "")
            return intent in ("ask", "chat", "") or "查" in text or "搜" in text
        return False

    def evaluate(
        self,
        user_input: str,
        perception_data: dict,
        mood: dict | None = None,
        config: WebToolConfig | None = None,
    ) -> tuple[bool, str, str]:
        cfg = config or self.get_config()
        mood = mood or {}
        if not cfg.enabled:
            return False, "blocked", "disabled"

        explicit = self.is_explicit_request(user_input, perception_data)
        strong_negative = self._is_strong_negative(mood)
        level = cfg.autonomy_level

        if level == 0:
            return False, "blocked", "disabled"
        if level == 1:
            if explicit:
                return True, "executed", "explicit"
            return False, "blocked", "not_explicit"
        if level == 2:
            if explicit:
                return True, "executed", "explicit"
            if self._needs_external_info(perception_data):
                return True, "executed", "external_info"
            return False, "blocked", "not_required"
        if level == 3:
            if explicit:
                return True, "fob" if strong_negative else "executed", "explicit"
            if strong_negative:
                return False, "blocked", "mood_blocked"
            return True, "executed", "proactive"
        if level == 4:
            if explicit and strong_negative:
                return False, "blocked", "mood_refused"
            if strong_negative:
                return False, "blocked", "mood_blocked"
            return True, "executed", "explore"
        return False, "blocked", "invalid_level"

    def build_policy_block(self, ctx: Context, alma=None) -> str:
        cfg = self.get_config()
        ctx.web_tool_enabled = cfg.enabled
        ctx.web_autonomy_level = cfg.autonomy_level
        if not cfg.enabled:
            return ""

        mood = self.mood_from_ctx(ctx, alma)
        lines = [
            "[Web Tool Policy]",
            f"Enabled: {cfg.enabled}",
            f"Autonomy: {cfg.autonomy_level} - {LEVEL_LABELS.get(cfg.autonomy_level, '未知')}",
            f"当前心情: {mood.get('emotion', 'Neutral')} P={mood.get('P', 0):.2f} A={mood.get('A', 0):.2f} D={mood.get('D', 0):.2f}",
            "",
            "触发规则:",
        ]
        lines.extend(self._rules_for_level(cfg.autonomy_level))
        lines.extend([
            "",
            "心情规则:",
            "- 心情越好越愿意查询，心情越差越不愿意主动查询。",
            "- 坏心情下可以更简短、不耐烦，但不能编造搜索结果。",
            "- 只有联网能提供实时/外部/事实信息时才调用 <invoke_web>。",
            "- 查询必须写具体 query，例如 <invoke_web query=\"农现在什么版本\"/>。",
            "- 不要伪造搜索结果；查询失败时诚实说明。",
        ])
        return "\n".join(lines)

    def _rules_for_level(self, level: int) -> list[str]:
        if level == 1:
            return [
                "- 只有用户明确要求“帮我查/搜一下”时才调用。",
                "- 用户明确要求时必须执行，心情只影响回复语气。",
            ]
        if level == 2:
            return [
                "- 用户明确要求时调用。",
                "- 事实性、时效性、外部知识问题可自行调用。",
                "- 正常闲聊不要调用。",
                "- 坏心情降低非必要查询深度，但明确请求仍执行。",
            ]
        if level == 3:
            return [
                "- 用户明确要求时调用，坏心情下可以敷衍但通常仍执行。",
                "- 有信息价值或 Sana 好奇时可调用。",
                "- 强烈坏心情下可以拒绝或敷衍非必要查询。",
            ]
        if level == 4:
            return [
                "- 更愿意主动查询。",
                "- 强烈坏心情下可以拒绝或敷衍明确请求。",
                "- 非必要查询也可以触发。",
            ]
        return ["- 联网查询被关闭。"]

    def _needs_external_info(self, perception_data: dict) -> bool:
        intent = perception_data.get("intent", "")
        if intent == "ask":
            return True
        if intent in ("share", "chat"):
            return bool(perception_data.get("entities"))
        return False

    def _is_strong_negative(self, mood: dict) -> bool:
        try:
            p = float(mood.get("P", 0) or 0)
        except (TypeError, ValueError):
            p = 0.0
        if p < -0.35:
            return True
        emotion = str(mood.get("emotion", ""))
        try:
            intensity = float(mood.get("intensity", 0) or 0)
        except (TypeError, ValueError):
            intensity = 0.0
        return emotion in ("Distress", "Anger", "Reproach") and intensity >= 0.6

    def mood_from_ctx(self, ctx: Context, alma=None) -> dict:
        if ctx.emotional_trajectory:
            last = ctx.emotional_trajectory[-1]
            pad = last.get("pad", {})
            return {
                "P": pad.get("P", 0),
                "A": pad.get("A", 0),
                "D": pad.get("D", 0),
                "emotion": last.get("emotion", "Neutral"),
                "intensity": last.get("intensity", 0),
            }
        if alma is not None:
            return {
                "P": alma.current_mood.get("P", 0),
                "A": alma.current_mood.get("A", 0),
                "D": alma.current_mood.get("D", 0),
                "emotion": alma.current_transient_emotion,
                "intensity": alma.emotion_intensity,
            }
        return {"P": 0, "A": 0, "D": 0, "emotion": "Neutral", "intensity": 0}
