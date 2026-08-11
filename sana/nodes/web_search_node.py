import queue
import re
import threading
import time
from dataclasses import asdict

from sana.config import registry
from sana.core.context import Context
from sana.core.node import NodeResult, PipelineNode
from sana.models.search_context import EntityContext, FactType, SearchIntent
from sana.services.entity_resolver import EntityResolution, EntityResolver
from sana.services.entity_context_builder import EntityContextBuilder
from sana.services.fact_intent_classifier import FactIntentClassifier
from sana.services.result_reranker import ResultReranker
from sana.services.result_verifier import ToolResultVerifier
from sana.services.retrieval_scorer import RetrievalScorer
from sana.services.web_alias_store import WebAliasStore
from sana.services.web_query_planner import WebQueryPlanner
from sana.services.web_search_service import WebSearchService
from sana.services.web_tool_config import WebToolConfigStore
from sana.services.web_tool_policy import WebToolPolicy


WEB_SEARCH_TIMEOUT_SECONDS = 30

_CONTEXT_STOPWORDS = {
    "当前", "版本", "更新", "2026", "年", "月", "这个", "那个",
    "什么", "怎么", "改成", "什么样", "啦", "听说", "很大",
    "问题", "推荐", "最强", "情况", "了吗", "吗", "了", "的",
}


class WebSearchNode(PipelineNode):
    def __init__(
        self,
        config_store: WebToolConfigStore,
        policy: WebToolPolicy,
        alias_store: WebAliasStore,
        resolver: EntityResolver,
        planner: WebQueryPlanner,
        search_service: WebSearchService,
        scorer: RetrievalScorer,
        verifier: ToolResultVerifier | None = None,
        reranker: ResultReranker | None = None,
        entity_context_builder: EntityContextBuilder | None = None,
        fact_intent_classifier: FactIntentClassifier | None = None,
    ):
        self.config_store = config_store
        self.policy = policy
        self.alias_store = alias_store
        self.resolver = resolver
        self.planner = planner
        self.search_service = search_service
        self.scorer = scorer
        self.verifier = verifier
        self.reranker = reranker
        self.entity_context_builder = entity_context_builder or EntityContextBuilder()
        self.fact_intent_classifier = fact_intent_classifier or FactIntentClassifier()

    def process(self, ctx: Context) -> NodeResult:
        config = self.config_store.load()
        ctx.web_tool_enabled = config.enabled
        ctx.web_autonomy_level = config.autonomy_level
        mood = self.policy.mood_from_ctx(ctx)
        allowed, status, reason = self.policy.evaluate(
            ctx.user_input,
            ctx.perception_data,
            mood,
            config,
        )
        ctx.tool_trace = {
            "triggered": True,
            "tool": "web",
            "status": status,
            "reason": reason,
        }

        if not allowed:
            ctx.web_error = reason
            return self._finish(ctx, EntityResolution(), status, reason)

        recent = [m.get("content", "") for m in ctx.working_memory if m.get("content")]
        resolution = self.resolver.self_check(
            ctx.user_input,
            ctx.perception_data.get("entities", []),
            recent,
        )
        if resolution.need_clarify and resolution.raw:
            clarify_heads = [
                f"{resolution.raw} 是什么意思",
                f"{resolution.raw} 简称 别名 游戏",
            ]
            clarify_results = self._search_with_timeout(clarify_heads, None, config)
            if clarify_results:
                confirmed = self.resolver.clarify_from_results(resolution, clarify_results)
                if confirmed.canonical and confirmed.confidence >= self.resolver.THRESHOLD:
                    resolution = confirmed
                    self.resolver.learn_alias(confirmed)

        pre_context = self.entity_context_builder.build(ctx.user_input, resolution, heads=[])
        search_intent = self.fact_intent_classifier.classify(ctx.user_input, pre_context)
        heads = self.planner.build_heads(
            ctx.user_input,
            resolution,
            ctx.perception_data,
            ctx.current_time,
            config.max_query_heads,
            entity_context=pre_context,
            search_intent=search_intent,
        )
        entity_context = self.entity_context_builder.build(ctx.user_input, resolution, heads)
        ctx.web_query_heads = heads
        context_terms = entity_context.context_terms
        ctx.web_context_terms = context_terms
        ctx.web_entity_context = entity_context.to_dict()
        ctx.web_search_intent = search_intent.to_dict()
        ctx.web_retry_queries = []
        raw_results = self._search_with_timeout(
            heads,
            resolution.canonical or None,
            config,
            context_terms,
            search_intent,
        )
        if raw_results is None:
            ctx.web_error = "web_search_timeout"
            return self._finish(ctx, resolution, "failed", ctx.web_error, [])
        rerank_started = time.monotonic()
        if self.reranker is not None:
            raw_results = self.reranker.rerank(
                ctx.user_input,
                heads,
                raw_results,
                ctx.current_time,
                timeout=config.rerank_timeout_seconds,
            )
            if not raw_results and self.reranker.last_trace.get("reranker_error") == "reranker filtered all candidates":
                if ctx.web_retry_count < 1:
                    ctx.web_retry_count += 1
                    retry_query = _disambiguation_query(heads, context_terms, resolution)
                    ctx.web_retry_queries.append(retry_query)
                    raw_retry = self._search_with_timeout(
                        [retry_query],
                        resolution.canonical or None,
                        config,
                        context_terms,
                        search_intent,
                    )
                    if raw_retry:
                        raw_retry = self.reranker.rerank(
                            ctx.user_input,
                            [retry_query],
                            raw_retry,
                            ctx.current_time,
                            timeout=config.rerank_timeout_seconds,
                        )
                        raw_results = raw_retry
        ctx.tool_trace["rerank_elapsed_ms"] = int((time.monotonic() - rerank_started) * 1000)
        canonical_terms = ([resolution.canonical] + resolution.aliases) if resolution.canonical else []
        results = self.scorer.merge(
            raw_results,
            config.max_injected_results,
            canonical_terms,
            ctx.current_time,
        )
        ctx.web_verification = {}
        if results and self.verifier is not None:
            verification = self.verifier.verify(
                ctx.user_input,
                heads,
                results,
                ctx.current_time,
            )
            ctx.web_verification = asdict(verification)
            if verification.should_retry() and ctx.web_retry_count < 1:
                retry_query = verification.suggested_retry_query or (heads[0] if heads else "")
                if retry_query:
                    ctx.web_retry_count += 1
                    ctx.web_retry_queries.append(retry_query)
                    raw_retry = self._search_with_timeout(
                        [retry_query],
                        resolution.canonical or None,
                        config,
                        context_terms,
                        search_intent,
                    )
                    if raw_retry is not None:
                        retry_results = self.scorer.merge(
                            raw_retry,
                            config.max_injected_results,
                            canonical_terms,
                            ctx.current_time,
                        )
                        if retry_results:
                            results = self.scorer.merge(
                                results + retry_results,
                                config.max_injected_results,
                                canonical_terms,
                                ctx.current_time,
                            )

        ctx.web_query_heads = heads
        ctx.web_results = results
        ctx.web_entity = asdict(resolution)
        if not results:
            ctx.web_error = "no_results"
            status = "failed"
        else:
            status = "executed"
        return self._finish(ctx, resolution, status, ctx.web_error, results)

    def _search_with_timeout(
        self,
        heads: list[str],
        direct_canonical: str | None,
        config,
        context_terms: list[str] | None = None,
        search_intent: SearchIntent | None = None,
    ) -> list[dict] | None:
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def run():
            try:
                result_queue.put(
                    (
                        "ok",
                        self.search_service.search(
                            heads,
                            direct_canonical=direct_canonical,
                            config=config,
                            context_terms=context_terms,
                            search_intent=search_intent,
                        ),
                    )
                )
            except Exception as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            timeout = float(
                getattr(config, "web_total_timeout_seconds", WEB_SEARCH_TIMEOUT_SECONDS)
                or WEB_SEARCH_TIMEOUT_SECONDS
            )
            status, value = result_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if status == "error":
            raise value
        return value

    def _finish(
        self,
        ctx: Context,
        resolution: EntityResolution,
        status: str,
        error: str,
        results: list[dict] | None = None,
    ) -> NodeResult:
        results = results or []
        config = self.config_store.load()
        heads = ctx.web_query_heads
        parts = []
        if status == "blocked":
            parts.append("[Web Tool Blocked]")
        else:
            parts.append("[Web Search Results]")
            if resolution.canonical:
                parts.append(
                    f"实体解析: {resolution.raw or ctx.user_input} -> {resolution.canonical} "
                    f"(confidence {resolution.confidence:.2f})"
                )
            if heads:
                parts.append("Query heads: " + " | ".join(heads))
            entity_context = getattr(ctx, "web_entity_context", {})
            if entity_context:
                parts.append(f"Entity: {entity_context.get('canonical', '')} "
                             f"domain={entity_context.get('domain', '')} "
                             f"kind={entity_context.get('entity_kind', '')} "
                             f"context={entity_context.get('context_terms', [])}")
            search_intent = getattr(ctx, "web_search_intent", {})
            if search_intent:
                parts.append(f"Fact intent: {search_intent.get('fact_types', [])} "
                             f"required_pages={search_intent.get('required_page_types', [])}")
            if results:
                for item in results[:config.max_injected_results]:
                    snippet = (item.get("snippet") or "")[:200]
                    parts.append(
                        f"{item.get('rank', 0)}. {item.get('title', '')}\n"
                        f"   URL: {item.get('url', '')}\n"
                        f"   摘要: {snippet}"
                    )
            else:
                parts.append("没有抓到可用结果。")
            if ctx.web_verification:
                parts.append("[Verification]")
                parts.append(f"Confidence: {ctx.web_verification.get('confidence', 0)}")
                missing = ctx.web_verification.get("missing_facts", [])
                if missing:
                    parts.append("Missing facts: " + ", ".join(str(x) for x in missing))
            parts.append("[Grounding]")
            parts.append("- 对事实/时效问题，只能基于上面的搜索结果回答。")
            parts.append("- 如果结果没有可靠答案，明确说“没查到可靠信息”，不要用训练记忆补版本号。")
        if error:
            parts.append(f"错误: {error}")
        ctx.augmented_input += "\n\n" + "\n".join(parts)

        try:
            backend = registry.get_backend("chat")
            chat_config = registry.get_config("chat")
            resp = backend.chat(
                chat_config.model_id,
                [
                    {"role": "system", "content": ctx.system_prompt},
                    {"role": "user", "content": ctx.augmented_input},
                ],
                system_prompt=ctx.system_prompt,
                timeout=30,
            )
            ctx.llm_raw_response = resp.content
        except Exception as exc:
            error = str(exc)
            ctx.web_error = error
            status = "failed"
            ctx.llm_raw_response = (
                "<thinking>联网查询执行失败</thinking>\n"
                "<chat>联网查询失败了，这次没查到可靠结果。</chat>"
            )

        ctx.tool_trace.update({
            "status": status,
            "query_heads": heads,
            "results_count": len(results),
            "entity": asdict(resolution),
            "error": error,
            "retrieval_confidence": ctx.web_verification.get("confidence", 0),
            "missing_facts": ctx.web_verification.get("missing_facts", []),
            "retry_count": ctx.web_retry_count,
        })
        search_trace = getattr(self.search_service, "last_trace", {})
        ctx.tool_trace.update({
            "phase": search_trace.get("phase", ""),
            "context_terms": search_trace.get("context_terms", []),
            "entity_context": getattr(ctx, "web_entity_context", {}),
            "search_intent": getattr(ctx, "web_search_intent", {}),
            "retry_queries": getattr(ctx, "web_retry_queries", []),
            "provider_timeout": search_trace.get("provider_timeout", False),
            "provider_elapsed_ms": search_trace.get("provider_elapsed_ms", 0),
            "crawl_elapsed_ms": search_trace.get("crawl_elapsed_ms", 0),
            "provider_sources": search_trace.get(
                "provider_run_sources",
                search_trace.get("provider_sources", []),
            ),
            "provider_count": search_trace.get(
                "provider_run_count",
                search_trace.get("provider_count", 0),
            ),
            "provider_errors": search_trace.get(
                "provider_run_errors",
                search_trace.get("provider_errors", {}),
            ),
            "provider_run_sources": search_trace.get("provider_run_sources", []),
            "provider_run_count": search_trace.get("provider_run_count", 0),
            "provider_run_errors": search_trace.get("provider_run_errors", {}),
            "provider_success_sources": search_trace.get("provider_success_sources", []),
            "provider_success_count": search_trace.get("provider_success_count", 0),
            "provider_ok_sources": search_trace.get("provider_ok_sources", []),
            "provider_ok_count": search_trace.get("provider_ok_count", 0),
            "provider_result_count": search_trace.get("provider_result_count", 0),
            "official_sources": search_trace.get("official_sources", []),
            "discovery_sources": search_trace.get(
                "provider_run_sources",
                search_trace.get("provider_sources", search_trace.get("discovery_sources", [])),
            ),
            "discovery_count": search_trace.get("discovery_count", 0),
            "article_count": search_trace.get("article_count", 0),
            "filtered_nav_count": search_trace.get("filtered_nav_count", 0),
            "crawl_tasks": search_trace.get("crawl_tasks", []),
            "crawl_sources": search_trace.get("crawl_sources", []),
            "katana_visited_urls": search_trace.get("katana_visited_urls", []),
            "katana_records": search_trace.get("katana_records", 0),
            "katana_rounds": search_trace.get("katana_rounds", 0),
            "katana_skipped_slow_hosts": search_trace.get("katana_skipped_slow_hosts", []),
            "katana_total_timeout_seconds": search_trace.get("katana_total_timeout_seconds", 0),
            "katana_available": search_trace.get("katana_available", False),
            "http_fallback_count": search_trace.get("http_fallback_count", 0),
            "crawl_error": search_trace.get("katana_error", ""),
        })
        rerank_trace = getattr(self.reranker, "last_trace", {}) if self.reranker else {}
        ctx.tool_trace.update({
            "reranked_count": rerank_trace.get("reranked_count", 0),
            "reranker_output_count": rerank_trace.get("reranker_output_count", 0),
            "reranker_filtered_count": rerank_trace.get("reranker_filtered_count", 0),
            "reranker_verdict_count": rerank_trace.get("reranker_verdict_count", 0),
            "reranker_scores": rerank_trace.get("reranker_scores", []),
            "reranker_error": rerank_trace.get("reranker_error", ""),
            "reranker_fallback": rerank_trace.get("reranker_fallback", ""),
        })
        return NodeResult(next="format_check", context=ctx)


def _context_terms_for(
    user_input: str,
    heads: list[str],
    resolution: EntityResolution,
) -> list[str]:
    terms = [str(alias).strip() for alias in (resolution.aliases or []) if str(alias).strip()]
    excluded = {
        str(resolution.canonical or "").lower(),
        *(str(alias).lower() for alias in (resolution.aliases or []) if str(alias).strip()),
    }
    for value in [user_input, *heads]:
        for part in re.split(r"[\s,，。；;、/]+", str(value or "")):
            part = part.strip().lower()
            if (
                part
                and len(part) > 1
                and part not in excluded
                and part not in _CONTEXT_STOPWORDS
            ):
                terms.append(part)
    return list(dict.fromkeys(terms))[:10]


def _disambiguation_query(
    heads: list[str],
    context_terms: list[str],
    resolution: EntityResolution,
) -> str:
    base = resolution.canonical or (heads[0] if heads else "")
    alias_values = {
        str(alias).lower()
        for alias in (resolution.aliases or [])
        if str(alias).strip()
    }
    extra_terms = [
        term for term in (context_terms or [])
        if term.lower() not in alias_values
    ] or list(context_terms or [])
    extra = " ".join(extra_terms[:3])
    return f"{base} {extra} 补丁说明 patch notes".strip()
