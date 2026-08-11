from concurrent.futures import ThreadPoolExecutor, as_completed

from sana.models.search import SearchResult
from sana.services.search_provider import (
    BaiduProvider,
    BingProvider,
    BingRssProvider,
    DirectSourceProvider,
    DuckDuckGoProvider,
    SearXNGProvider,
    SearchProvider,
)
from sana.services.web_tool_config import WebToolConfig


class SearchProviderRegistry:
    def __init__(
        self,
        providers: list[SearchProvider] | None = None,
        direct_provider: DirectSourceProvider | None = None,
    ):
        self.providers = providers if providers is not None else [
            BingRssProvider(),
            DuckDuckGoProvider(),
            BingProvider(),
            BaiduProvider(),
            SearXNGProvider(),
        ]
        self.direct_provider = direct_provider or DirectSourceProvider()
        self.unavailable_sources: set[str] = set()
        self.cumulative_sources: set[str] = set()
        self.cumulative_errors: dict[str, str] = {}
        self.last_trace: dict = {}

    def reset_run_state(self) -> None:
        self.unavailable_sources = set()
        self.cumulative_sources = set()
        self.cumulative_errors = {}
        self.last_trace = {}

    def search(
        self,
        query: str,
        config: WebToolConfig,
        canonical: str | None = None,
    ) -> list[SearchResult]:
        enabled = self._enabled_providers(config, canonical)
        enabled_names = {provider.name for provider in enabled}
        self.cumulative_sources.update(enabled_names)
        results: list[SearchResult] = []
        errors: dict[str, str] = {}
        success_sources: set[str] = set()
        ok_sources: set[str] = set()
        if not enabled:
            self.last_trace = self._snapshot_trace(results, enabled_names, errors, success_sources, ok_sources)
            return results

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(enabled)))) as executor:
            futures = {
                executor.submit(provider.search, query, config, canonical): provider
                for provider in enabled
            }
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    provider_results = future.result()
                    results.extend(provider_results)
                    success_sources.add(provider.name)
                    if provider_results:
                        ok_sources.add(provider.name)
                except Exception as exc:
                    errors[provider.name] = str(exc)
                    self.cumulative_errors[provider.name] = str(exc)
                    self.unavailable_sources.add(provider.name)

        self.cumulative_errors.update(errors)
        self.last_trace = self._snapshot_trace(results, enabled_names, errors, success_sources, ok_sources)
        return results

    def _snapshot_trace(
        self,
        results: list[SearchResult],
        enabled_names: set[str],
        errors: dict[str, str],
        success_sources: set[str],
        ok_sources: set[str],
    ) -> dict:
        return {
            "provider_sources": sorted(enabled_names),
            "provider_count": len(enabled_names),
            "provider_errors": dict(errors),
            "provider_run_sources": sorted(enabled_names),
            "provider_run_count": len(enabled_names),
            "provider_run_errors": dict(errors),
            "provider_success_sources": sorted(success_sources),
            "provider_success_count": len(success_sources),
            "provider_ok_sources": sorted(ok_sources),
            "provider_ok_count": len(ok_sources),
            "provider_result_count": len(results),
            "cumulative_provider_sources": sorted(self.cumulative_sources),
            "cumulative_provider_errors": dict(self.cumulative_errors),
        }

    def _enabled_providers(
        self,
        config: WebToolConfig,
        canonical: str | None,
    ) -> list[SearchProvider]:
        candidates = list(self.providers)
        if canonical:
            candidates.append(self.direct_provider)
        enabled = []
        for provider in candidates:
            if provider.name in self.unavailable_sources:
                continue
            try:
                if provider.enabled(config):
                    enabled.append(provider)
            except Exception:
                continue
        return enabled
