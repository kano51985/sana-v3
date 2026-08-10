# Sana 互联网查询工具实现计划

日期：2026-08-10
状态：实施中

## 目标

按照 `docs/superpowers/specs/2026-08-10-internet-query-tool-design.md` 实现 Sana 互联网查询工具，包括：

- 等级式自主触发和心情影响。
- AI 自检实体识别和低置信实体澄清。
- 必应/百度/官网直抓多查询头检索。
- Pipeline 工具拦截和执行。
- Streamlit 面板配置与调试状态标识。
- `SanaAgent.chat()` 返回 `tool_trace`。

本计划不包含提交 Git；实现完成后由用户自行决定提交时机。

## 完成定义

- 所有单元测试和集成测试通过。
- `chat()` 返回 `tool_trace`。
- Streamlit 侧栏可以修改并持久化联网配置。
- 页面能显示“联网查询工具已触发”等状态徽标。
- `农` 在 AI 自检和实体澄清流程下可以解析为 `王者荣耀`。
- 搜索失败、策略拦截、LLM 标签异常都不会中断聊天。
- `docs/pipeline-flow.md` 更新为包含 `web_search` 分支。

## 阶段 0：配置与别名缓存

### 新增文件

- `sana/services/web_tool_config.py`
- `sana/data/web_aliases.json`
- `sana/services/web_alias_store.py`
- `tests/test_services/test_web_tool_config.py`
- `tests/test_services/test_web_alias_store.py`

### 任务

1. 实现 `WebToolConfig` dataclass：
   - `enabled`
   - `autonomy_level`
   - `max_query_heads`
   - `results_per_head`
   - `max_injected_results`
   - `timeout_seconds`
   - `total_timeout_seconds`
   - `allow_bing`
   - `allow_baidu`
   - `allow_direct`
   - `mood_influence`
2. 实现 `WebToolConfigStore`：
   - 从 `user_profile.json` 的 `web_tool` 键读取。
   - 缺失时返回默认配置，不报错。
   - 保存时保留其他档案字段。
   - 非法值回退默认值。
3. 创建 `sana/data/web_aliases.json` 初始种子数据：
   - `王者荣耀`：`农`、`农药`、`王者`
   - `英雄联盟`：`撸啊撸`、`撸`、`lol`、`LOL`
   - `和平精英`：`吃鸡`
   - `金铲铲之战`：`铲铲`
   - `原神`：`原`
4. 实现 `WebAliasStore`：
   - 加载 JSON。
   - 按别名反查规范名。
   - 高置信学习写入，保存 `confidence`、`source_urls`、`updated_at`、`learned_by_ai`。
   - 保存时原子写临时文件后替换，避免损坏。

### 验收

- 配置读写往返一致。
- 非法配置不抛出异常。
- 别名缓存新增条目后可以立即查询到。

## 阶段 1：Context、策略与动态 Prompt

### 修改文件

- `sana/core/context.py`
- `sana/nodes/prompt_builder_node.py`

### 新增文件

- `sana/services/web_tool_policy.py`
- `tests/test_services/test_web_tool_policy.py`
- `tests/test_nodes/test_prompt_builder_web_tool.py`

### 任务

1. `Context` 新增字段：
   - `web_tool_enabled`
   - `web_autonomy_level`
   - `web_policy_block`
   - `web_query_heads`
   - `web_results`
   - `web_entity`
   - `web_error`
   - `tool_trace`
2. 实现 `WebToolPolicy.is_explicit_request()`：
   - 关键词规则。
   - 结合 `perception_data.intent`。
3. 实现 `WebToolPolicy.evaluate()`：
   - 等级 0：拦截。
   - 等级 1：只有显式请求放行。
   - 等级 2：显式请求放行，事实/时效/知识请求放行。
   - 等级 3：主动放行非必要请求；强烈坏心情可拦截非必要请求。
   - 等级 4：探索放行非必要请求；强烈坏心情可拦截显式请求。
4. 实现 `WebToolPolicy.build_policy_block()`：
   - 输出启用状态、等级、当前心情、触发规则、显式请求规则、坏心情规则、不伪造结果约束。
5. `PromptBuilderNode` 在启用联网时把策略块拼入 system prompt。

### 验收

- 等级和心情变化时策略块文本不同。
- 策略判断覆盖 0-4 等级矩阵。
- 现有无工具 Pipeline 测试不回归。

## 阶段 2：实体识别与查询规划

### 新增文件

- `sana/services/entity_resolver.py`
- `sana/services/web_query_planner.py`
- `tests/test_services/test_entity_resolver.py`
- `tests/test_services/test_web_query_planner.py`

### 任务

1. 实现 `EntityResolution` 数据结构：
   - `raw`
   - `canonical`
   - `aliases`
   - `confidence`
   - `need_clarify`
   - `evidence`
2. 实现 `EntityResolver.self_check()`：
   - 输入用户原文、感知实体、最近对话、别名缓存。
   - 本地缓存命中且可明确匹配时直接返回高置信。
   - 未命中时调用 LLM 输出 JSON。
   - LLM 失败时回退本地缓存，不阻塞主流程。
3. 实现 `EntityResolver.clarify_from_results()`：
   - 将澄清搜索结果交给 LLM 判断。
   - 多个可信来源指向同一规范名且置信度达到阈值时返回高置信。
4. 实现 `EntityResolver.learn_alias()`：
   - 只有高置信结果才写入别名缓存。
5. 实现 `WebQueryPlanner.build_heads()`：
   - 原文头。
   - 规范别名头。
   - 上下文限定词头。
   - 未识别规范名时只生成两个头。

### 验收

- `农` 在已有本地表或 AI 自检成功后返回 `王者荣耀`。
- 低置信时 `need_clarify=True`。
- 澄清证据不足时不写入缓存。
- 查询头数量受 `max_query_heads` 限制。

## 阶段 3：抓取服务与结果评分

### 新增文件

- `sana/services/web_search_service.py`
- `sana/services/retrieval_scorer.py`
- `tests/test_services/test_web_search_service.py`
- `tests/test_services/test_retrieval_scorer.py`

### 任务

1. 实现 `DirectSourceRegistry`：
   - 只维护可信官网/百科域名。
   - 不接受任意用户 URL。
2. 实现 `BingParser`：
   - 使用固定 HTML fixture 验证解析。
   - 提取标题、URL、摘要。
3. 实现 `BaiduParser`：
   - 使用固定 HTML fixture 验证解析。
   - 解析失败返回空结果。
4. 实现 `WebSearchService.search()`：
   - `ThreadPoolExecutor` 并行执行查询头。
   - 每个头按必应、百度、直抓顺序降级。
   - 设置请求头、超时、总预算。
   - 结果统一为 `query_head/title/url/snippet/source/fetched_at`。
   - `403/429` 标记来源本轮不可用。
5. 实现 `RetrievalScorer`：
   - 规范化 URL。
   - 按标题和 URL 去重。
   - 计算评分。
   - 跨查询头合并。
   - 限制最终结果数。

### 验收

- 必应解析 fixture 能提取至少一条结果。
- 百度解析 fixture 失败时返回空而不是异常。
- 单头所有来源失败时整体仍能继续。
- 重复结果只保留一条。

## 阶段 4：Pipeline 节点与 Agent 接线

### 修改文件

- `sana/nodes/tool_intercept_node.py`
- `sana/nodes/memory_update_node.py`
- `sana/agent.py`

### 新增文件

- `sana/nodes/web_search_node.py`
- `tests/test_nodes/test_web_search_node.py`
- 扩展现有 `tests/test_nodes/test_tool_intercept_node.py`
- 扩展 `tests/test_nodes/test_pipeline_routing.py`

### 任务

1. `ToolInterceptNode` 增加解析：
   - `<invoke_web query="..."/>`
   - 路由到 `web_search`。
   - 保留 `<invoke_memory>` 路由。
   - 标签格式非法时不触发。
2. 实现 `WebSearchNode.process()`：
   - 读取 `WebToolConfig`。
   - 调用 `WebToolPolicy.evaluate()`。
   - 被拦截时不发网络请求，调用第二次 LLM 生成正常回复。
   - 未拦截时执行实体自检、澄清、查询头生成、抓取、评分。
   - 把 `[Web Search Results]` 拼入 `augmented_input`。
   - 第二次调用主 LLM。
   - 写入 `tool_trace`。
   - 写入工作记忆 tool 消息。
3. `MemoryUpdateNode` 支持写入 tool 消息：
   - tool 消息只进 `working_memory`。
   - 不进入 `chat_buffer`。
4. `SanaAgent` 接线：
   - 创建配置、策略、别名、实体、规划、搜索、评分服务。
   - 注册 `web_search` 节点。
   - `chat()` 返回值包含 `tool_trace`。

### 验收

- fake LLM 输出 `<invoke_web>` 时进入 `web_search`。
- fake search service 返回结果时第二次 LLM 收到结果注入。
- 策略拦截时没有调用搜索服务。
- `tool_trace` 正确返回。
- 工作记忆中出现 tool 消息，`chat_buffer` 不出现。

## 阶段 5：Streamlit 面板与调试标识

### 修改文件

- `interfaces/streamlit_app.py`

### 任务

1. 侧栏新增“联网查询”展开区：
   - 启用开关。
   - 自主等级 0-4。
   - 查询头数、每头结果数、最终注入结果数。
   - 单请求超时。
   - 必应/百度/直抓开关。
   - 保存配置按钮。
2. 配置保存到 `user_profile.json` 的 `web_tool`。
3. 回复气泡下方显示状态徽标：
   - `联网查询工具已触发`
   - `记忆工具已触发`
   - `联网查询被心情拦截`
   - `联网查询失败`
   - `联网查询未触发`
4. 侧栏显示最近工具轨迹：
   - 查询头。
   - 实体解析结果。
   - 结果标题和 URL。
   - 评分。
   - 错误。
   - `tool_trace` JSON。
5. 历史消息渲染时也读取 `tool_trace` 并显示徽标。

### 验收

- 面板保存后重新启动仍能读回配置。
- 每轮 `chat()` 后页面能显示对应状态徽标。
- 轨迹区域能看到“农 → 王者荣耀”的解析结果。

## 阶段 6：文档与收尾验证

### 修改文件

- `docs/pipeline-flow.md`

### 任务

1. 更新 Pipeline 流程图，加入 `web_search` 分支。
2. 更新 Context 字段表。
3. 更新 `SanaAgent.chat()` 返回结构。
4. 运行完整测试：
   - `python -m pytest -q`
5. 手动冒烟：
   - 在 Streamlit 面板选择等级 2。
   - 输入“农现在什么版本”。
   - 确认工具状态徽标出现。
   - 确认 `tool_trace.entity.canonical` 为 `王者荣耀`。
   - 如果网络不可用，确认显示 `联网查询失败` 且聊天不中断。

### 验收

- 文档与最终代码一致。
- 测试全绿。
- 手动冒烟记录不进入自动测试。

## 风险与降级

- 必应/百度页面结构变化：解析器集中在一个文件，后续只改选择器。
- 搜索引擎反爬：`403/429` 直接降级，不重试。
- LLM 输出错误标签：不触发网络请求，不中断聊天。
- 联网过慢：单请求和总预算超时后继续生成回复。
- 实体 AI 自检增加一次 LLM 调用：失败时回退本地缓存和原词查询。
- 工作记忆增加 tool 消息：只进短期记忆，不进入长期总结。

## 涉及文件总览

新增：

- `sana/services/web_tool_config.py`
- `sana/services/web_tool_policy.py`
- `sana/services/web_alias_store.py`
- `sana/services/entity_resolver.py`
- `sana/services/web_query_planner.py`
- `sana/services/web_search_service.py`
- `sana/services/retrieval_scorer.py`
- `sana/nodes/web_search_node.py`
- `sana/data/web_aliases.json`

修改：

- `sana/core/context.py`
- `sana/nodes/tool_intercept_node.py`
- `sana/nodes/prompt_builder_node.py`
- `sana/nodes/memory_update_node.py`
- `sana/agent.py`
- `interfaces/streamlit_app.py`
- `docs/pipeline-flow.md`

测试新增或扩展：

- `tests/test_services/test_web_tool_config.py`
- `tests/test_services/test_web_alias_store.py`
- `tests/test_services/test_web_tool_policy.py`
- `tests/test_services/test_entity_resolver.py`
- `tests/test_services/test_web_query_planner.py`
- `tests/test_services/test_web_search_service.py`
- `tests/test_services/test_retrieval_scorer.py`
- `tests/test_nodes/test_web_search_node.py`
- 扩展 `tests/test_nodes/test_tool_intercept_node.py`
- 扩展 `tests/test_nodes/test_prompt_builder_web_tool.py`
- 扩展 `tests/test_nodes/test_pipeline_routing.py`

## 实施顺序

建议按阶段 0 到阶段 6 顺序实施。每个阶段结束后先运行该阶段测试，再进入下一阶段。
