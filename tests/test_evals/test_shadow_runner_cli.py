from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sana.app.shadow_api_client import ShadowAPIError, ShadowCandidateAPI
from sana.app.shadow_runner import InteractiveShadowReview
from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.review import (
    ReviewCitationProjection,
    ReviewClaimProjection,
    ReviewProjection,
)
from sana.modules.shadow_campaign.runner import CampaignReviewCandidate


PATH = Path(__file__).parents[2] / "scripts" / "run_shadow_campaign.py"
SPEC = importlib.util.spec_from_file_location("run_shadow_campaign", PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


def test_create_without_live_confirmation_has_zero_external_side_effects() -> None:
    calls: list[str] = []

    def client_factory(*args, **kwargs):
        calls.append("client")
        raise AssertionError("HTTP client must not be created")

    def token_prompt(prompt: str) -> str:
        calls.append("token")
        raise AssertionError("Token must not be requested")

    code = RUNNER.main(
        [
            "create",
            "--api-url",
            "http://candidate:8000",
            "--campaign-key",
            "safe-key",
            "--manifest",
            "cases.jsonl",
            "--profile",
            "docker-smoke-v1",
        ],
        client_factory=client_factory,
        token_prompt=token_prompt,
        environ={},
    )

    assert code == 2
    assert calls == []


def test_cli_has_no_token_argument() -> None:
    parser = RUNNER._parser()

    try:
        parser.parse_args(
            [
                "list",
                "--api-url",
                "http://candidate:8000",
                "--token",
                "must-not-be-supported",
            ]
        )
    except SystemExit as error:
        assert error.code == 2
    else:  # pragma: no cover
        raise AssertionError("CLI unexpectedly accepted a token argument")


def test_noninteractive_cli_refuses_when_environment_token_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    code = RUNNER.main(
        ["list", "--api-url", "http://candidate:8000"],
        environ={},
    )

    assert code == 1


@pytest.mark.asyncio
async def test_api_client_retries_transient_failures_with_bounded_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            return httpx.Response(503, json={"secret": "must-not-surface"})
        return httpx.Response(
            200,
            json={
                "tenant_id": "11111111-1111-1111-1111-111111111111",
                "user_id": "22222222-2222-2222-2222-222222222222",
                "issuer": "test",
                "subject": "owner",
            },
        )

    async def sleeper(value: float) -> None:
        delays.append(value)

    async with ShadowCandidateAPI(
        "http://candidate:8000",
        "local-secret-token",
        transport=httpx.MockTransport(handler),
        sleeper=sleeper,
        random_source=lambda: 0.5,
    ) as api:
        identity = await api.authenticate()

    assert identity.subject == "owner"
    assert attempts == 4
    assert delays == [0.5, 1.0, 2.0]


@pytest.mark.asyncio
async def test_api_client_never_retries_auth_and_never_exposes_body_or_token() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            401,
            json={"token": "local-secret-token", "answer": "private"},
        )

    async with ShadowCandidateAPI(
        "http://candidate:8000",
        "local-secret-token",
        transport=httpx.MockTransport(handler),
    ) as api:
        with pytest.raises(ShadowAPIError) as captured:
            await api.authenticate()

    rendered = f"{captured.value!r} {captured.value}"
    assert attempts == 1
    assert captured.value.code == "authentication_rejected"
    assert "local-secret-token" not in rendered
    assert "private" not in rendered


def test_lost_create_response_is_discoverable_and_resumable(
    monkeypatch,
    capsys,
) -> None:
    tenant_id, user_id, campaign_id = uuid4(), uuid4(), uuid4()
    events: list[str] = []
    campaigns: dict[str, str] = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def authenticate(self):
            events.append("authenticate")
            return SimpleNamespace(
                tenant_id=tenant_id,
                user_id=user_id,
                issuer="test",
                subject="owner",
            )

    class StatefulRunner:
        async def create(self, principal, command):
            del principal, command
            campaigns[str(campaign_id)] = "RUNNING"
            raise RuntimeError("simulated response channel loss")

        async def list(self, principal):
            del principal
            return ({"id": campaign_id, "status": campaigns[str(campaign_id)]},)

        async def resume(self, principal, selected, manifest):
            del principal, manifest
            assert selected == campaign_id
            campaigns[str(campaign_id)] = "COMPLETED"
            return {"campaign_id": selected, "status": "COMPLETED"}

    runner = StatefulRunner()

    def client_factory(api_url, token):
        events.append("client")
        assert api_url == "http://candidate:8000"
        assert token == "local-token"
        return Client()

    @asynccontextmanager
    async def runtime_factory(principal, api, args):
        del api, args
        events.append("runtime")
        assert principal.tenant_id == tenant_id
        yield SimpleNamespace(
            runner=runner,
            create_command=lambda principal, args, manifest: object(),
        )

    monkeypatch.setattr(RUNNER, "_load_manifest", lambda path: object())
    common = ["--api-url", "http://candidate:8000"]
    create_code = RUNNER.main(
        [
            "create",
            *common,
            "--confirm-live",
            "--campaign-key",
            "stable-key",
            "--manifest",
            "cases.jsonl",
            "--profile",
            "docker-smoke-v1",
        ],
        client_factory=client_factory,
        runtime_factory=runtime_factory,
        environ={"SANA_ACCESS_TOKEN": "local-token"},
    )
    assert create_code == 1
    assert campaigns[str(campaign_id)] == "RUNNING"

    list_code = RUNNER.main(
        ["list", *common],
        client_factory=client_factory,
        runtime_factory=runtime_factory,
        environ={"SANA_ACCESS_TOKEN": "local-token"},
    )
    assert list_code == 0
    assert str(campaign_id) in capsys.readouterr().out

    resume_code = RUNNER.main(
        [
            "resume",
            *common,
            "--campaign-id",
            str(campaign_id),
            "--manifest",
            "cases.jsonl",
        ],
        client_factory=client_factory,
        runtime_factory=runtime_factory,
        environ={"SANA_ACCESS_TOKEN": "local-token"},
    )
    assert resume_code == 0
    assert campaigns[str(campaign_id)] == "COMPLETED"
    assert events == [
        "client",
        "authenticate",
        "runtime",
        "client",
        "authenticate",
        "runtime",
        "client",
        "authenticate",
        "runtime",
    ]


@pytest.mark.asyncio
async def test_review_shows_ephemeral_material_but_persists_only_structure() -> None:
    tenant_id, user_id, campaign_id = uuid4(), uuid4(), uuid4()
    human_result, missing_result = uuid4(), uuid4()
    human_candidate = CampaignReviewCandidate(
        human_result,
        uuid4(),
        uuid4(),
        "case-human",
        1,
        "answerable",
        "COMPLETE",
        "rubric-v1",
        False,
    )
    missing_candidate = CampaignReviewCandidate(
        missing_result,
        uuid4(),
        uuid4(),
        "case-missing",
        1,
        "answerable",
        "NONE",
        "rubric-v1",
        False,
    )

    class CampaignRunner:
        async def review_candidates(self, principal, selected_campaign):
            del principal
            assert selected_campaign == campaign_id
            return (human_candidate, missing_candidate)

    claim_id, fact_id = uuid4(), uuid4()
    projection = ReviewProjection(
        tenant_id,
        campaign_id,
        human_result,
        human_candidate.conversation_id,
        human_candidate.search_run_id,
        human_candidate.case_id,
        1,
        "rubric-v1",
        (
            ReviewClaimProjection(
                claim_id,
                "FACTUAL",
                fact_id,
                "VERIFIED",
                (
                    ReviewCitationProjection(
                        uuid4(),
                        claim_id,
                        uuid4(),
                        fact_id,
                        uuid4(),
                        uuid4(),
                        1,
                        "ACCEPTED",
                        1.0,
                        "OFFICIAL",
                        datetime(2026, 8, 15, tzinfo=UTC),
                        0,
                        6,
                        "[1]",
                        "https://example.test/private?query=ephemeral",
                        "quoted evidence",
                    ),
                ),
                "private claim",
            ),
        ),
        "private answer",
    )

    class Reviews:
        def __init__(self) -> None:
            self.human = []
            self.system = []

        async def projection(self, principal, selected_campaign, result_id):
            del principal
            assert selected_campaign == campaign_id and result_id == human_result
            return projection

        async def submit_human(self, principal, submission):
            del principal
            self.human.append(submission)

        async def record_system(self, submission):
            self.system.append(submission)

    answers = iter(("correct", "pass", "pass", "pass", "pass", ""))
    rendered: list[str] = []
    reviews = Reviews()
    coordinator = InteractiveShadowReview(
        CampaignRunner(),  # type: ignore[arg-type]
        reviews,  # type: ignore[arg-type]
        reader=lambda prompt: next(answers),
        writer=rendered.append,
    )
    receipt = await coordinator.review(
        Principal(tenant_id, user_id, "test", "owner"),
        campaign_id,
    )

    assert receipt.human_reviewed == 1 and receipt.system_reviewed == 1
    assert "private answer" in "\n".join(rendered)
    assert "private claim" in "\n".join(rendered)
    assert len(reviews.human) == 1 and len(reviews.system) == 1
    assert not hasattr(reviews.human[0], "answer_text")
    assert reviews.system[0].reason_codes == ("expected_answer_missing",)
