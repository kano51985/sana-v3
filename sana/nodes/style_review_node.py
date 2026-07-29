from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
import re
import json
from sana.config import registry

MAX_RETRIES = 2

class StyleReviewNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        raw = ctx.llm_raw_response or ""
        if not raw.strip():
            return NodeResult(next="compliance_review", context=ctx)
        needs_llm = False
        has_thinking = bool(re.search(r"<thinking>", raw, re.IGNORECASE))
        has_chat = bool(re.search(r"<chat>", raw, re.IGNORECASE))
        if not has_thinking or not has_chat:
            missing = "thinking" if not has_thinking else "chat"
            return self._reject(ctx, f"回复缺少 <{missing}> 标签，请补充完整")
        name = ctx.current_profile.get("name", "")
        if name:
            chat_content = self._extract_tag(raw, "chat")
            if chat_content and name not in chat_content:
                needs_llm = True
        if needs_llm:
            result = self._llm_check(ctx)
            if result and not result.get("pass", True):
                return self._reject(ctx, result.get("revision", "回复风格不符合当前人设，请调整"))
        return NodeResult(next="compliance_review", context=ctx)

    def _extract_tag(self, text, tag):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def _reject(self, ctx, feedback):
        if ctx.review_retry_count >= MAX_RETRIES:
            print(f"[风格审查] 重试已达上限({MAX_RETRIES})，放行")
            return NodeResult(next="compliance_review", context=ctx)
        ctx.review_retry_count += 1
        ctx.review_feedback = feedback
        ctx.augmented_input += f"\n\n[Style Review]: {feedback}"
        print(f"[风格审查] 不通过: {feedback}")
        return NodeResult(fallback="llm_call", context=ctx)

    def _llm_check(self, ctx):
        system = (
            "你是一个角色风格审查官。判断一段回复是否符合角色设定，且情绪表现是否自洽。\n\n"
            "角色设定摘要：\n"
            "- 名称：Sana（24岁，全职游戏主播，粉丝1w+，业余漫展coser）\n"
            "- 性格层次：表面对外开朗直率；中层对熟人爱撒娇；深层只对\"白日\"完全放松\n"
            "- 常用语气词：好耶、贴贴、救命、非酋、保底人、离大谱\n"
            "- 情绪基调：元气少女，但会根据情绪有5档变化（元气→日常→微吐槽→轻度emo→重度破防）\n\n"
            f"当前情绪状态：{ctx.alma_override}\n\n"
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
                print(f"[风格审查] LLM 判定: pass={result.get('pass')}")
                return result
        except Exception as e:
            print(f"[风格审查] LLM 调用失败: {e}")
        return None
