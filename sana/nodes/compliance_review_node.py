import re, json
from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.config import registry, AGENT_NAME
from sana.services.alma_engine import ALMAEngine

class ComplianceReviewNode(PipelineNode):
    def __init__(self, alma: ALMAEngine):
        self.alma = alma

    def process(self, ctx: Context) -> NodeResult:
        # === 用户行为检测 ===
        repeat = ctx.perception_data.get("user_repeat_count", 1)
        bt = ctx.perception_data.get("user_behavior_type", "normal")
        if repeat > 1 or bt not in ("normal", "praise", ""):
            result = self._handle_user_behavior(ctx, repeat, bt)
            if result:
                return result

        # === 现有检查 ===
        raw = ctx.llm_raw_response or ""
        if not raw.strip():
            return NodeResult(context=ctx)
        needs_llm = False
        chat_content = self._extract_tag(raw, "chat") or raw
        user_entities = set(ctx.perception_data.get("entities", []))
        response_entities = self._extract_entities(chat_content)
        if user_entities and response_entities:
            if not (user_entities & response_entities):
                needs_llm = True
        wm = ctx.working_memory
        history_lengths = [len(m.get("content", "")) for m in wm if m.get("role") == AGENT_NAME]
        if history_lengths:
            cur_len = len(raw)
            ratio = cur_len / (sum(history_lengths) / len(history_lengths))
            if ratio > 3.0 or ratio < 0.3:
                needs_llm = True
        last_assistant = ""
        if wm and len(wm) >= 2:
            for m in reversed(wm):
                if m.get("role") == AGENT_NAME:
                    last_assistant = m.get("content", "")
                    break
        if last_assistant:
            current_chat = self._extract_tag(raw, "chat") or raw
            sim = self._text_similarity(current_chat, last_assistant)
            if sim > 0.85:
                return self._reject(ctx, "回复与上一条内容高度重复，请改写")
        if needs_llm:
            result = self._llm_check(ctx)
            if result and not result.get("pass", True):
                return self._reject(ctx, result.get("revision", "回复偏离了当前话题，请回到正题"))
        return NodeResult(next="response_parser", context=ctx)

    def _handle_user_behavior(self, ctx, repeat, bt):
        if bt in ("blame", "tease"):
            emotion = ["Reproach"]
            intensity = min(0.3 + 0.15 * (repeat - 1), 0.8)
        elif bt == "dump":
            emotion = ["Distress"]
            intensity = min(0.3 + 0.1 * (repeat - 1), 0.7)
        elif bt == "ignore":
            emotion = ["Distress", "Reproach"]
            intensity = 0.4
        elif repeat > 1 and bt in ("ask", "chat", "normal"):
            emotion = ["Reproach"]
            intensity = min(0.2 * (repeat - 1), 0.6)
        else:
            return None

        self.alma.process_event(emotion, intensity=intensity)
        ctx.alma_override = self.alma.get_alma_prompt()
        note = f"（用户行为: {bt}，连续第 {repeat} 次）"
        ctx.augmented_input += f"\n\n[Behavior Note]: {note}"
        print(f"[行为审查] 用户行为: {bt}, 重复:{repeat}次, 情绪注入:{emotion}")

        if repeat >= 2 or bt in ("blame", "tease", "dump", "ignore"):
            if ctx.review_retry_count < 2:
                ctx.review_retry_count += 1
                ctx.review_feedback = note
                print(f"[行为审查] fallback llm_call")
                return NodeResult(fallback="llm_call", context=ctx)
        return None

    def _extract_tag(self, text, tag):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def _extract_entities(self, text):
        quoted = re.findall(r'"([^"]+)"', text)
        book_titles = re.findall(r'《([^》]+)》', text)
        return set(quoted + book_titles)

    def _text_similarity(self, a, b):
        if not a or not b:
            return 0.0
        set_a, set_b = set(a), set(b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    def _reject(self, ctx, feedback):
        if ctx.review_retry_count >= 2:
            print(f"[话题审查] 重试已达上限(2)，放行")
            return NodeResult(next="response_parser", context=ctx)
        ctx.review_retry_count += 1
        ctx.review_feedback = feedback
        ctx.augmented_input += f"\n\n[Compliance Review]: {feedback}"
        print(f"[话题审查] 不通过: {feedback}")
        return NodeResult(fallback="llm_call", context=ctx)

    def _llm_check(self, ctx):
        history = ctx.working_memory[-4:] if ctx.working_memory else []
        context_str = "\n".join(
            f"[{m['role']}]: {m['content'][:200]}"
            for m in history if isinstance(m, dict)
        )
        system = (
            "你是一个话题相关性审查官。判断一段回复是否与当前对话上下文相关。\n\n"
            "对话上下文：\n" + (context_str or "(无)") +
            f"\n\n白日当前输入：{ctx.user_input}\n\n"
            "输出格式：{\"pass\": true/false, \"revision\": \"调整建议\"}"
        )
        user_prompt = f"原始回复：\n{ctx.llm_raw_response}"
        try:
            backend = registry.get_backend("perception")
            cfg = registry.get_config("perception")
            resp = backend.chat(cfg.model_id, [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt}
            ], system_prompt=system, timeout=10)
            raw = resp.content.strip()
            jm = re.search(r"\{.*\}", raw, re.DOTALL)
            if jm:
                result = json.loads(jm.group(0))
                print(f"[话题审查] LLM 判定: pass={result.get('pass')}")
                return result
        except Exception as e:
            print(f"[话题审查] LLM 调用失败: {e}")
        return None