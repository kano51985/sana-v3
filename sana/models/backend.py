from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ModelResponse:
    content: str = ""
    thinking: str = ""
    raw: str = ""

class ModelBackend(ABC):
    @abstractmethod
    def chat(self, model_id: str, messages: list[dict], **kwargs) -> ModelResponse:
        ...
