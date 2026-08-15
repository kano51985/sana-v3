from __future__ import annotations

from copy import deepcopy
import json

import pytest

from sana.app.shadow_provenance import parse_shadow_attestation_bytes
from sana.modules.shadow_campaign.domain import snapshot_hash
from sana.modules.shared.errors import InvariantViolation


IMAGE = f"sha256:{'d' * 64}"
SERVICES = (
    "migrate",
    "artifact-init",
    "api",
    "dispatcher",
    "worker",
    "campaign-runner",
)
VOLUMES = (
    "sana-shadow-eval-postgres",
    "sana-shadow-eval-redis",
    "sana-shadow-eval-search-artifacts",
    "sana-shadow-eval-campaign-reports",
)


def _attestation() -> dict[str, object]:
    resource_limits = {
        "api": {"cpus": "1.0", "memory": "512m"},
        "worker": {"cpus": "2.0", "memory": "2g"},
        "campaign-runner": {"cpus": "1.0", "memory": "512m"},
    }
    images = {service: IMAGE for service in SERVICES}
    volume_ids = {name: f"volume-{index}" for index, name in enumerate(VOLUMES, 1)}
    topology_source = {
        "execution_class": "LIVE_DEEPSEEK",
        "container_images": images,
        "network": "sana-shadow-eval-net",
        "network_id": "network-identity",
        "volume_ids": volume_ids,
        "api_loopback": "127.0.0.1",
        "database_published": False,
        "redis_published": False,
        "worker_concurrency": 2,
        "queues": ["crawl", "fast", "maintenance", "research"],
        "resource_limits": resource_limits,
        "docker_socket_mounted": False,
    }
    return {
        "schema_version": "shadow-provenance-v2",
        "candidate": {
            "commit_sha": "a" * 40,
            "source_clean": True,
            "image_id": IMAGE,
            "oci_revision": "a" * 40,
            "alembic_head": "0011_document_fetch_lineage",
            "config_hash": "c" * 64,
        },
        "harness": {
            "commit_sha": "b" * 40,
            "source_clean": True,
            "fileset_hash": "e" * 64,
            "collector_schema_version": "shadow-collector-v2",
        },
        "environment": {
            "compose_project": "sana-shadow-eval",
            "execution_class": "LIVE_DEEPSEEK",
            "container_images": images,
            "network": "sana-shadow-eval-net",
            "network_id": "network-identity",
            "volume_ids": volume_ids,
            "api_loopback": "127.0.0.1",
            "database_published": False,
            "redis_published": False,
            "worker_concurrency": 2,
            "queues": ["fast", "research", "crawl", "maintenance"],
            "resource_limits": resource_limits,
            "docker_socket_mounted": False,
            "initial_queue_depth": 0,
            "active_non_campaign_runs": 0,
            "pending_outbox": 0,
            "migration_head": "0011_document_fetch_lineage",
            "config_hash": "c" * 64,
            "topology_hash": snapshot_hash(topology_source),
        },
    }


def _encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_sanitized_attestation_binds_source_image_config_and_topology() -> None:
    attestation = parse_shadow_attestation_bytes(_encode(_attestation()))

    assert attestation.provenance.candidate_commit_sha == "a" * 40
    assert attestation.provenance.candidate_oci_revision == "a" * 40
    assert attestation.provenance.candidate_image_id == IMAGE
    assert attestation.provenance.environment_identity_hash == snapshot_hash(
        attestation.environment_snapshot
    )
    assert len(attestation.attestation_hash) == 64


@pytest.mark.parametrize(
    ("path", "value", "code"),
    (
        (("candidate", "oci_revision"), "f" * 40, "provenance_oci_revision_mismatch"),
        (("candidate", "source_clean"), False, "provenance_source_dirty"),
        (
            ("environment", "api_loopback"),
            "0.0.0.0",
            "provenance_endpoint_exposure",
        ),
        (
            ("environment", "initial_queue_depth"),
            1,
            "provenance_environment_not_empty",
        ),
        (
            ("environment", "docker_socket_mounted"),
            True,
            "provenance_docker_socket",
        ),
        (
            ("environment", "execution_class"),
            "UNMARKED_TEST",
            "provenance_execution_class_mismatch",
        ),
    ),
)
def test_unsafe_provenance_is_rejected(path, value, code) -> None:
    payload = deepcopy(_attestation())
    payload[path[0]][path[1]] = value  # type: ignore[index]

    with pytest.raises(InvariantViolation) as captured:
        parse_shadow_attestation_bytes(_encode(payload))

    assert captured.value.code == code


def test_attestation_rejects_secret_fields_before_persistence() -> None:
    payload = deepcopy(_attestation())
    payload["candidate"]["access_token"] = "private"  # type: ignore[index]

    with pytest.raises(InvariantViolation) as captured:
        parse_shadow_attestation_bytes(_encode(payload))

    assert captured.value.code == "provenance_secret_field"
