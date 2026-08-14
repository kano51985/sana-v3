from sana.modules.content.domain import FetchMode
from sana.modules.content.fetch_strategy import (
    FetchCapabilities,
    FetchHints,
    FetchStrategy,
)
from sana.platform.fetch.katana_fetcher import KatanaFetcher
from sana.platform.security.ssrf import SSRFGuard


def test_http_is_the_default_content_fetch_strategy() -> None:
    decision = FetchStrategy(
        FetchCapabilities(http=True, katana=True, browser=True)
    ).choose(FetchHints())

    assert decision.mode is FetchMode.HTTP
    assert decision.reason == "http_first"


def test_navigation_capability_is_not_selected_when_unavailable() -> None:
    decision = FetchStrategy(FetchCapabilities()).choose(
        FetchHints(site_navigation=True)
    )

    assert decision.mode is None
    assert decision.reason == "site_navigation_capability_unavailable"


async def test_missing_katana_binary_is_reported_as_unavailable() -> None:
    fetcher = KatanaFetcher(SSRFGuard(), which=lambda _: None)

    status = await fetcher.probe()

    assert status.name == "katana"
    assert status.available is False
    assert status.detail == "executable_not_found"
