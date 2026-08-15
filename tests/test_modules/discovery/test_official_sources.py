from sana.modules.discovery.official_sources import DirectSourcePolicy
from sana.modules.search_planning.domain import FactRequirement, FactType


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
        "https://api-docs.deepseek.com/quick_start/pricing/",
    )


def test_standards_toolchains_and_reviews_have_direct_fallbacks() -> None:
    policy = DirectSourcePolicy()

    assert policy.urls_for("HTTP 404", FactType.BACKGROUND) == (
        "https://www.rfc-editor.org/rfc/rfc9110.html",
    )
    assert policy.urls_for("HTTP 404 reason phrase", FactType.CURRENT_VALUE) == (
        "https://www.iana.org/assignments/http-status-codes/"
        "http-status-codes-1.csv",
        "https://www.rfc-editor.org/rfc/rfc9110.html#name-404-not-found",
    )
    assert policy.urls_for("Git object model", FactType.BACKGROUND) == (
        "https://git-scm.com/docs/gitdatamodel.html",
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


def test_fact_semantics_select_reviewed_primary_pages() -> None:
    policy = DirectSourcePolicy()

    python_support = FactRequirement(
        "python_security_support",
        FactType.CURRENT_VALUE,
        "Python security support and release date",
        "Python",
    )
    python_maintenance = FactRequirement(
        "python_latest_maintenance_version",
        FactType.CURRENT_VALUE,
        "Latest Python maintenance version",
        "Python",
    )
    git_states = FactRequirement(
        "git_file_states",
        FactType.BACKGROUND,
        "Git working tree, staging area, and file states",
        "Git",
    )
    http_get = FactRequirement(
        "get_safe_idempotent",
        FactType.BACKGROUND,
        "Whether HTTP GET is both safe and idempotent",
        "HTTP GET method",
    )
    http_get_safe = FactRequirement(
        "get_safe",
        FactType.BACKGROUND,
        "Whether HTTP GET is safe according to HTTP semantics",
        "HTTP GET method",
    )
    http_get_idempotent = FactRequirement(
        "get_idempotent",
        FactType.BACKGROUND,
        "Whether HTTP GET is idempotent according to HTTP semantics",
        "HTTP GET method",
    )
    dns_transport = FactRequirement(
        "dns_transport_protocols",
        FactType.CURRENT_VALUE,
        "DNS uses specifically TCP and UDP",
        "DNS",
    )
    dns_registry = FactRequirement(
        "dns_registry_authority",
        FactType.BACKGROUND,
        "The authoritative DNS port registry",
        "DNS",
    )
    json_media_type = FactRequirement(
        "json_media_type",
        FactType.CURRENT_VALUE,
        "The registered media type for JSON",
        "JSON media type",
    )
    sha_size = FactRequirement(
        "sha256_digest_length_bits",
        FactType.CURRENT_VALUE,
        "SHA-256 digest length in bits",
        "SHA-256",
    )
    rfc3339_offset = FactRequirement(
        "semantics_plus_00_00",
        FactType.COMPARISON,
        "Semantics of the +00:00 offset",
        "RFC 3339 UTC representations",
    )
    sql_isolation = FactRequirement(
        "read_uncommitted_anomaly_guarantees",
        FactType.COMPARISON,
        "PostgreSQL anomaly guarantees for Read Uncommitted isolation",
        "Read Uncommitted",
    )

    assert policy.urls_for_fact("Python releases", python_support) == (
        "https://devguide.python.org/versions/",
    )
    assert policy.urls_for_fact("Python releases", python_maintenance) == (
        "https://www.python.org/downloads/",
    )
    assert policy.urls_for_fact("Git states", git_states) == (
        "https://git-scm.com/book/en/v2/Getting-Started-What-is-Git%3F",
    )
    assert policy.urls_for_fact("HTTP GET method", http_get) == (
        "https://www.iana.org/assignments/http-methods/http-methods.xhtml",
    )
    assert policy.urls_for_fact("HTTP GET method", http_get_safe) == (
        "https://www.iana.org/assignments/http-methods/http-methods.xhtml",
    )
    assert policy.urls_for_fact("HTTP GET method", http_get_idempotent) == (
        "https://www.iana.org/assignments/http-methods/http-methods.xhtml",
    )
    assert policy.urls_for_fact("DNS", dns_transport) == (
        "https://www.iana.org/assignments/service-names-port-numbers/"
        "service-names-port-numbers.xhtml?search=53",
    )
    assert policy.urls_for_fact("DNS", dns_registry) == (
        "https://www.iana.org/assignments/service-names-port-numbers/"
        "service-names-port-numbers.xhtml",
    )
    assert policy.urls_for_fact("JSON media type", json_media_type) == (
        "https://www.iana.org/assignments/media-types/application/json",
    )
    assert policy.urls_for_fact("SHA-256", sha_size) == (
        "https://www.rfc-editor.org/rfc/rfc6234.txt",
    )
    assert policy.urls_for_fact("RFC 3339", rfc3339_offset) == (
        "https://www.rfc-editor.org/rfc/rfc3339.txt",
    )
    assert policy.urls_for_fact(
        "PostgreSQL transaction isolation levels",
        sql_isolation,
    ) == (
        "https://www.postgresql.org/docs/current/transaction-iso.html",
    )


def test_full_campaign_standards_have_reviewed_direct_sources() -> None:
    policy = DirectSourcePolicy()

    assert policy.urls_for("JSON standard", FactType.BACKGROUND)
    assert policy.urls_for("SHA-256", FactType.CURRENT_VALUE)
    assert policy.urls_for("DNS", FactType.CURRENT_VALUE)
    assert policy.urls_for("TLS 1.3", FactType.VERSION)
    assert policy.urls_for("TLS 1.3", FactType.CURRENT_VALUE) == (
        "https://www.rfc-editor.org/rfc/rfc8446.txt",
    )
    assert policy.urls_for("RFC 3339", FactType.COMPARISON)
    assert policy.urls_for("SQL transaction isolation", FactType.COMPARISON)
    assert policy.urls_for("PostgreSQL", FactType.CURRENT_VALUE)
    assert policy.urls_for("SQLite", FactType.BACKGROUND)
    assert policy.urls_for("CAP theorem", FactType.BACKGROUND)
    assert policy.urls_for("database ACID", FactType.BACKGROUND)
    assert policy.urls_for("数据库 ACID 性质", FactType.BACKGROUND) == (
        "https://www.ibm.com/think/topics/transaction-management",
    )


def test_current_campaign_semantics_resolve_to_reviewed_pages() -> None:
    policy = DirectSourcePolicy()
    deepseek_price = FactRequirement(
        "output_price",
        FactType.CURRENT_VALUE,
        "deepseek-v4-flash output price",
        "deepseek-v4-flash",
    )
    isolation = FactRequirement(
        "read_committed",
        FactType.CURRENT_VALUE,
        "Read Committed anomaly guarantees",
        "SQL transaction isolation",
    )
    apex_community = FactRequirement(
        "community_team_composition",
        FactType.TEAM_META,
        "community team composition perspective",
        "Apex Legends",
    )

    assert policy.version == "direct-sources-v8"
    assert policy.urls_for_fact("DeepSeek V4 Flash", deepseek_price) == (
        "https://api-docs.deepseek.com/quick_start/pricing/",
    )
    assert policy.urls_for_fact("SQL transaction isolation", isolation) == (
        "https://www.postgresql.org/docs/current/transaction-iso.html",
    )
    assert policy.urls_for_fact("Apex Legends", apex_community) == (
        "https://apexranked.com/meta",
        "https://games.gg/apex-legends/guides/"
        "apex-legends-season-29-tier-list/",
    )
