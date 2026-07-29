import requests, json, re
from sana.config import registry

class PerceptionLayer:
    def analyze(self, user_text: str, recent_messages: list[str] = None) -> dict:
        backend = registry.get_backend("perception")
        cfg = registry.get_config("perception")
        print(f"[感知层] 分析输入: {user_text[:60]}...")

        # 构建历史上下文文本
        recent_history = ""
        if recent_messages:
            recent_history = "\n".join(f"- {m}" for m in recent_messages)

        system = (
            "You are a text feature extractor. Output ONLY valid JSON.\n\n"
            "Recent user messages (most recent first):\n"
            f"{recent_history}\n"
            "Current user message: {user_text}\n\n"
            "Fields:\n"
            "- occ_emotion: list of OCC emotion labels.\n"
            '  Must be from ["Joy","Distress","Anger","Admiration","Reproach","Neutral"].\n'
            "  Mapping guide:\n"
            "    happy/excited/grateful/proud  -> Joy\n"
            "    sad/tired/frustrated/lonely/hurt/longing -> Distress\n"
            "    angry/annoyed/furious/irritated -> Anger\n"
            "    impressed/loving/adored/cared_for -> Admiration\n"
            "    disappointed/blaming/jealous -> Reproach\n"
            "    calm/neutral/bored/uninterested -> Neutral\n"
            "- emotion: short Chinese description of dominant emotion (free text)\n"
            '- intent: "chat"|"complain"|"ask"|"share"|"praise"|"joke"|"blame"\n'
            "- entities: list of named entities\n"
            "- relation: str\n"
            "- intensity: float 0.0-1.0\n"
            "- user_repeat_count: int.\n"
            "  How many consecutive messages (including current) share the same core intent.\n"
            "  1 = first occurrence or intent just changed. 2+ = repeating.\n"
            '- user_behavior_type: str.\n'
            '  "normal"|"blame"|"tease"|"dump"|"ignore"|"praise"|"other"\n\n'
            "Example:\n"
            '{"occ_emotion":["Neutral"],"emotion":"询问","intent":"ask","entities":[],"relation":"","intensity":0.3,"user_repeat_count":1,"user_behavior_type":"normal"}'
        )

        user_prompt = f"Recent history:\n{recent_history}\n---\nCurrent: {user_text}"
        try:
            resp = backend.chat(cfg.model_id, [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt}
            ], system_prompt=system, timeout=10)
            raw = resp.content
            jm = re.search(r"\{.*\}", raw, re.DOTALL)
            if jm:
                result = json.loads(jm.group(0))
                print(f"[感知层] 结果: occ_emotion={result.get('occ_emotion')}, user_repeat={result.get('user_repeat_count')}, behavior={result.get('user_behavior_type')}, 情绪={result.get('emotion')}, 意图={result.get('intent')}, 强度={result.get('intensity')}, 实体={result.get('entities')}")
                return result
        except Exception as e:
            print(f"[感知层] 分析失败: {e}")
        return {"occ_emotion": ["Neutral"], "emotion": "calm",
                "intent": "chat", "entities": [],
                "relation": "", "intensity": 0.5,
                "user_repeat_count": 1, "user_behavior_type": "normal"}