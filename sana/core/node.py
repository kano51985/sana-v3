from abc import ABC, abstractmethod
from dataclasses import dataclass
from sana.core.context import Context

@dataclass
class NodeResult:
    next: str | None = None
    fallback: str | None = None
    context: Context | None = None

class PipelineNode(ABC):
    @abstractmethod
    def process(self, ctx: Context) -> NodeResult:
        ...
