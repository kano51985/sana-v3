from sana.core.engine import PipelineEngine
from sana.core.context import Context
from sana.models.registry import ModelRegistry
from sana.services.alma_engine import ALMAEngine
from sana.services.perception import PerceptionLayer
from sana.services.memory_service import MemoryManager
from sana.services.memory_summarizer import MemorySummarizer
from sana.services.mongo_client import RawMemoryDB
from sana.services.profile_manager import ProfileManager
from sana.services.web_tool_config import WebToolConfigStore
from sana.services.web_tool_policy import WebToolPolicy
from sana.services.web_alias_store import WebAliasStore
from sana.services.entity_resolver import EntityResolver
from sana.services.web_query_planner import WebQueryPlanner
from sana.services.web_search_service import WebSearchService
from sana.services.retrieval_scorer import RetrievalScorer
from sana.services.tool_registry import ToolRegistry
from sana.services.tool_intent_detector import ToolIntentDetector
from sana.services.result_verifier import ToolResultVerifier
from sana.nodes.input_node import InputNode
from sana.nodes.perception_node import PerceptionNode
from sana.nodes.alma_node import ALMANode
from sana.nodes.memory_recall_node import MemoryRecallNode
from sana.nodes.profile_load_node import ProfileLoadNode
from sana.nodes.working_memory_node import WorkingMemoryNode
from sana.nodes.prompt_builder_node import PromptBuilderNode
from sana.nodes.persona_selection_node import PersonaSelectionNode
from sana.nodes.llm_call_node import LLMCallNode
from sana.nodes.tool_intercept_node import ToolInterceptNode
from sana.nodes.deep_dive_node import DeepDiveNode
from sana.nodes.response_parser_node import ResponseParserNode
from sana.nodes.sentence_segment_node import SentenceSegmentNode
from sana.nodes.memory_update_node import MemoryUpdateNode
from sana.nodes.consolidation_node import ConsolidationNode
from sana.nodes.format_check_node import FormatCheckerNode
from sana.nodes.directive_node import DirectiveNode
from sana.nodes.web_search_node import WebSearchNode
from sana.services.emotional_directive import EmotionalDirective
from sana.services.behavioral_reasoner import BehavioralReasoner
from sana.nodes.behavioral_reasoner_node import BehavioralReasonerNode
import json, os, sys

class SanaAgent:
    def __init__(self):
        # Load persisted model config before building anything else
        self._load_model_config()

        # Services
        self.alma = ALMAEngine()
        self.directive = EmotionalDirective()
        self.reasoner = BehavioralReasoner()
        self.perception = PerceptionLayer()
        self.memory = MemoryManager()
        self.raw_db = RawMemoryDB()
        self.summarizer = MemorySummarizer()
        self.profile_mgr = ProfileManager()
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.web_config_store = WebToolConfigStore(os.path.join(base_dir, "user_profile.json"))
        self.web_policy = WebToolPolicy(self.web_config_store)
        self.web_alias_store = WebAliasStore(os.path.join(base_dir, "sana", "data", "web_aliases.json"))
        self.entity_resolver = EntityResolver(self.web_alias_store)
        self.web_planner = WebQueryPlanner()
        self.web_search_service = WebSearchService(self.web_config_store)
        self.retrieval_scorer = RetrievalScorer()
        self.tool_registry = ToolRegistry()
        self.tool_intent_detector = ToolIntentDetector(tool_registry=self.tool_registry)
        self.result_verifier = ToolResultVerifier()
        self.last_web_trace: dict = {}
        self.consolidation_node = ConsolidationNode(
            self.summarizer, self.raw_db, self.memory, self.profile_mgr
        )
        self._consolidating = False

        # Session state
        self.working_memory: list[dict] = []
        self.chat_buffer: list[dict] = []

        # Assemble pipeline
        self.engine = (
            PipelineEngine()
            .register("input", InputNode())
            .register("perception", PerceptionNode(self.perception))
            .register("behavioral_reasoner", BehavioralReasonerNode(self.reasoner, alma=self.alma))
            .register("alma", ALMANode(self.alma))
            .register("directive", DirectiveNode(self.alma, self.directive))
            .register("persona_selection", PersonaSelectionNode())
            .register("memory_recall", MemoryRecallNode(self.memory))
            .register("profile_load", ProfileLoadNode(self.profile_mgr))
            .register("working_memory", WorkingMemoryNode(self.working_memory))
            .register("prompt_builder", PromptBuilderNode(web_policy=self.web_policy, alma=self.alma))
            .register("llm_call", LLMCallNode())
            .register("tool_intercept", ToolInterceptNode(
                self.raw_db,
                intent_detector=self.tool_intent_detector,
                tool_registry=self.tool_registry,
            ))
            .register("deep_dive", DeepDiveNode())
            .register("web_search", WebSearchNode(
                config_store=self.web_config_store,
                policy=self.web_policy,
                alias_store=self.web_alias_store,
                resolver=self.entity_resolver,
                planner=self.web_planner,
                search_service=self.web_search_service,
                scorer=self.retrieval_scorer,
                verifier=self.result_verifier,
            ))
            .register("format_check", FormatCheckerNode())
            .register("response_parser", ResponseParserNode())
            .register("sentence_segment", SentenceSegmentNode())
            .register("memory_update", MemoryUpdateNode(self.working_memory, self.chat_buffer))
            .register("consolidation", self.consolidation_node)
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
            self.last_web_trace = result.context.tool_trace
        return {
            "chat": result.context.chat if result.context else "Error",
            "thinking": result.context.thinking if result.context else "",
            "perception": result.context.perception_data if result.context else {},
            "segments": result.context.segments if result.context else [],
            "tool_trace": result.context.tool_trace if result.context else {},
        }

    def consolidate_memory(self) -> dict:
        if self._consolidating:
            return self._manual_result("busy", "已有聚合任务正在进行", error="busy")
        if not self.chat_buffer:
            return self._manual_result("empty", "当前没有待聚合的对话")

        self._consolidating = True
        try:
            return self.consolidation_node.consolidate(self.chat_buffer, force=True)
        except Exception as e:
            return self._manual_result("error", f"聚合失败: {e}", error=str(e))
        finally:
            self._consolidating = False

    def _manual_result(self, code, message, error=None):
        return {
            "ok": code == "empty",
            "code": code,
            "message": message,
            "batch_id": None,
            "event_count": 0,
            "update_count": 0,
            "cleared": False,
            "error": error,
        }
