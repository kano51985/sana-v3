from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_default_windows_entry_uses_api_only_streamlit_client() -> None:
    batch = read("start.bat")
    streamlit = read("start-streamlit.ps1")

    assert "start-streamlit.ps1" in batch
    assert '"sana\\clients\\streamlit\\app.py"' in streamlit
    assert "DEEPSEEK_API_KEY" not in batch
    assert "setx" not in batch.lower()


def test_legacy_ui_requires_an_explicit_rollback_flag() -> None:
    streamlit = read("start-streamlit.ps1")
    legacy = read("interfaces/streamlit_app.py")

    assert "SANA_UI_MODE" in streamlit
    assert "$Legacy" in streamlit
    assert "旧版回滚入口" in legacy
    assert "不具备新平台的多租户隔离" in legacy


def test_worker_entry_uses_the_builtin_production_handler_by_default() -> None:
    worker = read("start-worker.ps1")

    assert "SANA_STEP_HANDLER_FACTORY" in worker
    assert "sana.app.production_worker:create_handler" in worker
    assert "sana.app.outbox_dispatcher" in worker


def test_compose_declares_durable_services_and_opt_in_workers() -> None:
    compose = read("deployment/docker-compose.yml")

    for service in (
        "postgres:",
        "redis:",
        "artifact-init:",
        "migrate:",
        "api:",
        "dispatcher:",
        "worker:",
        "streamlit:",
    ):
        assert service in compose
    assert 'profiles: ["workers"]' in compose
    assert "/health/ready" in compose
    assert "sana.app.worker_entrypoint:app" in compose
    assert "sana.app.production_worker:create_handler" in compose
    assert "sana-artifacts:/var/lib/sana/artifacts" in compose
    assert 'user: "0:0"' in compose
    assert "chown -R sana:sana /var/lib/sana/artifacts" in compose
    assert "condition: service_completed_successfully" in compose
    assert "user_profile.json" not in read("deployment/Dockerfile")


def test_operations_runbook_preserves_rollback_data_and_lists_blockers() -> None:
    runbook = read("docs/operations/search-platform.md")

    assert "禁止删除 MongoDB、Chroma" in runbook
    assert "Reconciler" in runbook
    assert "不能宣称最终切流完成" in runbook
