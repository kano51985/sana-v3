"""HTTP-first fetch selection based on startup capability snapshot."""

from dataclasses import dataclass

from sana.modules.content.domain import FetchMode


@dataclass(frozen=True, slots=True)
class FetchCapabilities:
    http: bool = True
    katana: bool = False
    browser: bool = False


@dataclass(frozen=True, slots=True)
class FetchHints:
    requires_javascript: bool = False
    site_navigation: bool = False


@dataclass(frozen=True, slots=True)
class FetchDecision:
    mode: FetchMode | None
    reason: str


class FetchStrategy:
    def __init__(self, capabilities: FetchCapabilities) -> None:
        self._capabilities = capabilities

    def choose(self, hints: FetchHints) -> FetchDecision:
        if hints.site_navigation:
            if self._capabilities.katana:
                return FetchDecision(FetchMode.KATANA, "site_navigation")
            if self._capabilities.browser:
                return FetchDecision(FetchMode.BROWSER, "site_navigation_fallback")
            return FetchDecision(None, "site_navigation_capability_unavailable")
        if hints.requires_javascript:
            if self._capabilities.browser:
                return FetchDecision(FetchMode.BROWSER, "javascript_required")
            if self._capabilities.katana:
                return FetchDecision(FetchMode.KATANA, "javascript_fallback")
            return FetchDecision(None, "javascript_capability_unavailable")
        if self._capabilities.http:
            return FetchDecision(FetchMode.HTTP, "http_first")
        return FetchDecision(None, "http_capability_unavailable")
