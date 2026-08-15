# Sana DeepSeek 质量闭环实施计划

日期：2026-08-15

状态：待实施

对应设计：`docs/superpowers/specs/2026-08-15-deepseek-quality-stage-design.md`

执行约束：本计划及后续实现均不创建 Git commit、不推送远程，除非用户之后明确授权。现有未提交的旧搜索实验、`user_profile.json`、`.agents/skills/sana-team/` 和 `sana/search/` 不纳入本阶段改动。

## 1. 完成定义

只有以下条件全部满足，本阶段才算完成：

1. Planner、单一批量 Verifier、Synthesizer 使用同一个 `deepseek-v4-flash` Provider 完成 Docker 真实闭环。
2. DeepSeek 请求显式使用非思考模式和 JSON Output。
3. FAST/RESEARCH 的模型调用硬上限分别为 4/8，结构修复和 retry 均计数。
4. 模型不能绕过 exact quote、DocumentVersion、Coverage、Claim 和 Citation 安全门。
5. 每个实际 Provider 调用有 tenant-scoped durable audit；崩溃窗口显示 `POSSIBLY_BILLED`，不宣称外部 exactly-once。
6. 默认 pytest 不读取真实密钥、不访问 DeepSeek。
7. 不超过 20 个 live Run 的功能冒烟通过，永久配置错误立即停止。
8. Docker 功能开关关闭后可回到当前确定性管线，无需回滚数据库。

## 2. 实施顺序原则

- 先建立不可变类型、迁移、RLS、审计和 Provider 契约，再改工作流行为。
- 每项先写失败测试，再实现最小代码，通过局部测试后继续。
- 数据库迁移先上线、功能开关保持关闭；模型路径最后显式启用。
- 默认测试只能使用 FakeModelGateway 或 MockTransport。
- 任何 live API 调用必须在 Docker 配置、测试和日志脱敏通过后执行。

## 3. 任务 1：冻结基线与模型配置边界

### 文件

```text
tests/test_app/test_production_worker.py
tests/test_operations/test_entrypoints.py
tests/test_platform/models/test_no_registry_credentials.py
deployment/docker-compose.yml
docs/operations/search-platform.md
```

### 实施

- 增加失败测试，证明 API/Streamlit 环境不包含 `DEEPSEEK_API_KEY`。
- 增加 Worker 功能开关默认关闭的测试。
- 增加角色配置校验测试：模型路径开启时，Planner/Verifier/Synthesizer provider/model 必须完整。
- 明确 `deepseek-v4-flash`、官方 base URL、thinking disabled 和 live-eval 上限配置。
- 记录当前全量 pytest、Compose health、现有 deterministic E2E 结果，作为改造前基线。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_app/test_production_worker.py tests/test_operations/test_entrypoints.py tests/test_platform/models/test_no_registry_credentials.py
docker compose -f deployment/docker-compose.yml --profile workers config --quiet
```

验收：测试阶段不需要发出真实 DeepSeek 请求；现有 Worker 默认行为不变。

## 4. 任务 2：扩展 Model Gateway 的请求与调用上下文

### 文件

```text
sana/modules/model_gateway/domain.py
sana/modules/model_gateway/ports.py
sana/modules/model_gateway/service.py
sana/modules/orchestration/step_handlers/base.py
sana/app/sql_step_execution.py
tests/test_modules/model_gateway/test_budget.py
tests/test_modules/model_gateway/test_structured_output.py
tests/test_app/test_sql_step_execution.py
```

### 实施

- 新增 `OutputFormat`、`ThinkingMode`、`ModelInvocationContext`、`ModelInvocationStatus` 和脱敏 audit value objects。
- `ModelRequest` 增加 output format、thinking mode、prompt template version 和 parser schema version。
- `StepExecutionContext` 增加 attempt ID、attempt number 和 TraceContext。
- `BudgetUsage` 增加 prompt/completion token 计数；模型调用次数从 StepBudgetCost 迁移为 durable invocation reservation，移除双重计数路径。
- `SqlStepExecutionStore.claim` 不再丢弃 trace context，并按 StepType→BudgetPhase 设置 execution deadline：非合成步骤不得超过 non-synthesis deadline，合成步骤使用 hard deadline。
- Model Gateway 的每个实际 provider retry/repair 使用独立调用序号；持久化 AuditSink 是调用硬上限的唯一权威预留入口。
- 在 Provider 返回后记录 token；预算异常携带已发生调用的脱敏审计信息。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_modules/model_gateway tests/test_app/test_sql_step_execution.py
```

验收：调用、retry、repair、deadline 和 token 用量有确定性测试；旧调用方可通过安全默认值平滑迁移。

## 5. 任务 3：新增模型审计与完整证据血缘迁移

### 文件

```text
alembic/versions/0007_deepseek_quality_pipeline.py
sana/platform/db/models/model_gateway.py
sana/platform/db/models/search.py
sana/platform/db/models/__init__.py
tests/test_platform/db/test_schema_metadata.py
tests/test_platform/db/test_migration_heads.py
tests/test_platform/db/test_rls.py
```

### 实施

- 从 `0006_merge_evidence_memory_heads` 建立单一 Alembic head。
- 新建 `model_invocations`：tenant/run/step/attempt、role、provider、model、call_no、logical_call_key、status、provider_called、reused_from、token、字符数、template/schema version、错误和时间字段。
- 唯一约束覆盖 `(attempt_id, role, call_no)`；logical call key 建查询索引，不宣称跨 Attempt exactly-once。
- 为新表启用并 FORCE RLS，补 tenant-local ID 约束。
- `evidence_candidates` 增加 `source_identity`、`source_authority`。
- `citations` 增加 document version、chunk、quote、start/end offset。
- 迁移采用 nullable→回填→NOT NULL；authority 无可靠依据时回填 UNKNOWN。
- downgrade 只回退本阶段 schema，不删除旧业务表。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_platform/db/test_schema_metadata.py tests/test_platform/db/test_migration_heads.py tests/test_platform/db/test_rls.py
docker compose -f deployment/docker-compose.yml run --rm migrate
```

验收：Alembic 只有一个 head；新表与新增列均受 RLS 保护；已有 Docker 数据无损升级。

## 6. 任务 4：实现 durable ModelInvocationAuditSink

### 文件

```text
sana/platform/db/model_audit.py
sana/modules/model_gateway/ports.py
sana/modules/model_gateway/service.py
sana/app/reconciliation.py
sana/app/production_worker.py
tests/test_platform/db/test_model_audit.py
tests/test_app/test_reconciliation_pump.py
```

### 实施

- 定义 `ModelInvocationAuditSink.start/complete/fail/reuse` port。
- SQL adapter 每个调用使用短 tenant UoW：出站前持久化 STARTED，响应或错误后封账。
- start 事务锁定 SearchRun，验证 Step RUNNING、Attempt 当前且未完成、deadline 未到，原子检查并递增 4/8 调用预算；失败、retry、repair 和 ABANDONED 均计数。
- audit 写入失败时禁止调用 Provider，避免产生不可审计费用。
- 结构化响应内容写入 tenant-scoped artifact；审计只保存 reference、大小和 schema version，不保存正文。
- logical call key 包含 provider、model、thinking/output format、template/parser version 和 Step input refs。
- 新 Attempt 可复用校验通过的 COMPLETED artifact，并写 REUSED 记录；复用不增加 provider-call budget。
- Reconciler 回收 lease 时，将 Attempt 下仍为 STARTED 的调用封账为 `ABANDONED/POSSIBLY_BILLED`。
- complete/fail 事务把可用 prompt/completion tokens 累加至 SearchRun usage；REUSED 不递增调用数或 token。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_platform/db/test_model_audit.py tests/test_app/test_reconciliation_pump.py tests/test_modules/model_gateway
```

验收：成功、retry、repair、错误、复用和崩溃窗口均产生准确且幂等的脱敏记录。

## 7. 任务 5：更新 DeepSeek V4 Provider 契约

### 文件

```text
sana/platform/models/_openai_compatible.py
sana/platform/models/deepseek.py
tests/test_platform/models/test_no_registry_credentials.py
tests/test_platform/models/test_deepseek_v4.py
```

### 实施

- DeepSeek base URL 改为官方 `https://api.deepseek.com`，请求路径为 `/chat/completions`。
- V4 请求明确包含 `model=deepseek-v4-flash`、`thinking.type=disabled` 和 `response_format.type=json_object`。
- 共享 OpenAI-compatible adapter 只发送通用字段；DeepSeek 专有 thinking 字段由 DeepSeek adapter 映射，避免污染其他 Provider。
- 不持久化或返回 raw Provider payload；只暴露 text、model、token usage 和安全响应元数据。
- mock 401/403、429、5xx、timeout、空 content、非法 usage 和 keep-alive 后的正常 JSON。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_platform/models
```

验收：payload 与 DeepSeek V4 官方接口一致，测试不读取宿主机真实 Key。

## 8. 任务 6：实现批量候选选择与模型 Verifier

### 文件

```text
sana/modules/evidence/candidate_selector.py
sana/modules/evidence/model_verifier.py
sana/modules/evidence/source_authority.py
sana/modules/evidence/domain.py
sana/app/search_operations.py
pyproject.toml
deployment/requirements-runtime.txt
tests/test_modules/evidence/test_candidate_selector.py
tests/test_modules/evidence/test_model_verifier.py
tests/test_modules/evidence/test_evidence_levels.py
```

### 实施

- 候选选择每 Fact 最多 3 个、全局最多 12 个、每段最多 600 字符。
- 使用离线 PSL 解析 registrable domain；同一 Fact 优先不同 source identity。
- 增加带内置 PSL snapshot 的解析依赖，运行时禁止自动联网更新 PSL。
- authority 仅由版本化 allowlist 决定，模型不能提供或覆盖。
- Verifier parser 只接受输入集合中的 fact/candidate ID、枚举 support type、exact quote、confidence 和 allowlisted reason codes。
- quote 必须重新定位至 Chunk/DocumentVersion exact span，随后调用 `EvidenceBuilder` 和 `EvidenceVerifier`。
- 单次模型输出可对多个 Fact/候选给出判断；重复判断按稳定规则去重。
- 模型失败时运行 lexical fallback，并标记 degraded；fallback 结果最高只允许 PARTIAL。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_modules/evidence
```

验收：伪造 quote、跨 tenant/run、错误 document version、同源伪装独立来源均无法成为 ACCEPTED Evidence。

## 9. 任务 7：实现受约束的模型 Synthesizer

### 文件

```text
sana/modules/answer/model_synthesizer.py
sana/modules/answer/synthesizer.py
sana/modules/answer/citation_validator.py
sana/app/search_operations.py
tests/test_modules/answer/test_model_synthesizer.py
tests/test_modules/answer/test_citation_validator.py
```

### 实施

- Synthesizer 输入仅包含 Fact、Coverage 和 ACCEPTED Evidence。
- parser 输出 ProposedClaim，不接受 URL、citation label、support status 或 AnswerQuality。
- 生产路径调用 `CoverageEvaluator`、`ClaimSynthesizer`、`CitationValidator`，删除现有手写的重复安全规则。
- Citation persistence 使用 Validator 生成的完整 lineage snapshot。
- AnswerQuality/StopReason 由 coverage 和 deadline policy 确定。
- 模型失败时使用 deterministic evidence renderer；记录 degraded，不丢失已验证 Evidence。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_modules/answer tests/test_modules/evidence
```

验收：任何 factual claim 都有合法 Citation；无证据 Fact 只能缺失、UNCERTAINTY 或被删除。

## 10. 任务 8：把工作流改为单一 VERIFY fan-in

### 文件

```text
sana/app/workflow_completion.py
sana/app/search_operations.py
sana/modules/orchestration/search_workflow.py
tests/test_app/test_sql_step_execution.py
tests/test_workflows/test_fast_search.py
tests/test_workflows/test_fast_partial.py
tests/test_workflows/test_research_search.py
```

### 实施

- EXTRACT 成功/失败后统一进入 `_maybe_verify` barrier。
- 等待所有 EXTRACT terminal 后只创建 `step_key=verify`。
- VERIFY 输入包含 plan ref 和所有成功 extract refs；无 extract 时直接进入 synthesize。
- VERIFY 输出包含多个 Evidence 和完整 coverage assessment；completion 在同一事务持久化。
- SYNTHESIZE 只依赖唯一 verify ref；verify 失败时按 degraded policy 创建 synthesize。
- successor 创建、Evidence/Claim/Citation 写入和最终 Run 状态继续使用稳定 UUID/on-conflict 约束。
- 实际 LLM 调用数只由 AuditSink 原子预留并写入 SearchRun usage；StepBudgetCost 不重复累计；超预算在网络调用前拒绝。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_workflows tests/test_app/test_sql_step_execution.py
```

验收：多个 Extract 只能生成一个 VERIFY；重复 delivery 和 barrier 竞争不能生成第二个 VERIFY/SYNTHESIZE/assistant message。

## 11. 任务 9：生产 Worker 接线、开关与回滚

### 文件

```text
sana/app/production_worker.py
deployment/docker-compose.yml
start-worker.ps1
docs/operations/search-platform.md
tests/test_app/test_production_worker.py
tests/test_operations/test_entrypoints.py
```

### 实施

- 一个 DeepSeek Provider、一个 AuditSink、一个 Model Gateway 配置三个角色。
- 增加 model pipeline 开关及角色 provider/model 配置。
- 开关关闭时保持当前 deterministic 管线；开启时配置不完整则 Worker 拒绝启动。
- Compose 继续只向 Worker 注入模型 Key；不把 Key 写入文件、镜像、日志或 UI。
- Worker 启动日志只显示 provider/model/开关，不显示 credential。
- 记录一条配置关闭的回滚命令；不回滚 migration。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_app/test_production_worker.py tests/test_operations/test_entrypoints.py
docker compose -f deployment/docker-compose.yml --profile workers config --quiet
```

验收：开关两种状态均可启动；模型配置错误 fail closed；所有常驻容器继续非 root。

## 12. 任务 10：Eval、隐私与预算报告

### 文件

```text
scripts/run_live_search_evals.py
evals/live_search_cases.jsonl
sana/modules/orchestration/evaluation.py
sana/platform/telemetry/metrics.py
sana/platform/telemetry/redaction.py
tests/test_evals/test_live_runner_safety.py
tests/test_platform/telemetry/test_redaction.py
```

### 实施

- live runner 要求 `--confirm-live`，默认拒绝运行。
- `--max-runs` 硬限制不超过 20；永久错误时 fail-fast。
- 报告每个 Run 的 mode、status、quality、facts、citations、model calls/tokens、degraded、latency。
- 小样本只报告 observed p50/p95/max，不声称生产 SLO。
- 模型费用由版本化费率配置估算，tokens 才是持久事实。
- Redactor 明确拒绝 Authorization、API Key、prompt、网页正文、reasoning content 和 Provider raw。

### 验证

```powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_evals/test_live_runner_safety.py tests/test_platform/telemetry
.\venv\Scripts\python.exe scripts/run_search_evals.py --fixtures evals/search_cases.jsonl --pretty
```

验收：未给确认标志时零网络；报告不会包含 fixture 原文、Key 或网页正文。

## 13. 任务 11：全量离线与 Docker 迁移验证

### 执行

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m compileall -q sana
git diff --check
docker compose -f deployment/docker-compose.yml --profile workers build
docker compose -f deployment/docker-compose.yml --profile workers up -d
docker run --rm sana-v2:local python -m pip check
```

### 验收

- 全量测试零失败，默认测试日志没有真实 API 请求。
- migrate/artifact-init 正常退出；PostgreSQL/Redis/API healthy。
- API、Dispatcher、Worker、Streamlit 为非 root。
- 四个 Celery exchange/routing key 仍隔离。
- 功能开关关闭时旧 deterministic FAST 回归成功。

## 14. 任务 12：受控 DeepSeek Live Eval

### 前置检查

- 只检查 `DEEPSEEK_API_KEY` 是否非空，不输出其值。
- 显式设置 pipeline enabled、三个角色为 DeepSeek V4 Flash、thinking disabled。
- 确认 Redis 队列为空、Worker 日志无永久配置错误。

### 执行顺序

1. 直接 Provider JSON contract smoke：1 次。
2. FAST 单事实：2–3 个 Run。
3. RESEARCH 多事实/Apex：2–3 个 Run。
4. 无答案和 Provider 部分失败：2 个 Run。
5. 仅在前述通过时扩展，整轮最多 20 个 Run。

### 停止条件

- 401/403、余额不足、无效模型名。
- Citation 无法回溯或模型 quote 越权成为 ACCEPTED。
- FAST/RESEARCH hard deadline 超时。
- 任一 Run 超出 4/8 调用上限。
- 日志或审计出现 Key、原始 prompt、网页全文或 reasoning content。

### 输出

- Run ID、mode、quality、stop reason。
- fact coverage/citation traceability。
- role-level calls/tokens/latency/degraded。
- observed p50/p95/max 和超时数。
- 明确声明该样本不能证明生产 SLO。

## 15. 任务 13：模型调用 Chaos 与回滚验证

### 场景

1. Worker 在 Provider 请求发出后 SIGKILL。
2. Worker 在模型 artifact/audit 完成后、Step 提交前 SIGKILL。
3. Redis 队列清空。
4. 429/5xx retry 和 repair 中途重启。
5. 关闭 model pipeline 开关并滚动重启 Worker。

### 验收

- 场景 1 的 audit 为 ABANDONED/POSSIBLY_BILLED，Run 可恢复且只有一个答案。
- 场景 2 复用已校验 artifact，不产生新的实际 Provider 调用。
- 所有旧 Attempt 最终封账，无 STARTED 审计永久悬空。
- 关闭开关后 deterministic 管线可用，数据库无需 downgrade。

## 16. 最终交付

- 设计与实施计划保持未提交状态。
- 汇报实际改动文件、migration head、测试数量、Docker 状态、live Run 数与 token 用量。
- 单独列出未关闭项：生产 Shadow SLO、生产模型切换、多主机 artifact、OIDC、记忆迁移。
- 不创建 commit、不 push，等待用户后续明确指令。
