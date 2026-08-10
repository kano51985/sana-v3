from dataclasses import dataclass, field

@dataclass
class Context:
    user_input: str = ""
    review_retry_count: int = 0
    review_feedback: str = ""
    perception_data: dict = field(default_factory=dict)
    alma_override: str = ""
    recalled_context: str = ""
    current_profile: dict = field(default_factory=dict)
    system_prompt: str = ""
    augmented_input: str = ""
    current_time: str = ""
    llm_raw_response: str = ""
    full_reply: str = ""
    thinking: str = ""
    chat_raw: str = ""
    chat: str = ""
    segments: list = field(default_factory=list)
    tool_triggered: bool = False
    tool_target_batch: str = ""
    tool_target_web: str = ""
    web_tool_enabled: bool = False
    web_autonomy_level: int = 2
    web_should_query: bool = False
    web_suggested_query: str = ""
    web_retry_count: int = 0
    web_verification: dict = field(default_factory=dict)
    web_policy_block: str = ""
    web_query_heads: list = field(default_factory=list)
    web_results: list = field(default_factory=list)
    web_entity: dict = field(default_factory=dict)
    web_error: str = ""
    tool_trace: dict = field(default_factory=dict)
    chat_buffer: list = field(default_factory=list)
    behavioral_insight: dict = field(default_factory=dict)
    persona_layer: str = ""
    persona_directive: str = ""
    emotional_directive: str = ""
    emotional_trajectory: list = field(default_factory=list)
    consolidation_pending: bool = False
    working_memory: list = field(default_factory=list)
