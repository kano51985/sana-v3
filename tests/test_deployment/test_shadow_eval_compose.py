from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "deployment" / "docker-compose.shadow-eval.yml"
DOCKERFILE = ROOT / "deployment" / "Dockerfile"
LAUNCHER = ROOT / "scripts" / "run_shadow_campaign.ps1"


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
    worker_command = config["services"]["worker"]["command"]
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert worker_command[worker_command.index("--concurrency") + 1] == "2"
    assert "ARG OCI_REVISION=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${OCI_REVISION}"' in dockerfile
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
    assert "docker.sock" in launcher
    assert "down', '--remove-orphans'" in launcher
    assert "down', '--volumes'" not in launcher
