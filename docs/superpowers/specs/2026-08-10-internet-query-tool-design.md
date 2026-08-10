# Sana 互联网查询工具设计

日期：2026-08-10
状态：待评审

## 背景

当前 Sana 只有 `<invoke_memory>` 一个工具调用分支，由主 LLM 输出标签后由 `ToolInterceptNode` 正则拦截。项目已经具备感知层实体提取、ALMA/PAD 心情计算、情绪指令注入、Streamlit 侧栏配置和 Pipeline 节点机制。

本次目标是为 Sana 增加一个“互联网查询工具”：Sana 可以自行判定是否调用，调用决策受当前心情和可配置自主等级影响；遇到“农”这类游戏代号时，能够自动识别为“王者荣耀”。

## 目标

- 新增全领域互联网查询能力，默认采用免费网页抓取。
- Sana 自主决定是否调用，但必须有确定性的等级和心情兜底，避免乱调。
- 提供 0-4 级自主等级，可在 Streamlit 面板选择，Prompt 随等级动态拼接。
- 心情对触发决策有强影响，同时显式请求的执行规则随等级变化。
- 实体识别采用 AI 自检优先 + 本地别名缓存兜底；低置信时默认先做实体澄清查询，高置信后自动维护别名缓存。
- 多查询头并行检索，使用轻量评分合并结果，不引入真实神经网络模型。
- 页面显示“xx 工具已触发”状态标识，侧栏显示最近工具轨迹，方便调试。
- 查询结果只进入当前轮和当前会话工作记忆，不自动写入长期记忆。

## 非目标

- 不采用原生 function calling 作为主协议。
- 不实现真实多头注意力或额外向量模型。
- 不抓取用户提供的任意 URL。
- 不做后台异步搜索。
- 不把搜索结果自动沉淀进长期记忆。
- 不在自动测试中依赖真实联网结果。

## 决策摘要

- 主协议：LLM 输出 `<invoke_web>` 标签，Pipeline 拦截并执行。
- 数据源：必应为主，百度为备用，官网/百科直抓为第二层兜底。
- 自主等级：0 关闭、1 显式、2 克制、3 主动、4 探索。
- 心情影响：强影响；等级越高，坏心情越可能拒绝或敷衍非必要请求。
- 实体识别：AI 自检优先，本地别名缓存兜底，低置信时进行一轮实体澄清查询。
- 查询头：默认最多 3 个，分别为原文头、规范别名头、上下文限定词头。
- 结果评分：确定性轻量评分，去重后最多注入 6-8 条。
- 调试：`SanaAgent.chat()` 返回 `tool_trace`，Streamlit 显示工具状态徽标。

## 整体架构

新增以下服务与节点：

| 组件 | 职责 |
|------|------|
| `WebToolConfig` | 保存启用状态、自主等级、查询头数、结果数、超时和来源开关。 |
| `WebToolPolicy` | 根据等级和 ALMA 心情生成动态策略块，并做确定性放行/拦截。 |
| `WebAliasStore` | 维护初始别名表和 AI 自动学习出的别名缓存。 |
| `EntityResolver` | AI 自检实体，必要时发起实体澄清查询，高置信后更新别名缓存。 |
| `WebQueryPlanner` | 生成最多 3 个查询头。 |
| `WebSearchService` | 并行抓取必应、百度、官网/百科，并解析结果。 |
| `DirectSourceRegistry` | 维护允许直抓的官网/百科域名，作为 `WebSearchService` 内部表使用。 |
| `RetrievalScorer` | 去重、评分、合并多来源结果。 |
| `WebSearchNode` | 执行策略判断、实体识别、检索、结果注入和第二次 LLM 回复。 |
| `ToolInterceptNode` | 增加 `<invoke_web>` 识别并路由到 `WebSearchNode`。 |
| `PromptBuilderNode` | 动态拼入 `[Web Tool Policy]`。 |
| `SanaAgent.chat()` | 返回 `tool_trace` 给界面调试。 |

## 配置

配置文件放在 `user_profile.json`，字段结构如下：

```json
{
  "web_tool": {
    "enabled": true,
    "autonomy_level": 2,
    "max_query_heads": 3,
    "results_per_head": 3,
    "max_injected_results": 8,
    "timeout_seconds": 2.5,
    "total_timeout_seconds": 8,
    "allow_bing": true,
    "allow_baidu": true,
    "allow_direct": true,
    "mood_influence": "strong"
  }
}
```

别名缓存单独放在 `sana/data/web_aliases.json`，避免和用户档案混在一起。初始数据示例：

```json
{
  "王者荣耀": {
    "aliases": ["农", "农药", "王者"],
    "source": "seed"
  },
  "英雄联盟": {
    "aliases": ["撸啊撸", "撸", "lol", "LOL"],
    "source": "seed"
  },
  "和平精英": {
    "aliases": ["吃鸡"],
    "source": "seed"
  },
  "金铲铲之战": {
    "aliases": ["铲铲"],
    "source": "seed"
  },
  "原神": {
    "aliases": ["原"],
    "source": "seed"
  }
}
```

AI 自动写入的条目会额外记录 `confidence`、`source_urls`、`updated_at` 和 `learned_by_ai` 字段，便于审计和回滚。

## 自主等级与心情策略

### 等级定义

| 等级 | 名称 | 行为 |
|------|------|------|
| 0 | 关闭 | 不启用联网查询，`<invoke_web>` 被忽略。 |
| 1 | 显式 | 只有明确说“帮我查/搜一下”才触发；明确请求必须执行，心情只影响回复语气。 |
| 2 | 克制 | 默认等级；明确请求必须执行；事实性、时效性、知识性问题可自行触发；坏心情降低非必要查询深度。 |
| 3 | 主动 | 有信息价值或 Sana 好奇时可触发；明确请求通常执行，强烈坏心情可以敷衍或拒绝非必要请求。 |
| 4 | 探索 | 更愿意主动查询；强烈坏心情下，明确请求也可能被拒绝或敷衍。 |

### 显式请求识别

显式请求由关键词规则和感知层意图共同判断，关键词包括但不限于：

```text
帮我查、查一下、查查、搜一下、搜搜、百度一下、帮我看看、查一查、搜一搜
```

同时要求 `perception_data.intent` 属于 `ask` 或 `chat` 且包含明确动作词。规则不命中时不视为显式请求。

### 心情偏置

- `P` 越高越好奇，`P` 越低越不积极。
- `A` 越高越有动力，`A` 越低越懒。
- `D` 越高越坚持，`D` 越低越容易放弃或敷衍。
- `Joy`、`Admiration` 增加触发意愿。
- `Distress`、`Anger`、`Reproach` 降低非必要触发。
- 强烈坏心情定义为 `P < -0.35`，或负面情绪强度 `>= 0.6`。

### 确定性兜底

`WebToolPolicy` 在 LLM 输出 `<invoke_web>` 后执行一次校验：

- 等级 0：拦截，不发网络请求。
- 等级 1-2：显式请求放行；非显式请求按等级规则判断。
- 等级 3：强烈坏心情时可拒绝非必要请求。
- 等级 4：强烈坏心情时可拒绝或敷衍显式请求。
- 被拦截时状态为 `blocked`，LLM 根据心情和等级直接生成正常回复。

## 动态 Prompt 拼接

`PromptBuilderNode` 在启用联网查询时，把策略块拼到 system prompt 中。示例：

```text
[Web Tool Policy]
Enabled: true
Autonomy: 2 - 克制
当前心情: 心情平稳

触发规则:
- 只有需要实时、外部、事实或时效信息，或用户明确要求时，才使用 <invoke_web>。
- 用户明确要求时必须执行，但坏心情下可以更简短。
- 查询必须包含具体 query，不要伪造搜索结果。
- 不知道答案时不要编造，宁可诚实说没查到。
```

策略块由 `WebToolPolicy.build_policy_block()` 生成，等级和心情变化时内容动态变化。

## 实体识别与两阶段查询

### AI 自检

每次进入 `WebSearchNode` 后，`EntityResolver` 先执行一次 AI 自检：

输入：

- 用户原文
- 感知层 `entities`
- 最近对话
- 本地别名缓存
- 可选召回记忆

AI 输出 JSON：

```json
{
  "recognized": true,
  "canonical": "王者荣耀",
  "aliases": ["农", "农药"],
  "confidence": 0.95,
  "evidence": "用户语境是游戏，农是农药/王者荣耀的简称"
}
```

### 两阶段流程

1. AI 自检置信度 `>= 0.85`：直接使用规范名，不额外做澄清查询。
2. 置信度低于阈值或无法识别：先发一轮“实体澄清查询”，例如 `农 是什么意思`、`农 农药 游戏简称`。
3. 澄清结果交回 AI 自检；多个可信来源指向同一规范名且置信度 `>= 0.85` 时，使用该规范名。
4. 高置信结果自动写入 `web_aliases.json`，记录来源 URL 和置信度。
5. 找不到可靠证据时不写入缓存，不强行猜测，直接使用原词继续主查询或诚实说明不确定。
6. 实体澄清最多只做一轮，不递归。

## 多查询头

默认生成 3 个查询头：

| 查询头 | 示例 |
|--------|------|
| 原文头 | `农现在什么版本` |
| 规范别名头 | `王者荣耀 农药` |
| 上下文限定词头 | `王者荣耀 最新版本` |

上下文限定词从用户原文和感知层意图中提取，例如：版本、英雄、皮肤、赛事、更新、攻略、规则、价格、新闻、今天、最新。

`max_query_heads` 可以在面板调节。未识别出规范名时，只使用原文头和上下文头。

## 抓取服务

`WebSearchService.search()` 使用 `ThreadPoolExecutor` 并行执行查询头，最大并发数等于 `max_query_heads`。

每个查询头按以下来源顺序尝试：

1. 必应：`https://cn.bing.com/search?q=...`
2. 百度：`https://www.baidu.com/s?wd=...`
3. 官网/百科直抓：只访问 `WebAliasStore` 和 `DirectSourceRegistry` 中维护的域名。

请求使用固定 `User-Agent` 和 `Accept-Language: zh-CN`。解析器使用标准库 HTMLParser 和简单选择器匹配，不新增第三方依赖。

结果统一为：

```python
{
  "query_head": "王者荣耀 最新版本",
  "title": "...",
  "url": "https://...",
  "snippet": "...",
  "source": "bing",
  "fetched_at": "...",
}
```

## 评分与合并

`RetrievalScorer` 对每条结果计算确定性分数：

```text
score = 30 * alias_hit
      + 20 * query_term_overlap
      + 15 * source_quality
      + 5 * snippet_bonus
      - position_penalty
```

- `alias_hit`：规范名或别名出现在标题/摘要中。
- `query_term_overlap`：查询词在标题/摘要中的覆盖率。
- `source_quality`：官网、百科、Wiki、知乎等可信来源加分。
- `snippet_bonus`：摘要长度适中加分。
- `position_penalty`：结果越靠后分数越低。

按归一化 URL 和标题去重，跨查询头合并后取最高分，最终注入 LLM 的结果不超过 `max_injected_results`。

## Pipeline 接入

更新后的主流程：

```text
input → perception → behavioral_reasoner → alma → directive
→ persona_selection → memory_recall → profile_load → working_memory
→ prompt_builder
→ llm_call
→ tool_intercept
   ├── 无工具 → format_check
   ├── <invoke_memory> → deep_dive → format_check
   └── <invoke_web> → web_search → format_check
→ response_parser → sentence_segment → memory_update → consolidation
```

`WebSearchNode` 内部流程：

1. 检查 `WebToolPolicy`。
2. 实体 AI 自检；低置信时做一轮实体澄清查询。
3. 生成查询头并抓取。
4. 评分、合并、截断。
5. 把 `[Web Search Results]` 拼进 `augmented_input`。
6. 第二次调用主 LLM 生成最终回复。
7. 写入 `tool_trace` 和当前轮工作记忆中的 tool 消息。

## Context 与返回结构

`Context` 新增字段：

```python
web_tool_enabled: bool = False
web_autonomy_level: int = 2
web_policy_block: str = ""
web_query_heads: list[str] = field(default_factory=list)
web_results: list[dict] = field(default_factory=list)
web_entity: dict = field(default_factory=dict)
web_error: str = ""
tool_trace: dict = field(default_factory=dict)
```

`SanaAgent.chat()` 返回：

```json
{
  "tool_trace": {
    "triggered": true,
    "tool": "web",
    "status": "executed",
    "query_heads": ["农", "王者荣耀 农药", "王者荣耀 最新版本"],
    "results_count": 6,
    "entity": {
      "raw": "农",
      "canonical": "王者荣耀",
      "confidence": 0.95
    },
    "error": ""
  }
}
```

工作记忆新增一条 `tool` 消息，例如：

```json
{
  "role": "tool",
  "content": "[Web] 查询：王者荣耀 最新版本；结果 6 条；来源 bing/baidu"
}
```

该消息只进入 `working_memory`，不进入 `chat_buffer`，因此不会参与长期记忆总结，也不会显示为 Sana 的可见聊天内容。

## Streamlit 面板

侧栏新增“联网查询”展开区，控件包括：

- 启用/关闭开关
- 自主等级 0-4 选择
- 每轮最大查询头数
- 每头结果数
- 最终注入结果数
- 单请求超时时间
- 必应/百度/官网兜底开关
- 最近一次工具轨迹

轨迹区域显示：

- 查询头
- 识别出的实体
- 结果标题和 URL
- 评分
- 失败原因
- `tool_trace` 原始 JSON

回复气泡下方显示调试状态徽标，文案固定为：

- `联网查询工具已触发`
- `记忆工具已触发`
- `联网查询被心情拦截`
- `联网查询失败`
- `联网查询未触发`

状态徽标优先使用 `st.badge`；版本不支持时使用 HTML 小标签兜底。不使用表情符号。

## 错误处理

- 单头、单来源失败不中断：按必应 → 百度 → 官网/百科顺序降级。
- `403 / 429`：标记该来源本轮不可用，不重试相同 URL。
- 单请求默认超时 2.5 秒，整轮默认不超过 8 秒。
- 实体澄清最多一轮，避免递归。
- 不抓取用户提供的任意 URL，只访问搜索页和本地维护的直抓域名。
- 结果过长时截断，不膨胀 Prompt。
- 策略拦截时不发网络请求。
- 所有异常写入 `tool_trace`，聊天不中断。
- 配置非法时回退默认值。

## 测试策略

单元测试：

- `WebToolPolicy`：0-4 等级、显式请求、正负心情、拒绝/敷衍规则。
- `EntityResolver`：`农 → 王者荣耀`、未知实体低置信、高置信自动入库、不污染缓存。
- `WebQueryPlanner`：3 个查询头组成。
- 必应/百度 HTML 解析：固定 HTML fixture 验证标题、URL、摘要。
- `RetrievalScorer`：去重、排序、合并上限。
- `ToolInterceptNode`：memory / web / 禁用路由。
- `WebSearchNode`：执行、拦截、失败、无结果四种路径。
- `PromptBuilderNode`：不同等级和心情生成不同 `[Web Tool Policy]`。
- 配置持久化：面板保存后能读回。

集成测试：

- fake LLM + fake search service 跑完整 Pipeline。
- 验证 `<invoke_web>` 进入查询分支。
- 验证查询结果注入第二次 LLM 调用。
- 验证 `chat()` 返回 `tool_trace`。
- 验证 Streamlit 所需状态字段存在。

真实联网只做手动冒烟测试，不进自动测试。

## 涉及文件

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
- 对应测试文件

修改：

- `sana/core/context.py`
- `sana/nodes/tool_intercept_node.py`
- `sana/nodes/prompt_builder_node.py`
- `sana/nodes/memory_update_node.py`
- `sana/agent.py`
- `interfaces/streamlit_app.py`
- `docs/pipeline-flow.md`

## 未来扩展

- 接入托管搜索 API，作为免费抓取的备用来源。
- 在面板提供别名缓存编辑和回滚入口。
- 可选 LLM 重排，进一步提高结果相关性。
- 把高价值查询结果按用户确认后写入长期记忆。
