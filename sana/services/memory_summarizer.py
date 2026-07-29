import json, re
from sana.config import registry

class MemorySummarizer:
    def consolidate_buffer(self, chat_buffer, current_profile):
        if not chat_buffer:
            print(f"[总结层] 缓存为空，跳过总结")
            return {'events': [], 'profile_updates': []}
        print(f"[总结层] 开始总结 {len(chat_buffer)} 条对话记录")
        buf = '\n'.join([f'[{m["role"]}]: {m["content"]}' for m in chat_buffer])
        system = f'''You are a memory consolidation engine.
Current profile: {json.dumps(current_profile, ensure_ascii=False)}
Output ONLY valid JSON with events and profile_updates arrays.'''
        backend = registry.get_backend('summarize')
        cfg = registry.get_config('summarize')
        try:
            resp = backend.chat(cfg.model_id, [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': buf}
            ], system_prompt=system, timeout=40)
            raw = resp.content.strip()
            raw = re.sub(r'^```json\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            jm = self._extract_json(raw)
            if jm:
                evts = jm.get('events', [])
                upds = jm.get('profile_updates', [])
                print(f"[总结层] 完成: 提取 {len(evts)} 个事件, {len(upds)} 条档案更新")
                return jm
        except Exception as e:
            print(f'Summarizer error: {e}')
        return {'events': [], 'profile_updates': []}

    def _extract_json(self, text):
        s = text.find('{')
        if s == -1: return None
        cnt = 0
        for i in range(s, len(text)):
            if text[i] == '{': cnt += 1
            elif text[i] == '}': cnt -= 1
            if cnt == 0:
                try: return json.loads(text[s:i+1])
                except: return None
        return None
