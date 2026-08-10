# Pause 标签规范化与 Katana 爬虫集成计划

日期：2026-08-10
状态：待评审

## 背景

当前存在两个问题：

1. LLM 可能输出 `< pause />`、`< pause ms="600"/>` 这类带空格的不规范 pause 标签，当前解析器只匹配 `<pause...>`，导致标签显示在聊天页面，同时气泡延迟没有生效。
2. 免费抓取必应/百度 HTML 不稳定，容易被重定向、验证码或 SSL 中断拦截，导致联网查询经常返回 0 条结果。

## 方向

- pause 规范化放在 `FormatCheckerNode`，形成“生成 → 标签规范化 → 拆句 → 气泡延迟”的闭环。
- 搜索发现使用免费接口：Bing RSS、DuckDuckGo HTML。
- 内容抓取使用 Katana 爬虫，抓取搜索发现出的候选 URL、官网和 Wiki。
- 不引入付费搜索 API。

## Phase 1：Pause 标签规范化与气泡延迟闭环

### 目标

无论 LLM 输出 `<pause>`、`< pause />`、`< pause ms="600"/>`，都只表现为气泡之间的延迟，不显示原始标签。

### 改动

- 新增 `TagNormalizer`，集中处理不规范标签。
- `FormatCheckerNode` 在标签校验前先调用 `TagNormalizer.normalize()`。
- `pause_parser.PAUSE_RE` 改为允许 `<` 后出现空格，例如 `<\s*pause\b...>`。
- `SentenceSegmentNode` 继续从规范化后的标签生成 `delay`。
- Streamlit 继续使用 `segments[].delay` 控制气泡延迟。

### 闭环

```text
LLM 输出
  -> FormatCheckerNode
     -> TagNormalizer.normalize()
     -> <pause/> / <pause ms="600"/>
  -> ResponseParserNode
     -> chat_raw 保留规范化标签
  -> SentenceSegmentNode
     -> segments[].delay = 0.6 / 0.6 等
  -> Streamlit _render_live_assistant
     -> time.sleep(delay)
     -> 气泡延迟出现，不显示标签
```

### 测试

- `< pause />` 被规范化为 `<pause/>`。
- `< pause ms="600"/>` 被规范化为 `<pause ms="600"/>`。
- `strip_pause_tags()` 能清除所有带空格变体。
- `SentenceSegmentNode.build_segments()` 能生成对应 `delay`。
- `chat` 和 `segments[].text` 中不出现 pause 标签。

## Phase 2：免费搜索发现层

### 目标

用不需要 API Key 的免费接口先得到候选 URL，不再直接依赖必应/百度 HTML。

### 数据源

- Bing RSS：`https://www.bing.com/search?format=rss&q=...`
- DuckDuckGo HTML：`https://html.duckduckgo.com/html/?q=...`
- 保留现有必应/百度 HTML 作为备用来源。

### 改动

- 新增 `SearchDiscoveryService`。
- 新增 `BingRssParser` 和 `DuckDuckGoParser`。
- 统一返回：

```python
{
  "title": "...",
  "url": "https://...",
  "snippet": "...",
  "source": "bing_rss" | "duckduckgo",
  "fetched_at": "..."
}
```

- 每个来源失败只标记该来源，不中断查询。

### 测试

- 固定 XML/HTML fixture 能解析出标题、URL、摘要。
- 一个来源失败时继续尝试其他来源。

## Phase 3：Katana 爬虫内容抓取层

### 目标

用 Katana 抓取搜索发现出的候选 URL、官网和 Wiki 页面，解决 requests 直抓被反爬或 TLS 拦截的问题。

### 依赖

Katana 是免费开源工具，但当前机器 PATH 中没有安装。实现时通过配置指定二进制路径，不把 Katana 作为 Python 包依赖。

### 改动

- 新增 `KatanaCrawler`。
- 配置项：
  - `katana_bin`
  - `max_depth`
  - `max_pages`
  - `timeout_seconds`
  - `concurrency`
  - `allowed_domains`
  - `enabled`
- 调用方式：

```bash
katana -u "https://ys.mihoyo.com/" \
  -d 2 \
  -jc \
  -json \
  -silent \
  -timeout 5 \
  -concurrency 3
```

- 具体 flags 以安装版本为准。
- 解析 Katana JSON 输出，提取可抓取 URL。
- 对抓取到的 HTML 做内容提取：标题、正文片段、日期、版本号。
- 只允许 `https` 和 `allowed_domains` 中的域名。
- 二进制不存在、超时或解析失败时返回错误，不影响其他来源。

### 测试

- 使用 fake subprocess 输出模拟 Katana JSON。
- 使用 HTML fixture 验证内容提取。
- Katana 未安装时走降级，不中断对话。

## Phase 4：WebSearchNode 集成

### 目标

把搜索发现和 Katana 抓取接入现有联网查询流程。

### 流程

```text
WebSearchNode
  -> WebQueryPlanner 生成短查询
  -> SearchDiscoveryService
     Bing RSS -> DuckDuckGo -> 备用 HTML 搜索
  -> 得到候选 URL
  -> KatanaCrawler 抓取候选页 / 官网 / Wiki
  -> 内容提取
  -> RetrievalScorer 去重、新鲜度评分
  -> ToolResultVerifier
  -> 最终 LLM 接地回复
```

### 改动

- `WebSearchService` 内部增加发现层和爬虫层，或拆成 `SearchDiscoveryService + KatanaCrawler + ContentExtractor`。
- `tool_trace` 增加：
  - `discovery_sources`
  - `crawl_sources`
  - `crawl_error`
  - `katana_available`

## Phase 5：测试与文档

- 新增 Phase 1 到 Phase 4 对应单元测试。
- 新增 fake search discovery 和 fake Katana 的 Pipeline 集成测试。
- 更新 `docs/pipeline-flow.md`。
- 更新 Streamlit 面板中的来源和爬虫状态显示。

## 验收标准

- 页面不再显示任何形式的 pause 标签。
- 气泡延迟仍按 `segments[].delay` 正常工作。
- “原神当前版本、最近角色、配队”能通过免费搜索发现层拿到候选 URL。
- Katana 可用时能抓取候选页面并提取内容。
- Katana 不可用时查询仍能降级，不报错、不中断。
- 49 个现有测试保持通过，新增测试全部通过。

## 涉及文件

新增：

- `sana/services/tag_normalizer.py`
- `sana/services/search_discovery_service.py`
- `sana/services/katana_crawler.py`
- `sana/services/content_extractor.py`
- 对应测试文件

修改：

- `sana/nodes/format_check_node.py`
- `sana/nodes/pause_parser.py`
- `sana/services/web_search_service.py`
- `sana/nodes/web_search_node.py`
- `sana/core/context.py`
- `interfaces/streamlit_app.py`
- `docs/pipeline-flow.md`
