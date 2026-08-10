from dataclasses import asdict

from sana.config import registry
from sana.core.context import Context
from sana.core.node import NodeResult, PipelineNode
from sana.services.entity_resolver import EntityResolution, EntityResolver
from sana.services.result_verifier import ToolResultVerifier
from sana.services.retrieval_scorer import RetrievalScorer
from sana.services.web_alias_store import WebAliasStore
from sana.services.web_query_planner import WebQueryPlanner
from sana.services.web_search_service import WebSearchService
from sana.services.web_tool_config import WebToolConfigStore
from sana.services.web_tool_policy import WebToolPolicy


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
    ):
        self.config_store = config_store
        self.policy = policy
        self.alias_store = alias_store
        self.resolver = resolver
        self.planner = planner
        self.search_service = search_service
        self.scorer = scorer
        self.verifier = verifier

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
            clarify_results = self.search_service.search(clarify_heads, config=config)
            if clarify_results:
                confirmed = self.resolver.clarify_from_results(resolution, clarify_results)
                if confirmed.canonical and confirmed.confidence >= self.resolver.THRESHOLD:
                    resolution = confirmed
                    self.resolver.learn_alias(confirmed)

        heads = self.planner.build_heads(
            ctx.user_input,
            resolution,
            ctx.perception_data,
            ctx.current_time,
            config.max_query_heads,
        )
        raw_results = self.search_service.search(
            heads,
            direct_canonical=resolution.canonical or None,
            config=config,
        )
        canonical_terms = ([resolution.canonical] + resolution.aliases) if resolution.canonical else []
        results = self.scorer.merge(
            raw_results,
            config.max_injected_results,
            canonical_terms,
            ctx.current_time,
        )
        ctx.web_retry_count = 0
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
                    raw_retry = self.search_service.search(
                        [retry_query],
                        direct_canonical=resolution.canonical or None,
                        config=config,
                    )
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
            "discovery_sources": search_trace.get("discovery_sources", []),
            "crawl_sources": search_trace.get("crawl_sources", []),
            "katana_available": search_trace.get("katana_available", False),
            "crawl_error": search_trace.get("katana_error", ""),
        })
        return NodeResult(next="format_check", context=ctx)
