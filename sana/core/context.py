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
    llm_raw_response: str = ""
    full_reply: str = ""
    thinking: str = ""
    chat: str = ""
    tool_triggered: bool = False
    tool_target_batch: str = ""
    chat_buffer: list = field(default_factory=list)
    behavioral_insight: dict = field(default_factory=dict)
    persona_layer: str = ""
    persona_directive: str = ""
    emotional_directive: str = ""
    emotional_trajectory: list = field(default_factory=list)
    consolidation_pending: bool = False
    working_memory: list = field(default_factory=list)