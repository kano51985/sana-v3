# Sana 多用户双模式搜索平台重构设计

日期：2026-08-14

状态：设计章节已确认，等待用户审阅书面规格

范围：平台运行时、多用户边界、联网搜索、证据闭环、记忆迁移和 Streamlit 客户端重做

## 1. 摘要

本次重构采用“模块化单体 + 显式持久化状态机”路线，将当前进程内的单用户 Agent 改造成可部署的多用户服务：

- FastAPI 提供认证、会话、Run API 和 Server-Sent Events（SSE）。
- PostgreSQL 是业务状态、工作流状态和审计记录的唯一事实源。
- Redis 承担 Celery broker、热缓存、限流和短期进度流；Redis 数据丢失后可由 PostgreSQL 恢复。
- Celery Worker 执行快速搜索、深度研究和受控抓取任务。
- Streamlit 重做为纯客户端，不再持有全局 Agent、用户状态、密钥或数据库访问权。
- 联网能力使用完全自动的 FAST / RESEARCH 双模式；用户不需要选择模式。
- 现有 MongoDB、ChromaDB 和 `user_profile.json` 只迁移用户记忆资产；旧配置、密钥、搜索历史和旧界面状态不迁移。

该方案保留现有 `SearchPlan`、事实意图、候选筛选和证据验证方向，但不保留当前的串行编排、共享可变服务和不准确的状态/Trace 语义。

## 2. 背景与问题证据

最新测试请求同时询问 Apex Legends 的角色改动、当前版本和当前阵容。日志显示：

- Plan 为 `PARTIAL`，包含 4 个 Task 和 12 条 Query。
- 每条 Query 都被拼入 `sana！我好久没碰apex啦` 等对话片段。
- Provider 阶段约 8 秒，Crawl 阶段约 19 秒。
- `katana_available=false`，`http_fallback_count=0`。
- 系统产生 5 条 Evidence，但 `retrieval_confidence=0`，4 个事实全部缺失。
- `query_count=12` 表示计划数量，而非真实完成数量。

代码审阅进一步确认：

1. `EntityContextBuilder` 会把中文对话片段作为 context term，`WebQueryPlanner` 再把它们拼回 Query。
2. Planner、实体提取和逐 Query refine 发生在 Search deadline 之外，`max_llm_calls` 没有执行约束。
3. Task 串行执行，但每个 Task 内又嵌套 Query 线程池和 Provider 线程池，形成难以控制的并发与取消语义。
4. Katana 未在启动时预检；降级发生得太晚，HTTP fallback 得不到保留预算。
5. `EvidenceBuilder` 会把一个 Task 的全部结果包装成对应事实的 Evidence，即使结果并不支持该事实。
6. 搜索摘要可能被建模为已抓取文档，SearchHit、Document 和 Evidence 的语义没有真正分离。
7. 状态被持久化，但没有 lease、checkpoint、resume、幂等键或安全重放，所以并不构成可恢复执行。
8. `SearchPlanExecutor`、`WebSearchNode` 和 `WebSearchService` 仍是大型编排器，`sana/search/discovery` 等目标分层没有真正接管能力。
9. 当前测试从 Windows 用户环境读取了真实 DeepSeek 密钥并发出网络请求，测试环境不隔离。
10. `Context`、Agent 实例、Provider trace、`slow_hosts`、工作记忆等包含共享可变状态，不满足多用户并发隔离。

## 3. 已确认的产品决策

### 3.1 双模式

- `FAST`：面向简单事实问题，执行软预算 12 秒，用户可见 p95 不超过 15 秒。
- `RESEARCH`：面向复杂、多事实、多跳、强时效或高后果问题，硬上限 120 秒。
- 模式完全由系统自动决定，不提供手动模式开关。
- FAST 证据不足时按价值自动升级；普通低风险问题按时返回 PARTIAL，高价值事实才升级为 RESEARCH。

### 3.2 部署目标

- 直接按多用户服务器设计。
- 所有状态均为 request/run scoped，不允许通过全局 Agent 实例共享用户数据。
- 支持水平扩展 API 和 Worker。

### 3.3 基础设施

- PostgreSQL：权威业务与工作流存储。
- Redis：Celery broker、缓存、限流和短期事件流。
- Celery：后台 Worker 适配器；领域模型不依赖 Celery 类型。
- FastAPI：HTTP API 和 SSE。
- 不引入 Temporal，不采用微服务拆分。

### 3.4 兼容范围

- 保留并迁移用户记忆数据。
- 允许重做 Streamlit、配置系统、搜索历史和内部模块结构。
- 旧 Streamlit 仅在回滚窗口内保留，不继续扩展。

## 4. 目标与非目标

### 4.1 目标

- 建立可恢复、可取消、可审计的 Run/Step/Attempt 执行模型。
- 保证多租户会话、记忆、搜索状态、密钥和配额隔离。
- 将 Query 规划、发现、抓取、证据验证和回答合成分成可独立测试的模块。
- 严格区分 SearchHit、DocumentVersion、Chunk、Evidence 和 Citation。
- 在 deadline 内保留验证与回答合成时间。
- 提供可量化的路由、质量、时延、成本与恢复指标。
- 对旧用户记忆提供可重跑、可核验的迁移流程。

### 4.2 非目标

- 本周期不改变 ALMA、人格选择和行为推理的产品语义；只把它们改成多用户安全的 request-scoped 模块。
- 不建立独立 Planner、Crawler、Evidence 微服务。
- 不引入 Kafka、Temporal 或独立向量数据库集群。
- 不迁移旧搜索运行记录、旧 Web 配置或旧模型密钥。
- 不要求第一阶段支持任意规模的长篇 Deep Research；RESEARCH 上限为 120 秒。
- 不在第一阶段保留原始 HTML；默认只持久化经过安全清洗且有长度上限的正文版本。

## 5. 核心原则

1. PostgreSQL 是唯一事实源；Redis 和 Worker 都是可替换、可恢复的执行设施。
2. LLM 决定语义目标，确定性代码负责预算、状态、并发、停止和恢复。
3. 外部调用必须位于 Step Attempt 中，并具备 deadline、错误类型和幂等键。
4. SearchHit 不是 Evidence；Citation 只能引用已验证的正文证据。
5. 状态、回答质量和停止原因是三个正交维度。
6. 每个模块拥有自己的模型和 Repository 接口，禁止跨模块直接访问数据表。
7. 网页内容始终是不可信数据，不能影响工具权限或工作流控制。
8. 测试默认禁止真实网络、真实密钥和真实用户配置。
9. 先用量化指标验证新链路，再切流和删除旧实现。

## 6. 运行时拓扑

```text
Streamlit / Web / API Client
          |
          | HTTPS + SSE
          v
FastAPI API Process
  - Auth / Tenant Context
  - Conversation API
  - Search Run API
  - Rate Limit / Quota
  - SSE Progress
          |
          | transaction: SearchRun + OutboxEvent
          v
PostgreSQL <--------------------> Outbox Dispatcher
  - business state                        |
  - workflow state                        | publish step_id
  - evidence/citations                    v
  - audit/migration                  Redis / Celery Broker
                                              |
                 +----------------------------+---------------------------+
                 |                            |                           |
             FAST Worker                RESEARCH Worker              CRAWL Worker
                 |                            |                           |
                 +----------------------------+---------------------------+
                                              |
                                      Ports / Adapters
                           Model / Search / HTTP / Browser / Memory
```

这仍是一套代码库。API、Worker 和迁移工具是不同进程入口，但共享领域模块和基础设施适配器。

FastAPI 自带的进程内 BackgroundTasks 不用于重型搜索；官方文档也建议需要跨进程或跨服务器执行时使用 Celery 等队列工具。SSE 用于服务端到客户端的单向进度流。

## 7. 模块边界

建议目录：

```text
sana/
  app/
    api/                    # FastAPI routes, auth dependencies, SSE
    worker/                 # Celery entrypoints; only passes IDs
    migration/              # legacy memory import CLI
  modules/
    identity/               # tenant, user, auth subject
    conversation/           # conversation, message, response run
    orchestration/          # run, step, attempt, lease, outbox
    search_planning/        # normalized intent, facts, query specs
    discovery/              # provider ports, SearchHit
    content/                # fetch, document version, chunk
    evidence/               # candidate, support/contradict, coverage
    answer/                 # claims, citation, synthesis
    memory/                 # profile and imported memories
    model_gateway/          # model roles, quotas, structured calls
  platform/
    db/                     # SQLAlchemy mappings, migrations, UoW
    queue/                  # Celery/Redis adapter
    telemetry/              # OpenTelemetry setup
    security/               # SSRF, secret encryption, sanitization
  clients/
    streamlit/              # API-only Streamlit client
```

模块通信只使用 typed command/query/event：

- API 调用 Application Service。
- Application Service 在事务内更新聚合并写 Outbox。
- Worker 只接收 `step_id`、`run_id` 和 trace context，不通过消息传递大对象。
- Repository 返回领域对象，不向上泄漏 ORM 模型。
- Provider、Fetcher、Model 和 Memory 都位于 Port 后面。

## 8. 自动路由

### 8.1 路由输入

`ComplexityRouter` 使用以下特征：

- Required Fact 数量。
- 是否需要强时效信息。
- 是否需要比较、多跳或跨文档合成。
- 是否属于高后果类别。
- 是否存在明确的完整性或多源引用要求。
- 预估 Provider、Fetch 和 LLM 成本。
- 当前租户配额与系统健康状态。

### 8.2 路由实现

- 规则优先，常见情况不调用 LLM。
- 只有边界案例允许一次小模型结构化判定。
- 路由结果包含 `mode`、`reason_codes`、`policy_version` 和 `confidence`，并持久化。
- Router 不能读取未隔离的全局对话；只接收当前消息和允许的会话摘要。

### 8.3 直接进入 RESEARCH 的条件

- Required Fact 数量不少于 3。
- 需要比较或多跳推理。
- 强时效事实与多项事实同时出现。
- 高后果事实需要交叉验证。
- 用户语义明确要求完整来源或研究报告。

因此，本次 Apex 测试应在开始时直接进入 RESEARCH，而不是先消耗 FAST 预算。

### 8.4 FAST 升级条件

FAST 验证后仍有 Required Gap，并同时满足以下任一条件时升级：

- 强时效信息缺失。
- 高后果事实缺失。
- 已有证据冲突。
- 问题明确要求完整覆盖。

普通低风险问题不升级，在 12 秒软预算内生成 PARTIAL 回答并列出缺口。

## 9. 状态模型

### 9.1 三个正交维度

`RunStatus`：

```text
QUEUED | RUNNING | WAITING | SUCCEEDED | FAILED | CANCELLED
```

`AnswerQuality`：

```text
COMPLETE | PARTIAL | NONE
```

`StopReason`：

```text
FACTS_COVERED
TIME_BUDGET
PROVIDER_FAILURE
POLICY_BLOCKED
USER_CANCELLED
INFRASTRUCTURE_FAILURE
```

受控超时并成功返回 PARTIAL 时：

```text
status = SUCCEEDED
answer_quality = PARTIAL
stop_reason = TIME_BUDGET
```

只有工作流无法产出受控响应时才使用 `FAILED`。不再把 TIMEOUT 状态改写为 PARTIAL，也不再把回答质量塞入运行状态。

### 9.2 Step 状态

```text
READY | RUNNING | RETRY_WAIT | SUCCEEDED | FAILED | SKIPPED | CANCELLED
```

### 9.3 Attempt

每次外部调用或可重试执行都有独立 Attempt：

```text
id
tenant_id
run_id
step_id
attempt_no
idempotency_key
lease_owner
leased_until
deadline_at
started_at
completed_at
error_type
error_code
input_ref
output_ref
```

数据库唯一约束保证 `(step_id, attempt_no)` 和 `idempotency_key` 不重复。

### 9.4 恢复语义

- Outbox dispatcher 发布的队列消息只包含 Step ID。
- Worker 通过事务和 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 Step lease。
- Worker 崩溃后 lease 过期，reaper 将 Step 重新置为 READY。
- Reconciler 周期扫描 READY、已到期 RETRY_WAIT 和 lease 过期的 Step，并使用稳定 task ID 幂等重投；因此即使 Outbox 已标记发布后 Redis 被清空，活跃 Run 仍能恢复。
- 成功输出是不可变记录；恢复时跳过已成功 Step。
- 外部副作用使用稳定幂等键；Celery 使用 late acknowledgement，但 PostgreSQL 状态而非 Celery result backend 决定是否已完成。
- Celery 任务必须幂等，因为 late acknowledgement 下 Worker 崩溃可能导致消息再次执行。

## 10. PostgreSQL 数据模型

### 10.1 身份与会话

```text
tenants
users
user_identities
conversations
messages
response_runs
```

所有用户数据表包含 `tenant_id`。应用连接使用不具备 `BYPASSRLS` 的数据库角色，并为读写操作启用 Row-Level Security（RLS）。

### 10.2 工作流

```text
search_runs
search_steps
step_attempts
outbox_events
run_events
```

`search_runs` 包含：

```text
id, tenant_id, conversation_id, message_id
mode, route_reason_codes, policy_version
status, answer_quality, stop_reason
soft_deadline_at, hard_deadline_at
budget_snapshot, usage_snapshot
created_at, started_at, completed_at
version
```

`search_steps` 使用稳定 `step_key`，并以 `(run_id, plan_revision, step_key)` 唯一约束实现恢复和扩展。

### 10.3 搜索与证据

```text
fact_requirements
query_specs
provider_attempts
search_hits
fetch_artifacts
documents
document_versions
document_chunks
evidence_candidates
verified_evidence
answer_claims
citations
```

### 10.4 记忆迁移

```text
memory_items
memory_embeddings
migration_ledger
legacy_archives
```

`migration_ledger` 记录：来源系统、来源 ID、内容 hash、目标 ID、迁移版本、状态、错误和执行时间。导入器可安全重跑。

## 11. 搜索执行图

```text
Route
  -> Plan
  -> Parallel Discovery
  -> Candidate Selection
  -> Fetch
  -> Extract / Normalize
  -> Evidence Candidate Build
  -> Fact Verification
       |-- facts covered ----------> Synthesize
       |-- ordinary FAST gap ------> Synthesize PARTIAL
       `-- valuable required gap --> Expand Plan -> Discovery
  -> Claim/Citation Validation
  -> Complete Run
```

升级或补查通过增加新的 Plan revision 和 Step 实现；已完成 Step 不重跑。

## 12. 预算模型

### 12.1 用户可见预算

- FAST 用户可见硬目标：15 秒。
- FAST 执行软预算：12 秒，额外 3 秒覆盖排队、网络传输和安全收尾。
- RESEARCH 硬上限：120 秒。

deadline 从 API 接受消息并成功创建 Run 时开始，不从 Executor 启动时开始。

### 12.2 FAST 默认预算

```text
route + plan       1.2s
discovery          4.2s
fetch + extract    3.6s
verify             2.0s
synthesize reserve 1.0s
```

- Provider fan-out：最多 2 类。
- Query：总计最多 4 条，每个 Fact 最多 2 条。
- Fetch：最多 4 个文档。
- LLM：Router 最多 1 次、Planner 1 次、Verifier 1 次、Synthesis 1 次；规则 Router 不消耗调用。

### 12.3 RESEARCH 默认预算

- Query：初始最多 8 条，自适应扩展后总计最多 12 条。
- Provider fan-out：最多 4 类。
- Fetch：最多 12 个文档。
- Expansion：最多 2 轮。
- 每轮必须满足最小 expected evidence gain，否则停止。
- 所有阶段共享 Run hard deadline，但各自持有不可挪用的合成保留预算。

预算策略由版本化 `SearchPolicy` 配置，不由普通用户界面自由修改。

## 13. Query 规划与防污染

### 13.1 一次规划

- 每个 Run 最多一次主 Planner 调用。
- Planner 输出 `NormalizedIntent`、`FactRequirement` 和语义 QuerySpec 草案。
- 不再为每个 Fact 单独调用 Planner。
- 不再为每条 Query 调用 LLM refiner。

### 13.2 确定性 Query Compiler

Compiler 根据实体、Fact 类型、locale、freshness 和来源偏好生成少量变体，并执行：

- Unicode/空白/标点规范化。
- 对话称呼、情绪表达和完整句清除。
- 规范实体存在性检查。
- 最大 64 字符限制。
- locale 与时间锚点验证。
- 规范化 signature 去重。
- Fact ID 强关联。
- 禁止 Query 包含未批准的近期对话片段。

`QuerySpec`：

```text
id, tenant_id, run_id, fact_id
text, normalized_signature
locale, freshness_window
query_kind, provider_classes
created_by, plan_revision
```

Query 数量和 LLM 调用数在 Plan 校验阶段即受预算约束。

## 14. Discovery 与 Provider

- 每个 Provider Adapter 是无共享可变状态的对象或纯调用接口。
- Provider 返回 `ProviderResponse(results, attempt_metrics, typed_error)`，不修改全局 `last_trace`。
- Provider attempt 独立记录成功、空结果、超时、连接重置、限流和协议错误。
- 全局并发、租户并发、Provider 并发和域名并发分别限流。
- Provider circuit breaker 按 Provider + region 隔离，不因某个 Query 失败永久拉黑来源。
- 取消信号和 remaining deadline 必须传递到 HTTP 客户端。
- 已开始但无法取消的调用结果在 deadline 后丢弃，不允许继续修改 Run。

Candidate Selection 是便宜阶段，只使用 SearchHit 元数据、实体匹配、页面类型、来源质量和新鲜度；LLM rerank 只作用于少量候选。

## 15. Content 获取策略

### 15.1 能力预检

启动与周期健康检查确认：

- HTTP fetcher 可用。
- Browser/Katana 二进制可用。
- Provider credential/config 可用。
- Model role 可用。

不可用能力不会进入 Plan。

### 15.2 策略顺序

1. 对普通文章页优先使用受控 HTTP fetch。
2. 只有 JavaScript 页面、站内导航或明确深爬需求才使用 Browser/Katana。
3. 官方站点首页只作为发现入口，不能直接作为目标事实证据。
4. 每个 host 有并发、响应大小、重定向和时间预算限制。
5. 抓取器必须执行 SSRF 防护：拒绝本机、私网、链路本地地址、危险协议和 DNS 重绑定。

`FetchArtifact` 记录响应状态、最终 URL、content type、长度、hash、fetcher 类型和错误，不等同于 Document。

`DocumentVersion` 只在正文提取和安全清洗成功后建立，包含稳定内容 hash、抓取时间、语言和来源元数据。

## 16. 证据与事实闭环

### 16.1 数据链

```text
SearchHit
  -> FetchArtifact
  -> DocumentVersion
  -> DocumentChunk
  -> EvidenceCandidate
  -> VerifiedEvidence
  -> AnswerClaim
  -> Citation
```

禁止跳级：SearchHit/snippet 不能直接生成 VerifiedEvidence 或 Citation。

### 16.2 证据等级

`L0 DISCOVERY`：

- 搜索摘要、标题和 URL 元数据。
- 只用于候选筛选和补查决策。
- 不能让 Fact 进入 COVERED。

`L1 GROUNDED`：

- quote 必须逐字存在于指定 DocumentVersion/Chunk。
- Evidence 记录 `supports` 或 `contradicts`、Fact ID、文档版本和精确 offset。
- 可让 Fact 进入 COVERED。

`L2 VERIFIED`：

- 一手官方来源无冲突；或
- 两个独立非官方来源一致且无冲突。
- 才能让 Fact 进入 VERIFIED。

### 16.3 冲突

- 支持与反驳证据同时存在时，Fact 为 PARTIAL。
- 冲突原因和来源对用户可见。
- 强时效/高后果冲突触发 RESEARCH 扩展；低风险冲突在 deadline 内返回说明。

### 16.4 Citation

- Citation 必须引用 VerifiedEvidence 或允许展示的 L1 Grounded Evidence。
- 每个 AnswerClaim 保存 Citation IDs。
- 合成后执行 Claim/Citation validator；无法映射的事实陈述删除、弱化或改为未确认。

## 17. Model Gateway

所有模型调用通过统一 Gateway：

- 角色：router、planner、verifier、synthesizer、conversation、memory。
- 统一 timeout、retry、结构化输出校验、token/cost 计数和 trace。
- Gateway 接受 Run budget，不允许调用方硬编码 10 秒或无限重试。
- 非法结构最多修复一次，且计入同一预算。
- Provider key 由服务端 secret store 提供；生产代码不读取 Windows 用户注册表。
- 测试注入 FakeModelGateway，默认禁止任何真实网络。

模型 fallback 只能在 policy 明确允许时发生，并记录实际模型、原因和成本。

## 18. Outbox、Queue 与事件流

### 18.1 Transactional Outbox

创建/更新 Run 与对应 OutboxEvent 在同一 PostgreSQL 事务内提交。Dispatcher 发布成功后标记 `published_at`；重复发布由 Step 幂等约束吸收。

Outbox 保证“数据库提交后最终会首次发布”，Reconciler 保证“broker 丢失后活跃 Step 会再次出现”。两者都允许重复消息，Worker 以 PostgreSQL Step 状态和稳定幂等键吸收重复。

### 18.2 Celery

- Redis 为 broker。
- Task payload 只含 ID 和 trace context。
- 使用 late acknowledgement。
- prefetch multiplier 为 1，避免长任务被单 Worker 大量预取。
- Celery result backend 不作为业务状态源。
- Queue 分为 `fast`、`research`、`crawl` 和 `maintenance`。

### 18.3 进度事件

视觉讨论中的“Redis Pub/Sub”在书面自审中收紧为 Redis Streams：Pub/Sub 无持久和重放能力，不适合 SSE 重连。流程为：

- 权威 `run_events` 写 PostgreSQL。
- Outbox 将短期事件镜像到 Redis Stream。
- SSE 使用事件 ID 和 `Last-Event-ID` 支持重连。
- Redis Stream 丢失时，API 先从 PostgreSQL 返回 Run snapshot，再继续读取新事件。

## 19. API 契约

核心端点：

```text
POST   /v1/conversations
GET    /v1/conversations
GET    /v1/conversations/{conversation_id}
POST   /v1/conversations/{conversation_id}/messages
GET    /v1/runs/{run_id}
GET    /v1/runs/{run_id}/events
POST   /v1/runs/{run_id}/cancel
GET    /v1/runs/{run_id}/evidence
GET    /v1/users/me/memories
```

创建消息请求必须携带 `Idempotency-Key`。响应返回 message ID、run ID 和初始状态；客户端通过 SSE 获取阶段进度和最终回答。

SSE 事件：

```text
run.queued
run.routed
step.started
step.progress
fact.covered
fact.missing
run.upgraded
answer.partial
answer.completed
run.failed
run.cancelled
```

API 不直接执行搜索、爬取和 LLM 调用。

## 20. 多用户安全

### 20.1 身份与授权

- 生产环境使用 OIDC/OAuth2 AuthProvider adapter。
- 本地开发使用显式 dev adapter，不与生产配置共存。
- 每个请求解析不可伪造的 tenant/user context。
- Application Service 和 RLS 双重检查 tenant_id。
- 数据库应用角色不拥有 `BYPASSRLS`，迁移/运维角色与应用角色分离。

### 20.2 密钥

- Provider 和模型密钥由管理员配置。
- 数据库存储 encrypted secret reference；主密钥来自部署环境或外部 secret manager。
- API 永不返回密钥明文。
- 用户配置与管理员 Provider 配置分离。
- 旧 Windows 用户环境密钥不迁移，切换时要求重新录入。

### 20.3 网页安全

- SSRF：URL 解析、DNS 解析前后校验、重定向逐跳校验、私网地址拒绝、响应大小限制。
- Prompt injection：网页内容只进入 data channel；不得生成或改变 tool permission、system prompt、routing policy。
- HTML 经过安全解析和文本化，不向客户端返回未清洗 HTML。
- 下载类型采用 allowlist；首期只处理文本和允许的文档类型。

### 20.4 配额

- 每用户、每租户、每 Provider、每模型角色有并发和费用上限。
- FAST 与 RESEARCH 使用不同队列和并发池，避免研究任务饿死交互请求。

## 21. 错误处理与降级

错误类型：

```text
TRANSIENT       timeout, connection reset, 429, retryable 5xx
PERMANENT       auth, invalid configuration, non-retryable 4xx
BUDGET          deadline or quota exhausted
CONTENT         empty, unsupported, malicious or invalid document
MODEL_OUTPUT    invalid structured response
CANCELLED       explicit user/system cancellation
INTERNAL        unexpected invariant or infrastructure failure
```

策略：

- TRANSIENT：有界指数退避 + jitter，重试不超过 Step policy。
- PERMANENT：不重试；触发对应 Provider/能力降级。
- BUDGET：合作式取消剩余工作，使用已有证据收尾。
- CONTENT：隔离文档并尝试下一个候选。
- MODEL_OUTPUT：预算内最多修复一次。
- INTERNAL：Step 失败；若仍可生成受控 PARTIAL 则完成 Run，否则 Run FAILED。

禁止空 `except Exception: pass`。边界层可以捕获未知异常，但必须转换为 TypedError、记录 span exception 并保持 cause。

## 22. 可观测性

采用 OpenTelemetry trace、metrics 和 structured logs。核心 span：

```text
search.run
search.route
search.plan
search.provider
search.fetch
search.extract
search.verify
search.expand
search.synthesize
model.call
```

核心指标：

- FAST/RESEARCH p50、p95、p99 时延。
- Queue wait、Step latency、deadline overrun。
- 模式分布和 FAST→RESEARCH 升级率。
- Required Fact coverage 和 COMPLETE/PARTIAL 比例。
- SearchHit→L1、L1→L2 转化率。
- Provider 成功、空结果、超时、限流和 circuit 状态。
- Fetch 成功率、正文提取率、SSRF/内容拒绝数。
- LLM 调用数、token、费用、结构修复率。
- Worker lease 过期、重试、重复消息吸收数。

默认 trace 不记录原始用户对话、网页正文、密钥或完整 Prompt。调试内容采样必须经过管理员开关、脱敏和短期保留策略。

## 23. Streamlit 客户端重做

新 Streamlit 只调用 API：

- 登录与用户会话。
- 会话列表和聊天消息。
- Run 阶段进度、取消操作和失败提示。
- 来源、引用和未覆盖 Fact 面板。
- 不显示 FAST/RESEARCH 手动选择器，只可显示系统选择及原因。
- 用户偏好与管理员 Provider/模型配置分离。
- 不读取或写入 `user_profile.json`。
- 不直接连接 MongoDB、ChromaDB、PostgreSQL 或模型 Provider。

旧 Streamlit 复制为只读回滚入口；新功能只进入新客户端。

## 24. 记忆迁移

### 24.1 导入内容

- MongoDB 中可归属到当前用户的原始对话批次和结构化事件。
- ChromaDB 中可恢复原文的长期记忆。
- `user_profile.json` 中的人物关系、偏好、习惯和有效用户档案。

### 24.2 不导入内容

- WebTool 配置。
- 模型配置与密钥。
- 旧 SearchPlan、SearchTask、SearchQuery、Trace 和搜索历史。
- 无法确认所属用户的数据。

### 24.3 向量处理

- 不直接复制旧 embedding 作为新索引事实。
- 对可恢复原文重新 embedding，并保存 `embedding_model`、`embedding_version` 和内容 hash。
- 只有向量、没有可恢复原文的数据进入只读归档，不进入在线召回。

### 24.4 迁移安全

- 迁移前生成只读备份和数量/hash 清单。
- dry-run 输出映射、冲突和丢弃原因。
- migration_ledger 保证可重跑。
- 用户级抽样核验完成后才切换 Memory Store。
- 旧数据在回滚窗口结束前不删除。

## 25. 测试与评估

### 25.1 单元与属性测试

- Run/Step/Attempt 状态转换和非法转换。
- Budget 分配、保留预算、deadline 和停止条件。
- Query Compiler 的长度、实体、locale、去重和对话污染属性。
- Evidence quote 必须存在于 DocumentVersion。
- Citation/Claim 映射完整性。
- SSRF 地址和重定向防护。
- RLS tenant predicate 和授权策略。

### 25.2 Contract 测试

- Provider、Fetcher、Model 使用录制 fixture 或 fake adapter。
- 默认阻断 socket；真实网络测试必须带显式 marker 和独立测试凭据。
- 用户环境、Windows 注册表和真实 `user_profile.json` 不参与测试。
- Provider schema、错误映射和 timeout/cancel 行为必须通过契约测试。

### 25.3 集成测试

- 临时 PostgreSQL、Redis 和 Celery Worker。
- Outbox 重复发布。
- Worker 崩溃、lease 过期和恢复。
- Redis 清空后从 PostgreSQL 恢复。
- Reconciler 对已发布但 broker 丢失的 READY Step 进行幂等重投。
- API Idempotency-Key 重复提交。
- SSE 断线后通过 Last-Event-ID 重连。
- RLS 跨租户读写拒绝。

### 25.4 Chaos 与安全测试

- 杀死 Worker。
- Provider timeout、429、5xx 和连接重置。
- 模型输出非法 JSON。
- 恶意 HTML、超大响应、重定向到私网和 DNS 重绑定。
- 网页 prompt injection 尝试改变工具策略。
- 数据库短暂断连和重复消息。

### 25.5 回归与 Eval 数据集

建立版本化 JSONL 场景集，至少覆盖：

- 单事实 FAST。
- 多事实 RESEARCH。
- 强时效、高后果、冲突来源。
- Provider 部分失败。
- 无可靠答案。
- 多语言和实体别名。
- 最新 Apex 日志。

Apex 用例验收：

- 自动路由 RESEARCH。
- Query 不包含 `sana！我好久没碰apex啦` 等对话片段。
- 版本、角色改动和阵容分别关联 Fact ID。
- 每个已回答事实拥有 L1/L2 Evidence 和可回溯 Citation。
- 未覆盖事实逐项明确列出，不得用模型记忆补齐。

## 26. 切流门槛

- FAST p95 ≤ 15 秒。
- RESEARCH p95 ≤ 120 秒。
- Query 对话片段污染率为 0。
- Citation 可回溯率为 100%。
- Required Fact 无证据时，COMPLETE 误报率为 0。
- 跨租户数据访问测试零容忍。
- Worker 崩溃和 Redis 清空后可自动恢复。
- 已成功外部副作用不会因重投产生重复效果。
- 默认测试无真实网络、真实密钥和用户配置依赖。
- Apex 回归场景达到上述事实与引用要求。

指标需在 Shadow/Eval 环境连续通过后才切换默认链路。

## 27. 分阶段交付

### Phase 0：基线与安全护栏

- 建立日志回放/Eval 数据集。
- 隔离测试凭据和网络。
- 固化现有记忆资产清单与备份。
- 定义 OpenTelemetry schema 和验收仪表盘。

### Phase 1：平台骨架

- FastAPI、PostgreSQL、Alembic、Redis、Celery。
- Identity、Conversation、Run/Step/Attempt、Outbox。
- RLS、OIDC adapter、SSE 和取消。

### Phase 2：FAST 搜索

- 一次 Planner + Query Compiler。
- Provider Port、HTTP-first Fetch、SearchHit/Document/Evidence 分离。
- FAST deadline、验证和 Citation。
- 与旧链路做 Shadow 对比，不影响用户回答。

### Phase 3：RESEARCH 与恢复

- 自动路由和按价值升级。
- Plan revision、Expansion、冲突处理。
- Worker crash/Redis loss 恢复。
- RESEARCH 队列、配额和 120 秒上限。

### Phase 4：客户端与记忆迁移

- 重做 Streamlit API 客户端。
- dry-run 和正式导入用户记忆。
- 管理员模型/Provider 配置入口。

### Phase 5：切流与清理

- 新链路成为默认。
- 保留旧 Streamlit 和旧存储只读回滚窗口。
- 达到门槛并完成迁移核验后，删除旧 WebSearchNode 编排、旧 Web 配置面板和 Mongo 搜索状态写入。

## 28. 被拒绝的路线

### 28.1 LangGraph 作为核心运行时

LangGraph 提供 durable execution、checkpoint 和图路由，但本项目仍需自行解决外部调用幂等、证据语义、多租户数据和预算。当前优先采用显式领域状态机，避免把恢复语义绑定到框架 replay 行为。未来可以在 Orchestration Port 后评估替换，而不改变领域模型。

### 28.2 Temporal

Temporal 的恢复保证最强，但会增加独立控制面、Worker SDK 约束和运维成本。当前 PostgreSQL Outbox + Redis/Celery + 显式 Step lease 已足以满足 120 秒以内的搜索工作流。

### 28.3 微服务

当前代码规模和团队边界不足以抵消分布式事务、事件版本和跨服务调试成本。模块化单体保留未来拆分接口，但当前不拆进程所有权。

## 29. 设计依据

本设计依据：

- `C:\Users\Administrator\Downloads\search-architecture-refactor-plan.md`
- 最新测试日志 `pasted-text.txt`
- 当前工作区未提交的 `sana/search/` 重构及相关测试
- `docs/current-pipeline-search-architecture.md`
- LangGraph durable execution / checkpoint / idempotency 官方文档：<https://docs.langchain.com/oss/python/langgraph/overview>
- Temporal durable execution 官方文档：<https://docs.temporal.io/>
- OpenTelemetry semantic conventions：<https://opentelemetry.io/docs/concepts/semantic-conventions/>
- FastAPI BackgroundTasks 与 SSE 官方文档：<https://fastapi.tiangolo.com/tutorial/background-tasks/>、<https://fastapi.tiangolo.com/tutorial/server-sent-events/>
- Celery task idempotency 与 late acknowledgement 官方文档：<https://docs.celeryq.dev/en/latest/userguide/tasks.html>
- Redis Streams 官方文档：<https://redis.io/docs/latest/develop/use-cases/streaming/>
- PostgreSQL Row-Level Security 与 `SKIP LOCKED` 官方资料：<https://www.postgresql.org/docs/current/ddl-rowsecurity.html>、<https://www.postgresql.org/docs/current/sql-select.html>

## 30. 最终验收定义

重构完成不是“出现了新目录或 SearchPlan 表”，而是同时满足：

1. 一个用户请求对应一个可恢复 SearchRun。
2. FAST/RESEARCH 路由有版本、有原因、可评估。
3. 所有外部调用受同一 Run deadline 和费用预算约束。
4. Worker、Redis 或单一 Provider 失败不会丢失 Run。
5. Query 不包含对话噪声。
6. SearchHit 不能伪装成 Document/Evidence。
7. 每个回答事实能回溯到正文版本和 Citation。
8. 多租户数据、密钥、配额和会话严格隔离。
9. 测试默认完全隔离真实网络和真实用户配置。
10. 用户记忆迁移可核验、可重跑、可回滚。
