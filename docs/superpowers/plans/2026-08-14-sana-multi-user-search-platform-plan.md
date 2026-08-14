# Sana 多用户双模式搜索平台实施计划

日期：2026-08-14

状态：等待用户审阅后执行

关联设计：`docs/superpowers/specs/2026-08-14-sana-multi-user-search-platform-design.md`

## 1. 实施策略

采用并行新建、逐步切流，而不是在现有 `WebSearchNode` 上继续堆叠：

1. 新平台代码进入 `sana/app`、`sana/modules`、`sana/platform` 和 `sana/clients`。
2. Phase 0–3 不修改当前工作区中已有未提交改动的搜索文件。
3. 新链路通过稳定 Port 复用可验证的解析逻辑，不复用共享可变 Registry/Trace。
4. 新 FAST 链路先以 Shadow 模式运行；达到门槛后才接管用户回答。
5. RESEARCH、记忆迁移和新 Streamlit 客户端在 FAST 稳定后接入。
6. 旧实现仅在回滚窗口结束后删除。

每个任务单独提交，只使用显式 `git add <files>`；禁止 `git add .`，避免把用户现有未提交改动带入提交。

## 2. 工作区保护规则

当前已有未提交修改：

```text
docs/pipeline-flow.md
sana/agent.py
sana/core/context.py
sana/models/deepseek_backend.py
sana/nodes/llm_call_node.py
sana/nodes/web_search_node.py
sana/services/*search/rerank/verify files
user_profile.json
sana/search/
```

执行规则：

- 前 12 个任务优先新建文件，不覆盖上述文件。
- 必须接线前先保存 `git status --short` 和 `git diff --stat` 基线。
- 若目标文件同时包含用户改动，逐块合并，不做整文件替换。
- 不使用 `git reset --hard`、`git checkout --` 或自动清理未跟踪文件。
- 每次提交前运行 `git diff --cached --name-only`，确认只包含本任务文件。

## 3. 任务 0：测试隔离与项目依赖基线

### 文件

```text
pyproject.toml                              # 新建
requirements.txt                           # 后续兼容导出，首次任务只补缺失运行依赖
tests/conftest.py                           # 新建
tests/test_architecture/test_test_isolation.py
tests/test_services/test_model_backends.py  # 精确修复真实用户环境泄漏
```

### 实施

- 以 `pyproject.toml` 作为依赖和工具配置事实源。
- 运行依赖加入 FastAPI、Uvicorn、Pydantic Settings、SQLAlchemy、asyncpg、Alembic、Redis、Celery、HTTPX、OpenTelemetry、PyJWT/cryptography。
- 开发依赖加入 pytest、pytest-asyncio、Hypothesis、respx 和 fakeredis。
- 单元测试自动设置 `SANA_TESTING=1`，阻断 Windows 用户注册表凭据和真实 `user_profile.json`。
- 默认 monkeypatch socket/HTTPX/requests 外网调用；带 `live_network` marker 才允许网络。
- 修复 DeepSeek 无密钥测试：显式 patch credential provider，而不是只清空进程环境。
- 保留现有 `unittest` 用例可运行，新增测试使用 pytest。

### 验证

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests/test_services/test_model_backends.py -q
python -m pytest tests/test_architecture/test_test_isolation.py -q
python -m pytest -q
```

验收：全量测试不发出真实网络请求；当前 144 项基线不再受真实 DeepSeek 密钥影响。

### 提交

```text
test: isolate credentials and live network access
```

## 4. 任务 1：新平台包骨架与共享类型

### 文件

```text
sana/app/__init__.py
sana/modules/__init__.py
sana/modules/shared/__init__.py
sana/modules/shared/clock.py
sana/modules/shared/errors.py
sana/modules/shared/ids.py
sana/modules/shared/result.py
sana/platform/__init__.py
sana/clients/__init__.py
tests/test_modules/shared/test_errors.py
tests/test_modules/shared/test_ids.py
```

### 实施

- 定义 `TypedError` 和错误类别：TRANSIENT、PERMANENT、BUDGET、CONTENT、MODEL_OUTPUT、CANCELLED、INTERNAL。
- 定义可注入 `Clock` 和确定性测试时钟。
- 定义 UUID ID 工厂和 trace context 数据结构。
- 领域层不得导入 FastAPI、Celery、SQLAlchemy、Redis 或旧 `sana.services`。

### 验证

```powershell
python -m pytest tests/test_modules/shared -q
```

### 提交

```text
feat: add platform module boundaries and shared types
```

## 5. 任务 2：Run/Step/Attempt 领域状态机

### 文件

```text
sana/modules/orchestration/__init__.py
sana/modules/orchestration/domain.py
sana/modules/orchestration/policy.py
sana/modules/orchestration/transitions.py
sana/modules/orchestration/ports.py
tests/test_modules/orchestration/test_run_state.py
tests/test_modules/orchestration/test_step_state.py
tests/test_modules/orchestration/test_budget.py
```

### 实施

- 实现 `RunStatus`、`AnswerQuality`、`StopReason`、`StepStatus`。
- `SearchRun` 保存 mode、routing decision、budget snapshot 和 usage。
- `SearchStep` 使用稳定 `step_key`、plan revision 和不可变 input/output ref。
- `StepAttempt` 保存 lease、deadline、幂等键和 typed error。
- 状态转换只能通过 domain methods；非法转换抛出明确 invariant error。
- 实现 FAST 12 秒软预算/15 秒用户目标和 RESEARCH 120 秒硬预算。
- 状态与回答质量分离：受控超时允许 `SUCCEEDED + PARTIAL + TIME_BUDGET`。

### 验证

```powershell
python -m pytest tests/test_modules/orchestration -q
```

验收：Hypothesis 随机状态序列不能绕过不变量；合成保留预算不可被 Discovery/Fetch 消耗。

### 提交

```text
feat: add durable run step attempt state machine
```

## 6. 任务 3：PostgreSQL Schema 与 Alembic

### 文件

```text
alembic.ini
alembic/env.py
alembic/versions/0001_identity_conversation.py
alembic/versions/0002_orchestration_outbox.py
alembic/versions/0003_search_evidence.py
alembic/versions/0004_memory_migration.py
sana/platform/db/base.py
sana/platform/db/session.py
sana/platform/db/models/*.py
tests/test_platform/db/test_schema_metadata.py
```

### 实施

- 建立设计文档列出的 identity、conversation、workflow、search/evidence、memory 表。
- 为每个用户数据表增加 `tenant_id`、外键、必要唯一约束和时间索引。
- 为 `(run_id, plan_revision, step_key)`、`idempotency_key`、Outbox 未发布项和 lease 扫描建立索引。
- RLS migration 启用并强制 tenant policy；应用角色不得 `BYPASSRLS`。
- SQLAlchemy mappings 位于 platform 层，不泄漏到 domain。

### 验证

无 Docker 环境时运行 metadata/unit tests；PostgreSQL 集成测试使用显式 `SANA_TEST_DATABASE_URL`。

```powershell
python -m pytest tests/test_platform/db/test_schema_metadata.py -q
python -m pytest -m postgres -q
```

### 提交

```text
feat: add postgres schema migrations and tenant policies
```

## 7. 任务 4：Unit of Work、Repository 与租户隔离

### 文件

```text
sana/platform/db/uow.py
sana/modules/identity/domain.py
sana/modules/identity/ports.py
sana/modules/conversation/domain.py
sana/modules/conversation/ports.py
sana/modules/orchestration/repository.py
tests/test_platform/db/test_uow.py
tests/test_platform/db/test_rls.py
tests/test_modules/conversation/test_conversation_service.py
```

### 实施

- 定义 request-scoped UnitOfWork。
- 事务开始时使用安全的 local tenant context；事务结束后不残留连接级 tenant 状态。
- Repository 按聚合读写，禁止跨 tenant 查询。
- Conversation message 创建与 Response/Search Run 创建使用同一事务。
- 读模型返回 DTO，不返回 ORM entity。

### 验证

```powershell
python -m pytest tests/test_platform/db/test_uow.py -q
python -m pytest -m postgres tests/test_platform/db/test_rls.py -q
```

验收：同一连接池复用连接时，tenant A 无法读取 tenant B 数据。

### 提交

```text
feat: add tenant scoped repositories and unit of work
```

## 8. 任务 5：Transactional Outbox、Celery 与 Reconciler

### 文件

```text
sana/modules/orchestration/outbox.py
sana/modules/orchestration/reconciler.py
sana/modules/orchestration/lease.py
sana/platform/queue/celery_app.py
sana/platform/queue/tasks.py
sana/platform/queue/dispatcher.py
tests/test_modules/orchestration/test_outbox.py
tests/test_modules/orchestration/test_reconciler.py
tests/test_platform/queue/test_duplicate_delivery.py
```

### 实施

- Run/Step 更新与 OutboxEvent 同事务提交。
- Celery task 只接收 `step_id` 和 trace context。
- 配置 late ack、prefetch=1、fast/research/crawl/maintenance 四个队列。
- Worker 领取 DB lease 后执行；稳定 task ID 和 Step 状态吸收重复投递。
- Reconciler 扫描 READY、到期 RETRY_WAIT 和 lease 过期 Step 并幂等重投。
- Redis/Celery result backend 不参与业务完成判断。

### 验证

```powershell
python -m pytest tests/test_modules/orchestration/test_outbox.py -q
python -m pytest tests/test_modules/orchestration/test_reconciler.py -q
python -m pytest -m redis tests/test_platform/queue -q
```

验收：模拟 Redis 清空和 Worker 崩溃后，Run 从 PostgreSQL 自动恢复，成功 Step 不重复生效。

### 提交

```text
feat: add outbox celery delivery and workflow reconciliation
```

## 9. 任务 6：FastAPI、OIDC Port、Conversation/Run API 与 SSE

### 文件

```text
sana/app/api/main.py
sana/app/api/dependencies.py
sana/app/api/auth.py
sana/app/api/routes/conversations.py
sana/app/api/routes/runs.py
sana/app/api/routes/events.py
sana/app/api/schemas/*.py
sana/platform/events/redis_stream.py
tests/test_app/api/test_conversations.py
tests/test_app/api/test_run_idempotency.py
tests/test_app/api/test_sse_resume.py
```

### 实施

- AuthProvider Port 支持生产 OIDC adapter 和显式 dev adapter。
- `POST /messages` 要求 Idempotency-Key，并在事务中创建 message、run 和 outbox。
- Run GET、cancel 和 evidence endpoints 按 tenant 授权。
- 权威 run_events 存 PostgreSQL，Redis Streams 作为短期 SSE 加速层。
- SSE 支持 Last-Event-ID；Redis 缺失时先返回 PostgreSQL snapshot。
- API 不直接执行 LLM、Search 或 Fetch。

### 验证

```powershell
python -m pytest tests/test_app/api -q
```

### 提交

```text
feat: add multi tenant conversation run api and sse
```

## 10. 任务 7：Model Gateway 与凭据边界

### 文件

```text
sana/modules/model_gateway/domain.py
sana/modules/model_gateway/ports.py
sana/modules/model_gateway/service.py
sana/platform/models/deepseek.py
sana/platform/models/openai.py
sana/platform/models/local.py
sana/platform/security/secrets.py
tests/test_modules/model_gateway/test_budget.py
tests/test_modules/model_gateway/test_structured_output.py
tests/test_platform/models/test_no_registry_credentials.py
```

### 实施

- 统一 router/planner/verifier/synthesizer/conversation/memory 角色。
- 所有调用接收 absolute deadline 和 usage budget。
- 结构化输出非法时最多修复一次，并计入预算。
- 统一 retry/timeout/error mapping；不在 Backend 内 sleep 到超过 Run deadline。
- 生产只从 SecretProvider 获取 key；Windows 用户注册表只留在 legacy adapter，且新平台不引用。
- FakeModelGateway 成为测试默认。

### 验证

```powershell
python -m pytest tests/test_modules/model_gateway -q
python -m pytest tests/test_platform/models -q
```

### 提交

```text
feat: add budget aware model gateway and secret isolation
```

## 11. 任务 8：自动 Router、一次 Planner 与 Query Compiler

### 文件

```text
sana/modules/search_planning/domain.py
sana/modules/search_planning/router.py
sana/modules/search_planning/planner.py
sana/modules/search_planning/query_compiler.py
sana/modules/search_planning/policy.py
tests/test_modules/search_planning/test_router.py
tests/test_modules/search_planning/test_query_compiler.py
tests/fixtures/evals/apex_multi_fact.json
```

### 实施

- 规则 Router 优先，边界案例最多一次小模型调用。
- 持久化 mode、reason codes、policy version 和 confidence。
- Planner 一次输出 NormalizedIntent 和 FactRequirements。
- Query Compiler 确定性生成变体，执行 64 字符限制、实体/locale/freshness 检查、signature 去重。
- 禁止使用完整 user message 和 recent conversation 作为 Query suffix。
- FAST 最多 4 Query；RESEARCH 初始最多 8、总计最多 12。

### 验证

```powershell
python -m pytest tests/test_modules/search_planning -q
```

验收：Apex fixture 直接路由 RESEARCH；生成 Query 不包含 `sana！我好久没碰apex啦` 等片段。

### 提交

```text
feat: add automatic mode routing and clean query compiler
```

## 12. 任务 9：无共享状态的 Discovery Provider

### 文件

```text
sana/modules/discovery/domain.py
sana/modules/discovery/ports.py
sana/modules/discovery/service.py
sana/platform/search/bing_rss.py
sana/platform/search/direct_source.py
sana/platform/search/searxng.py
sana/platform/search/circuit_breaker.py
tests/test_modules/discovery/test_discovery_service.py
tests/test_platform/search/test_provider_contract.py
```

### 实施

- Provider 返回 `ProviderResponse(hits, metrics, typed_error)`，不写 `last_trace`。
- ProviderAttempt 独立持久化；同一来源可在不同 Query 上同时出现成功和失败。
- 首批实现 Bing RSS、Direct Source 和可选 SearXNG；Baidu/HTML search adapter 在契约稳定后加入。
- 使用 HTTPX absolute deadline、合作式取消和响应大小限制。
- Provider、租户和全局并发分别限流。
- Circuit breaker 有 closed/open/half-open 状态和健康探测。

### 验证

```powershell
python -m pytest tests/test_modules/discovery -q
python -m pytest tests/test_platform/search -q
```

### 提交

```text
feat: add stateless discovery providers and circuit breaking
```

## 13. 任务 10：HTTP-first Content、SSRF 与 DocumentVersion

### 文件

```text
sana/modules/content/domain.py
sana/modules/content/ports.py
sana/modules/content/fetch_strategy.py
sana/modules/content/extractor.py
sana/modules/content/chunker.py
sana/platform/fetch/http_fetcher.py
sana/platform/fetch/katana_fetcher.py
sana/platform/security/ssrf.py
tests/test_modules/content/test_fetch_strategy.py
tests/test_modules/content/test_document_versions.py
tests/test_platform/security/test_ssrf.py
```

### 实施

- 启动健康检查识别 HTTP/Browser/Katana 能力；不可用能力不进入计划。
- 普通文章优先 HTTP；JS/站内导航才使用 Browser/Katana。
- URL 解析、DNS 解析前后、重定向逐跳执行 SSRF 校验。
- 限制响应大小、content type、重定向数和每 host 并发。
- SearchHit、FetchArtifact、Document、DocumentVersion、Chunk 分离。
- 只有提取成功的正文建立 DocumentVersion；snippet 不得将 crawl_status 设为 crawled。

### 验证

```powershell
python -m pytest tests/test_modules/content -q
python -m pytest tests/test_platform/security/test_ssrf.py -q
```

### 提交

```text
feat: add http first content pipeline and ssrf protection
```

## 14. 任务 11：Evidence、Fact Coverage、Claim 与 Citation

### 文件

```text
sana/modules/evidence/domain.py
sana/modules/evidence/builder.py
sana/modules/evidence/verifier.py
sana/modules/evidence/coverage.py
sana/modules/answer/domain.py
sana/modules/answer/synthesizer.py
sana/modules/answer/citation_validator.py
tests/test_modules/evidence/test_evidence_levels.py
tests/test_modules/evidence/test_conflicts.py
tests/test_modules/answer/test_citation_validator.py
```

### 实施

- L0 snippet 仅 discovery，不能改变 Fact coverage。
- L1 quote 必须存在于指定 DocumentVersion/Chunk 并保存 offset。
- L2 由官方一手来源或两个独立一致来源产生。
- Evidence 明确 `supports` / `contradicts`。
- 冲突 Fact 为 PARTIAL，并可触发高价值扩展。
- Synthesizer 输出结构化 AnswerClaims；Citation validator 确保每个事实 claim 有证据映射。
- 无法映射的 claim 删除、弱化或标成未确认。

### 验证

```powershell
python -m pytest tests/test_modules/evidence -q
python -m pytest tests/test_modules/answer -q
```

验收：SearchHit/snippet 无法直接生成 Citation；引用可回溯率在 fixture 中为 100%。

### 提交

```text
feat: add grounded evidence coverage and citation validation
```

## 15. 任务 12：FAST 工作流

### 文件

```text
sana/modules/orchestration/search_workflow.py
sana/modules/orchestration/step_handlers/*.py
tests/test_workflows/test_fast_search.py
tests/test_workflows/test_fast_deadline.py
tests/test_workflows/test_fast_partial.py
```

### 实施

- 实现 Route→Plan→Discover→Select→Fetch→Extract→Verify→Synthesize Step 图。
- 并行只发生在 Executor/Worker 调度层，不在 Provider 内嵌套无界线程池。
- budget reservation 在提交 Step 前扣减；执行后按实际 usage 归还/记录。
- deadline 到达时取消低价值 Step，使用已有 L1/L2 证据收尾。
- FAST ordinary gap 返回 PARTIAL，不自动拉长到 120 秒。

### 验证

```powershell
python -m pytest tests/test_workflows/test_fast_search.py -q
python -m pytest tests/test_workflows/test_fast_deadline.py -q
python -m pytest tests/test_workflows/test_fast_partial.py -q
```

### 提交

```text
feat: add bounded fast search workflow
```

## 16. 任务 13：RESEARCH、按价值升级与 Plan Revision

### 文件

```text
sana/modules/orchestration/research_workflow.py
sana/modules/search_planning/expansion.py
sana/modules/evidence/evidence_gain.py
tests/test_workflows/test_research_search.py
tests/test_workflows/test_fast_upgrade.py
tests/test_workflows/test_research_recovery.py
```

### 实施

- 实现强时效、高后果、冲突和完整性需求的升级规则。
- Expansion 新增 plan revision 和 Step，不重跑旧成功 Step。
- 最多 2 轮扩展；expected evidence gain 低于门槛时停止。
- RESEARCH hard deadline 120 秒，始终保留合成预算。
- cancel 写入 Run 后，Worker 在每个外部调用和阶段边界合作式停止。

### 验证

```powershell
python -m pytest tests/test_workflows/test_research_search.py -q
python -m pytest tests/test_workflows/test_fast_upgrade.py -q
python -m pytest tests/test_workflows/test_research_recovery.py -q
```

### 提交

```text
feat: add research expansion upgrade and recovery
```

## 17. 任务 14：OpenTelemetry 与质量指标

### 文件

```text
sana/platform/telemetry/config.py
sana/platform/telemetry/spans.py
sana/platform/telemetry/metrics.py
sana/platform/telemetry/redaction.py
tests/test_platform/telemetry/test_redaction.py
tests/test_platform/telemetry/test_span_hierarchy.py
```

### 实施

- 建立 search.run、route、plan、provider、fetch、verify、synthesize、model.call span。
- 记录 mode、policy version、latency、usage、fact coverage 和 stop reason。
- 默认不记录原始对话、网页正文、密钥或完整 prompt。
- 输出 FAST/RESEARCH latency、coverage、升级率、provider 健康、成本、lease/retry 指标。

### 验证

```powershell
python -m pytest tests/test_platform/telemetry -q
```

### 提交

```text
feat: add redacted search tracing and quality metrics
```

## 18. 任务 15：Shadow、Eval 与 Apex 回归

### 文件

```text
sana/modules/orchestration/shadow.py
evals/search_cases.jsonl
scripts/run_search_evals.py
tests/test_evals/test_search_acceptance.py
```

### 实施

- Shadow 运行新链路但不影响用户可见旧答案。
- 保存结构化指标差异，不保存未脱敏的完整 prompt/正文。
- Eval 包含 FAST、RESEARCH、冲突、无答案、Provider 失败、多语言和 Apex。
- 报告 mode accuracy、query pollution、fact coverage、citation traceability、latency 和 cost。

### 验证

```powershell
python scripts/run_search_evals.py --fixtures evals/search_cases.jsonl
python -m pytest tests/test_evals/test_search_acceptance.py -q
```

验收：Apex 场景路由 RESEARCH、Query 污染为 0，事实逐项覆盖或明确缺口。

### 提交

```text
test: add shadow evaluation and apex regression gate
```

## 19. 任务 16：用户记忆迁移

### 文件

```text
sana/app/migration/cli.py
sana/app/migration/readers/mongo.py
sana/app/migration/readers/chroma.py
sana/app/migration/readers/user_profile.py
sana/app/migration/service.py
tests/test_app/migration/test_dry_run.py
tests/test_app/migration/test_idempotency.py
tests/fixtures/migration/*.json
```

### 实施

- dry-run 生成来源数量、hash、用户映射、冲突和丢弃原因。
- 只导入对话/事件、可恢复原文的 Chroma memory、关系与偏好。
- 不导入模型密钥、Web 配置或搜索历史。
- 对原文重新 embedding 并记录 model/version；仅有旧向量的数据归档。
- migration_ledger 保证重复运行不重复导入。
- 正式迁移前创建只读备份清单；不在脚本中删除旧数据。

### 验证

```powershell
python -m pytest tests/test_app/migration -q
python -m sana.app.migration.cli --dry-run
```

### 提交

```text
feat: add idempotent legacy memory migration
```

## 20. 任务 17：重做 Streamlit API 客户端

执行本任务前必须读取并使用 `developing-with-streamlit` 技能。

### 文件

```text
sana/clients/streamlit/app.py
sana/clients/streamlit/api_client.py
sana/clients/streamlit/session.py
sana/clients/streamlit/views/chat.py
sana/clients/streamlit/views/evidence.py
sana/clients/streamlit/views/settings.py
tests/test_clients/streamlit/test_api_client.py
tests/test_clients/streamlit/test_session_isolation.py
```

### 实施

- 登录、会话列表、消息、SSE 进度、取消 Run、来源与缺失 Fact。
- 不提供 FAST/RESEARCH 手动开关，只显示系统选择与原因。
- 用户设置和管理员 Provider/模型配置分离。
- 客户端不读取 JSON、数据库或密钥。
- 新入口与旧 `interfaces/streamlit_app.py` 并存于回滚窗口。

### 验证

- API client 单元测试。
- 两个浏览器 session 不共享 tenant/user/conversation。
- 手工检查 SSE 重连、取消、PARTIAL 与 evidence 展示。

### 提交

```text
feat: add isolated streamlit api client
```

## 21. 任务 18：接线、切流与旧链路退出

### 文件

```text
start-api.ps1
start-worker.ps1
start-streamlit.ps1
deployment/docker-compose.yml
docs/operations/search-platform.md
docs/pipeline-flow.md                  # 与用户现有改动逐块合并
interfaces/streamlit_app.py            # 回滚入口标记
sana/agent.py                          # 仅在最终兼容接线时修改
```

### 实施

- 提供 PostgreSQL、Redis、API、Worker 和 Streamlit 的部署清单；本机无 Docker 时保留外部服务 URL 配置。
- 新消息入口默认使用新 API/Run；旧入口通过 feature flag 保留。
- Shadow 指标达到切流门槛后关闭旧搜索回答路径。
- 回滚窗口内不删除 Mongo/Chroma/user_profile 原数据。
- 回滚窗口结束并核验迁移后，另建删除任务；本计划不自动执行数据删除。
- 旧 `WebSearchNode`、Mongo search repository 和旧 Web 配置只在确认无调用后移除。

### 验证

```powershell
python -m pytest -q
python scripts/run_search_evals.py --fixtures evals/search_cases.jsonl
```

手工验收：多用户隔离、FAST、RESEARCH、升级、取消、Worker 重启、Redis 清空、Apex 场景、记忆召回和旧入口回滚。

### 提交

```text
feat: cut over to multi user search platform
```

## 22. CI 与验收门

每次 PR/提交组运行：

```powershell
python -m pytest -q
python -m pytest -m "not postgres and not redis and not live_network" -q
```

有 PostgreSQL/Redis 的集成环境运行：

```powershell
python -m pytest -m "postgres or redis" -q
python scripts/run_search_evals.py --fixtures evals/search_cases.jsonl
```

最终切流门槛：

- FAST p95 ≤ 15 秒。
- RESEARCH p95 ≤ 120 秒。
- Query 对话污染率 0。
- Citation 可回溯率 100%。
- Required Fact 无证据时 COMPLETE 误报率 0。
- 跨租户访问测试零容忍。
- Worker 崩溃/Redis 清空可恢复。
- 测试默认无真实网络、真实密钥和用户配置。
- Apex 场景满足设计文档要求。

## 23. 计划完成定义

只有以下条件同时满足，才标记本计划完成：

1. 新 API、PostgreSQL、Redis/Celery 和 Streamlit 客户端可启动。
2. FAST/RESEARCH 完全自动路由并通过 Eval。
3. Run/Step/Attempt 可恢复且幂等。
4. SearchHit、DocumentVersion、Evidence、Claim 和 Citation 不可跳级。
5. 多租户 RLS、SSRF、密钥和配额边界通过测试。
6. 用户记忆完成 dry-run、导入和抽样核验。
7. 旧搜索链路退出默认路径，但仍在回滚窗口内可恢复。
8. 全量单元、集成、Chaos 和回归门槛通过。
