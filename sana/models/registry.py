from dataclasses import dataclass, field
from sana.models.backend import ModelBackend
from sana.models.local_backend import LocalModelBackend
from sana.models.openai_backend import OpenAIModelBackend

@dataclass
class ModelConfig:
    model_id: str = ""
    backend_name: str = "local"
    params: dict = field(default_factory=lambda: {"temperature": 0.7})

class ModelRegistry:
    def __init__(self, backends: dict[str, ModelBackend] = None):
        self.backends = backends or {
            "local": LocalModelBackend(),
            "openai": OpenAIModelBackend(api_key=""),
        }
        self.models: dict[str, ModelConfig] = {
            "perception": ModelConfig(model_id="qwen2.5-0.5b-instruct", backend_name="local", params={"temperature": 0.1}),
            "chat": ModelConfig(model_id="google/gemma-4-e4b", backend_name="local", params={"temperature": 0.8}),
            "summarize": ModelConfig(model_id="google/gemma-4-e4b", backend_name="local", params={"temperature": 0.3}),
        }
    def get_backend(self, role: str) -> ModelBackend:
        cfg = self.models.get(role)
        if not cfg:
            raise KeyError(f"No model configured for role {role!r}")
        be = self.backends.get(cfg.backend_name)
        if not be:
            raise KeyError(f"Backend {cfg.backend_name!r} not registered")
        return be
    def get_config(self, role: str) -> ModelConfig:
        return self.models.get(role)
