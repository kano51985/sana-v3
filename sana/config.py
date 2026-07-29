from sana.models.registry import ModelRegistry, ModelConfig
from sana.models.local_backend import LocalModelBackend
from sana.models.openai_backend import OpenAIModelBackend
from sana.models.deepseek_backend import DeepSeekBackend

# 角色名称
AGENT_NAME = "Sana"
USER_NAME = "白日"

API_URL = "http://localhost:1234/api/v1/chat"
CHROMA_DB_PATH = "D:/MyProduct/sana_project/sana_memory_db"
COLLECTION_NAME = "sana_memories"

registry = ModelRegistry(backends={
    "local": LocalModelBackend(base_url=API_URL),
    "openai": OpenAIModelBackend(api_key=""),
    "deepseek": DeepSeekBackend(api_key=""),
})

registry.models["perception"] = ModelConfig(
    model_id="qwen2.5-0.5b-instruct", backend_name="local", params={"temperature": 0.1}
)
registry.models["chat"] = ModelConfig(
    model_id="deepseek-chat", backend_name="deepseek", params={"temperature": 0.8}
)
registry.models["summarize"] = ModelConfig(
    model_id="google/gemma-4-e4b", backend_name="local", params={"temperature": 0.3}
)

# Snapshot of project-level defaults (set by this file, before any user override)
_DEFAULTS: dict[str, ModelConfig] = {
    role: ModelConfig(
        model_id=registry.models[role].model_id,
        backend_name=registry.models[role].backend_name,
        params=dict(registry.models[role].params),
    )
    for role in registry.models
}


def apply_model_config(cfg: dict):
    """Apply a persisted model-config dict (from user_profile.json) to the registry."""
    for role, settings in cfg.items():
        if role in registry.models:
            registry.models[role] = ModelConfig(
                model_id=settings.get("model_id", registry.models[role].model_id),
                backend_name=settings.get("backend_name", registry.models[role].backend_name),
                params=dict(settings.get("params", registry.models[role].params)),
            )


def dump_model_config() -> dict:
    """Dump current registry model config to a JSON-serializable dict."""
    return {
        role: {
            "backend_name": cfg.backend_name,
            "model_id": cfg.model_id,
            "params": dict(cfg.params),
        }
        for role, cfg in registry.models.items()
    }


def reset_model_config():
    """Restore registry to the project-level defaults from this file."""
    for role, cfg in _DEFAULTS.items():
        registry.models[role] = ModelConfig(
            model_id=cfg.model_id,
            backend_name=cfg.backend_name,
            params=dict(cfg.params),
        )
