import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field

from sana.config import registry
from sana.services.candidate_scorer import CandidateScorer


MIN_FALLBACK_SCORE = 25.0


@dataclass
class RerankVerdict:
    relevant: bool = True
    confidence: float = 0.0
    answer_fragments: list[str] = field(default_factory=list)
    reason: str = ""


class ResultReranker:
    CONFIDENCE_THRESHOLD = 0.7
    MAX_INPUT = 8

    def __init__(self, backend_role: str = "chat"):
        self.backend_role = backend_role
        self.last_trace: dict = {}

    def rerank(
        self,
        user_input: str,
        queries: list[str],
        candidates: list[dict],
        current_time: str = "",
        max_candidates: int | None = None,
        timeout: float | None = None,
    ) -> list[dict]:
        self.last_trace = {}
        if not candidates:
            return []
        selected = candidates[:max_candidates or self.MAX_INPUT]
        system = (
            "You are a web search result reranker. Output ONLY valid JSON.\n"
            "Judge whether each candidate can directly support an answer to the user's question.\n"
            'Output format: {"items": [{"url": "...", "relevant": true, "confidence": 0.9, "answer_fragments": ["..."], "reason": "..."}]}\n'
            "Rules:\n"
            "- relevant must be true only when the candidate contains useful facts, not just navigation.\n"
            "- confidence must be 0.0-1.0.\n"
            "- answer_fragments should quote compact facts from the snippet or text.\n"
            "- If a candidate is stale or unrelated, set relevant=false."
        )
        lines = []
        for item in selected:
            text = str(item.get("text") or item.get("snippet") or "").strip()[:500]
            lines.append(
                f"- title={item.get('title', '')}\n"
                f"  url={item.get('url', '')}\n"
                f"  content={text}"
            )
        user_prompt = (
            f"User input: {user_input}\n"
            f"Queries: {queries}\n"
            f"Current time: {current_time}\n"
            f"Candidates:\n{chr(10).join(lines)}\n"
            "Return the rerank verdicts for every candidate url."
        )
        llm_started = time.monotonic()
        data = self._llm_json(system, user_prompt, timeout=timeout or 10)
        if time.monotonic() - llm_started > (timeout or 10) * 1.2:
            return self._fallback(
                selected,
                user_input,
                queries,
                current_time,
                max_candidates,
                "reranker hard timeout",
            )
        if not data:
            return self._fallback(
                selected,
                user_input,
                queries,
                current_time,
                max_candidates,
                "LLM rerank 失败，保留候选",
            )

        verdicts = self._parse_verdicts(data)
        if not verdicts:
            return self._fallback(
                selected,
                user_input,
                queries,
                current_time,
                max_candidates,
                "LLM rerank 未返回有效 verdict，保留候选",
            )
        output = []
        scores = []
        for item in selected:
            verdict = verdicts.get(str(item.get("url", "") or ""), RerankVerdict())
            if not verdict.relevant or verdict.confidence < self.CONFIDENCE_THRESHOLD:
                continue
            out = dict(item)
            out["rerank_confidence"] = round(verdict.confidence, 2)
            out["rerank_reason"] = verdict.reason
            out["answer_fragments"] = verdict.answer_fragments
            out["rerank_score"] = round(verdict.confidence * 100, 2)
            output.append(out)
            scores.append({
                "url": item.get("url", ""),
                "relevant": True,
                "confidence": verdict.confidence,
                "score": out["rerank_score"],
            })

        missing_verdicts = len(selected) - len(verdicts)
        if not output and missing_verdicts:
            return self._fallback(
                selected,
                user_input,
                queries,
                current_time,
                max_candidates,
                f"LLM rerank 缺少 {missing_verdicts} 个 verdict，使用候选分数兜底",
            )
        filtered_count = len(selected) - len(output)
        error = ""
        if not output and filtered_count:
            error = "reranker filtered all candidates"
        elif missing_verdicts:
            error = f"reranker missing verdicts for {missing_verdicts} candidates"

        self.last_trace = {
            "reranked_count": len(selected),
            "reranker_output_count": len(output),
            "reranker_filtered_count": filtered_count,
            "reranker_verdict_count": len(verdicts),
            "reranker_scores": scores,
            "reranker_error": error,
            "reranker_fallback": "",
        }
        return output

    def _fallback(
        self,
        selected: list[dict],
        user_input: str,
        queries: list[str],
        current_time: str,
        max_candidates: int | None,
        error: str,
    ) -> list[dict]:
        fallback = CandidateScorer().rank(
            [dict(item) for item in selected],
            user_input=user_input,
            query_heads=queries,
            current_time=current_time,
        )
        limit = max_candidates or self.MAX_INPUT
        output = [
            item
            for item in fallback
            if float(item.get("_candidate_score") or 0) >= MIN_FALLBACK_SCORE
        ][:limit]
        scores = []
        for item in output:
            confidence = round(min(1.0, max(0.0, float(item.get("_candidate_score") or 0) / 100.0)), 2)
            item["rerank_confidence"] = confidence
            item["rerank_reason"] = "fallback candidate score"
            item["rerank_score"] = float(item.get("_candidate_score") or 0)
            item["answer_fragments"] = []
            if item.get("url"):
                scores.append({
                    "url": item["url"],
                    "relevant": True,
                    "confidence": confidence,
                    "score": item["rerank_score"],
                })
        if not output:
            error = "fallback filtered all candidates"
        self.last_trace = {
            "reranked_count": len(selected),
            "reranker_output_count": len(output),
            "reranker_filtered_count": len(selected) - len(output),
            "reranker_verdict_count": 0,
            "reranker_scores": scores,
            "reranker_error": error,
            "reranker_fallback": "candidate_scorer",
        }
        return output

    @staticmethod
    def _parse_verdicts(data: dict) -> dict[str, RerankVerdict]:
        items = data.get("items", [])
        verdicts = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            try:
                confidence = float(item.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            fragments = [str(x) for x in item.get("answer_fragments", []) if str(x).strip()]
            verdicts[url] = RerankVerdict(
                relevant=bool(item.get("relevant")),
                confidence=confidence,
                answer_fragments=fragments,
                reason=str(item.get("reason") or "").strip(),
            )
        return verdicts

    def _llm_json(self, system: str, user_prompt: str, timeout: float = 10) -> dict | None:
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def run():
            try:
                backend = registry.get_backend(self.backend_role)
                cfg = registry.get_config(self.backend_role)
                resp = backend.chat(
                    cfg.model_id,
                    [{"role": "user", "content": user_prompt}],
                    system_prompt=system,
                    timeout=timeout,
                )
                result_queue.put(("ok", resp))
            except Exception as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            status, value = result_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if status == "error":
            return None
        match = re.search(r"\{.*\}", value.content or "", re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
