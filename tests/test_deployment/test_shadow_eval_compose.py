from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "deployment" / "docker-compose.shadow-eval.yml"
DOCKERFILE = ROOT / "deployment" / "Dockerfile"
LAUNCHER = ROOT / "scripts" / "run_shadow_campaign.ps1"
AUDITOR = ROOT / "scripts" / "audit_shadow_campaign.ps1"
FIXTURE_COMPOSE = ROOT / "deployment" / "docker-compose.shadow-fake.yml"


def _config() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_shadow_compose_has_one_fixed_isolated_topology() -> None:
    config = _config()

    assert config["name"] == "sana-shadow-eval"
    assert set(config["services"]) == {
        "postgres",
        "provision-db-role",
        "redis",
        "migrate",
        "artifact-init",
        "api",
        "dispatcher",
        "worker",
        "campaign-runner",
    }
    assert "streamlit" not in config["services"]
    assert config["networks"]["shadow-eval"]["name"] == "sana-shadow-eval-net"
    assert {item["name"] for item in config["volumes"].values()} == {
        "sana-shadow-eval-postgres",
        "sana-shadow-eval-redis",
        "sana-shadow-eval-search-artifacts",
        "sana-shadow-eval-campaign-reports",
    }
    for service in config["services"].values():
        assert service.get("networks") == ["shadow-eval"]


def test_only_api_is_published_and_only_on_loopback() -> None:
    services = _config()["services"]

    assert services["postgres"].get("ports") is None
    assert services["redis"].get("ports") is None
    assert services["api"]["ports"] == [
        "127.0.0.1:${SANA_SHADOW_API_PORT:-18000}:8000"
    ]
    assert all(
        service_name == "api" or service.get("ports") is None
        for service_name, service in services.items()
    )


def test_candidate_services_share_image_and_secrets_are_role_scoped() -> None:
    services = _config()["services"]
    candidate_names = {
        "migrate",
        "artifact-init",
        "api",
        "dispatcher",
        "worker",
        "campaign-runner",
    }
    images = {services[name]["image"] for name in candidate_names}

    assert images == {"${SANA_SHADOW_IMAGE:-sana-shadow-eval:local}"}
    assert services["worker"]["environment"]["DEEPSEEK_API_KEY"] == (
        "${DEEPSEEK_API_KEY:?set DEEPSEEK_API_KEY}"
    )
    assert services["campaign-runner"]["environment"]["SANA_ACCESS_TOKEN"] == (
        "${SANA_ACCESS_TOKEN:?set SANA_ACCESS_TOKEN}"
    )
    for name, service in services.items():
        if name != "worker":
            assert "DEEPSEEK_API_KEY" not in service.get("environment", {})
        if name != "campaign-runner":
            assert "SANA_ACCESS_TOKEN" not in service.get("environment", {})


def test_runner_has_read_only_attestation_and_no_docker_socket() -> None:
    config = _config()
    rendered = COMPOSE.read_text(encoding="utf-8").casefold()
    runner = config["services"]["campaign-runner"]

    assert runner["profiles"] == ["runner"]
    assert any(
        str(mount).endswith(":/run/sana/attestation.json:ro")
        for mount in runner["volumes"]
    )
    assert "docker.sock" not in rendered
    assert runner["read_only"] is True
    assert runner["security_opt"] == ["no-new-privileges:true"]


def test_worker_concurrency_and_image_revision_are_frozen() -> None:
    config = _config()
    services = config["services"]
    worker = services["worker"]
    worker_command = worker["command"]
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert worker_command[worker_command.index("--concurrency") + 1] == "2"
    worker_health = config["services"]["worker"]["healthcheck"]
    assert worker_health["test"][:3] == ["CMD", "python", "-c"]
    assert "socket.create_connection(('redis', 6379)" in worker_health["test"][3]
    assert "PING" in worker_health["test"][3]
    assert "celery" not in worker_health["test"][3]
    assert "CMD-SHELL" not in worker_health["test"]
    assert "SANA_WORKER_LIVE_EVAL_MAX_RUNS" not in worker["environment"]
    assert services["campaign-runner"]["depends_on"]["worker"] == {
        "condition": "service_healthy"
    }
    assert "ARG OCI_REVISION=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${OCI_REVISION}"' in dockerfile
    assert dockerfile.startswith(
        "FROM python:3.12-slim@sha256:"
        "dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65"
    )
    assert "COPY scripts ./scripts" in dockerfile
    assert "COPY evals ./evals" in dockerfile


def test_host_launcher_hashes_sanitized_inputs_and_never_accepts_token_argv() -> None:
    launcher = LAUNCHER.read_text(encoding="utf-8")
    parameter_block = launcher.split(")", 1)[0]

    assert "$Token" not in parameter_block
    assert "Read-Host $Prompt -AsSecureString" in launcher
    assert "config', '--no-interpolate', '--format', 'json'" in launcher
    assert "status --porcelain=v1 --untracked-files=all" in launcher
    assert "org.opencontainers.image.revision" in launcher
    assert ".HostConfig.PortBindings" in launcher
    assert " compose @ComposeArgs port " not in launcher
    assert "parse_shadow_attestation_bytes" in launcher
    assert "$env:SANA_SHADOW_IMAGE = $attestedImage" in launcher
    assert "docker image inspect --format '{{.Id}}' $imageId" in launcher
    assert "docker.sock" in launcher
    assert "down', '--remove-orphans'" in launcher
    assert "down', '--volumes'" not in launcher
    assert "shadow-provenance-v2" in launcher
    assert "OFFLINE_FIXTURE" in launcher


def test_offline_override_cannot_receive_a_real_provider_credential() -> None:
    config = yaml.safe_load(FIXTURE_COMPOSE.read_text(encoding="utf-8"))
    worker = config["services"]["worker"]["environment"]
    runner = config["services"]["campaign-runner"]["environment"]

    assert worker == {
        "SANA_WORKER_OFFLINE_FIXTURE_ENABLED": "true",
        "SANA_WORKER_MODEL_PIPELINE_ENABLED": "false",
        "SANA_WORKER_DISCOVERY_PROVIDERS": "fixture",
        "DEEPSEEK_API_KEY": "shadow-offline-fixture-no-provider-call",
    }
    assert runner == {"SANA_SHADOW_OFFLINE_FIXTURE": "true"}


def test_post_campaign_auditor_is_fail_closed_and_secret_safe() -> None:
    auditor = AUDITOR.read_text(encoding="utf-8")
    parameter_block = auditor.split(")", 1)[0]

    assert "$Token" not in parameter_block
    assert "$Password" not in parameter_block
    assert "status --porcelain=v1 --untracked-files=all" in auditor
    assert "Campaign candidate image" in auditor
    assert "Campaign harness fileset" in auditor
    assert "Campaign environment identity" in auditor
    assert "active_reservation_count" in auditor
    assert "provider_called_count" in auditor
    assert "relforcerowsecurity" in auditor
    assert "LLEN" in auditor
    assert "{{.State.Status}}|{{.State.Health.Status}}|{{.State.Paused}}" in auditor
    assert "worker process count" in auditor
    assert "@('logs', $container)" in auditor
    assert "Campaign report integrity/privacy scan failed" in auditor
    assert "privacy_scan=PASS" in auditor
    assert "Write-Output $protectedValues" not in auditor
