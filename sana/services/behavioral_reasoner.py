class BehavioralReasoner:
    """行为模式分析器：检测用户行为模式，输出情绪调整建议"""

    def analyze(self, perception_data: dict, working_memory: list, ocean: dict) -> dict:
        repeat = perception_data.get("user_repeat_count", 1)
        bt = perception_data.get("user_behavior_type", "normal")
        occ = perception_data.get("occ_emotion", ["Neutral"])

        result = {
            "patterns": [],
            "emotion_additions": [],
            "suggested_intensity": 0.0,
        }

        has_negative = any(e in occ for e in ["Reproach", "Anger", "Distress"])

        # 模式1：重复测试/逗弄
        if repeat >= 3 and bt in ("normal", "chat", "ask") and not has_negative:
            conf = min(0.3 + 0.15 * (repeat - 2), 0.9)
            result["patterns"].append({
                "type": "tease_test",
                "confidence": round(conf, 2),
                "detail": f"用户重复{repeat}次相同意图，推断为测试或逗弄"
            })
            result["emotion_additions"].append("Reproach")
            result["suggested_intensity"] = round(max(0.4, min(0.3 + 0.15 * (repeat - 2), 0.8)), 2)

        # 模式2：用户责怪/发泄
        if bt == "blame":
            result["patterns"].append({
                "type": "venting",
                "confidence": 0.7,
                "detail": "用户表达责怪，推断为发泄情绪"
            })
            result["emotion_additions"].append("Distress")
            result["suggested_intensity"] = round(max(0.5, min(0.4 + 0.1 * repeat, 0.8)), 2)
        elif bt == "dump":
            result["patterns"].append({
                "type": "venting",
                "confidence": 0.5,
                "detail": "用户在倾倒负面情绪"
            })
            if "Distress" not in result["emotion_additions"]:
                result["emotion_additions"].append("Distress")
            result["suggested_intensity"] = round(max(0.5, min(0.4 + 0.1 * repeat, 0.8)), 2)

        # 模式3：用户无视/冷淡
        if bt == "ignore":
            result["patterns"].append({
                "type": "cold_shoulder",
                "confidence": 0.6,
                "detail": "用户行为冷淡或无视"
            })
            result["emotion_additions"].extend(["Distress", "Reproach"])
            result["suggested_intensity"] = 0.4

        # OCEAN 调制
        n = ocean.get("N", 0.5)
        a = ocean.get("A", 0.7)
        if result["patterns"]:
            if n > 0.7:
                for p in result["patterns"]:
                    p["confidence"] = round(min(p["confidence"] + 0.1, 1.0), 2)
                result["suggested_intensity"] = round(min(result["suggested_intensity"] * 1.2, 1.0), 2)
            if a > 0.7:
                for p in result["patterns"]:
                    p["confidence"] = round(max(p["confidence"] - 0.1, 0), 2)
                result["suggested_intensity"] = round(result["suggested_intensity"] * 0.8, 2)

        return result
