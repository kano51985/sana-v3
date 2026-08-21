from pathlib import Path

import yaml


def test_worker_compose_exposes_document_reuse_policy_and_rollback_switch() -> None:
    compose = yaml.safe_load(
        Path("deployment/docker-compose.yml").read_text(encoding="utf-8")
    )
    worker = compose["services"]["worker"]["environment"]

    assert {
        key: worker[key]
        for key in (
            "SANA_WORKER_DOCUMENT_REUSE_ENABLED",
            "SANA_WORKER_DOCUMENT_REUSE_POLICY_VERSION",
            "SANA_WORKER_REUSE_STABLE_FRESH_SECONDS",
            "SANA_WORKER_REUSE_STABLE_FALLBACK_SECONDS",
            "SANA_WORKER_REUSE_RECENT_FRESH_SECONDS",
            "SANA_WORKER_REUSE_RECENT_FALLBACK_SECONDS",
            "SANA_WORKER_REUSE_CURRENT_FRESH_SECONDS",
            "SANA_WORKER_REUSE_CURRENT_FALLBACK_SECONDS",
        )
    } == {
        "SANA_WORKER_DOCUMENT_REUSE_ENABLED": (
            "${SANA_WORKER_DOCUMENT_REUSE_ENABLED:-true}"
        ),
        "SANA_WORKER_DOCUMENT_REUSE_POLICY_VERSION": (
            "${SANA_WORKER_DOCUMENT_REUSE_POLICY_VERSION:-document-reuse-v1}"
        ),
        "SANA_WORKER_REUSE_STABLE_FRESH_SECONDS": (
            "${SANA_WORKER_REUSE_STABLE_FRESH_SECONDS:-86400}"
        ),
        "SANA_WORKER_REUSE_STABLE_FALLBACK_SECONDS": (
            "${SANA_WORKER_REUSE_STABLE_FALLBACK_SECONDS:-2592000}"
        ),
        "SANA_WORKER_REUSE_RECENT_FRESH_SECONDS": (
            "${SANA_WORKER_REUSE_RECENT_FRESH_SECONDS:-21600}"
        ),
        "SANA_WORKER_REUSE_RECENT_FALLBACK_SECONDS": (
            "${SANA_WORKER_REUSE_RECENT_FALLBACK_SECONDS:-604800}"
        ),
        "SANA_WORKER_REUSE_CURRENT_FRESH_SECONDS": (
            "${SANA_WORKER_REUSE_CURRENT_FRESH_SECONDS:-900}"
        ),
        "SANA_WORKER_REUSE_CURRENT_FALLBACK_SECONDS": (
            "${SANA_WORKER_REUSE_CURRENT_FALLBACK_SECONDS:-7200}"
        ),
    }
