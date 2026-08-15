"""Import all mappings so Alembic sees complete metadata."""

from sana.platform.db.models.conversation import Conversation, Message, ResponseRun
from sana.platform.db.models.identity import Tenant, User, UserIdentity
from sana.platform.db.models.memory import (
    LegacyArchive,
    MemoryEmbedding,
    MemoryItem,
    MigrationLedger,
)
from sana.platform.db.models.model_gateway import ModelInvocationRecord
from sana.platform.db.models.orchestration import (
    OutboxEvent,
    RunEvent,
    SearchRunRecord,
    SearchStepRecord,
    StepAttemptRecord,
)
from sana.platform.db.models.search import (
    AnswerClaim,
    Citation,
    Document,
    DocumentChunk,
    DocumentVersion,
    EvidenceCandidate,
    FactRequirement,
    FetchArtifact,
    ProviderAttempt,
    QuerySpec,
    SearchHit,
    VerifiedEvidence,
)
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowManualReviewRecord,
    ShadowRunResultRecord,
)

__all__ = [
    "AnswerClaim",
    "Citation",
    "Conversation",
    "Document",
    "DocumentChunk",
    "DocumentVersion",
    "EvidenceCandidate",
    "FactRequirement",
    "FetchArtifact",
    "LegacyArchive",
    "MemoryEmbedding",
    "MemoryItem",
    "Message",
    "MigrationLedger",
    "ModelInvocationRecord",
    "OutboxEvent",
    "ProviderAttempt",
    "QuerySpec",
    "ResponseRun",
    "RunEvent",
    "SearchHit",
    "SearchRunRecord",
    "SearchStepRecord",
    "ShadowCampaignRecord",
    "ShadowManualReviewRecord",
    "ShadowRunResultRecord",
    "StepAttemptRecord",
    "Tenant",
    "User",
    "UserIdentity",
    "VerifiedEvidence",
]
