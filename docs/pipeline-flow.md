# Sana Agent Pipeline Flow

> 当前默认入口（2026-08-14）：`start.bat` → 新 Streamlit API 客户端 → FastAPI → PostgreSQL Run/Step/Outbox。旧 `interfaces/streamlit_app.py` 仅作为显式回滚入口保留，不再是默认入口。生产 Worker 的具体算子装配仍是切流阻断项，详见 `docs/operations/search-platform.md`。

```text
Browser session
  -> Streamlit API client (no DB/key/profile access)
  -> FastAPI authentication + automatic FAST/RESEARCH routing
  -> PostgreSQL transaction: Message + Run + first Step + Outbox
  -> tenant-aware Outbox Dispatcher
  -> Redis/Celery queue: tenant_id + step_id + trace only
  -> configured Worker
  -> PostgreSQL durable state + Redis SSE acceleration
```

新链路以 PostgreSQL 为唯一工作流事实源。控制进程会从 PostgreSQL 扫描 READY、到期 RETRY_WAIT 与 lease 过期的 RUNNING Step 并重投，但在真实 PostgreSQL/Redis chaos 验收通过前，不能把恢复保证标记为完成。回滚窗口内不删除 MongoDB、Chroma、`user_profile.json` 或旧 Web 搜索代码。

> 本文档描述 Sana Agent 的完整流水线架构、每个节点的职责、核心参数，以及当前代码的实际实现。

## 架构总览

Sana 是一个基于 **Pipeline（流水线）模式** 的情感 AI Agent。`SanaAgent.chat()` 创建 `Context` 后交给 `PipelineEngine.run(ctx)` 执行；当前 `sana/agent.py` 注册 19 个节点，其中 `deep_dive` 和 `web_search` 是条件分支节点。每个节点修改 `Context`，并通过 `NodeResult.next` 或 `NodeResult.fallback` 决定下一步。

```text
input
  -> perception
  -> behavioral_reasoner
  -> alma
  -> directive
  -> persona_selection
  -> memory_recall
  -> profile_load
  -> working_memory
  -> prompt_builder
  -> llm_call
  -> tool_intercept
     |-- 无工具 -> format_check
     |-- 记忆工具 -> deep_dive -> format_check
     `-- 联网工具 -> web_search -> format_check
  -> format_check -> response_parser
  -> response_parser
  -> sentence_segment
  -> memory_update
  -> consolidation
```

`FormatCheckerNode` 发现缺失标签且尚未达到重试上限时，会通过 `fallback="llm_call"` 回炉重写。

工具调用采用两阶段协议：第一轮模型要么输出普通回复，要么只输出工具调用；如果模型漏掉工具标签，`ToolIntentDetector` 会做通用语义判断并自动进入 `web_search`。

**当前实现偏差**：

- `style_review` / `compliance_review` 节点已删除，生成后路径为 `format_check -> response_parser`。

## 核心数据结构

**`Context`** (`sana/core/context.py`) — 流水线的唯一数据载体，每个节点从中读取、写入。

| 字段 | 类型 | 写入节点 | 用途 |
|------|------|----------|------|
| `user_input` | str | 外部传入 | 用户本次输入的原始文本 |
| `perception_data` | dict | PerceptionNode | 感知层分析结果（情绪、意图、实体、行为等） |
| `behavioral_insight` | dict | BehavioralReasonerNode | 用户行为模式分析结果 |
| `alma_override` | str | ALMANode | ALMA 情感状态快照文本 |
| `recalled_context` | str | MemoryRecallNode | 从向量数据库召回的相关记忆 |
| `current_profile` | dict | ProfileLoadNode | 当前用户档案 |
| `system_prompt` | str | PromptBuilderNode | 组装后的系统提示词 |
| `augmented_input` | str | PromptBuilderNode / FormatCheckerNode | 注入所有上下文后的增强输入 |
| `current_time` | str | PromptBuilderNode | 当前时间注入串 |
| `llm_raw_response` | str | LLMCallNode / DeepDiveNode / WebSearchNode | LLM 的原始输出 |
| `full_reply` | str | 预留 | 当前代码未写入 |
| `thinking` | str | ResponseParserNode | 从 LLM 输出解析出的内心 OS |
| `chat` | str | ResponseParserNode | 从 LLM 输出解析出的回复内容 |
| `segments` | list[dict] | SentenceSegmentNode | 拆句结果：每项包含 `text` 与 `delay` |
| `tool_triggered` | bool | ToolInterceptNode | 是否检测到工具调用 |
| `tool_target_batch` | str | ToolInterceptNode | 工具调用的目标 batch ID |
| `tool_target_web` | str | ToolInterceptNode | 联网工具的 query |
| `web_tool_enabled` | bool | PromptBuilderNode / WebSearchNode | 是否启用联网工具 |
| `web_autonomy_level` | int | PromptBuilderNode / WebSearchNode | 当前自主等级 |
| `web_should_query` | bool | ToolInterceptNode / WebSearchNode | 通用意图判断是否判定需要联网 |
| `web_suggested_query` | str | ToolInterceptNode / WebSearchNode | 通用意图判断给出的建议 query |
| `web_retry_count` | int | WebSearchNode | 当前轮结果验证后的重试次数 |
| `web_verification` | dict | WebSearchNode | 结果验证 JSON：置信度、缺失事实、建议重试 query |
| `web_policy_block` | str | PromptBuilderNode | 动态拼入的策略块 |
| `web_query_heads` | list[str] | WebSearchNode | 本轮查询头 |
| `web_results` | list[dict] | WebSearchNode | 抓取评分后的结果 |
| `web_entity` | dict | WebSearchNode | 实体解析结果 |
| `web_error` | str | WebSearchNode | 联网错误信息 |
| `tool_trace` | dict | ToolInterceptNode / WebSearchNode | 工具调试轨迹 |
| `review_retry_count` | int | FormatCheckerNode | 当前轮次格式回炉次数 |
| `review_feedback` | str | FormatCheckerNode | 格式不通过时追加的 revision 指令 |
| `chat_buffer` | list | MemoryUpdateNode / ConsolidationNode | 累积的对话历史（用于总结） |
| `persona_layer` | str | PersonaSelectionNode | 当前选择的人格层：`deep` / `surface` |
| `persona_directive` | str | PersonaSelectionNode | 人格层对应的系统提示片段 |
| `emotional_directive` | str | DirectiveNode | ALMA 状态翻译成的行为指令 |
| `emotional_trajectory` | list | ALMANode | 最近 5 轮情绪轨迹 |
| `consolidation_pending` | bool | 预留 | 当前代码未写入 |
| `working_memory` | list | WorkingMemoryNode / MemoryUpdateNode | 工作记忆（短期对话记录） |

---

## 流水线节点详解

### 节点 0：InputNode（输入节点）

**文件**：`sana/nodes/input_node.py`

**定位**：流水线起点，透传节点。

```python
class InputNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        return NodeResult(next="perception", context=ctx)
```

| 项目 | 内容 |
|------|------|
| 做什么 | 什么也不做，直接转发到 `perception` 节点 |
| 核心参数 | 无 |
| 为什么存在 | 作为流水线的锚点入口，方便 `start_at("input")` 统一启动 |

---

### 节点 1：PerceptionNode（感知层节点）

**文件**：`sana/nodes/perception_node.py`
**关联 Service**：`sana/services/perception.py` → `PerceptionLayer`

**定位**：对用户输入进行第一层理解，并结合最近用户消息识别重复意图和行为模式。

```python
class PerceptionNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        recent = []
        for m in reversed(ctx.working_memory):
            if m.get("role") == USER_NAME:
                recent.append(m["content"])
            if len(recent) >= 5:
                break
        ctx.perception_data = self.perception.analyze(ctx.user_input, recent)
        return NodeResult(next="behavioral_reasoner", context=ctx)
```

**核心参数（ctx.perception_data）**：

| 字段 | 类型 | 示例 | 意义 |
|------|------|------|------|
| `occ_emotion` | list[str] | `["Distress"]` | OCC 情绪标签列表 |
| `emotion` | str | `"疲惫"` | 简化的情绪描述 |
| `intent` | str | `"complain"` | 用户意图分类 |
| `entities` | list[str] | `["原神", "甘雨"]` | 提取的命名实体 |
| `relation` | str | `"friend"` | 用户与提及对象的关系 |
| `intensity` | float | `0.7` | 情绪强度 |
| `user_repeat_count` | int | `3` | 连续相同核心意图的次数 |
| `user_behavior_type` | str | `"tease"` | 用户行为模式：`normal` / `blame` / `tease` / `dump` / `ignore` / `praise` / `other` |

**实现方式**：调用 LLM（`perception` 角色）做零样本提取，输出 JSON 后解析；LLM 失败时返回 Neutral/chat/normal 的兜底结果。

---

### 节点 2：BehavioralReasonerNode（行为推理节点）

**文件**：`sana/nodes/behavioral_reasoner_node.py`
**关联 Service**：`sana/services/behavioral_reasoner.py` → `BehavioralReasoner`

**定位**：根据感知层输出和当前会话历史，识别用户重复测试、责怪、倾倒情绪、无视等行为，并生成情绪调整建议。

```python
class BehavioralReasonerNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        ocean = (self.alma.ocean if self.alma else ctx.current_profile.get("ocean", {}))
        ctx.behavioral_insight = self.reasoner.analyze(
            perception_data=ctx.perception_data,
            working_memory=ctx.working_memory,
            ocean=ocean,
        )
        return NodeResult(next="alma", context=ctx)
```

**输出（ctx.behavioral_insight）**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `patterns` | list[dict] | 识别到的行为模式，含 `type`、`confidence`、`detail` |
| `emotion_additions` | list[str] | 建议追加到 ALMA 的 OCC 情绪 |
| `suggested_intensity` | float | 建议的情绪强度 |

**设计思路**：不直接改 ALMA，只把分析结果交给 `ALMANode` 消费，保持行为推理和情感计算解耦。

**当前限制**：`BehavioralReasoner` 只会把 `user_repeat_count >= 3` 且 `user_behavior_type` 为 `normal` / `chat` / `ask` 的情况识别为 `tease_test`；如果感知层直接返回 `user_behavior_type="tease"`，当前规则不会注入 `Reproach`。`DirectiveNode` 仍会读取 `tease` 并提高指令等级，但 ALMA 情绪不会因此被主动压低。

---

### 节点 3：ALMANode（情感计算节点）

**文件**：`sana/nodes/alma_node.py`
**关联 Service**：`sana/services/alma_engine.py` → `ALMAEngine`

**定位**：模拟 Agent 的"情绪变化"，并把行为推理建议合并进当前情绪状态。

```python
class ALMANode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        occ = ctx.perception_data.get("occ_emotion", ["Neutral"])
        intensity = float(ctx.perception_data.get("intensity", 0.5))
        normalized = self._normalize_occ(occ)
        normalized = self._apply_behavioral_adjustment(ctx, normalized)
        self.alma.process_event(normalized, intensity=intensity)
        ctx.alma_override = self.alma.get_alma_prompt()
        ctx.emotional_trajectory.append(...)
        return NodeResult(next="directive", context=ctx)
```

**ALMAEngine 核心模型**：

| 组件 | 说明 |
|------|------|
| **OCEAN 人格** | 5 维固定人格参数（O=开放性, C=尽责性, E=外向性, A=宜人性, N=神经质） |
| **基线 PAD** | 从 OCEAN 映射出基线情绪状态（Pleasure, Arousal, Dominance），值域 [-1, 1] |
| **时间衰减** | 情绪随自然时间指数衰减到基线 |
| **事件调制** | OCC 情绪标签映射到 PAD 偏移量 |
| **情绪标签归一化** | 中文/英文变体通过静态映射兜底到 OCC 标准标签 |

**输出**：

- `ctx.alma_override`：如 `[ALMA] P=0.32 A=-0.15 D=0.60 emotion=Joy+Admiration`
- `ctx.emotional_trajectory`：每轮追加 `{ turn, emotion, intensity, pad }`，最多保留 5 轮

**说明**：当前 `PromptBuilderNode` 不直接消费 `alma_override`，而是消费 `DirectiveNode` 生成的 `emotional_directive` 和 `emotional_trajectory`；`alma_override` 仍保留在 Context 中作为状态快照。

---

### 节点 4：DirectiveNode（情绪指令节点）

**文件**：`sana/nodes/directive_node.py`
**关联 Service**：`sana/services/emotional_directive.py` → `EmotionalDirective`

**定位**：把 ALMA 的 PAD 和当前情绪翻译成显式的自然语言行为指令，供 PromptBuilder 拼入 system prompt。

```python
class DirectiveNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        ctx.emotional_directive = self.directive.generate(
            emotion_label=self.alma.current_transient_emotion,
            intensity=self.alma.emotion_intensity,
            repeat_count=ctx.perception_data.get("user_repeat_count", 1),
            user_behavior=ctx.perception_data.get("user_behavior_type", "normal"),
            ocean=self.alma.ocean,
            pad=self.alma.current_mood,
        )
        return NodeResult(next="persona_selection", context=ctx)
```

**输出（ctx.emotional_directive）**：包含 `[情绪状态]`、`核心情绪`、`[行为指引]`、`[回应策略]` 四个部分。

**设计思路**：让模型看到的是可直接执行的回复态度，而不是原始 PAD 数值。

---

### 节点 5：PersonaSelectionNode（人格选择节点）

**文件**：`sana/nodes/persona_selection_node.py`

**定位**：在 LLM 生成前选择深层或表层人格，写入人格指令。

```python
class PersonaSelectionNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        user_name = self._detect_user(ctx)
        ctx.persona_layer = self._select_layer(user_name)
        ctx.persona_directive = self._build_directive(ctx.persona_layer)
        return NodeResult(next="memory_recall", context=ctx)
```

**核心参数**：

| 字段 | 值 | 说明 |
|------|------|------|
| `persona_layer` | `"deep"` / `"surface"` | 当前单用户模式下默认 `deep` |
| `persona_directive` | str | 深层开放或表层社交的系统提示片段 |

**设计思路**：人格选择只负责生成前角色表现，不再兼任生成后审查。

---

### 节点 6：MemoryRecallNode（记忆召回节点）

**文件**：`sana/nodes/memory_recall_node.py`
**关联 Service**：`sana/services/memory_service.py` → `MemoryManager`

**定位**：从长期记忆中召回与当前对话相关的历史信息。

```python
class MemoryRecallNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        entities = ctx.perception_data.get("entities", [])
        query = " ".join(entities) + " " + ctx.user_input
        ctx.recalled_context = self.memory.recall(query)
        return NodeResult(next="profile_load", context=ctx)
```

**核心参数**：

| 参数 | 来源 | 用途 |
|------|------|------|
| 查询文本 | entities + user_input | 用感知提取的实体增强检索相关性 |
| `n_results=3` | MemoryManager 默认值 | 每次召回最多 3 条历史记忆 |

**后端**：ChromaDB（持久化向量数据库），存储路径由 `CHROMA_DB_PATH` 配置。

---

### 节点 7：ProfileLoadNode（档案加载节点）

**文件**：`sana/nodes/profile_load_node.py`
**关联 Service**：`sana/services/profile_manager.py` → `ProfileManager`

**定位**：加载当前用户档案。

```python
class ProfileLoadNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        ctx.current_profile = self.profile_mgr.load_profile()
        return NodeResult(next="working_memory", context=ctx)
```

**核心参数（ctx.current_profile）**：

```json
{
  "name": "白日",
  "gaming_preferences": {},
  "general_preferences": {},
  "model_config": {}
}
```

**设计思路**：将用户个性化信息注入提示词，档案由 ConsolidationNode 自动更新。

---

### 节点 8：WorkingMemoryNode（工作记忆节点）

**文件**：`sana/nodes/working_memory_node.py`

**定位**：将当前会话的短期对话历史注入 Context。

```python
class WorkingMemoryNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        ctx.working_memory = self.working_memory
        return NodeResult(next="prompt_builder", context=ctx)
```

**核心参数（working_memory）**：`list[{"role": "user"/"assistant", "content": str}]`

---

### 节点 9：PromptBuilderNode（提示词组装节点）

**文件**：`sana/nodes/prompt_builder_node.py`
**关联 Prompt 模板**：`sana/prompts/system.py` → `SANA_SYSTEM_PROMPT`

**定位**：按层级组装 system prompt 和 augmented input。

PromptBuilder 默认通过 `CurrentTimeProvider` 读取当前时间并写入 `ctx.current_time`，作为 `[当前时间]` 块放到 system prompt 最前面；同一轮内格式重试和深潜复用同一份 system prompt。

```python
class PromptBuilderNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        layers = [SANA_SYSTEM_PROMPT]
        if ctx.persona_directive:
            layers.append(ctx.persona_directive)
        if ctx.emotional_directive:
            layers.append(ctx.emotional_directive)
        if self.web_policy:
            ctx.web_policy_block = self.web_policy.build_policy_block(ctx, self.alma)
            if ctx.web_policy_block:
                layers.append(ctx.web_policy_block)
            if ctx.web_tool_enabled:
                layers.append("[Tool Protocol] ...")
        ctx.system_prompt = "\n\n".join(layers) + FORMAT_CONSTRAINT
        ctx.augmented_input = build_augmented_input(ctx)
        return NodeResult(next="llm_call", context=ctx)
```

**system_prompt 分层**：

```text
Layer 0    当前时间（CurrentTimeProvider）
Layer 1    角色设定卡（Sana 人设、关系、游戏标签、生活细节）
Layer 1.5  人格指令（PersonaSelectionNode）
Layer 2    情绪指令（DirectiveNode）
Layer 2.5  联网策略（WebToolPolicy）
Layer 2.6  工具协议（Tool Protocol）
强制约束   第一轮二选一：普通回复，或只输出工具调用
```

`[Tool Protocol]` 要求第一轮输出必须是二选一：不需要工具时输出 `<thinking>` + `<chat>`；需要工具时只输出 `<invoke_web query="..."/>`，不允许混入聊天内容。工具执行后再由第二次 LLM 调用生成最终回复。

**augmented_input 结构**：

```text
[情绪轨迹]       ← 最近 5 轮 emotion / intensity / PAD
[Profile]        ← 用户档案
[Recent Conversation] ← 工作记忆
[Recall]         ← 向量召回的长时记忆
[User]           ← 当前用户输入
```

---

### 节点 10：LLMCallNode（LLM 调用节点）

**文件**：`sana/nodes/llm_call_node.py`
**关联 Service**：`sana/models/registry.py` + 具体 backend

**定位**：调用大语言模型获取回复。

```python
class LLMCallNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        try:
            resp = backend.chat(cfg.model_id, [...], timeout=10)
            ctx.llm_raw_response = resp.content
            return NodeResult(next="tool_intercept", context=ctx)
        except Exception as e:
            ctx.llm_raw_response = f"<chat>[LLM Error: {e}]</chat>"
            return NodeResult(next="response_parser", context=ctx)
```

**核心参数**：

| 参数 | 来源 | 意义 |
|------|------|------|
| `model_id` | `registry.models["chat"].model_id` | 根据配置选择模型 |
| `backend_name` | `registry.models["chat"].backend_name` | 选择后端（local / openai / deepseek） |
| `temperature` | `registry.models["chat"].params.temperature` | LLM 温度参数 |
| `timeout=10` | 硬编码 | 调用超时（秒） |

**支持的 Backend 类型**：

| Backend | 实现文件 | 调用方式 |
|---------|----------|----------|
| local | `models/local_backend.py` | HTTP POST → 本地 API |
| openai | `models/openai_backend.py` | OpenAI SDK |
| deepseek | `models/deepseek_backend.py` | OpenAI SDK（兼容 OpenAI 协议） |

---

### 节点 11：ToolInterceptNode（工具拦截节点）

**文件**：`sana/nodes/tool_intercept_node.py`
**关联 Service**：`RawMemoryDB` + `ToolRegistry` + `ToolIntentDetector`

**定位**：检查第一轮输出是普通回复还是工具调用；如果模型没有输出工具标签，但通用意图判断认为需要工具，则自动进入对应工具分支。

```python
class ToolInterceptNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        m = re.search("<invoke_memory>(batch_[a-zA-Z0-9_]+)</invoke_memory>", ctx.llm_raw_response)
        if m:
            ctx.tool_triggered = True
            ctx.tool_target_batch = m.group(1)
            return NodeResult(next="deep_dive", context=ctx)
        web = re.search(r'<invoke_web query="([^"]+)"/>', ctx.llm_raw_response)
        if web:
            ctx.tool_triggered = True
            ctx.tool_target_web = web.group(1)
            return NodeResult(next="web_search", context=ctx)
        if ctx.web_tool_enabled and self.intent_detector:
            intent = self.intent_detector.detect(
                ctx.user_input,
                ctx.perception_data,
                ctx.llm_raw_response,
            )
            if intent.needs_tool and intent.tool_name == "web":
                ctx.tool_target_web = intent.query
                return NodeResult(next="web_search", context=ctx)
        return NodeResult(next="format_check", context=ctx)
```

**核心参数**：

| 参数 | 条件 | 意义 |
|------|------|------|
| `tool_triggered=True` | LLM 输出匹配 `<invoke_memory>batch_xxx</invoke_memory>` | 需要执行深潜查询 |
| `tool_target_batch` | 同上 | 需要查询的原始对话批次 ID |
| `tool_triggered=True` | LLM 输出匹配 `<invoke_web query="..."/>` | 需要执行联网查询 |
| `tool_target_web` | 同上 | 联网查询的 query |
| `auto_triggered` | 模型未输出标签，但 `ToolIntentDetector` 判定需要 web | 自动进入 `web_search` |

`ToolIntentDetector` 是通用的 LLM 结构化意图判断，不依赖“当前/版本/角色/配对”这类关键词表。

---

### 节点 12：WebSearchNode（联网查询节点）

**文件**：`sana/nodes/web_search_node.py`
**关联 Services**：`WebToolConfigStore` + `WebToolPolicy` + `EntityResolver` + `WebQueryPlanner` + `WebSearchService` + `SearchDiscoveryService` + `KatanaCrawler` + `ContentExtractor` + `RetrievalScorer` + `ToolResultVerifier`

**定位**：执行策略判断、实体识别、多查询头抓取、评分、结果注入和第二次 LLM 回复。

```python
class WebSearchNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        config = self.config_store.load()
        allowed, status, reason = self.policy.evaluate(...)
        if not allowed:
            return self._finish(ctx, EntityResolution(), status, reason)
        resolution = self.resolver.self_check(...)
        heads = self.planner.build_heads(...)
        raw_results = self.search_service.search(heads, direct_canonical=resolution.canonical)
        results = self.scorer.merge(raw_results, config.max_injected_results)
        ctx.web_results = results
        return self._finish(ctx, resolution, "executed", "", results)
```

**核心参数**：

| 参数 | 意义 |
|------|------|
| `web_query_heads` | 本轮生成的查询头 |
| `web_results` | 抓取评分后的结果 |
| `web_entity` | AI 自检后的实体解析 |
| `tool_trace` | 工具状态调试轨迹 |

当 `ToolInterceptNode` 以 `auto_triggered` 状态进入时，`WebSearchNode` 仍会按正常流程执行，只是 `tool_trace.status` 会保留触发来源。

`WebQueryPlanner` 使用 LLM 生成短查询头，避免把整段用户消息直接作为搜索词；每个查询头会合并必应、百度、Bing RSS、DuckDuckGo 与官网/百科结果。`RetrievalScorer` 增加时间与版本新鲜度评分；`ToolResultVerifier` 在置信度不足时最多重试一次，并在最终 LLM 回复中加入 `[Grounding]` 约束，要求只基于搜索结果回答事实和时效问题。`KatanaCrawler` 负责抓取候选 URL 和官方来源，`ContentExtractor` 负责从 HTML 中提取标题、正文、日期和版本号。

---

### 节点 13：DeepDiveNode（深潜节点）

**文件**：`sana/nodes/deep_dive_node.py`

**定位**：当 LLM 请求查看原始对话时，再次调用 LLM 获取更详细的信息。

```python
class DeepDiveNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        if ctx.tool_triggered and ctx.tool_target_batch:
            detail = f"[Details for {ctx.tool_target_batch}]"
            enhanced = ctx.augmented_input + "\n" + detail
            resp = backend.chat(cfg.model_id, [...], timeout=30)
            ctx.llm_raw_response = resp.content
        return NodeResult(next="format_check", context=ctx)
```

**当前实现状态**：`detail` 仍是占位字符串；`RawMemoryDB.fetch_raw_memory(batch_id)` 已在服务层实现，但 `DeepDiveNode` 尚未调用它读取真实原始对话。

---

### 节点 14：FormatCheckerNode（格式校验节点）

**文件**：`sana/nodes/format_check_node.py`

**定位**：只做 `<thinking>` / `<chat>` 开始标签完整性检查，不做内容或风格审查。

```python
class FormatCheckerNode(PipelineNode):
    MAX_RETRIES = 2

    def process(self, ctx: Context) -> NodeResult:
        if not self._should_check_format(ctx):
            return NodeResult(next="response_parser", context=ctx)
        if missing_tag(ctx.llm_raw_response):
            return self._reject(ctx, "回复缺少标签，请补充完整")
        return NodeResult(next="response_parser", context=ctx)
```

**行为规则**：

| 条件 | 行为 |
|------|------|
| 空回复 | 直接放行到 `response_parser` |
| 最近情绪轨迹 P < -0.2 或 intensity > 0.6 | 跳过格式校验，直接放行 |
| 缺少 `<thinking>` 或 `<chat>` 开始标签，且未达上限 | 写入 `review_feedback`、`review_retry_count += 1`，追加反馈后 `fallback="llm_call"` |
| 达到 2 次重试上限 | 自动补全标签，返回 `response_parser` |

**注意**：当前实现只检查开始标签，不检查闭合标签；格式校验通过后直接进入 `response_parser`。

---

### 节点 15：ResponseParserNode（响应解析节点）

**文件**：`sana/nodes/response_parser_node.py`

**定位**：解析 LLM 的原始输出，分离"思考过程"和"最终回复"。

```python
class ResponseParserNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        raw = ctx.llm_raw_response or ""
        if not raw.strip():
            ctx.chat = "[Empty response]"
            return NodeResult(next="sentence_segment", context=ctx)
        ctx.thinking = extract_tag(raw, "thinking")
        ctx.chat_raw = extract_tag(raw, "chat") or fallback_chat(raw)
        ctx.chat = clean_pause_tags(ctx.chat_raw)
        return NodeResult(next="sentence_segment", context=ctx)
```

**核心参数**：

| 输出字段 | 解析规则 | 用途 |
|----------|----------|------|
| `ctx.thinking` | `<thinking>...</thinking>` 标签内容 | LLM 的内心 OS / 思考过程 |
| `ctx.chat_raw` | `<chat>...</chat>` 标签内容 | 内部保留 pause 控制文本 |
| `ctx.chat` | 从 `chat_raw` 清洗 pause 标签 | 最终展示给用户的纯文本 |

**容错逻辑**：

- 空回复返回 `[Empty response]`。
- `<chat>` 标签被 `</thinking>` 误闭合时仍可解析。
- 没有 `<chat>` 时剥离残留标签并取最后一个非空行。

---

### 节点 16：SentenceSegmentNode（拆句节点）

**文件**：`sana/nodes/sentence_segment_node.py`

**定位**：将完整回复拆成逐句 segments，并把 `<pause>` 标签解析为每句出现前的延迟。

```python
class SentenceSegmentNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        ctx.segments = self.build_segments(ctx.chat_raw or ctx.chat)
        ctx.chat = clean_pause_tags(ctx.chat)
        return NodeResult(next="memory_update", context=ctx)
```

**核心参数（ctx.segments）**：

| 字段 | 类型 | 示例 | 意义 |
|------|------|------|------|
| `text` | str | `"白夜你终于来啦~"` | 该句可见文本 |
| `delay` | float | `0.6` | 该句出现前等待的秒数 |

**拆句规则**：按句号、问号、感叹号、分号、省略号、换行、`~` / `～` 拆分；`<pause>` 或 `<pause ms="800"/>` 可覆盖默认停顿。

---

### 节点 17：MemoryUpdateNode（记忆更新节点）

**文件**：`sana/nodes/memory_update_node.py`

**定位**：将本轮对话写入工作记忆和对话缓存。

```python
class MemoryUpdateNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        self.working_memory.append({"role": USER_NAME, "content": ctx.user_input})
        self.working_memory.append({"role": AGENT_NAME, "content": ctx.chat})
        if ctx.tool_trace.get("triggered"):
            self.working_memory.append({"role": "tool", "content": _tool_note(ctx.tool_trace)})
        self.chat_buffer.append({"role": USER_NAME, "content": ctx.user_input})
        self.chat_buffer.append({"role": AGENT_NAME, "content": ctx.chat})
        return NodeResult(next="consolidation", context=ctx)
```

**核心参数**：

| 列表 | 生命周期 | 用途 |
|------|----------|------|
| `working_memory` | 整个会话期，滑动窗口最多 20 条 | 短期对话历史，回传给 PromptBuilder 拼入提示词 |
| `chat_buffer` | 每次总结保存后清空 | 累积对话用于批量总结 |

工具调用轨迹会作为 `role="tool"` 的消息写入 `working_memory`，但不会写入 `chat_buffer`，因此不会进入长期记忆总结。

---

### 节点 18：ConsolidationNode（总结节点）

**文件**：`sana/nodes/consolidation_node.py`
**关联 Services**：`MemorySummarizer` + `RawMemoryDB` + `MemoryManager` + `ProfileManager`

**定位**：当对话缓存积累到一定量时，触发"记忆总结与归档"。

```python
class ConsolidationNode(PipelineNode):
    def process(self, ctx: Context) -> NodeResult:
        if len(ctx.chat_buffer) >= 20:
            batch = list(ctx.chat_buffer)
            bid = self.raw_db.save_raw_buffer(batch)
            ctx.chat_buffer.clear()
            result = self.summarizer.consolidate_buffer(batch, self.profile_mgr.load_profile())
            if result:
                self.memory.save_consolidated_events(result.get("events", []), batch_id=bid)
                self.profile_mgr.apply_batch_updates(result.get("profile_updates", []))
        return NodeResult(context=ctx)
```

**四个步骤**：

| 步骤 | 写入位置 | 数据内容 | 目的 |
|------|----------|----------|------|
| ① 原始日志 | MongoDB `sana_brain.raw_dialogue_batches` | 完整对话 json | 保留可追溯的原始数据 |
| ② LLM 总结 | — | 提取事件 + 档案更新建议 | 将非结构化对话压缩为结构化信息 |
| ③ 事件存储 | ChromaDB `sana_memories` | 事件摘要 + 实体 + 时间戳 | 供后续语义检索 |
| ④ 档案更新 | `user_profile.json` | 用户偏好/习惯更新 | 让 AI 越来越了解用户 |

**触发条件**：`chat_buffer` 累计 >= 20 条消息（即 10 轮对话后触发总结）。

**手动入口**：`SanaAgent.consolidate_memory()` 调用 `ConsolidationNode.consolidate(..., force=True)`；Streamlit 聊天输入框上方的“手动聚合记忆”按钮会触发该入口。总结失败时会自动重试一次并把错误提示回填给模型修正；仍失败时保留缓存并返回错误信息，不保存原始对话。总结成功后才保存原始对话、写入长期记忆并清空 `chat_buffer`。

---

## 完整数据流（文字版）

```text
用户输入 "今天好累啊…"
  -> PerceptionNode
     情绪=Distress, 意图=complain, 强度=0.7,
     user_repeat_count=1, user_behavior_type=normal
  -> BehavioralReasonerNode
     输出 behavioral_insight；未命中异常行为时情绪加成保持空
  -> ALMANode
     Distress -> P 下降，更新 emotional_trajectory
  -> DirectiveNode
     生成 [情绪状态] / [行为指引] / [回应策略]
  -> PersonaSelectionNode
     默认用户 -> deep，写入深层开放人格指令
  -> MemoryRecallNode
     查询 "今天好累啊" 召回历史记忆
  -> ProfileLoadNode
     读取 user_profile.json
  -> WorkingMemoryNode
     注入最近对话
  -> PromptBuilderNode
     system_prompt = 角色卡 + 人格指令 + 情绪指令 + Web Tool Policy + Tool Protocol + 格式约束
     augmented_input = 情绪轨迹 + Profile + Recent Conversation + Recall + User
  -> LLMCallNode
     调用 DeepSeek/OpenAI/Local LLM
  -> ToolInterceptNode
     有 <invoke_memory> -> DeepDiveNode
     有 <invoke_web> 或 ToolIntentDetector 判定需要 web -> WebSearchNode
     无工具 -> FormatCheckerNode
  -> WebSearchNode
     实体识别、多查询头抓取、评分，第二次 LLM 基于结果生成最终回复
  -> FormatCheckerNode
     缺标签且未超限 -> fallback llm_call
     其他 -> ResponseParserNode
  -> ResponseParserNode
     输出 ctx.chat + ctx.thinking
  -> SentenceSegmentNode
     输出 ctx.segments + 清理后的 ctx.chat
  -> MemoryUpdateNode
     写入 working_memory + chat_buffer；工具轨迹以 tool 消息写入 working_memory
  -> ConsolidationNode
     chat_buffer >= 20 时保存原始日志、总结事件、更新档案
```

---

## 文件索引

| 层级 | 路径 | 说明 |
|------|------|------|
| 入口 | `interfaces/cli.py` | 命令行交互界面 |
| | `interfaces/streamlit_app.py` | Streamlit 图形界面 + 控制面板 |
| 启动 | `sana/agent.py` | SanaAgent：创建服务实例、组装流水线、暴露 `chat()` 接口 |
| 流水线引擎 | `sana/core/engine.py` | PipelineEngine：注册节点、顺序执行、跳转 |
| | `sana/core/context.py` | Context 数据类：流水线的数据总线 |
| | `sana/core/node.py` | PipelineNode 抽象基类 + NodeResult |
| 节点 | `sana/nodes/*.py` | 19 个具体节点实现 |
| | `sana/nodes/sentence_segment_node.py` | 将回复拆成逐句 segments |
| | `sana/nodes/web_search_node.py` | 互联网查询工具执行节点 |
| 服务层 | `sana/services/alma_engine.py` | ALMA 情感计算引擎（OCEAN+PAD） |
| | `sana/services/behavioral_reasoner.py` | 用户行为模式推理 |
| | `sana/services/emotional_directive.py` | 情绪状态到自然语言指令的翻译 |
| | `sana/services/perception.py` | LLM 驱动的感知层 |
| | `sana/services/tool_registry.py` | 工具 schema 注册表 |
| | `sana/services/tool_intent_detector.py` | 通用 LLM 工具意图判断 |
| | `sana/services/web_tool_config.py` | 联网工具配置读取与持久化 |
| | `sana/services/web_tool_policy.py` | 联网工具等级与心情策略 |
| | `sana/services/web_alias_store.py` | 游戏/实体别名缓存 |
| | `sana/data/web_aliases.json` | 游戏/实体别名缓存数据 |
| | `sana/services/entity_resolver.py` | AI 自检实体识别 |
| | `sana/services/web_query_planner.py` | 多查询头生成 |
| | `sana/services/web_search_service.py` | 必应/百度/官网抓取 |
| | `sana/services/search_discovery_service.py` | Bing RSS / DuckDuckGo 搜索发现 |
| | `sana/services/katana_crawler.py` | Katana 爬虫封装 |
| | `sana/services/content_extractor.py` | HTML 内容提取 |
| | `sana/services/retrieval_scorer.py` | 搜索结果去重评分 |
| | `sana/services/result_verifier.py` | 搜索结果是否足以回答问题的验证 |
| | `sana/services/tag_normalizer.py` | pause 等标签规范化 |
| | `sana/services/memory_service.py` | ChromaDB 向量记忆存取 |
| | `sana/services/memory_summarizer.py` | LLM 驱动的对话总结 |
| | `sana/services/mongo_client.py` | MongoDB 原始日志存储 |
| | `sana/services/profile_manager.py` | 用户档案 JSON 管理 |
| 模型 | `sana/models/registry.py` | 模型配置注册中心 |
| | `sana/models/backend.py` | ModelBackend 抽象基类 |
| | `sana/models/local_backend.py` | 本地 LLM API 调用 |
| | `sana/models/openai_backend.py` | OpenAI API 调用 |
| | `sana/models/deepseek_backend.py` | DeepSeek API 调用 |
| 提示词 | `sana/prompts/system.py` | Sana 完整角色设定 system prompt |
| 配置 | `sana/config.py` | 默认模型配置 + 用户配置持久化 |
| | `user_profile.json` | 用户档案 + 模型配置持久化 |

---

## 设计思路核心脉络

1. **Pipeline 模式**：每个节点只做一件事，Context 传递所有状态；新增/移除/重排节点不影响其他节点。
2. **感知-行为-情感-认知四层结构**：Perception（感知输入）→ BehavioralReasoner（行为推理）→ ALMA（情感反应）→ Directive / Persona / Memory / Prompt（认知加工）→ LLM（生成输出）。
3. **双层记忆**：短期工作记忆（working_memory，滑动窗口 20 条） + 长期向量记忆（ChromaDB，语义检索）。
4. **情绪驱动**：ALMA 计算 PAD，DirectiveNode 翻译成行为指令，让模型输出随情绪状态变化。
5. **人格分层**：PersonaSelectionNode 在生成前选择 `deep` / `surface` 人格层，控制 Sana 对当前用户的开放程度。
6. **格式回炉**：FormatCheckerNode 检查标签完整性，缺失时最多回炉 2 次，超限后自动补全。
7. **自动学习**：ConsolidationNode 定期将对话总结为记忆和档案更新，实现"越聊越了解你"。
8. **结构化工具协议**：`ToolRegistry` 注册工具 schema；第一轮要么输出普通回复，要么只输出工具调用，不允许混入聊天内容；`ToolIntentDetector` 用通用 LLM 结构化判断兜底隐含工具意图。
9. **联网查询链路**：`WebSearchNode` 负责等级/心情策略、实体识别、LLM 查询规划、多来源抓取、新鲜度评分、结果验证、结果注入和第二次 LLM 回复。

---

## 当前实现与设计文档的差异

| 设计文档 | 预期 | 当前代码 |
|----------|------|----------|
| `2026-08-04-style-review-routing-cleanup-design.md` | `format_check -> style_review -> compliance_review` | `style_review` / `compliance_review` 已删除；当前路径为 `format_check -> response_parser` |
| `2026-07-29-style-compliance-review-design.md` | StyleReview / ComplianceReview 可回炉重写 | 两个节点均已删除，当前仅 `FormatCheckerNode` 做格式回炉 |
| `2026-07-29-user-behavior-detection-design.md` | ComplianceReviewNode 接收 ALMA 并处理用户行为 | `ComplianceReviewNode` 已删除；行为处理由 BehavioralReasonerNode + ALMANode 实现 |
