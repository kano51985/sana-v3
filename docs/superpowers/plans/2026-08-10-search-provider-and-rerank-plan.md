# SearchProvider 抽象与召回质量优化计划

日期：2026-08-10
状态：待评审

## 背景

当前联网查询已经具备：

- LLM 短查询规划
- Bing RSS / DuckDuckGo 搜索发现
- Katana 爬虫
- HTML 内容提取
- 新鲜度评分
- 结果验证与接地回复

但实测仍然出现“召回数量多、有效答案少”的问题。主要原因：

- 搜索发现层返回了站点头/导航页。
- Katana 以站点头为 seed，抓到大量栏目页。
- 没有先做摘要级过滤，低相关候选直接进入爬取。
- 没有对最终候选做 LLM 相关性重排。

## 目标

- 参考 Web AI 平台的“搜索引擎索引 + 多路查询 + 摘要优先 + rerank”思路。
- 不建自建全网索引。
- 不强制依赖付费搜索 API。
- 通过可替换 `SearchProvider` 抽象，后续能无缝接入 SearXNG、Bing Web Search API、Tavily 等。

## 架构

```text
WebQueryPlanner
  -> SearchProvider 接口
     BingRssProvider
     DuckDuckGoProvider
     （可选）SearXNGProvider
     （可选）BingWebSearchProvider
     （可选）TavilyProvider
  -> SearchCandidate 统一数据
  -> CandidateClassifier
     过滤站点头 / 导航页 / 无关栏目
  -> SnippetFirstRanker
     基于标题 + 摘要 + 实体 + 时间信号打分
  -> CrawlPlanner
     只选高相关候选交给 Katana
  -> KatanaCrawler
  -> ContentExtractor
     提取正文、日期、版本号
  -> ResultReranker
     LLM 对少量候选做相关性重排
  -> ToolResultVerifier
  -> 最终 LLM 接地回复
```

## Phase 1：SearchProvider 抽象

### 新增

- `SearchProvider` 抽象基类
- `SearchResult` 数据结构
- `BingRssProvider`
- `DuckDuckGoProvider`
- `SearchProviderRegistry`

### SearchResult 结构

```python
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str
    published_at: str
    fetched_at: str
    url_kind: str
    entity_mentions: list[str]
    raw: dict
```

### 行为

- `WebSearchService` 不再直接调用具体解析器，而是通过 `SearchProviderRegistry` 调用启用的 provider。
- 每个 provider 失败不影响其他 provider。
- `SearchProviderRegistry` 记录 `provider_sources`、`provider_count`。

## Phase 2：CandidateClassifier

### 目标

判断发现结果是具体文章页、站点头、导航页还是未知。

### 判断信号

- URL 路径深度。
- URL 是否为站点根路径。
- URL 是否带文章特征参数。
- 标题/摘要是否提到解析实体。
- 标题/摘要是否包含问题所需的事实类型。
- 可选 LLM 结构化判断。

### 输出

```json
{
  "url_kind": "article",
  "relevance": 0.8,
  "entity_match": true,
  "reason": "标题和摘要都提到原神当前版本"
}
```

### 过滤规则

- `site_homepage` 不进入优先爬取。
- `category` 只在没有文章候选时作为备用。
- `unknown` 进入低优先队列。
- `article` 且 `entity_match=true` 进入高优先队列。

## Phase 3：SnippetFirstRanker

### 目标

在爬取前，先用摘要做第一轮评分，避免 Katana 爬一堆导航页。

### 评分输入

- 用户问题
- 查询头
- 解析实体
- 标题
- 摘要
- 发布时间
- 域名

### 行为

- 实体没有出现在标题/摘要中的结果降权。
- 没有时间信号的时效性问题降权。
- 分数低于阈值的候选不进入 `CrawlPlanner`。
- 每个查询头最多保留 N 个高优先候选。

## Phase 4：CrawlPlanner

### 目标

决定哪些 URL 值得交给 Katana，以及用什么模式爬。

### 模式

- `article_mode`：深度 1，只抓具体文章页。
- `site_mode`：抓官方来源，发现新的相关 URL。
- `skip`：不抓导航页。

### 输出

```python
@dataclass
class CrawlTask:
    url: str
    mode: str
    priority: float
    reason: str
```

## Phase 5：ContentExtractor 与 ResultReranker

### ContentExtractor

- 优先提取 `<main>`、`<article>`、`<h1>` 后正文。
- 过滤导航、广告、评论区。
- 输出标准化 `ContentDocument`：
  - `title`
  - `url`
  - `text`
  - `published_at`
  - `version`
  - `source`

### ResultReranker

- 输入：用户问题、查询头、候选标题/URL/正文片段。
- 输出：

```json
{
  "relevant": true,
  "confidence": 0.9,
  "answer_fragments": ["7.0版本", "诺德卡莱"],
  "reason": "正文包含当前版本和新角色信息"
}
```

- 只保留 `relevant=true` 且 `confidence >= 0.7` 的结果。
- 候选超过 8 条时只重排前 8 条，控制 LLM 成本。

## Phase 6：集成与追踪

### WebSearchNode 流程

```text
查询规划
-> SearchProvider 多路召回
-> CandidateClassifier
-> SnippetFirstRanker
-> CrawlPlanner
-> KatanaCrawler
-> ContentExtractor
-> ResultReranker
-> ToolResultVerifier
-> 最终回复
```

### tool_trace 新增

- `provider_sources`
- `discovery_count`
- `article_count`
- `filtered_nav_count`
- `crawl_tasks`
- `reranked_count`
- `reranker_scores`

## Phase 7：可选 SearXNGProvider

- 预留 `SearXNGProvider` 配置：
  - `searxng_url`
  - `enabled`
  - `timeout_seconds`
- 默认关闭，不要求用户部署。
- 接入后通过统一 `SearchResult` 返回结果。

## 测试计划

- `SearchProvider`：Bing RSS / DuckDuckGo 解析统一为 `SearchResult`。
- `CandidateClassifier`：文章页、站点头、导航页分类。
- `SnippetFirstRanker`：低相关候选被过滤。
- `CrawlPlanner`：只有高优先候选进入 Katana。
- `ContentExtractor`：正文抽取和导航过滤。
- `ResultReranker`：fake LLM 高相关/低相关判断。
- 集成测试：fake provider + fake crawler 跑完整流程。
- 回归：现有 60 个测试保持通过。

## 验收标准

- “原神当前版本、最近角色、配队”返回的候选以具体文章页为主。
- 站点头/导航页不再作为主要 `crawl_sources`。
- Katana 只抓经过筛选的候选，不抓大量无关栏目。
- `ToolResultVerifier` 输入的是高质量候选，而非 8 条无关导航页。
- 不依赖付费 API 也能运行。
- 后续新增 Bing Web Search API / Tavily / SearXNG 时，只增加 provider，不改主流程。

## 涉及文件

新增：

- `sana/services/search_provider.py`
- `sana/services/search_provider_registry.py`
- `sana/services/candidate_classifier.py`
- `sana/services/snippet_ranker.py`
- `sana/services/crawl_planner.py`
- `sana/services/result_reranker.py`
- `sana/models/search.py` 或对应 dataclass 文件
- 对应测试文件

修改：

- `sana/services/search_discovery_service.py`
- `sana/services/web_search_service.py`
- `sana/services/katana_crawler.py`
- `sana/services/content_extractor.py`
- `sana/nodes/web_search_node.py`
- `sana/core/context.py`
- `sana/services/web_tool_config.py`
- `interfaces/streamlit_app.py`
- `docs/pipeline-flow.md`
