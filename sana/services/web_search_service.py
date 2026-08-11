import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from urllib.parse import urlsplit, urlunsplit

import requests

from sana.models.search_context import SearchIntent
from sana.services.candidate_classifier import CandidateClassifier
from sana.services.candidate_scorer import CandidateScorer
from sana.services.content_extractor import ContentExtractor
from sana.services.crawl_planner import CrawlPlanner
from sana.services.katana_crawler import KatanaCrawler
from sana.services.official_source_learner import OfficialSourceLearner
from sana.services.search_parsers import BaiduParser, BingParser
from sana.services.search_provider import DirectSourceProvider
from sana.services.search_provider_registry import SearchProviderRegistry
from sana.services.web_tool_config import WebToolConfig, WebToolConfigStore


LINK_INTENT_TERMS = (
    "版本", "更新", "角色", "公告", "新闻", "资讯",
    "攻略", "配队", "爆料", "最新",
    "version", "update", "character", "news", "guide", "announcement", "leak",
)


class WebSearchService:
    def __init__(
        self,
        config_store: WebToolConfigStore | None = None,
        config: WebToolConfig | None = None,
        provider_registry: SearchProviderRegistry | None = None,
        official_learner: OfficialSourceLearner | None = None,
    ):
        self.config_store = config_store or WebToolConfigStore()
        self._config = config
        self.official_learner = official_learner or OfficialSourceLearner()
        self.session = requests.Session()
        self.provider_registry = provider_registry or SearchProviderRegistry(
            direct_provider=DirectSourceProvider(registry=self.official_learner)
        )
        self.classifier = CandidateClassifier()
        self.candidate_scorer = CandidateScorer()
        self.crawl_planner = CrawlPlanner()
        self.crawler = KatanaCrawler()
        self.content_extractor = ContentExtractor()
        self.last_trace: dict = {}
        self.last_official_urls: list[str] = []
        self.last_scored_candidates: list[dict] = []
        self.filtered_nav_count = 0

    def search(
        self,
        heads: list[str],
        direct_canonical: str | None = None,
        config: WebToolConfig | None = None,
        context_terms: list[str] | None = None,
        search_intent: SearchIntent | None = None,
    ) -> list[dict]:
        cfg = config or self._config or self.config_store.load()
        heads = [h for h in heads if h][:max(1, cfg.max_query_heads)]
        if not heads:
            return []

        self.filtered_nav_count = 0
        self.last_official_urls = []
        self.last_scored_candidates = []
        self.last_context_terms = context_terms or []
        self.last_search_intent = search_intent.to_dict() if search_intent else None
        self.last_trace = {"phase": "providers"}
        self.provider_registry.reset_run_state()
        provider_results = self._collect(heads, cfg, direct_canonical)
        provider_timeout = bool(self.last_trace.get("provider_timeout"))
        provider_elapsed_ms = int(self.last_trace.get("provider_elapsed_ms", 0))
        self.last_trace = {
            **self.provider_registry.last_trace,
            "phase": "providers",
            "discovery_sources": (
                self.provider_registry.last_trace.get("provider_run_sources")
                or self.provider_registry.last_trace.get("provider_sources", [])
            ),
            "discovery_count": len(provider_results),
            "provider_timeout": provider_timeout,
            "provider_elapsed_ms": provider_elapsed_ms,
        }
        crawl_start = time.monotonic()
        try:
            crawl_results = self._crawl(
                provider_results,
                direct_canonical,
                heads,
                cfg,
                context_terms or [],
                search_intent,
            )
        except Exception as exc:
            self.last_trace["phase"] = "crawl"
            self.last_trace["crawl_error"] = str(exc)
            crawl_results = []
        self.last_trace["crawl_elapsed_ms"] = int((time.monotonic() - crawl_start) * 1000)
        provider_for_output = self.last_scored_candidates or provider_results
        results = self._dedupe_results(provider_for_output + crawl_results)

        self.last_trace.update({
            "phase": "done",
            "discovery_count": len(provider_results),
            "context_terms": self.last_context_terms,
            "search_intent": self.last_search_intent,
            "article_count": sum(1 for item in results if item.get("_url_kind") == "article"),
            "filtered_nav_count": self.filtered_nav_count,
            "official_sources": self.last_official_urls,
            "crawl_tasks": self.crawler.last_trace.get("crawl_tasks", []),
            "crawl_sources": self.crawler.last_trace.get("crawl_sources", []),
            "katana_visited_urls": self.crawler.last_trace.get("katana_visited_urls", []),
            "katana_records": self.crawler.last_trace.get("katana_records", 0),
            "katana_rounds": self.crawler.last_trace.get("katana_rounds", 0),
            "katana_skipped_slow_hosts": self.crawler.last_trace.get("katana_skipped_slow_hosts", []),
            "katana_total_timeout_seconds": self.crawler.last_trace.get("katana_total_timeout_seconds", cfg.katana_total_timeout_seconds),
            "katana_available": self.crawler.last_trace.get("katana_available", False),
            "http_fallback_count": self.crawler.last_trace.get("http_fallback_count", 0),
            "katana_error": self.crawler.last_trace.get("katana_error", ""),
            "crawl_error": self.crawler.last_trace.get("katana_error", self.last_trace.get("crawl_error", "")),
        })
        return results

    def _collect(
        self,
        heads: list[str],
        cfg: WebToolConfig,
        canonical: str | None,
    ) -> list[dict]:
        results: list[dict] = []
        traces: list[dict] = []
        if not heads:
            return results
        start = time.monotonic()
        timed_out = False
        executor = ThreadPoolExecutor(max_workers=min(3, len(heads)))
        futures = {
            executor.submit(self._search_head, head, cfg, canonical): head
            for head in heads
        }
        try:
            for future in as_completed(futures, timeout=cfg.provider_timeout_seconds):
                try:
                    found, trace = future.result()
                    results.extend(found)
                    if trace:
                        traces.append(trace)
                except Exception:
                    continue
        except TimeoutError:
            timed_out = True
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)
        self.last_trace["provider_elapsed_ms"] = int((time.monotonic() - start) * 1000)
        if timed_out:
            self.last_trace["provider_timeout"] = True
        if traces:
            self.provider_registry.last_trace = self._merge_provider_traces(traces, results)
        return results

    def _search_head(
        self,
        head: str,
        cfg: WebToolConfig,
        canonical: str | None,
    ) -> tuple[list[dict], dict]:
        found = self.provider_registry.search(head, cfg, canonical)
        trace = dict(getattr(self.provider_registry, "last_trace", {}) or {})
        return [asdict(item) for item in found], trace

    def _merge_provider_traces(
        self,
        traces: list[dict],
        results: list[dict],
    ) -> dict:
        sources: set[str] = set()
        errors: dict[str, str] = {}
        success_sources: set[str] = set()
        ok_sources: set[str] = set()
        result_count = 0

        for trace in traces:
            sources.update(
                trace.get("provider_run_sources") or trace.get("provider_sources", [])
            )
            errors.update(
                trace.get("provider_run_errors") or trace.get("provider_errors", {})
            )
            success_sources.update(trace.get("provider_success_sources", []))
            ok_sources.update(trace.get("provider_ok_sources", []))
            result_count += int(trace.get("provider_result_count") or 0)

        if not sources:
            previous = getattr(self.provider_registry, "last_trace", {}) or {}
            sources.update(previous.get("provider_sources", []))
            errors.update(previous.get("provider_errors", {}))
        if not result_count:
            result_count = len(results)

        return {
            "provider_sources": sorted(sources),
            "provider_count": len(sources),
            "provider_errors": errors,
            "provider_run_sources": sorted(sources),
            "provider_run_count": len(sources),
            "provider_run_errors": errors,
            "provider_success_sources": sorted(success_sources),
            "provider_success_count": len(success_sources),
            "provider_ok_sources": sorted(ok_sources),
            "provider_ok_count": len(ok_sources),
            "provider_result_count": result_count,
        }

    def _crawl(
        self,
        candidates: list[dict],
        direct_canonical: str | None,
        heads: list[str],
        cfg: WebToolConfig,
        context_terms: list[str] | None = None,
        search_intent: SearchIntent | None = None,
    ) -> list[dict]:
        if not cfg.allow_katana:
            self.crawler.last_trace = {}
            return []

        entity_terms = list(
            dict.fromkeys(
                term
                for term in [direct_canonical] + (context_terms or [])
                if term
            )
        )
        question = " ".join(heads)
        classified = self.classifier.classify_many(candidates, entity_terms, question)
        recognized_official = []
        official_urls = []
        if direct_canonical:
            recognized_official = self.official_learner.recognize_from_candidates(
                candidates,
                direct_canonical,
                context_terms=context_terms or [],
            )
            official_urls = list(
                dict.fromkeys(
                    recognized_official
                    + self.official_learner.validate_learned(
                        direct_canonical,
                        context_terms or [],
                    )
                )
            )
            if not self.official_learner.last_judge_trace.get("fallback"):
                self.official_learner.learn(direct_canonical, recognized_official)
        self.last_official_urls = official_urls

        ranked = self.candidate_scorer.rank(
            classified,
            user_input=heads[0] if heads else "",
            query_heads=heads,
            entity_terms=entity_terms,
            current_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            official_domains={_host(url) for url in official_urls},
            context_terms=context_terms or [],
            required_page_types=search_intent.required_page_types if search_intent else [],
        )
        self.last_scored_candidates = ranked
        tasks = self.crawl_planner.plan(
            ranked,
            official_urls=official_urls,
            max_tasks=cfg.katana_max_pages,
            query_heads=heads,
            fact_types=search_intent.fact_types if search_intent else [],
        )
        self.filtered_nav_count = sum(
            1 for item in ranked if item.get("_url_kind") == "site_homepage"
        )
        all_tasks = list(tasks)
        first_task_keys = {_normalize_url(task.url) for task in tasks}
        visited_urls: list[str] = []
        record_count = 0
        crawl_deadline = time.monotonic() + float(cfg.katana_total_timeout_seconds)

        records = self.crawler.crawl(tasks, cfg, deadline=crawl_deadline)
        visited_urls.extend(record.get("url", "") for record in records if record.get("url"))
        record_count += len(records)
        items = self.content_extractor.extract_many(records)
        keywords = _link_keywords(heads, entity_terms)
        if not records and (official_urls or tasks):
            fetch_urls = [task.url for task in tasks]
            fetch_urls.extend(official_urls)
            http_records = self._fetch_pages_via_http(fetch_urls, cfg)
            self.crawler.last_trace["http_fallback_count"] = len(http_records)
            relevant_http = [
                record
                for record in http_records
                if self._http_record_context_match(record, entity_terms)
            ]
            if relevant_http:
                items.extend(self.content_extractor.extract_many(relevant_http))
                discovered_links = self.crawler.extract_relevant_links(
                    relevant_http,
                    keywords,
                    cfg,
                )
                if discovered_links:
                    article_records = self._fetch_pages_via_http(
                        [link.get("url") for link in discovered_links[:5]],
                        cfg,
                    )
                    relevant_article = [
                        record
                        for record in article_records
                        if self._http_record_context_match(record, entity_terms)
                    ]
                    items.extend(self.content_extractor.extract_many(relevant_article))

        scored_links: list[dict] = []
        discovered_links = self.crawler.extract_relevant_links(records, keywords, cfg)
        if discovered_links and len(all_tasks) < cfg.katana_max_pages:
            classified_links = self.classifier.classify_many(discovered_links, entity_terms, question)
            scored_links = self.candidate_scorer.rank(
                classified_links,
                user_input=heads[0] if heads else "",
                query_heads=heads,
                entity_terms=entity_terms,
                current_time=time.strftime("%Y-%m-%d %H:%M:%S"),
                official_domains={_host(url) for url in official_urls},
                context_terms=context_terms or [],
                required_page_types=search_intent.required_page_types if search_intent else [],
            )
            for link in scored_links:
                if float(link.get("_candidate_score") or 0) < CrawlPlanner.MIN_SCORE:
                    link["_candidate_score"] = CrawlPlanner.MIN_SCORE + 5
                    link["_snippet_score"] = CrawlPlanner.MIN_SCORE + 5
            remaining = max(0, cfg.katana_max_pages - len(all_tasks))
            second_tasks = self.crawl_planner.plan(
                scored_links,
                official_urls=[],
                max_tasks=remaining,
                query_heads=heads,
                fact_types=search_intent.fact_types if search_intent else [],
            )
            second_tasks = [
                task for task in second_tasks
                if _normalize_url(task.url) not in first_task_keys
            ]
            if second_tasks:
                records2 = self.crawler.crawl(second_tasks, cfg, deadline=crawl_deadline)
                visited_urls.extend(record.get("url", "") for record in records2 if record.get("url"))
                record_count += len(records2)
                items.extend(self.content_extractor.extract_many(records2))
                all_tasks.extend(second_tasks)

        second_urls = {task.url for task in all_tasks[len(tasks):]}
        scored_for_output = [
            item for item in scored_links if item.get("url") in second_urls
        ]
        self.last_scored_candidates = ranked + scored_for_output
        self.crawler.last_trace["crawl_tasks"] = [task.to_dict() for task in all_tasks]
        self.crawler.last_trace["crawl_sources"] = [task.url for task in all_tasks]
        self.crawler.last_trace["katana_visited_urls"] = list(dict.fromkeys(visited_urls))
        self.crawler.last_trace["katana_records"] = record_count
        self.crawler.last_trace["katana_rounds"] = 2 if len(all_tasks) > len(tasks) else 1

        head = heads[0] if heads else ""
        for item in items:
            item["query_head"] = head
            verdict = self.classifier.classify(item, entity_terms, question)
            item["_url_kind"] = verdict.url_kind
            item["_relevance"] = verdict.relevance
            item["_entity_match"] = verdict.entity_match
            item["_classify_reason"] = verdict.reason
        return items

    def _fetch_pages_via_http(
        self,
        urls: list[str],
        cfg: WebToolConfig,
    ) -> list[dict]:
        records = []
        for url in list(dict.fromkeys(str(item) for item in (urls or []) if str(item)))[:5]:
            try:
                resp = self.session.get(
                    url,
                    timeout=max(10.0, cfg.timeout_seconds + 5),
                )
                if resp.status_code != 200:
                    continue
                records.append({
                    "url": url,
                    "html": resp.text,
                    "source": "http_fallback",
                })
            except Exception:
                continue
        return records

    @staticmethod
    def _http_record_context_match(record: dict, entity_terms: list[str]) -> bool:
        terms = [str(term).strip().lower() for term in entity_terms if str(term).strip()]
        if not terms:
            return False
        text = " ".join([
            str(record.get("title") or ""),
            str(record.get("snippet") or ""),
            str(record.get("url") or ""),
            (str(record.get("html") or "")[:2000]),
        ]).lower()
        hits = sum(1 for term in terms if term in text)
        return hits >= 2

    @staticmethod
    def _dedupe_results(results: list[dict]) -> list[dict]:
        best: dict[str, dict] = {}
        for item in results:
            key = _normalize_url(item.get("url", ""))
            if not key:
                key = str(item.get("title", "") or "").strip().lower()
            if not key:
                continue
            score = float(
                item.get("_candidate_score")
                or item.get("_snippet_score")
                or item.get("_score")
                or 0
            )
            if item.get("text"):
                score += 20.0
            current = best.get(key)
            current_score = 0.0
            if current:
                current_score = float(
                    current.get("_candidate_score")
                    or current.get("_snippet_score")
                    or current.get("_score")
                    or 0
                )
            if current is None or score > current_score:
                best[key] = item
        return list(best.values())


def _host(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def _normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        query = "&".join(sorted(parts.query.split("&"))) if parts.query else ""
        return urlunsplit((parts.scheme.lower(), host, (parts.path or "").rstrip("/"), query, ""))
    except ValueError:
        return (url or "").strip().lower()


def _link_keywords(heads: list[str], entity_terms: list[str]) -> list[str]:
    excluded = {str(term).lower() for term in entity_terms if str(term).strip()}
    terms = []
    for value in heads:
        for part in re.split(r"[\s,，。；;、/]+", str(value or "")):
            part = part.strip().lower()
            if part and len(part) > 1 and part not in excluded:
                terms.append(part)
    return list(dict.fromkeys(terms + list(LINK_INTENT_TERMS)))
