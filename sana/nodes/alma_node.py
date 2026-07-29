from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.alma_engine import ALMAEngine

class ALMANode(PipelineNode):
    def __init__(self, alma: ALMAEngine):
        self.alma = alma
        # 情绪标签归一化：感知层 → ALMA OCC 标准标签
        self._emotion_map = {
            # 中文 → OCC
            "开心": "Joy", "高兴": "Joy", "快乐": "Joy", "兴奋": "Joy", "愉快": "Joy", "喜悦": "Joy", "欢喜": "Joy",
            "期待": "Joy", "期盼": "Joy",
            "沮丧": "Distress", "疲惫": "Distress", "累": "Distress", "难过": "Distress", "伤心": "Distress",
            "悲伤": "Distress", "痛苦": "Distress", "失望": "Distress", "失落": "Distress", "低落": "Distress",
            "焦虑": "Distress", "担忧": "Distress", "不安": "Distress", "郁闷": "Distress",
            "生气": "Anger", "愤怒": "Anger", "恼火": "Anger", "烦躁": "Anger",
            "不耐烦": "Anger", "厌烦": "Anger",
            "崇拜": "Admiration", "佩服": "Admiration", "赞赏": "Admiration", "喜欢": "Admiration",
            "责备": "Reproach", "批评": "Reproach", "指责": "Reproach", "抱怨": "Reproach",
            "平静": "Neutral", "一般": "Neutral", "普通": "Neutral",
            "询问": "Neutral", "好奇": "Neutral", "疑问": "Neutral", "困惑": "Neutral",
            "无聊": "Neutral", "无所谓": "Neutral", "平淡": "Neutral",
            # 英文变体 → OCC
            "tired": "Distress", "sad": "Distress", "upset": "Distress", "exhausted": "Distress",
            "happy": "Joy", "excited": "Joy", "glad": "Joy", "delighted": "Joy",
            "angry": "Anger", "furious": "Anger", "annoyed": "Anger",
            "admire": "Admiration", "adored": "Admiration",
            "reproach": "Reproach", "blame": "Reproach",
            "ask": "Neutral", "curious": "Neutral", "question": "Neutral",
        }
    def process(self, ctx: Context) -> NodeResult:
        occ = ctx.perception_data.get("occ_emotion", ["Neutral"])
        raw_intensity = ctx.perception_data.get("intensity", 0.5)
        try:
            intensity = float(raw_intensity)
        except (ValueError, TypeError):
            intensity = 0.5
        # 归一化情绪标签：感知层输出 → ALMA 可识别的 OCC 标准标签
        normalized = []
        for label in occ:
            if label in self.alma.emotion_to_pad_impact:
                normalized.append(label)
            elif label in self._emotion_map:
                normalized.append(self._emotion_map[label])
            else:
                print(f"[ALMA] 未知情绪标签 '{label}'，归为 Neutral")
                normalized.append("Neutral")
        if not normalized:
            normalized = ["Neutral"]

        # Apply behavioral reasoner emotion additions
        bi = ctx.behavioral_insight
        if bi and bi.get("emotion_additions"):
            for em in bi["emotion_additions"]:
                if em not in normalized:
                    normalized.append(em)
            si = bi.get("suggested_intensity", 0.0)
            if si > 0.0:
                raw = float(ctx.perception_data.get("intensity", 0.5))
                intensity = max(raw, min(si, 1.0))
                print(f"[ALMA] behavior adjust: +{bi['emotion_additions']} perception={raw:.2f} suggested={si:.2f} final={intensity:.2f}")

        self.alma.process_event(normalized, intensity=intensity)
        ctx.alma_override = self.alma.get_alma_prompt()
        print(f"[ALMA] state: {normalized} intensity={intensity:.2f} PAD=({self.alma.current_mood['P']:.2f},{self.alma.current_mood['A']:.2f},{self.alma.current_mood['D']:.2f})")

        # Update emotional trajectory for Layer 3
        ctx.emotional_trajectory.append({
            "turn": len(ctx.working_memory) // 2 + 1,
            "emotion": self.alma.current_transient_emotion,
            "intensity": self.alma.emotion_intensity,
            "pad": dict(self.alma.current_mood),
        })
        if len(ctx.emotional_trajectory) > 5:
            ctx.emotional_trajectory = ctx.emotional_trajectory[-5:]

        return NodeResult(next="directive", context=ctx)
