import json, re
from sana.config import registry

class MemorySummarizer:
    _VALID_ACTIONS = {"update", "add", "delete"}

    def consolidate_buffer(self, chat_buffer, current_profile, max_retries=1):
        if not chat_buffer:
            print("[总结层] 缓存为空，跳过总结")
            return {"ok": True, "events": [], "profile_updates": []}

        print(f"[总结层] 开始总结 {len(chat_buffer)} 条对话记录")
        buf = "\n".join([f'[{m["role"]}]: {m["content"]}' for m in chat_buffer])
        system = self._build_system_prompt(current_profile)
        backend = registry.get_backend("summarize")
        cfg = registry.get_config("summarize")
        last_error = ""
        attempts = max(0, max_retries) + 1

        for attempt in range(attempts):
            if attempt > 0:
                print(f"[总结层] 第 {attempt} 次重试，原因: {last_error}")
                user_content = (
                    "[Previous attempt failed]\n"
                    f"{last_error}\n"
                    "Return ONLY valid JSON matching the schema above.\n\n"
                    f"Conversation:\n{buf}"
                )
            else:
                user_content = buf

            try:
                resp = backend.chat(
                    cfg.model_id,
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    system_prompt=system,
                    timeout=40,
                )
                raw = resp.content.strip()
                raw = re.sub(r"^```json\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
                jm = self._extract_json(raw)
                if jm is None:
                    last_error = "总结模型没有返回有效 JSON"
                    continue

                events, updates, error = self._validate_result(jm)
                if error:
                    last_error = error
                    continue

                print(f"[总结层] 完成: 提取 {len(events)} 个事件, {len(updates)} 条档案更新")
                return {"ok": True, "events": events, "profile_updates": updates}
            except Exception as e:
                last_error = str(e)

        print(f"[总结层] 重试后仍失败: {last_error}")
        return {
            "ok": False,
            "error": last_error or "总结失败",
            "events": [],
            "profile_updates": [],
        }

    def _build_system_prompt(self, current_profile):
        return f"""You are a memory consolidation engine.
Current profile: {json.dumps(current_profile, ensure_ascii=False)}
Output ONLY valid JSON in this exact shape:
{{
  "events": [
    "short memory summary"
  ],
  "profile_updates": [
    {{
      "action": "update",
      "category": "general_preferences",
      "key": "short_snake_case_key",
      "value": "string or number or boolean"
    }}
  ]
}}
Rules:
- "events" and "profile_updates" are both required arrays; use [] when there is nothing to store.
- "action" must be one of: update, add, delete.
- For update/add, "value" is required. For delete, omit "value".
- "category" should be an existing top-level profile category such as general_preferences or gaming_preferences.
- Keep event summaries concise and in the user's language.
- Do not include any text outside the JSON object."""

    def _validate_result(self, data):
        if not isinstance(data, dict):
            return [], [], "总结结果必须是 JSON 对象"
        if not isinstance(data.get("events"), list):
            return [], [], '"events" 必须是数组'
        if not isinstance(data.get("profile_updates"), list):
            return [], [], '"profile_updates" 必须是数组'

        events = []
        for idx, event in enumerate(data["events"]):
            if isinstance(event, str):
                if event.strip():
                    events.append(event.strip())
                continue
            if not isinstance(event, dict):
                return [], [], f"events[{idx}] 必须是字符串或对象"
            summary = event.get("summary", "")
            entities = event.get("entities", [])
            if not isinstance(summary, str) or not summary.strip():
                return [], [], f"events[{idx}].summary 不能为空"
            if not isinstance(entities, list):
                entities = []
            events.append({"summary": summary.strip(), "entities": entities})

        updates = []
        for idx, update in enumerate(data["profile_updates"]):
            if not isinstance(update, dict):
                return [], [], f"profile_updates[{idx}] 必须是对象"
            action = update.get("action", "")
            category = update.get("category", "")
            key = update.get("key", "")
            if action not in self._VALID_ACTIONS:
                return [], [], f"profile_updates[{idx}].action 必须是 update/add/delete"
            if not isinstance(category, str) or not category.strip():
                return [], [], f"profile_updates[{idx}].category 不能为空"
            if not isinstance(key, str) or not key.strip():
                return [], [], f"profile_updates[{idx}].key 不能为空"
            if action in ("update", "add") and "value" not in update:
                return [], [], f"profile_updates[{idx}] 的 update/add 需要 value"
            updates.append({
                "action": action,
                "category": category.strip(),
                "key": key.strip(),
                "value": update.get("value"),
            })

        return events, updates, None

    def _extract_json(self, text):
        s = text.find("{")
        if s == -1:
            return None
        cnt = 0
        for i in range(s, len(text)):
            if text[i] == "{":
                cnt += 1
            elif text[i] == "}":
                cnt -= 1
            if cnt == 0:
                try:
                    return json.loads(text[s:i + 1])
                except Exception:
                    return None
        return None
