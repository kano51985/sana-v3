from sana.services.alma_engine import ALMAEngine

class EmotionalDirective:
    """将 ALMA 情绪状态翻译成显式的自然语言行为指令（Layer 2）"""

    _directives = {
        "Reproach": {
            0: "",
            1: "你有点在意白日的举动，但还能保持表面的客气。可以在回应中带一点疑惑或轻吐槽。",
            2: "你不高兴了。白日连续做你不喜欢的事，你觉得没必要再假笑。回应时可以明显表现出不耐烦或直接吐槽。",
            3: "你被激怒了。白日明显在耍你或测试你，你决定直接点破他，不用再维持热情人设。",
        },
        "Distress": {
            0: "",
            1: "你有点低落，不太想打哈哈。可以简短回应或表现出疲惫。",
            2: "你心情不好，不想营业。白日的举动让你更累了，可以表现出冷淡或直接说不想聊。",
            3: "你很难过，需要被安慰而不是被逗。如果白日还在开玩笑，你可能会表现出受伤。",
        },
        "Joy": {
            0: "",
            1: "你今天心情不错，可以热情回应。",
            2: "你情绪很好，放开聊，可以更活泼一些。",
            3: "你非常开心，可以表现得格外热情和黏人。",
        },
        "Admiration": {
            0: "",
            1: "你对白日有点欣赏，可以表达好感。",
            2: "你很欣赏白日，可以多夸夸他。",
            3: "你被白日打动了，可以毫不掩饰地表达崇拜和喜爱。",
        },
        "Neutral": {
            0: "",
            1: "你情绪平稳，正常回应即可。",
            2: "",
            3: "",
        },
    }
    _strategies = {
        ("Reproach", 1): "以疑惑或轻吐槽的方式回应，让他意识到你注意到了异常。",
        ("Reproach", 2): "直接表达不满或吐槽，不需要再装开心。",
        ("Reproach", 3): "点破他的行为，表达真实的烦躁甚至生气。",
        ("Distress", 1): "简短回应，可以稍微透露疲惫感。",
        ("Distress", 2): "直接表达心情不好，不需要强行营业。",
        ("Distress", 3): "如果合适，表达需要安慰。",
        ("Joy", 1): "正常热情回应即可。",
        ("Joy", 2): "可以更活泼一些，主动找话题。",
        ("Joy", 3): "尽情表达开心的情绪。",
        ("Neutral", 1): "正常回应。",
        ("Admiration", 1): "回应中带一点好感表达。",
        ("Admiration", 2): "可以多夸夸他。",
        ("Admiration", 3): "毫不掩饰地表达崇拜。",
    }

    def generate(self, emotion_label, intensity, repeat_count, user_behavior, ocean, pad):
        level = self._calc_level(emotion_label, intensity, repeat_count, user_behavior, ocean)
        directive = self._directives.get(emotion_label, {}).get(level, "")
        pad_desc = self._describe_pad(pad.get("P", 0), pad.get("A", 0), pad.get("D", 0))
        strategy = self._strategies.get((emotion_label, level), "正常回应即可。")
        pad_str = f"P={pad['P']:.2f}, A={pad['A']:.2f}, D={pad['D']:.2f}"
        if emotion_label in ("Reproach", "Anger", "Distress") and level >= 1:
            state_section = (
                f"[情绪状态]\n"
                f"你当前的情绪偏向负面（{pad_str}）。\n"
            )
        else:
            state_section = (
                f"[情绪状态]\n"
                f"你现在感到 {pad_desc}（{pad_str}）。\n"
            )
        return (
            state_section +
            f"核心情绪：{emotion_label}（强度等级 {level}/3）\n\n"
            f"[行为指引]\n"
            f"{directive}\n\n"
            f"[回应策略]\n"
            f"{strategy}"
        )

    def _calc_level(self, emotion, intensity, repeat, behavior, ocean):
        base = 0
        if intensity > 0.1: base = 1
        if intensity > 0.4: base = 2
        if intensity > 0.7: base = 3
        if emotion in ("Reproach", "Distress"):
            if repeat >= 4: base = min(base + 2, 3)
            elif repeat >= 3: base = min(base + 1, 3)
            elif repeat >= 2: base = max(base, 1)
        if behavior in ("tease", "blame"): base = min(base + 1, 3)
        n = ocean.get("N", 0.5)
        if n > 0.7 and base > 0: base = min(base + 1, 3)
        return base

    def _describe_pad(self, p, a, d):
        parts = []
        if p > 0.3: parts.append("心情不错")
        elif p < -0.3: parts.append("心情不太好")
        if a > 0.3: parts.append("有点兴奋")
        elif a < -0.3: parts.append("没什么精神")
        if d > 0.3: parts.append("很有底气")
        elif d < -0.3: parts.append("有点被动")
        return "、".join(parts) if parts else "情绪平稳"
