"""Strict sanitized provenance attestation for isolated Shadow Campaigns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
import json
from types import MappingProxyType
from typing import Any

from sana.modules.shadow_campaign.domain import canonical_snapshot, snapshot_hash
from sana.modules.shadow_campaign.service import CampaignProvenance
from sana.modules.shared.errors import InvariantViolation


ATTESTATION_SCHEMA_VERSION = "shadow-provenance-v1"
REQUIRED_IMAGE_SERVICES = frozenset(
    {"migrate", "artifact-init", "api", "dispatcher", "worker", "campaign-runner"}
)
EXPECTED_QUEUES = frozenset({"fast", "research", "crawl", "maintenance"})
EXPECTED_VOLUMES = frozenset(
    {
        "sana-shadow-eval-postgres",
        "sana-shadow-eval-redis",
        "sana-shadow-eval-search-artifacts",
        "sana-shadow-eval-campaign-reports",
    }
)
_ROOT_KEYS = frozenset({"schema_version", "candidate", "harness", "environment"})
_CANDIDATE_KEYS = frozenset(
    {
        "commit_sha",
        "source_clean",
        "image_id",
        "oci_revision",
        "alembic_head",
        "config_hash",
    }
)
_HARNESS_KEYS = frozenset(
    {"commit_sha", "source_clean", "fileset_hash", "collector_schema_version"}
)
_ENVIRONMENT_KEYS = frozenset(
    {
        "compose_project",
        "container_images",
        "network",
        "network_id",
        "volume_ids",
        "api_loopback",
        "database_published",
        "redis_published",
        "worker_concurrency",
        "queues",
        "resource_limits",
        "docker_socket_mounted",
        "initial_queue_depth",
        "active_non_campaign_runs",
        "pending_outbox",
        "migration_head",
        "config_hash",
        "topology_hash",
    }
)
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"Duplicate attestation key: {key}")
        result[key] = value
    return result


def _mapping(value: object, expected: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} must contain exactly the approved fields")
    return value


def _hex(value: object, lengths: tuple[int, ...], name: str) -> str:
    rendered = str(value).strip().lower()
    if len(rendered) not in lengths or any(
        character not in "0123456789abcdef" for character in rendered
    ):
        raise ValueError(f"{name} is not a valid hexadecimal identity")
    return rendered


def _text(value: object, maximum: int, name: str) -> str:
    rendered = str(value).strip()
    if not rendered or len(rendered) > maximum:
        raise ValueError(f"{name} is empty or too long")
    return rendered


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _assert_sanitized(value: object, path: str = "attestation") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise InvariantViolation(
                    "Provenance attestation contains a forbidden key",
                    code="provenance_secret_field",
                    details={"path": path},
                )
            _assert_sanitized(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_sanitized(item, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if "bearer " in lowered or "://" in lowered and "@" in lowered:
            raise InvariantViolation(
                "Provenance attestation contains credential-like text",
                code="provenance_secret_value",
                details={"path": path},
            )


@dataclass(frozen=True, slots=True)
class ShadowProvenanceAttestation:
    provenance: CampaignProvenance
    attestation_hash: str
    environment_snapshot: Mapping[str, Any]


def parse_shadow_attestation_bytes(payload: bytes) -> ShadowProvenanceAttestation:
    if not payload or len(payload) > 1_000_000:
        raise ValueError("Provenance attestation size is invalid")
    try:
        root = json.loads(payload, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Provenance attestation is not valid JSON") from error
    root = _mapping(root, _ROOT_KEYS, "attestation")
    _assert_sanitized(root)
    if root["schema_version"] != ATTESTATION_SCHEMA_VERSION:
        raise InvariantViolation(
            "Provenance attestation schema is unsupported",
            code="provenance_schema_mismatch",
        )
    candidate = _mapping(root["candidate"], _CANDIDATE_KEYS, "candidate")
    harness = _mapping(root["harness"], _HARNESS_KEYS, "harness")
    environment = _mapping(
        root["environment"],
        _ENVIRONMENT_KEYS,
        "environment",
    )
    candidate_commit = _hex(candidate["commit_sha"], (40, 64), "candidate commit")
    harness_commit = _hex(harness["commit_sha"], (40, 64), "harness commit")
    oci_revision = _hex(candidate["oci_revision"], (40, 64), "OCI revision")
    if oci_revision != candidate_commit:
        raise InvariantViolation(
            "OCI revision does not match the candidate commit",
            code="provenance_oci_revision_mismatch",
        )
    if candidate["source_clean"] is not True or harness["source_clean"] is not True:
        raise InvariantViolation(
            "Shadow Campaign provenance requires clean source trees",
            code="provenance_source_dirty",
        )
    image_id = _text(candidate["image_id"], 200, "candidate image ID")
    image_digest = image_id.rsplit("sha256:", 1)[-1]
    _hex(image_digest, (64,), "candidate image digest")
    config_hash = _hex(candidate["config_hash"], (64,), "candidate config hash")
    if config_hash != _hex(environment["config_hash"], (64,), "environment config hash"):
        raise InvariantViolation(
            "Candidate and environment config identities differ",
            code="provenance_config_mismatch",
        )
    if environment["compose_project"] != "sana-shadow-eval":
        raise InvariantViolation(
            "Compose project is not the dedicated Shadow evaluation project",
            code="provenance_project_mismatch",
        )
    images = environment["container_images"]
    if not isinstance(images, Mapping) or set(images) != REQUIRED_IMAGE_SERVICES:
        raise InvariantViolation(
            "Provenance service image set is incomplete",
            code="provenance_image_set_mismatch",
        )
    if any(value != image_id for value in images.values()):
        raise InvariantViolation(
            "Shadow services do not use one immutable candidate image",
            code="provenance_image_mismatch",
        )
    if environment["network"] != "sana-shadow-eval-net" or not _text(
        environment["network_id"],
        200,
        "network ID",
    ):
        raise InvariantViolation(
            "Shadow network identity is not isolated",
            code="provenance_network_mismatch",
        )
    volume_ids = environment["volume_ids"]
    if not isinstance(volume_ids, Mapping) or set(volume_ids) != EXPECTED_VOLUMES:
        raise InvariantViolation(
            "Shadow volume identity set is incomplete",
            code="provenance_volume_mismatch",
        )
    if any(not _text(value, 200, "volume ID") for value in volume_ids.values()):
        raise ValueError("Shadow volume ID is invalid")
    try:
        loopback = ipaddress.ip_address(str(environment["api_loopback"]))
    except ValueError as error:
        raise ValueError("API bind address is invalid") from error
    if not loopback.is_loopback or environment["database_published"] is not False or environment[
        "redis_published"
    ] is not False:
        raise InvariantViolation(
            "Shadow service publication boundary is unsafe",
            code="provenance_endpoint_exposure",
        )
    if environment["worker_concurrency"] != 2:
        raise InvariantViolation(
            "Shadow worker concurrency must be exactly two",
            code="provenance_worker_concurrency",
        )
    queues = environment["queues"]
    if not isinstance(queues, list) or frozenset(queues) != EXPECTED_QUEUES:
        raise InvariantViolation(
            "Shadow queue set differs from the approved topology",
            code="provenance_queue_mismatch",
        )
    if environment["docker_socket_mounted"] is not False:
        raise InvariantViolation(
            "Campaign Runner must not receive the Docker socket",
            code="provenance_docker_socket",
        )
    for field_name in (
        "initial_queue_depth",
        "active_non_campaign_runs",
        "pending_outbox",
    ):
        if _nonnegative_int(environment[field_name], field_name) != 0:
            raise InvariantViolation(
                "Shadow environment is not initially quiescent",
                code="provenance_environment_not_empty",
                details={"field": field_name},
            )
    alembic_head = _text(candidate["alembic_head"], 100, "Alembic head")
    if environment["migration_head"] != alembic_head:
        raise InvariantViolation(
            "Running migration head differs from candidate provenance",
            code="provenance_migration_mismatch",
        )
    resource_limits = environment["resource_limits"]
    if not isinstance(resource_limits, Mapping) or not resource_limits:
        raise InvariantViolation(
            "Shadow resource limits are missing",
            code="provenance_resource_limits_missing",
        )
    topology_source = {
        "container_images": dict(images),
        "network": environment["network"],
        "network_id": environment["network_id"],
        "volume_ids": dict(volume_ids),
        "api_loopback": str(loopback),
        "database_published": False,
        "redis_published": False,
        "worker_concurrency": 2,
        "queues": sorted(queues),
        "resource_limits": canonical_snapshot(resource_limits),
        "docker_socket_mounted": False,
    }
    topology_hash = _hex(environment["topology_hash"], (64,), "topology hash")
    if snapshot_hash(topology_source) != topology_hash:
        raise InvariantViolation(
            "Shadow topology digest does not match its sanitized inputs",
            code="provenance_topology_mismatch",
        )
    environment_snapshot = canonical_snapshot(
        {
            **dict(environment),
            "container_images": dict(images),
            "volume_ids": dict(volume_ids),
            "queues": sorted(queues),
            "api_loopback": str(loopback),
        }
    )
    provenance = CampaignProvenance(
        candidate_commit,
        True,
        image_id,
        oci_revision,
        alembic_head,
        config_hash,
        harness_commit,
        True,
        _hex(harness["fileset_hash"], (64,), "harness fileset hash"),
        _text(
            harness["collector_schema_version"],
            100,
            "collector schema version",
        ),
        snapshot_hash(environment_snapshot),
        environment_snapshot,
    )
    return ShadowProvenanceAttestation(
        provenance,
        snapshot_hash(root),
        MappingProxyType(environment_snapshot),
    )


__all__ = [
    "ATTESTATION_SCHEMA_VERSION",
    "EXPECTED_QUEUES",
    "EXPECTED_VOLUMES",
    "REQUIRED_IMAGE_SERVICES",
    "ShadowProvenanceAttestation",
    "parse_shadow_attestation_bytes",
]
