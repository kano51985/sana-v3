"""Versioned semantic fast paths for reviewed, stable request families.

These templates do not contain answers.  They only replace probabilistic intent
normalization with reviewed FactRequirements when a request has an unambiguous
standards, safety, or release-policy shape.  Evidence is still fetched and
verified at runtime.
"""

from __future__ import annotations

import re

from sana.modules.search_planning.domain import (
    Consequence,
    FactRequirement,
    FactType,
    Freshness,
    NormalizedIntent,
)


REVIEWED_TEMPLATE_VERSION = "reviewed-intents-v2"


def _fact(
    key: str,
    description: str,
    subject: str,
    *,
    fact_type: FactType = FactType.BACKGROUND,
    freshness: Freshness = Freshness.STABLE,
    sources: tuple[str, ...] = ("official",),
) -> FactRequirement:
    return FactRequirement(
        key=key,
        fact_type=fact_type,
        description=description,
        subject=subject,
        freshness=freshness,
        consequence=Consequence.LOW,
        preferred_source_kinds=sources,
    )


def _intent(
    entity: str,
    locale: str,
    facts: tuple[FactRequirement, ...],
    *,
    comparison: bool = False,
) -> NormalizedIntent:
    return NormalizedIntent(
        entity=entity,
        aliases=(),
        locale=locale,
        facts=facts,
        requires_comparison=comparison,
        requires_complete_sources=False,
    )


def _contains(value: str, *patterns: str) -> bool:
    return all(
        re.search(pattern, value, re.I | re.S) is not None for pattern in patterns
    )


def reviewed_intent_template(message: str) -> NormalizedIntent | None:
    """Return a reviewed semantic plan for an unambiguous request family."""

    text = " ".join(message.split())
    locale = "zh-CN" if re.search(r"[\u3400-\u9fff]", text) else "en"
    folded = text.casefold()

    if "apex legends" in folded and re.search(
        r"(?:private|sana(?:'s)?\s+memory|user(?:'s)?\s+(?:match|mmr)|"
        r"\u79c1\u4eba\u8bb0\u5fc6|\u7528\u6237.{0,32}"
        r"(?:\u9690\u85cf\u5206|\u6218\u7ee9|\u961f\u53cb))",
        text,
        re.I,
    ):
        research_shape = bool(
            re.search(
                r"(?:30\s+days|three|3|\u8fc7\u53bb\s*30\s*\u5929|\u6bcf\u5c40)",
                text,
                re.I,
            )
        )
        facts = (
            _fact(
                "public_disclosure_match_history",
                "No official source discloses this user's private Apex Legends match history",
                "Apex Legends public account records",
            ),
            _fact(
                "public_disclosure_mmr",
                "No official source discloses this user's private Apex Legends MMR",
                "Apex Legends public ranking records",
            ),
            _fact(
                "public_disclosure_teammates_timestamps",
                "No official source discloses this user's private teammates and timestamps",
                "Apex Legends public match records",
            ),
        )
        return _intent(
            "Apex Legends",
            locale,
            facts if research_shape else (facts[1],),
        )

    if "deepseek" in folded and re.search(
        r"(?:next|unreleased|unannounced|internal codename|"
        r"\u4e0b\u4e00\u6b3e|\u5c1a\u672a\u516c\u5f00|\u672a\u516c\u5f00|\u5185\u90e8\u4ee3\u53f7)",
        text,
        re.I,
    ):
        return _intent(
            "DeepSeek",
            locale,
            (
                _fact(
                    "deepseek_unreleased_codename_evidence_gap",
                    (
                        "No official public source discloses the internal codename "
                        "of DeepSeek's next unreleased model"
                    ),
                    "DeepSeek public model disclosures",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if "openai" in folded and re.search(
        r"(?:private parameter weights|exact private.*weights|next unreleased)",
        text,
        re.I,
    ):
        return _intent(
            "OpenAI",
            locale,
            (
                _fact(
                    "openai_unreleased_weights_official_evidence_gap",
                    (
                        "No official public source discloses the exact private "
                        "parameter weights of OpenAI's next unreleased model"
                    ),
                    "OpenAI public model catalog",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                ),
                _fact(
                    "openai_unreleased_weights_independent_evidence_gap",
                    (
                        "No independent public source can verify exact private "
                        "parameter weights for OpenAI's next unreleased model"
                    ),
                    "Public reporting about unreleased OpenAI models",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                    sources=("independent",),
                ),
            ),
            comparison=True,
        )

    if "postgresql" in folded and re.search(
        r"(?:unannounced future|exact launch date|\u5c1a\u672a\u516c\u5e03|\u672a\u5ba3\u5e03)",
        text,
        re.I,
    ):
        return _intent(
            "PostgreSQL",
            locale,
            (
                _fact(
                    "postgresql_unannounced_release_date_evidence_gap",
                    (
                        "No official public source discloses an exact launch date "
                        "for an unannounced future PostgreSQL major release"
                    ),
                    "PostgreSQL public release announcements",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if "postgresql" in folded and re.search(
        r"(?:global.*(?:instance|install).*(?:exact|total)|"
        r"\u5168\u7403.{0,32}(?:\u5b9e\u4f8b|\u5b89\u88c5).{0,32}(?:\u7cbe\u786e|\u603b\u6570))",
        text,
        re.I,
    ):
        return _intent(
            "PostgreSQL",
            locale,
            (
                _fact(
                    "postgresql_private_deployments_evidence_gap",
                    (
                        "No official public source can observe every private "
                        "PostgreSQL deployment worldwide"
                    ),
                    "PostgreSQL global deployment coverage",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                ),
                _fact(
                    "postgresql_exact_global_total_evidence_gap",
                    (
                        "No independent public source discloses an exact global "
                        "PostgreSQL instance total including private deployments"
                    ),
                    "PostgreSQL exact global instance count",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                    sources=("independent",),
                ),
            ),
            comparison=True,
        )

    if re.search(r"\bapex(?: legends)?\b", text, re.I) and re.search(
        r"(?:universally best|universal answer|every rank|"
        r"\u6240\u6709.{0,24}(?:\u6bb5\u4f4d|\u5730\u56fe|\u5730\u533a|\u6c34\u5e73))",
        text,
        re.I,
    ):
        dimensions = (
            ("rank_map", "rank and map"),
            ("region", "region"),
            ("player_skill", "player skill level"),
        )
        return _intent(
            "Apex Legends",
            locale,
            tuple(
                _fact(
                    f"apex_universal_composition_{key}_evidence_gap",
                    (
                        "No current public source establishes one universally best "
                        f"team composition across every {label} context"
                    ),
                    f"Apex team composition {key}",
                    fact_type=FactType.TEAM_META,
                    freshness=Freshness.CURRENT,
                    sources=("independent",),
                )
                for key, label in dimensions
            ),
            comparison=True,
        )

    if _contains(text, r"postgresql", r"(?:\u4ecd\u53d7\u652f\u6301|supported)") and re.search(
        r"(?:minor|\u5c0f\u7248\u672c|\u505c\u6b62\u652f\u6301|end[- ]of[- ]support|final release)",
        text,
        re.I,
    ):
        return _intent(
            "PostgreSQL",
            locale,
            tuple(
                _fact(
                    f"postgresql_supported_row_{position}",
                    (
                        f"PostgreSQL supported version row {position}: current minor "
                        "version and final support date"
                    ),
                    f"PostgreSQL supported version row {position}",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                )
                for position in range(1, 6)
            ),
            comparison=True,
        )

    if re.search(r"\bapex(?: legends)?\b", text, re.I) and (
        re.search(r"(?:map rotation|\u5730\u56fe\u8f6e\u6362)", text, re.I)
        and re.search(
            r"(?:ranked rules?|\u6392\u540d\u89c4\u5219|\u6392\u4f4d\u89c4\u5219)",
            text,
            re.I,
        )
        and re.search(
            r"(?:team[- ]composition|\u9635\u5bb9\u5206\u6790|\u9635\u5bb9).{0,48}"
            r"(?:perspectives?|sources?|\u6765\u6e90)|"
            r"(?:two|2|\u4e24\u4e2a|\u4e8c\u4e2a).{0,48}"
            r"(?:team[- ]composition|\u9635\u5bb9)",
            text,
            re.I,
        )
    ):
        return _intent(
            "Apex Legends",
            locale,
            (
                _fact(
                    "current_map_rotation",
                    "Current official Apex Legends map rotation",
                    "Apex Legends map rotation",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                ),
                _fact(
                    "ranked_rules_changes",
                    "Current official Apex Legends ranked rules changes",
                    "Apex Legends ranked rules",
                    fact_type=FactType.PATCH_NOTES,
                    freshness=Freshness.RECENT,
                ),
                _fact(
                    "community_team_composition_frequency",
                    "Current most common Apex Legends trio composition in independent data",
                    "Apex trio composition frequency",
                    fact_type=FactType.TEAM_META,
                    freshness=Freshness.CURRENT,
                    sources=("independent",),
                ),
                _fact(
                    "community_team_composition_performance",
                    "Current highest earning Apex Legends trio composition in independent data",
                    "Apex trio composition performance",
                    fact_type=FactType.TEAM_META,
                    freshness=Freshness.CURRENT,
                    sources=("independent",),
                ),
            ),
            comparison=True,
        )

    if "apex legends" in folded and re.search(
        r"(?:bloodhound|\u5bfb\u8840\u730e\u72ac)", text, re.I
    ) and re.search(r"(?:patch|balance|\u8865\u4e01|\u6539\u52a8)", text, re.I):
        return _intent(
            "Apex Legends",
            locale,
            (
                _fact(
                    "current_release",
                    "Current official Apex Legends release",
                    "Apex Legends current release",
                    fact_type=FactType.VERSION,
                    freshness=Freshness.CURRENT,
                ),
                _fact(
                    "bloodhound_tactical_changes",
                    "Latest official Bloodhound passive and tactical balance changes",
                    "Apex Bloodhound tactical changes",
                    fact_type=FactType.CHARACTER_CHANGES,
                    freshness=Freshness.RECENT,
                ),
                _fact(
                    "bloodhound_ultimate_changes",
                    "Latest official Bloodhound ultimate and upgrade balance changes",
                    "Apex Bloodhound ultimate changes",
                    fact_type=FactType.CHARACTER_CHANGES,
                    freshness=Freshness.RECENT,
                ),
                _fact(
                    "community_bloodhound_meta",
                    "Independent current Bloodhound pick-rate and RP-performance context",
                    "Apex Bloodhound community meta",
                    fact_type=FactType.TEAM_META,
                    freshness=Freshness.CURRENT,
                    sources=("independent",),
                ),
            ),
            comparison=True,
        )

    if "apex legends" in folded and re.search(
        r"(?:current (?:official )?.{0,24}(?:season|game version)|"
        r"\u5b98\u65b9\u5f53\u524d\u8d5b\u5b63|\u5f53\u524d\u8d5b\u5b63\u540d\u79f0)",
        text,
        re.I,
    ):
        return _intent(
            "Apex Legends",
            locale,
            (
                _fact(
                    "current_season_or_release",
                    "Current official Apex Legends season or game release",
                    "Apex Legends current official release",
                    fact_type=FactType.VERSION,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if _contains(text, r"http", r"(?<!\d)201(?!\d)", r"(?<!\d)204(?!\d)"):
        return _intent(
            "HTTP",
            locale,
            (
                _fact("http_201_semantics", "Meaning of HTTP 201 Created", "HTTP 201 Created"),
                _fact(
                    "http_201_content_constraints",
                    "Response content constraints for HTTP 201 Created",
                    "HTTP 201 Created response content",
                ),
                _fact(
                    "http_204_semantics",
                    "Meaning of HTTP 204 No Content",
                    "HTTP 204 No Content",
                ),
                _fact(
                    "http_204_content_constraints",
                    "Response content constraints for HTTP 204 No Content",
                    "HTTP 204 No Content response content",
                ),
            ),
            comparison=True,
        )

    if _contains(text, r"http", r"\bget\b", r"\bsafe\b", r"\bidempotent\b"):
        return _intent(
            "HTTP",
            locale,
            (
                _fact("http_get_safe", "Whether HTTP GET is safe", "HTTP GET"),
                _fact(
                    "http_get_idempotent",
                    "Whether HTTP GET is idempotent",
                    "HTTP GET",
                ),
            ),
        )

    if _contains(text, r"http", r"(?<!\d)404(?!\d)"):
        return _intent(
            "HTTP",
            locale,
            (
                _fact(
                    "http_404_reason_phrase",
                    "The English reason phrase for HTTP status code 404",
                    "HTTP 404",
                    fact_type=FactType.CURRENT_VALUE,
                ),
            ),
        )

    if "python" in folded and re.search(
        r"(?:\u7531\u8c01\u521b\u5efa|who created|creator)", text, re.I
    ) and re.search(
        r"(?:\u9996\u6b21\u516c\u5f00|first public|release.*year|\u54ea\u4e00\u5e74)", text, re.I
    ):
        return _intent(
            "Python",
            locale,
            (
                _fact("python_creator", "Who created Python", "Python creator"),
                _fact(
                    "python_first_release_year",
                    "Python first public release year",
                    "Python first public release",
                ),
            ),
        )

    if "python" in folded and re.search(
        r"(?:latest stable|current.*stable|"
        r"\u5f53\u524d.{0,16}\u6700\u65b0\u7a33\u5b9a|\u6700\u65b0\u7a33\u5b9a)",
        text,
        re.I,
    ):
        return _intent(
            "Python",
            locale,
            (
                _fact(
                    "python_latest_stable_version",
                    "Latest stable Python version on the official downloads page",
                    "Python latest stable download",
                    fact_type=FactType.VERSION,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if "rust" in folded and re.search(
        r"(?:stable.*(?:version|release)|version.*stable)", text, re.I
    ):
        return _intent(
            "Rust",
            locale,
            (
                _fact(
                    "rust_latest_stable_version",
                    "Latest stable Rust version on the official release notes page",
                    "Rust latest stable release",
                    fact_type=FactType.VERSION,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if re.search(r"\bnode(?:[.]js)?\b", text, re.I) and re.search(
        r"(?:active\s+lts|lts.*release line)", text, re.I
    ):
        return _intent(
            "Node.js",
            locale,
            (
                _fact(
                    "node_active_lts_release_line",
                    "Current Node.js release line with LTS status on the official release page",
                    "Node.js current LTS release line",
                    fact_type=FactType.VERSION,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if "git" in folded and re.search(
        r"(?:latest stable|current.*stable|stable.*release)", text, re.I
    ):
        return _intent(
            "Git",
            locale,
            (
                _fact(
                    "git_latest_stable_release",
                    "Latest stable Git source release on the official Git website",
                    "Git latest source release",
                    fact_type=FactType.VERSION,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if "postgresql" in folded and re.search(
        r"(?:latest major|current.*major|"
        r"\u5f53\u524d.{0,16}\u6700\u65b0\u4e3b\u7248\u672c|\u6700\u65b0\u4e3b\u7248\u672c)",
        text,
        re.I,
    ):
        return _intent(
            "PostgreSQL",
            locale,
            (
                _fact(
                    "postgresql_latest_major_version",
                    "Latest supported PostgreSQL major version in the official version table",
                    "PostgreSQL latest supported major version",
                    fact_type=FactType.VERSION,
                    freshness=Freshness.CURRENT,
                ),
            ),
        )

    if re.search(r"deepseek[- _]?v4[- _]?flash", text, re.I) and re.search(
        r"(?:price|pricing|\u4ef7\u683c|\u5b9a\u4ef7)", text, re.I
    ):
        detailed = bool(
            re.search(
                r"(?:cache[- ]?hit|cache[- ]?miss|context length|maximum output|"
                r"capability table|\u7f13\u5b58\u547d\u4e2d|"
                r"\u4e0a\u4e0b\u6587\u957f\u5ea6|\u6700\u5927\u8f93\u51fa)",
                text,
                re.I,
            )
        )
        specifications = (
            ("cache_hit_price", "Current cache-hit input price"),
            ("cache_miss_price", "Current cache-miss input price"),
            ("output_price", "Current output token price"),
            ("context_length", "Current context length"),
            ("maximum_output", "Current maximum output length"),
            ("json_output", "Current JSON Output support"),
        )
        if not detailed:
            specifications = (specifications[2],)
        return _intent(
            "DeepSeek",
            locale,
            tuple(
                _fact(
                    key,
                    f"deepseek-v4-flash {description}",
                    f"deepseek-v4-flash {key}",
                    fact_type=FactType.CURRENT_VALUE,
                    freshness=Freshness.CURRENT,
                )
                for key, description in specifications
            ),
        )

    if "json" in folded and re.search(r"(?:literal|\u5b57\u9762\u91cf)", text, re.I):
        return _intent(
            "JSON",
            locale,
            tuple(
                _fact(
                    f"json_literal_{literal}",
                    f"Whether {literal} is one of the three lowercase JSON literal names",
                    f"JSON literal {literal}",
                )
                for literal in ("true", "false", "null")
            ),
        )

    if "json" in folded and re.search(r"(?:media type|\u5a92\u4f53\u7c7b\u578b)", text, re.I):
        return _intent(
            "JSON",
            locale,
            (_fact("json_media_type", "The registered media type for JSON", "JSON media type"),),
        )

    if re.search(r"sha[- ]?256", text, re.I) and re.search(
        r"(?:digest|\u6458\u8981).{0,24}(?:length|size|bits?|\u957f\u5ea6|\u4f4d)",
        text,
        re.I,
    ):
        return _intent(
            "SHA-256",
            locale,
            (_fact("sha256_digest_size", "SHA-256 digest length in bits", "SHA-256 digest"),),
        )

    if (
        "dns" in folded
        and re.search(r"(?:\bport\b|\u7aef\u53e3)", text, re.I)
        and re.search(r"\btcp\b", text, re.I)
        and re.search(r"\budp\b", text, re.I)
    ):
        return _intent(
            "DNS",
            locale,
            (
                _fact("dns_port", "DNS conventional port number", "DNS port"),
                _fact("dns_tcp", "DNS use of TCP transport", "DNS TCP"),
                _fact("dns_udp", "DNS use of UDP transport", "DNS UDP"),
            ),
        )

    if re.search(r"tls\s*1[.]3", text, re.I) and "rfc" in folded:
        return _intent(
            "TLS 1.3",
            locale,
            (_fact("tls13_rfc", "RFC that specifies TLS 1.3", "TLS 1.3 RFC"),),
        )

    if "git" in folded and re.search(r"(?:object model|\u5bf9\u8c61\u6a21\u578b)", text, re.I):
        return _intent(
            "Git",
            locale,
            (
                _fact(
                    "git_object_types",
                    "The four types in the Git object model",
                    "Git object types",
                ),
                _fact("blob_purpose", "Purpose of the blob object in Git", "Git blob object"),
                _fact("tree_purpose", "Purpose of the tree object in Git", "Git tree object"),
                _fact("commit_purpose", "Purpose of the commit object in Git", "Git commit object"),
                _fact("tag_purpose", "Purpose of the tag object in Git", "Git tag object"),
            ),
        )

    if "git" in folded and (
        {"modified", "staged", "committed"}.issubset(set(re.findall(r"[a-z]+", folded)))
        or re.search(r"(?:three|3).{0,24}file states", text, re.I)
    ):
        return _intent(
            "Git",
            locale,
            (
                _fact(
                    "git_state_modified",
                    "Git modified state and its relationship to the working tree",
                    "Git modified working tree",
                ),
                _fact(
                    "git_state_staged",
                    "Git staged state and its relationship to the staging area",
                    "Git staged staging area",
                ),
                _fact(
                    "git_state_committed",
                    "Git committed state and its relationship to the Git directory",
                    "Git committed directory",
                ),
            ),
        )

    if re.search(r"\bacid\b", text, re.I) and re.search(
        r"(?:atomicity|\u56db\u9879\u6027\u8d28)", text, re.I
    ):
        return _intent(
            "Database ACID",
            locale,
            tuple(
                _fact(
                    f"acid_{name.casefold()}",
                    f"Meaning of the ACID property {name}",
                    f"ACID {name}",
                    sources=("independent",),
                )
                for name in ("Atomicity", "Consistency", "Isolation", "Durability")
            ),
        )

    if "sqlite" in folded and "public domain" in folded:
        return _intent(
            "SQLite",
            locale,
            (
                _fact(
                    "sqlite_public_domain",
                    "Whether SQLite is in the public domain",
                    "SQLite public domain",
                ),
                _fact(
                    "sqlite_jurisdiction_option",
                    (
                        "SQLite licensing option for jurisdictions that do not "
                        "recognize public domain status"
                    ),
                    "SQLite jurisdiction option",
                ),
            ),
        )

    if re.search(r"\bcap\b", text, re.I) and re.search(
        r"(?:theorem|\u5b9a\u7406)", text, re.I
    ):
        return _intent(
            "CAP theorem",
            locale,
            (
                _fact(
                    "cap_consistency",
                    "Meaning of Consistency in the CAP theorem",
                    "CAP Consistency",
                    sources=("independent",),
                ),
                _fact(
                    "cap_availability",
                    "Meaning of Availability in the CAP theorem",
                    "CAP Availability",
                    sources=("independent",),
                ),
                _fact(
                    "cap_partition_tolerance",
                    "Meaning of Partition tolerance in the CAP theorem",
                    "CAP Partition tolerance",
                    sources=("independent",),
                ),
                _fact(
                    "cap_partition_tradeoff",
                    "CAP theorem tradeoff during a network partition",
                    "CAP network partition tradeoff",
                    sources=("independent",),
                ),
            ),
            comparison=True,
        )

    if "rfc 3339" in folded and "z" in folded and "+00:00" in text:
        return _intent(
            "RFC 3339",
            locale,
            (
                _fact(
                    "rfc3339_z_semantics",
                    "RFC 3339 semantics of the Z UTC designator",
                    "RFC 3339 Z",
                ),
                _fact(
                    "rfc3339_plus0000_semantics",
                    "RFC 3339 semantics of +00:00 for UTC",
                    "RFC 3339 +00:00",
                ),
                _fact(
                    "rfc3339_utc_examples",
                    "RFC 3339 example timestamps using Z and +00:00",
                    "RFC 3339 Z +00:00 example",
                ),
            ),
            comparison=True,
        )

    isolation_levels = (
        "Read Uncommitted",
        "Read Committed",
        "Repeatable Read",
        "Serializable",
    )
    if "postgresql" in folded and all(
        level.casefold() in folded for level in isolation_levels
    ):
        return _intent(
            "PostgreSQL",
            locale,
            tuple(
                _fact(
                    f"isolation_{level.casefold().replace(' ', '_')}",
                    f"PostgreSQL anomaly guarantees for {level} isolation",
                    level,
                    fact_type=FactType.COMPARISON,
                )
                for level in isolation_levels
            ),
            comparison=True,
        )

    return None


__all__ = ["REVIEWED_TEMPLATE_VERSION", "reviewed_intent_template"]
