from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from sana.app.api.dependencies import (
    AppContainer,
    ConversationMessageView,
    ConversationView,
    EvidenceReportView,
    EventView,
    RunView,
)
from sana.app.api.main import create_app
from sana.modules.conversation.domain import SubmissionReceipt
from sana.modules.identity.domain import Principal
from sana.modules.orchestration.domain import RoutingDecision, SearchMode


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeAuthProvider:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    async def authenticate(self, bearer_token: str) -> Principal:
        return self.principal


class FakeRouter:
    def route(self, message: str) -> RoutingDecision:
        return RoutingDecision(SearchMode.FAST, ("test",), "search-v1", 1.0)


class FakeConversationService:
    def __init__(self) -> None:
        self.commands = []
        self.by_key = {}

    async def submit_message(self, command) -> SubmissionReceipt:
        self.commands.append(command)
        existing = self.by_key.get(command.idempotency_key)
        if existing is not None:
            return replace(existing, duplicate=True)
        receipt = SubmissionReceipt(uuid4(), uuid4(), uuid4(), "QUEUED")
        self.by_key[command.idempotency_key] = receipt
        return receipt


class FakeConversationCatalog:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.items: list[ConversationView] = []
        self.messages_by_id: dict = {}

    async def create(self, principal, title):
        assert principal == self.principal
        item = ConversationView(uuid4(), title or "新会话", "ACTIVE", NOW, NOW)
        self.items.insert(0, item)
        self.messages_by_id[item.id] = []
        return item

    async def list(self, principal):
        assert principal == self.principal
        return list(self.items)

    async def messages(self, principal, conversation_id):
        assert principal == self.principal
        return self.messages_by_id.get(conversation_id)


class FakeRunService:
    def __init__(self, run_id) -> None:
        self.run_id = run_id
        self.view = RunView(
            id=run_id,
            conversation_id=uuid4(),
            message_id=uuid4(),
            mode="FAST",
            routing_reason_codes=("single_or_low_complexity_fact",),
            route_confidence=0.9,
            policy_version="search-v1",
            status="RUNNING",
            answer_quality="NONE",
            stop_reason=None,
            soft_deadline_at=NOW + timedelta(seconds=12),
            hard_deadline_at=NOW + timedelta(seconds=15),
            created_at=NOW,
            started_at=NOW,
            completed_at=None,
        )

    async def get(self, principal, run_id):
        return self.view if run_id == self.run_id else None

    async def cancel(self, principal, run_id):
        if run_id != self.run_id:
            return None
        self.view = replace(
            self.view,
            status="CANCELLED",
            stop_reason="USER_CANCELLED",
            completed_at=NOW,
        )
        return self.view

    async def evidence(self, principal, run_id):
        return EvidenceReportView((), ()) if run_id == self.run_id else None


class FakeEventService:
    def __init__(self) -> None:
        self.after_sequences = []
        self.events = [
            EventView(6, "STEP_STARTED", {"step": "fetch"}, NOW),
            EventView(7, "RUN_COMPLETED", {"quality": "COMPLETE"}, NOW),
        ]

    async def subscribe(self, principal, run_id, after_sequence):
        self.after_sequences.append(after_sequence)
        for event in self.events:
            if event.sequence > after_sequence:
                yield event


@pytest_asyncio.fixture
async def api_context():
    principal = Principal(uuid4(), uuid4(), "test", "subject")
    run_id = uuid4()
    conversations = FakeConversationService()
    catalog = FakeConversationCatalog(principal)
    runs = FakeRunService(run_id)
    events = FakeEventService()
    container = AppContainer(
        auth_provider=FakeAuthProvider(principal),
        conversation_service=conversations,
        router=FakeRouter(),
        run_service=runs,
        event_service=events,
        conversation_catalog=catalog,
    )
    transport = ASGITransport(app=create_app(container=container))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            principal=principal,
            conversations=conversations,
            catalog=catalog,
            runs=runs,
            events=events,
            run_id=run_id,
            auth={"Authorization": "Bearer test"},
        )
