from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sana.modules.shared.errors import TypedError
from sana.platform.db.content_snapshots import SqlContentSnapshotReader


NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _Session:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.row)


class _UnitOfWork:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _Factory:
    def __init__(self, session):
        self.session = session
        self.tenant_ids = []

    def __call__(self, tenant_id):
        self.tenant_ids.append(tenant_id)
        return _UnitOfWork(self.session)


def _row(*, tenant_id, run_id, digest="a" * 64, metadata=None):
    return SimpleNamespace(
        source_fetch_artifact_id=uuid4(),
        source_run_id=run_id,
        source_document_version_id=uuid4(),
        request_url="https://docs.example.test/source",
        http_status=200,
        media_type="text/html",
        content_hash=digest,
        storage_uri=f"artifact://{tenant_id}/{run_id}/{digest}",
        fetched_at=NOW,
        fetch_metadata=(
            {"redirects": ["https://www.example.test/final"]}
            if metadata is None
            else metadata
        ),
    )


@pytest.mark.asyncio
async def test_reader_maps_latest_live_snapshot_and_scopes_every_join() -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    session = _Session(_row(tenant_id=tenant_id, run_id=run_id))
    factory = _Factory(session)
    reader = SqlContentSnapshotReader(factory)

    snapshot = await reader.latest_for_url(tenant_id, "b" * 64)

    assert snapshot is not None
    assert snapshot.source_run_id == run_id
    assert snapshot.final_url == "https://www.example.test/final"
    assert snapshot.redirects == ("https://www.example.test/final",)
    assert snapshot.content_hash == "a" * 64
    assert factory.tenant_ids == [tenant_id]
    statement = str(session.statements[0])
    assert statement.count("tenant_id") >= 6
    assert "document_version_fetches" in statement
    assert "document_versions" in statement
    assert "fetch_artifacts.fetcher" in statement


@pytest.mark.asyncio
async def test_reader_returns_none_when_no_extracted_live_snapshot_exists() -> None:
    tenant_id = uuid4()
    reader = SqlContentSnapshotReader(_Factory(_Session(None)))

    assert await reader.latest_for_url(tenant_id, "b" * 64) is None


@pytest.mark.asyncio
async def test_reader_rejects_malformed_hash_or_cache_metadata() -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    reader = SqlContentSnapshotReader(
        _Factory(_Session(_row(tenant_id=tenant_id, run_id=run_id)))
    )
    with pytest.raises(ValueError, match="canonical URL hash"):
        await reader.latest_for_url(tenant_id, "not-a-hash")

    bad_metadata_reader = SqlContentSnapshotReader(
        _Factory(
            _Session(
                _row(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    metadata={"redirects": "not-a-list"},
                )
            )
        )
    )
    with pytest.raises(TypedError) as raised:
        await bad_metadata_reader.latest_for_url(tenant_id, "b" * 64)
    assert raised.value.code == "cache_metadata_invalid"


@pytest.mark.asyncio
async def test_reader_rejects_storage_identity_or_digest_mismatch() -> None:
    tenant_id = uuid4()
    run_id = uuid4()
    wrong_tenant = uuid4()
    row = _row(tenant_id=wrong_tenant, run_id=run_id)
    reader = SqlContentSnapshotReader(_Factory(_Session(row)))

    with pytest.raises(TypedError) as raised:
        await reader.latest_for_url(tenant_id, "b" * 64)
    assert raised.value.code == "cache_artifact_identity_invalid"

    row = _row(tenant_id=tenant_id, run_id=run_id)
    row.storage_uri = row.storage_uri.removesuffix("a" * 64) + "c" * 64
    reader = SqlContentSnapshotReader(_Factory(_Session(row)))
    with pytest.raises(TypedError) as raised:
        await reader.latest_for_url(tenant_id, "b" * 64)
    assert raised.value.code == "cache_artifact_identity_invalid"
