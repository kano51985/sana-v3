from sana.modules.discovery.official_sources import OfficialSourcePolicy
from sana.modules.search_planning.domain import FactType


def test_official_sources_are_entity_and_fact_specific() -> None:
    policy = OfficialSourcePolicy()

    assert policy.urls_for("Python", FactType.VERSION) == (
        "https://www.python.org/downloads/",
    )
    assert policy.urls_for("Python", FactType.CURRENT_VALUE) == (
        "https://www.python.org/downloads/",
    )
    assert policy.urls_for("Python", FactType.TEAM_META) == ()
    assert policy.urls_for("Unrelated", FactType.VERSION) == ()


def test_canonical_product_prefix_uses_configured_vendor_sources() -> None:
    policy = OfficialSourcePolicy()

    assert policy.urls_for("DeepSeek V4 Flash", FactType.BACKGROUND) == (
        "https://api-docs.deepseek.com/",
    )


def test_standards_and_toolchains_have_deterministic_official_fallbacks() -> None:
    policy = OfficialSourcePolicy()

    assert policy.urls_for("HTTP 404", FactType.BACKGROUND) == (
        "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
    )
    assert policy.urls_for("Git object model", FactType.BACKGROUND) == (
        "https://git-scm.com/book/en/v2/Git-Internals-Git-Objects",
    )
    assert policy.urls_for("Rust", FactType.VERSION) == (
        "https://doc.rust-lang.org/stable/releases.html",
    )
    assert policy.urls_for("OpenAI", FactType.COMPARISON) == (
        "https://openai.com/",
        "https://openai.com/news/",
    )
