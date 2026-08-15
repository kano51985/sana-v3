from sana.modules.discovery.official_sources import DirectSourcePolicy
from sana.modules.search_planning.domain import FactType


def test_direct_sources_are_entity_and_fact_specific() -> None:
    policy = DirectSourcePolicy()

    assert policy.urls_for("Python", FactType.VERSION) == (
        "https://www.python.org/downloads/",
    )
    assert policy.urls_for("Python", FactType.CURRENT_VALUE) == (
        "https://www.python.org/downloads/",
    )
    assert policy.urls_for("Python", FactType.TEAM_META) == ()
    assert policy.urls_for("Unrelated", FactType.VERSION) == ()


def test_canonical_product_prefix_uses_configured_vendor_sources() -> None:
    policy = DirectSourcePolicy()

    assert policy.urls_for("DeepSeek V4 Flash", FactType.BACKGROUND) == (
        "https://api-docs.deepseek.com/",
    )


def test_standards_toolchains_and_reviews_have_direct_fallbacks() -> None:
    policy = DirectSourcePolicy()

    assert policy.urls_for("HTTP 404", FactType.BACKGROUND) == (
        "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
    )
    assert policy.urls_for("HTTP 404 reason phrase", FactType.CURRENT_VALUE) == (
        "https://www.iana.org/assignments/http-status-codes/"
        "http-status-codes-1.csv",
    )
    assert policy.urls_for("Git object model", FactType.BACKGROUND) == (
        "https://www.kernel.org/pub/software/scm/git/docs/gitglossary.html",
    )
    assert policy.urls_for("Rust", FactType.VERSION) == (
        "https://doc.rust-lang.org/stable/releases.html",
    )
    assert policy.urls_for("Apex Legends", FactType.TEAM_META) == (
        "https://apexranked.com/meta",
        "https://games.gg/apex-legends/guides/"
        "apex-legends-season-29-tier-list/",
    )
    assert policy.urls_for("OpenAI", FactType.COMPARISON) == (
        "https://developers.openai.com/api/docs/models/all",
    )
    assert policy.urls_for(
        "next unreleased OpenAI model",
        FactType.CURRENT_VALUE,
    ) == ("https://developers.openai.com/api/docs/models/all",)
