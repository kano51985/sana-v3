from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.content.domain import DocumentChunk, DocumentVersion
from sana.modules.evidence.candidate_selector import SelectedCandidate
from sana.modules.evidence.domain import EvidenceVerdict, SourceAuthority
from sana.modules.evidence.model_verifier import ModelEvidenceVerifier
from sana.modules.model_gateway.domain import ModelInvocationContext, ModelResult
from sana.modules.search_planning.domain import FactRequirement, FactType
from sana.modules.shared.ids import TraceContext


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class ParsingGateway:
    def __init__(self, text: str) -> None:
        self.text = text

    async def generate(self, role, messages, *, parser, **kwargs):
        return ModelResult("", "model", parsed=parser.parse(self.text))


class ForbiddenGateway:
    async def generate(self, *args, **kwargs):
        raise AssertionError("exact official values must not require a model call")


def candidate() -> SelectedCandidate:
    tenant_id, document_id = uuid4(), uuid4()
    text = "Apex Legends current version is 27.1 according to the patch notes."
    version = DocumentVersion(
        uuid4(),
        tenant_id,
        document_id,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
        "text/plain",
        "en",
        NOW,
    )
    chunk = DocumentChunk(
        0,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        12,
        0,
        len(text),
    )
    return SelectedCandidate(
        uuid4(),
        uuid4(),
        FactRequirement("version", FactType.VERSION, "current version", "Apex Legends"),
        document_id,
        version,
        uuid4(),
        chunk,
        "https://www.ea.com/games/apex",
        "Patch",
        "ea.com",
        SourceAuthority.OFFICIAL,
        text,
        0.9,
    )


def context(item: SelectedCandidate) -> ModelInvocationContext:
    return ModelInvocationContext(
        item.version.tenant_id,
        uuid4(),
        uuid4(),
        "verify",
        uuid4(),
        1,
        TraceContext.create(),
        ("extract:sha256",),
    )


@pytest.mark.asyncio
async def test_model_verdict_is_rebuilt_through_exact_span_gate() -> None:
    item = candidate()
    quote = "current version is 27.1"
    gateway = ParsingGateway(
        '{"verdicts":[{'
        f'"fact_id":"{item.fact_id}","candidate_id":"{item.id}",'
        f'"support_type":"SUPPORTS","quote":"{quote}","confidence":0.93,'
        '"reason_codes":["explicit_value"]}]}'
    )
    invocation = context(item)

    result = await ModelEvidenceVerifier(gateway).verify(
        (item,),
        invocation_context=invocation,
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].candidate.quote == quote
    assert item.version.text[
        result.evidence[0].candidate.start_offset : result.evidence[0].candidate.end_offset
    ] == quote


@pytest.mark.asyncio
async def test_forged_quote_cannot_become_accepted_model_evidence() -> None:
    item = candidate()
    gateway = ParsingGateway(
        '{"verdicts":[{'
        f'"fact_id":"{item.fact_id}","candidate_id":"{item.id}",'
        '"support_type":"SUPPORTS","quote":"forged value 99",'
        '"confidence":1,"reason_codes":["explicit_value"]}]}'
    )

    result = await ModelEvidenceVerifier(gateway).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is True
    assert all(evidence.candidate.quote != "forged value 99" for evidence in result.evidence)
    assert all(evidence.confidence <= 0.49 for evidence in result.evidence)


@pytest.mark.asyncio
async def test_model_omission_persists_a_rejected_candidate_audit() -> None:
    item = candidate()
    result = await ModelEvidenceVerifier(ParsingGateway('{"verdicts":[]}')).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED
    assert result.evidence[0].candidate.id == item.id
    assert result.evidence[0].verifier_version == "deepseek-verifier-v2"


def test_verifier_prompt_groups_candidates_by_fact_and_requires_compact_output() -> None:
    item = candidate()
    messages = ModelEvidenceVerifier._messages((item,))
    payload = json.loads(messages[1].content)

    assert "candidates" not in payload
    assert payload["facts"][0]["fact_id"] == str(item.fact_id)
    assert payload["facts"][0]["candidates"][0]["candidate_id"] == str(item.id)
    assert "at most one strongest verdict per fact" in messages[0].content


def explicit_http_candidate() -> SelectedCandidate:
    base = candidate()
    text = '404,Not Found,"[RFC9110, Section 15.5.5]"'
    version = replace(
        base.version,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    chunk = DocumentChunk(
        0,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        8,
        0,
        len(text),
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "http_404_reason_phrase",
            FactType.CURRENT_VALUE,
            "The English reason phrase for HTTP status code 404",
            "HTTP",
        ),
        version=version,
        chunk_id=uuid4(),
        chunk=chunk,
        url=(
            "https://www.iana.org/assignments/http-status-codes/"
            "http-status-codes-1.csv"
        ),
        source_identity="iana.org",
        quote=text,
        score=0.84,
    )


def explicit_json_terms_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "A JSON value can be one of the following three literal names: "
        "false, null, and true."
    )
    version = replace(
        base.version,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "json_literals_three",
            FactType.BACKGROUND,
            "The three JSON literals are: true, false, and null.",
            "JSON standard",
        ),
        version=version,
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            16,
            0,
            len(text),
        ),
        url="https://www.rfc-editor.org/rfc/rfc8259.html",
        source_identity="rfc-editor.org",
        quote=text,
        score=0.94,
    )


def http_method_registry_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "Method Name\nSafe\nIdempotent\nReference\n"
        "DELETE\nno\nyes\n[RFC9110]\nGET\nyes\nyes\n[RFC9110]"
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "get_safe",
            FactType.BACKGROUND,
            "Whether HTTP GET is safe according to HTTP semantics",
            "HTTP GET method",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            12,
            0,
            len(text),
        ),
        url="https://www.iana.org/assignments/http-methods/http-methods.xhtml",
        source_identity="iana.org",
        quote=text,
        score=0.88,
    )


def media_type_registry_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "Type name: application\nSubtype name: json\n"
        "Published specification: RFC 8259"
    )
    selected_quote = "Availability considerations: See RFC 8259"
    text = f"{text}\n{selected_quote}"
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "json_media_type",
            FactType.CURRENT_VALUE,
            "The registered media type for JSON",
            "JSON media type",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            12,
            0,
            len(text),
        ),
        url="https://www.iana.org/assignments/media-types/application/json",
        source_identity="iana.org",
        quote=selected_quote,
        score=0.34,
    )


def sha_digest_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "The SHA-224 and SHA-256 algorithms produce 224-bit and 256-bit * "
        "message digests for a given data stream."
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "sha256_digest_length_bits",
            FactType.BACKGROUND,
            "SHA-256 digest length in bits",
            "SHA-256",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            18,
            0,
            len(text),
        ),
        url="https://www.rfc-editor.org/rfc/rfc6234.txt",
        source_identity="rfc-editor.org",
        quote=text,
        score=0.92,
    )


def tls_rfc_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "Request for Comments: 8446 Mozilla\nCategory: Standards Track\n"
        "The Transport Layer Security (TLS) Protocol Version 1.3\nAbstract"
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "tls13_rfc",
            FactType.VERSION,
            "Which RFC specifies TLS 1.3",
            "TLS 1.3",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            18,
            0,
            len(text),
        ),
        url="https://www.rfc-editor.org/rfc/rfc8446.txt",
        source_identity="rfc-editor.org",
        quote=text,
        score=0.92,
    )


def reviewed_text_candidate(
    text: str,
    fact: FactRequirement,
    url: str,
    *,
    fact_id=None,
) -> SelectedCandidate:
    base = candidate()
    return replace(
        base,
        fact_id=fact_id or uuid4(),
        fact=fact,
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            32,
            0,
            len(text),
        ),
        url=url,
        source_identity=("postgresql.org" if "postgresql" in url else "rfc-editor.org"),
        quote=text,
        score=0.92,
    )


def git_object_candidate(
    text: str,
    *,
    key: str,
    description: str,
) -> SelectedCandidate:
    item = reviewed_text_candidate(
        text,
        FactRequirement(key, FactType.BACKGROUND, description, "Git"),
        "https://git-scm.com/docs/gitdatamodel.html",
    )
    return replace(item, source_identity="git-scm.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "fact",
        "url",
        "source_identity",
        "authority",
        "text",
        "expected_version",
        "expected_quote",
    ),
    (
        (
            FactRequirement(
                "dns_port_transports",
                FactType.BACKGROUND,
                "DNS port and transport protocols",
                "DNS",
            ),
            (
                "https://www.iana.org/assignments/service-names-port-numbers/"
                "service-names-port-numbers.xhtml?search=53"
            ),
            "iana.org",
            SourceAuthority.OFFICIAL,
            (
                "domain 53 tcp Domain Name Server DOMAIN 53 udp "
                "domain 53 udp Domain Name Server"
            ),
            "deterministic-dns-registry-v1",
            "domain 53 udp",
        ),
        (
            FactRequirement(
                "python_creator",
                FactType.BACKGROUND,
                "Who created Python?",
                "Python",
            ),
            "https://www.python.org/download/releases/2.1/license/",
            "python.org",
            SourceAuthority.OFFICIAL,
            (
                "Python was created in the early 1990s by Guido van Rossum as a "
                "successor of a language called ABC."
            ),
            "deterministic-python-origin-v1",
            "Guido van Rossum",
        ),
        (
            FactRequirement(
                "python_first_release_year",
                FactType.BACKGROUND,
                "Python first public release year",
                "Python",
            ),
            "https://docs.python.org/3/license.html#history-of-the-software",
            "python.org",
            SourceAuthority.OFFICIAL,
            "0.9.0 thru 1.2 n/a 1991-1995 CWI yes",
            "deterministic-python-origin-v1",
            "1991-1995",
        ),
        (
            FactRequirement(
                "json_literals",
                FactType.BACKGROUND,
                "the three lowercase JSON literal names",
                "JSON",
            ),
            "https://www.rfc-editor.org/rfc/rfc8259.html",
            "rfc-editor.org",
            SourceAuthority.OFFICIAL,
            (
                "following three literal names: false null true "
                "The literal names MUST be lowercase"
            ),
            "deterministic-json-literals-v1",
            "false null true",
        ),
        (
            FactRequirement(
                "http_201_semantics",
                FactType.BACKGROUND,
                "meaning of 201 Created",
                "HTTP 201",
            ),
            "https://www.rfc-editor.org/rfc/rfc9110.html#name-201-created",
            "rfc-editor.org",
            SourceAuthority.OFFICIAL,
            (
                "The 201 (Created) status code indicates that the request has been "
                "fulfilled and has resulted in one or more new resources being "
                "created. The primary resource created by the request is identified "
                "by either a Location header field or the target URI"
            ),
            "deterministic-http-status-v1",
            "201 (Created)",
        ),
        (
            FactRequirement(
                "http_204_content_constraint",
                FactType.BACKGROUND,
                "response content constraint for 204 No Content",
                "HTTP 204",
            ),
            "https://www.rfc-editor.org/rfc/rfc9110.html#name-204-no-content",
            "rfc-editor.org",
            SourceAuthority.OFFICIAL,
            (
                "The 204 (No Content) status code indicates that the server has "
                "successfully fulfilled the request and that there is no additional "
                "content to send in the response content"
            ),
            "deterministic-http-status-v1",
            "204 (No Content)",
        ),
        (
            FactRequirement(
                "acid_atomicity",
                FactType.BACKGROUND,
                "Explain Atomicity",
                "ACID",
            ),
            "https://www.ibm.com/think/topics/acid-transactions",
            "ibm.com",
            SourceAuthority.INDEPENDENT,
            (
                "Atomicity: Each transaction is treated as a single unit that "
                "either succeeds completely or fails completely. Consistency: "
                "Every transaction preserves database rules."
            ),
            "deterministic-acid-v1",
            "Atomicity:",
        ),
        (
            FactRequirement(
                "cap_consistency",
                FactType.BACKGROUND,
                "Explain Consistency in the CAP theorem",
                "CAP theorem",
            ),
            "https://www.ibm.com/think/topics/cap-theorem",
            "ibm.com",
            SourceAuthority.INDEPENDENT,
            (
                "Consistency means that all clients see the same data at the same "
                "time, no matter which node they connect to. For this to happen, "
                "whenever data is written to one node, it must be instantly "
                "forwarded or replicated across all the nodes in the system before "
                "the write is deemed successful."
            ),
            "deterministic-cap-v1",
            "Consistency means",
        ),
        (
            FactRequirement(
                "sqlite_public_domain",
                FactType.BACKGROUND,
                "Whether SQLite is in the public domain",
                "SQLite",
            ),
            "https://www.sqlite.org/copyright.html",
            "sqlite.org",
            SourceAuthority.OFFICIAL,
            "SQLite is in the public domain and does not require a license.",
            "deterministic-sqlite-v1",
            "public domain",
        ),
        (
            FactRequirement(
                "sqlite_jurisdiction_option",
                FactType.BACKGROUND,
                "Option for a jurisdiction that cannot use public domain",
                "SQLite",
            ),
            "https://www.sqlite.org/copyright.html",
            "sqlite.org",
            SourceAuthority.OFFICIAL,
            (
                "Hwaci\n, the company that employs all the developers of SQLite, "
                "will\nsell you a Warranty of Title for SQLite\n."
            ),
            "deterministic-sqlite-v1",
            "Warranty of Title",
        ),
        (
            FactRequirement(
                "output_price",
                FactType.CURRENT_VALUE,
                "current output price",
                "deepseek-v4-flash",
            ),
            "https://api-docs.deepseek.com/quick_start/pricing/",
            "api-docs.deepseek.com",
            SourceAuthority.OFFICIAL,
            "1M OUTPUT TOKENS $0.28",
            "deterministic-deepseek-pricing-v1",
            "$0.28",
        ),
        (
            FactRequirement(
                "postgresql_18_support",
                FactType.CURRENT_VALUE,
                "PostgreSQL 18 current minor and final release support date",
                "PostgreSQL 18",
            ),
            "https://www.postgresql.org/support/versioning/",
            "postgresql.org",
            SourceAuthority.OFFICIAL,
            "18 18.4 Yes September 25, 2025 November 14, 2030",
            "deterministic-postgresql-support-v1",
            "November 14, 2030",
        ),
        (
            FactRequirement(
                "rust_latest_stable_version",
                FactType.VERSION,
                "Latest stable Rust version on the official release notes page",
                "Rust latest stable release",
            ),
            "https://doc.rust-lang.org/stable/releases.html",
            "rust-lang.org",
            SourceAuthority.OFFICIAL,
            "Rust 1.97.1 8bab26f4f Rust Release Notes Version 1.97.1 (2026-07-16)",
            "deterministic-reviewed-release-v1",
            "Rust 1.97.1",
        ),
        (
            FactRequirement(
                "python_latest_stable_version",
                FactType.VERSION,
                "Latest stable Python version on the official downloads page",
                "Python latest stable download",
            ),
            "https://www.python.org/downloads/",
            "python.org",
            SourceAuthority.OFFICIAL,
            "Download the latest source release Download Python 3.14.7",
            "deterministic-reviewed-release-v1",
            "Download Python 3.14.7",
        ),
        (
            FactRequirement(
                "node_active_lts_release_line",
                FactType.VERSION,
                "Current Node.js release line with LTS status",
                "Node.js current LTS release line",
            ),
            "https://nodejs.org/en/about/previous-releases",
            "nodejs.org",
            SourceAuthority.OFFICIAL,
            "v 24 Krypton May 06, 2025 Aug 03, 2026 LTS Details",
            "deterministic-reviewed-release-v1",
            "v 24 Krypton",
        ),
        (
            FactRequirement(
                "git_latest_stable_release",
                FactType.VERSION,
                "Latest stable Git source release on the official Git website",
                "Git latest source release",
            ),
            "https://git-scm.com/",
            "git-scm.com",
            SourceAuthority.OFFICIAL,
            "Git Latest source release 2.55.0 Release Notes (2026-06-29)",
            "deterministic-reviewed-release-v1",
            "Latest source release 2.55.0",
        ),
        (
            FactRequirement(
                "current_season_or_release",
                FactType.VERSION,
                "Current official Apex Legends season or game release",
                "Apex Legends current official release",
            ),
            (
                "https://www.ea.com/games/apex-legends/apex-legends/news/"
                "overclocked-midseason-patch-notes"
            ),
            "ea.com",
            SourceAuthority.OFFICIAL,
            (
                "Apex Legends™: Overclocked Midseason Patch Notes June 22, 2026 "
                "INTRO Welcome back to Overclocked Split 2! Patch notes follow."
            ),
            "deterministic-reviewed-release-v1",
            "Welcome back to Overclocked Split 2!",
        ),
        (
            FactRequirement(
                "bloodhound_tactical_changes",
                FactType.CHARACTER_CHANGES,
                "Latest official Bloodhound passive and tactical balance changes",
                "Apex Bloodhound tactical changes",
            ),
            (
                "https://www.ea.com/games/apex-legends/apex-legends/news/"
                "breach-patch-notes"
            ),
            "ea.com",
            SourceAuthority.OFFICIAL,
            (
                "Apex Legends™: Breach Patch Notes February 9, 2026 BLOODHOUND "
                "Abilities Passive changed. Tactical Total scan duration "
                "up to 4s (was 3s), full-body duration remains half the full scan "
                "duration. Reduced screen coloration during scan. Fewer clues will "
                "pop-up on screen when your tactical successfully scans an enemy "
                "Ultimate: Cooldown reduced to 2.5 minutes."
            ),
            "deterministic-apex-reviewed-v1",
            "Total scan duration up to 4s",
        ),
        (
            FactRequirement(
                "bloodhound_ultimate_changes",
                FactType.CHARACTER_CHANGES,
                "Latest official Bloodhound ultimate and upgrade balance changes",
                "Apex Bloodhound ultimate changes",
            ),
            (
                "https://www.ea.com/games/apex-legends/apex-legends/news/"
                "breach-patch-notes"
            ),
            "ea.com",
            SourceAuthority.OFFICIAL,
            (
                "Apex Legends™: Breach Patch Notes February 9, 2026 BLOODHOUND "
                "Ultimate: Cooldown reduced to 2.5 minutes (was 4 minutes) "
                "Activation time reduced by ~20% Knocks while your ultimate is active "
                "increase the duration by 5s Upgrades Level 2 Taste of Blood"
            ),
            "deterministic-apex-reviewed-v1",
            "Cooldown reduced to 2.5 minutes",
        ),
        (
            FactRequirement(
                "current_map_rotation",
                FactType.CURRENT_VALUE,
                "Current official Apex Legends map rotation",
                "Apex Legends map rotation",
            ),
            (
                "https://www.ea.com/games/apex-legends/apex-legends/news/"
                "overclocked-midseason-patch-notes"
            ),
            "ea.com",
            SourceAuthority.OFFICIAL,
            (
                "Apex Legends™: Overclocked Midseason Patch Notes June 22, 2026 "
                "MAPS This split’s map rotations are as follows: Pubs Storm Point "
                "World’s Edge Kings Canyon Ranked Storm Point World’s Edge E-District "
                "Mixtape 6/23/26"
            ),
            "deterministic-apex-reviewed-v1",
            "Ranked Storm Point World’s Edge E-District",
        ),
        (
            FactRequirement(
                "ranked_rules_changes",
                FactType.PATCH_NOTES,
                "Current official Apex Legends ranked rules changes",
                "Apex Legends ranked rules",
            ),
            (
                "https://www.ea.com/games/apex-legends/apex-legends/news/"
                "overclocked-midseason-patch-notes"
            ),
            "ea.com",
            SourceAuthority.OFFICIAL,
            (
                "Apex Legends™: Overclocked Midseason Patch Notes June 22, 2026 "
                "RANKED Removing “Champion Squad” Screen We have removed the "
                "“Champion Squad” screen from the Ranked match start flow. You will "
                "still see your team on the Your Squad screen then immediately load "
                "into the dropship."
            ),
            "deterministic-apex-reviewed-v1",
            "immediately load into the dropship",
        ),
        (
            FactRequirement(
                "community_team_composition_frequency",
                FactType.TEAM_META,
                "Current most common Apex Legends trio composition in independent data",
                "Apex trio composition frequency",
            ),
            "https://apexranked.com/meta",
            "apexranked.com",
            SourceAuthority.INDEPENDENT,
            (
                "Last updated: 2026-08-15T04:48:55Z "
                "Most Common Trio Compositions 1 A Axle L Loba S Seer 2,045× seen "
                "+41.9 RP 2 A Axle L Loba P Pathfinder 713× seen +29.6 RP"
            ),
            "deterministic-apex-reviewed-v1",
            "A Axle L Loba S Seer",
        ),
        (
            FactRequirement(
                "community_team_composition_performance",
                FactType.TEAM_META,
                "Current highest earning Apex Legends trio composition",
                "Apex trio composition performance",
            ),
            "https://apexranked.com/meta",
            "apexranked.com",
            SourceAuthority.INDEPENDENT,
            (
                "Last updated: 2026-08-15T04:48:55Z "
                "Highest Earning Trio Compositions 1 A Axle L Loba V Valkyrie "
                "+68.7 RP 43 games 2 A Alter L Loba S Sparrow +62.8 RP 41 games"
            ),
            "deterministic-apex-reviewed-v1",
            "A Axle L Loba V Valkyrie",
        ),
        (
            FactRequirement(
                "community_bloodhound_meta",
                FactType.TEAM_META,
                "Independent current Bloodhound pick-rate and RP-performance context",
                "Apex Bloodhound community meta",
            ),
            "https://apexranked.com/meta",
            "apexranked.com",
            SourceAuthority.INDEPENDENT,
            (
                "Last updated: 2026-08-15T04:48:55Z "
                "B Bloodhound +36.3 Win 43.8% Pick 0.8% Games 314"
            ),
            "deterministic-apex-reviewed-v1",
            "Bloodhound +36.3",
        ),
    ),
)
async def test_reviewed_structured_sources_skip_probabilistic_verification(
    fact: FactRequirement,
    url: str,
    source_identity: str,
    authority: SourceAuthority,
    text: str,
    expected_version: str,
    expected_quote: str,
) -> None:
    item = reviewed_text_candidate(text, fact, url)
    item = replace(
        item,
        source_identity=source_identity,
        source_authority=authority,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].verifier_version == expected_version
    assert expected_quote in result.evidence[0].candidate.quote


@pytest.mark.asyncio
async def test_product_version_number_cannot_masquerade_as_requested_price() -> None:
    fact = FactRequirement(
        "output_price",
        FactType.CURRENT_VALUE,
        "current output price",
        "deepseek-v4-flash",
    )
    item = reviewed_text_candidate(
        "MODEL deepseek-v4-flash supports JSON Output.",
        fact,
        "https://api-docs.deepseek.com/quick_start/pricing/",
    )
    item = replace(
        item,
        source_identity="api-docs.deepseek.com",
        source_authority=SourceAuthority.OFFICIAL,
    )

    result = await ModelEvidenceVerifier(
        ParsingGateway('{"verdicts":[]}')
    ).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED


@pytest.mark.asyncio
async def test_exact_official_numeric_value_skips_model_verification() -> None:
    item = explicit_http_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].candidate.quote == "404,Not Found"
    assert result.evidence[0].verifier_version == "deterministic-explicit-value-v1"


@pytest.mark.asyncio
async def test_exact_official_term_list_skips_model_verification() -> None:
    item = explicit_json_terms_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].verifier_version == "deterministic-explicit-terms-v1"
    assert all(
        value in result.evidence[0].candidate.quote
        for value in ("true", "false", "null")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    (
        "https://www.iana.org/assignments/http-methods/http-methods.xhtml",
        "https://www.iana.org/assignments/http-methods",
    ),
)
async def test_reviewed_registry_table_skips_model_verification(
    url: str,
) -> None:
    item = http_method_registry_candidate()
    item = replace(item, url=url)

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].verifier_version == "deterministic-registry-table-v1"
    assert "GET\nyes\nyes" in result.evidence[0].candidate.quote


@pytest.mark.asyncio
async def test_registry_adapter_accepts_reviewed_fact_keys_not_just_description_grammar() -> None:
    item = http_method_registry_candidate()
    item = replace(
        item,
        fact=FactRequirement(
            "http_get_idempotent",
            FactType.BACKGROUND,
            "HTTP method property required by the request",
            "HTTP GET",
        ),
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verifier_version == "deterministic-registry-table-v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    (
        (
            "https://www.iana.org/assignments/service-names-port-numbers/"
            "service-names-port-numbers.csv"
        ),
        (
            "https://www.iana.org/assignments/"
            "service-names-port-numbers?search=53"
        ),
    ),
)
async def test_dns_registry_rows_are_a_reviewed_deterministic_source(
    url: str,
) -> None:
    text = (
        "domain,53,tcp,Domain Name Server,,,,,,,,\n"
        "domain,53,udp,Domain Name Server,,,,,,,,"
    )
    fact = FactRequirement(
        "dns_port",
        FactType.BACKGROUND,
        "DNS conventional port number",
        "DNS port",
    )
    item = reviewed_text_candidate(
        text,
        fact,
        url,
    )
    item = replace(
        item,
        source_identity="iana.org",
        source_authority=SourceAuthority.OFFICIAL,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verifier_version == "deterministic-dns-registry-v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fact", "text", "expected_transport"),
    (
        (
            FactRequirement(
                "dns_port",
                FactType.BACKGROUND,
                "DNS conventional port number",
                "DNS port",
            ),
            "domain,53,tcp,Domain Name Server,,,,,,,,",
            "tcp",
        ),
        (
            FactRequirement(
                "dns_tcp",
                FactType.BACKGROUND,
                "DNS use of TCP transport",
                "DNS TCP",
            ),
            "domain,53,tcp,Domain Name Server,,,,,,,,",
            "tcp",
        ),
        (
            FactRequirement(
                "dns_udp",
                FactType.BACKGROUND,
                "DNS use of UDP transport",
                "DNS UDP",
            ),
            "domain,53,udp,Domain Name Server,,,,,,,,",
            "udp",
        ),
    ),
)
async def test_dns_registry_validates_one_reviewed_row_per_fact(
    fact: FactRequirement,
    text: str,
    expected_transport: str,
) -> None:
    item = reviewed_text_candidate(
        text,
        fact,
        (
            "https://www.iana.org/assignments/service-names-port-numbers/"
            "service-names-port-numbers.csv"
        ),
    )
    item = replace(
        item,
        source_identity="iana.org",
        source_authority=SourceAuthority.OFFICIAL,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].verifier_version == "deterministic-dns-registry-v1"
    assert expected_transport in result.evidence[0].candidate.quote.casefold()


def test_dns_combined_transport_fact_requires_both_reviewed_rows() -> None:
    fact = FactRequirement(
        "dns_transport_protocols",
        FactType.BACKGROUND,
        "DNS use of both TCP and UDP transport protocols",
        "DNS transports",
    )
    item = reviewed_text_candidate(
        "domain,53,tcp,Domain Name Server,,,,,,,,",
        fact,
        (
            "https://www.iana.org/assignments/service-names-port-numbers/"
            "service-names-port-numbers.csv"
        ),
    )
    item = replace(
        item,
        source_identity="iana.org",
        source_authority=SourceAuthority.OFFICIAL,
    )

    assert ModelEvidenceVerifier._deterministic_dns_registry(item) is None


@pytest.mark.asyncio
async def test_git_file_state_uses_reviewed_official_definition() -> None:
    text = (
        "Modified means that you have changed the file but have not committed "
        "it to your database yet."
    )
    fact = FactRequirement(
        "git_state_modified",
        FactType.BACKGROUND,
        "Git modified state and its relationship to the working tree",
        "Git modified working tree",
    )
    item = reviewed_text_candidate(
        text,
        fact,
        "https://git-scm.com/book/en/v2/Getting-Started-What-is-Git%3F",
    )
    item = replace(
        item,
        source_identity="git-scm.com",
        source_authority=SourceAuthority.OFFICIAL,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verifier_version == "deterministic-git-state-v1"


@pytest.mark.asyncio
async def test_reviewed_media_registry_row_skips_model_verification() -> None:
    item = media_type_registry_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert "Type name: application" in result.evidence[0].candidate.quote
    assert "Subtype name: json" in result.evidence[0].candidate.quote
    assert result.evidence[0].verifier_version == "deterministic-registry-media-v1"


@pytest.mark.asyncio
async def test_reviewed_sha_digest_statement_skips_model_verification() -> None:
    item = sha_digest_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert "SHA-256 algorithms produce" in result.evidence[0].candidate.quote
    assert "256-bit" in result.evidence[0].candidate.quote
    assert result.evidence[0].verifier_version == "deterministic-sha-digest-v1"


@pytest.mark.asyncio
async def test_reviewed_rfc_header_skips_model_verification() -> None:
    item = tls_rfc_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert "Comments: 8446" in result.evidence[0].candidate.quote
    assert "TLS) Protocol Version 1.3" in result.evidence[0].candidate.quote
    assert result.evidence[0].verifier_version == "deterministic-rfc-title-v1"


@pytest.mark.asyncio
async def test_reviewed_rfc3339_can_bind_two_exact_premises_for_derived_example() -> None:
    fact_id = uuid4()
    fact = FactRequirement(
        "rfc3339_example_plus0000",
        FactType.BACKGROUND,
        "Provide an example of an RFC 3339 timestamp using '+00:00' for UTC.",
        "RFC 3339 '+00:00' example",
    )
    url = "https://www.rfc-editor.org/rfc/rfc3339.txt"
    equivalence = reviewed_text_candidate(
        'This differs\nsemantically from an offset of "Z" or "+00:00", '
        "which imply that UTC\nis the preferred reference point for the specified time.",
        fact,
        url,
        fact_id=fact_id,
    )
    example = reviewed_text_candidate(
        "5.8. Examples\n1985-04-12T23:20:50.52Z",
        fact,
        url,
        fact_id=fact_id,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (equivalence, example),
        invocation_context=context(equivalence),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    accepted = [
        item for item in result.evidence if item.verdict is EvidenceVerdict.ACCEPTED
    ]
    assert result.degraded is False
    assert len(accepted) == 2
    assert {item.verifier_version for item in accepted} == {
        "deterministic-rfc3339-utc-v1"
    }


@pytest.mark.asyncio
async def test_reviewed_postgresql_table_is_narrowed_to_requested_level() -> None:
    table = (
        "Isolation Level Dirty Read Nonrepeatable Read Phantom Read "
        "Serialization Anomaly "
        "Read uncommitted Allowed, but not in PG Possible Possible Possible "
        "Read committed Not possible Possible Possible Possible "
        "Repeatable read Not possible Not possible Allowed, but not in PG Possible "
        "Serializable Not possible Not possible Not possible Not possible"
    )
    fact = FactRequirement(
        "isolation_level_repeatable_read",
        FactType.COMPARISON,
        "PostgreSQL anomaly guarantees for Repeatable Read isolation",
        "Repeatable Read",
    )
    item = reviewed_text_candidate(
        table,
        fact,
        "https://www.postgresql.org/docs/current/transaction-iso.html",
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].candidate.quote.startswith("Repeatable read")
    assert "Read uncommitted" not in result.evidence[0].candidate.quote
    assert (
        result.evidence[0].verifier_version
        == "deterministic-postgresql-isolation-v1"
    )


def test_registry_verification_is_bound_to_exact_reviewed_page() -> None:
    item = replace(
        http_method_registry_candidate(),
        url="https://www.iana.org/assignments/http-methods/unreviewed.xhtml",
    )

    assert ModelEvidenceVerifier._deterministic_registry_boolean(item) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "description", "text", "expected"),
    (
        (
            "git_object_types",
            "What are the four types of objects in Git's object model?",
            "There are 4 types of objects: commits, trees, blobs, and tag objects.",
            "commits, trees, blobs, and tag objects",
        ),
        (
            "blob_purpose",
            "What is the purpose of the blob object in Git?",
            "A blob object contains a file's contents.",
            "file's contents",
        ),
        (
            "tree_purpose",
            "What is the purpose of the tree object in Git?",
            "A tree is how Git represents a directory. It can contain files or "
            "other trees (which are subdirectories).",
            "represents a directory",
        ),
    ),
)
async def test_reviewed_git_data_model_skips_model_verification(
    key: str,
    description: str,
    text: str,
    expected: str,
) -> None:
    item = git_object_candidate(
        text,
        key=key,
        description=description,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert expected in result.evidence[0].candidate.quote
    assert result.evidence[0].verifier_version == "deterministic-git-object-v1"


@pytest.mark.asyncio
async def test_private_future_weights_cannot_be_inferred_from_open_weight_models() -> None:
    item = reviewed_text_candidate(
        "Open-weight models under a permissive Apache 2.0 license.",
        FactRequirement(
            "openai_next_model_private_weights_public_availability",
            FactType.CURRENT_VALUE,
            "Whether the exact private parameter weights of the next unreleased "
            "OpenAI model are publicly available",
            "next unreleased OpenAI model",
        ),
        "https://developers.openai.com/api/docs/models/all",
    )
    gateway = ParsingGateway(
        '{"verdicts":[{'
        f'"fact_id":"{item.fact_id}","candidate_id":"{item.id}",'
        '"support_type":"CONTRADICTS","quote":"Open-weight models under a '
        'permissive Apache 2.0 license.","confidence":0.9,'
        '"reason_codes":["direct_contradiction"]}]}'
    )

    result = await ModelEvidenceVerifier(gateway).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED


@pytest.mark.asyncio
async def test_source_set_evidence_gap_is_rejected_without_calling_a_model() -> None:
    item = reviewed_text_candidate(
        "The public model catalog lists currently available models.",
        FactRequirement(
            "openai_unreleased_weights_official_evidence_gap",
            FactType.CURRENT_VALUE,
            "No official public source discloses exact private unreleased weights",
            "OpenAI public model catalog",
        ),
        "https://developers.openai.com/api/docs/models/all",
    )
    item = replace(
        item,
        source_identity="openai.com",
        source_authority=SourceAuthority.OFFICIAL,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED
    assert result.evidence[0].verifier_version == "deterministic-audit-v1"


@pytest.mark.asyncio
async def test_stale_reviewed_current_page_is_rejected_without_model_override() -> None:
    item = reviewed_text_candidate(
        (
            "Apex Legends™: Old Patch Notes January 1, 2025 INTRO "
            "Welcome back to an old season!"
        ),
        FactRequirement(
            "current_season_or_release",
            FactType.VERSION,
            "Current official Apex Legends season or game release",
            "Apex Legends current official release",
        ),
        (
            "https://www.ea.com/games/apex-legends/apex-legends/news/"
            "overclocked-midseason-patch-notes"
        ),
    )
    item = replace(
        item,
        source_identity="ea.com",
        source_authority=SourceAuthority.OFFICIAL,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED
    assert result.evidence[0].verifier_version == "deterministic-audit-v1"


def test_deterministic_mode_fails_closed_for_lexical_overlap() -> None:
    item = candidate()

    result = ModelEvidenceVerifier.deterministic(
        (item,),
        run_id=uuid4(),
        verified_at=NOW,
    )

    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED


@pytest.mark.asyncio
async def test_incomplete_official_term_list_still_uses_model() -> None:
    item = explicit_json_terms_candidate()
    text = "A JSON value can be the literal true."
    item = replace(
        item,
        version=replace(
            item.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            8,
            0,
            len(text),
        ),
        quote=text,
    )

    result = await ModelEvidenceVerifier(
        ParsingGateway('{"verdicts":[]}')
    ).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED


@pytest.mark.asyncio
async def test_numeric_mention_without_adjacent_value_still_uses_model() -> None:
    item = explicit_http_candidate()
    text = "Heuristically cacheable codes include 404, 405, and 410."
    version = replace(
        item.version,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    item = replace(
        item,
        version=version,
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            8,
            0,
            len(text),
        ),
        quote=text,
    )

    result = await ModelEvidenceVerifier(
        ParsingGateway('{"verdicts":[]}')
    ).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED

    assert len(result.evidence) == 1
    assert result.evidence[0].candidate.id == item.id
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED
    assert result.evidence[0].reason_codes == (
        "exact_source_span",
        "insufficient_direct_support",
    )
