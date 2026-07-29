from sana.core.engine import PipelineEngine
from sana.core.context import Context
from sana.models.registry import ModelRegistry
from sana.services.alma_engine import ALMAEngine
from sana.services.perception import PerceptionLayer
from sana.services.memory_service import MemoryManager
from sana.services.memory_summarizer import MemorySummarizer
from sana.services.mongo_client import RawMemoryDB
from sana.services.profile_manager import ProfileManager
from sana.nodes.input_node import InputNode
from sana.nodes.perception_node import PerceptionNode
from sana.nodes.alma_node import ALMANode
from sana.nodes.memory_recall_node import MemoryRecallNode
from sana.nodes.profile_load_node import ProfileLoadNode
from sana.nodes.working_memory_node import WorkingMemoryNode
from sana.nodes.prompt_builder_node import PromptBuilderNode
from sana.nodes.style_review_node import StyleReviewNode
from sana.nodes.compliance_review_node import ComplianceReviewNode
from sana.nodes.llm_call_node import LLMCallNode
from sana.nodes.tool_intercept_node import ToolInterceptNode
from sana.nodes.deep_dive_node import DeepDiveNode
from sana.nodes.response_parser_node import ResponseParserNode
from sana.nodes.memory_update_node import MemoryUpdateNode
from sana.nodes.consolidation_node import ConsolidationNode
import json, os, sys

class SanaAgent:
    def __init__(self):
        # Load persisted model config before building anything else
        self._load_model_config()

        # Services
        self.alma = ALMAEngine()
        self.perception = PerceptionLayer()
        self.memory = MemoryManager()
        self.raw_db = RawMemoryDB()
        self.summarizer = MemorySummarizer()
        self.profile_mgr = ProfileManager()

        # Session state
        self.working_memory: list[dict] = []
        self.chat_buffer: list[dict] = []

        # Assemble pipeline
        self.engine = (
            PipelineEngine()
            .register("input", InputNode())
            .register("perception", PerceptionNode(self.perception))
            .register("alma", ALMANode(self.alma))
            .register("memory_recall", MemoryRecallNode(self.memory))
            .register("profile_load", ProfileLoadNode(self.profile_mgr))
            .register("working_memory", WorkingMemoryNode(self.working_memory))
            .register("prompt_builder", PromptBuilderNode())
            .register("llm_call", LLMCallNode())
            .register("tool_intercept", ToolInterceptNode(self.raw_db))
            .register("deep_dive", DeepDiveNode())
            .register("style_review", StyleReviewNode())
            .register("compliance_review", ComplianceReviewNode(self.alma))
            .register("response_parser", ResponseParserNode())
            .register("memory_update", MemoryUpdateNode(self.working_memory, self.chat_buffer))
            .register("consolidation", ConsolidationNode(
                self.summarizer, self.raw_db, self.memory, self.profile_mgr))
            .start_at("input")
        )

    def _load_model_config(self):
        """Load model_config from user_profile.json if present."""
        try:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(base, "user_profile.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "model_config" in data:
                    from sana.config import apply_model_config
                    apply_model_config(data["model_config"])
                    print(f"[Agent] Loaded persisted model config")
        except Exception as e:
            print(f"[Agent] Failed to load model config: {e}")

    def chat(self, user_input: str) -> dict:
        ctx = Context(user_input=user_input)
        ctx.working_memory = self.working_memory
        ctx.chat_buffer = self.chat_buffer
        result = self.engine.run(ctx)
        if result.context:
            self.working_memory = result.context.working_memory
            self.chat_buffer = result.context.chat_buffer
        return {
            "chat": result.context.chat if result.context else "Error",
            "thinking": result.context.thinking if result.context else "",
            "perception": result.context.perception_data if result.context else {},
        }
