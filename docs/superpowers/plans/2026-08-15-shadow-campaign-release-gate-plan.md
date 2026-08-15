# Sana Shadow Campaign 与发布门禁实施计划

日期：2026-08-15

状态：任务 0–12 离线发布门禁已完成；任务 13 live smoke 待本地 DeepSeek credential

对应设计：docs/superpowers/specs/2026-08-15-shadow-campaign-release-gate-design.md

设计基线：6c15fb3 docs: complete shadow campaign architecture audit

实施收口：实际迁移保持线性 `0009_shadow_campaign_gate -> 0010_shadow_collector_audit`，当前唯一 Alembic head 为 `0010_shadow_collector_audit`。任务 11/12 的可重复测试映射、Docker 故障证据与已修复反例记录在 `docs/operations/shadow-campaign-fault-matrix.md`。离线 gate 不能替代任务 13 的真实 DeepSeek smoke。

## 1. 执行边界

- 实现从 6c15fb3 建立独立 clean worktree 和 codex/shadow-campaign-release-gate 分支；当前工作区中的旧搜索/UI/user_profile 改动保持原样，不复制进候选镜像。
- 每项任务遵循 failing test -> minimal implementation -> focused tests -> commit；禁止把多个未验证阶段压成一个提交。
- 所有默认测试使用 Fake/Mock Provider，不读取真实 DeepSeek Key，不访问公网。
- 只有离线测试、迁移、RLS、Docker 隔离、隐私扫描和故障注入全部通过后，才允许执行 6-run live smoke。
- 只有 smoke PASS、身份完全一致、24 小时内有效且成本预测不超过 0.10 美元，才允许创建 120-run Full Campaign。
- 本阶段不改 Streamlit，不接入实时用户 Shadow，不引入第二模型或第二生产搜索 Provider。
- 实现提交可以保留在任务分支；完成离线门后再决定推送，不能为了远程同步跳过验证。

## 2. 完成定义

以下条件全部满足才算完成：

1. Conversation 创建和 Message submission 在真实并发与四个崩溃窗口中保持单一 Conversation/SearchRun。
2. 0009 迁移为单一 Alembic head；Campaign/Result/Review FORCE RLS、owner-only 服务授权和 tenant-local FK 全部通过。
3. Campaign 创建、调度、暂停、恢复、停止、人工 review 和报告封账均幂等。
4. 每个 Result 的 call/cost reservation 只预留一次、只结算或释放一次；Campaign 总账与 Result/ModelInvocation 可反向核对。
5. Collector 使用一致性快照，完整度量 Fact/Evidence/Claim/Citation/ModelInvocation，并拒绝 source digest 漂移。
6. 发布门禁按 fatal -> sample sufficiency -> quality 的固定优先级决定，不能通过 CLI 临时改阈值。
7. Candidate、Harness、Environment provenance 可复验；运行环境与开发数据库、Redis、队列和 volume 完全隔离。
8. JSON/Markdown 报告内容寻址、可重复生成、无 prompt/answer/query/quote/secret/reviewer identity。
9. 6-run DeepSeek smoke PASS 后才运行 Full；Full 要么形成合格报告，要么以完整 stop intent/SKIPPED/账本受控封账。
10. 全量默认 pytest、compileall、migration、Docker health 与 privacy scan 全部通过。

## 3. 任务 0：建立 clean implementation worktree 与冻结基线

### 操作

- 从当前 6c15fb3 创建独立 worktree，例如 D:\MyProduct\sana_v2-shadow-campaign。
- 新分支命名 codex/shadow-campaign-release-gate。
- 验证新 worktree tracked/untracked runtime 文件均为空。
- 记录 Python、Docker、Compose、PostgreSQL、Redis、当前 Alembic head、候选 commit 与 image ID。
- 在不读取真实 Key 的前提下执行当前全量 pytest、compileall 和 Compose config。

### 验证

    .\venv\Scripts\python.exe -m pytest -q
    .\venv\Scripts\python.exe -m compileall -q sana
    docker compose -f deployment/docker-compose.yml --profile workers config --quiet
    git status --short

验收：新 worktree clean；基线失败若存在必须先分类并记录，不能混入本阶段修复。

## 4. 任务 1：建立 Shadow Campaign 纯领域内核

### 文件

    sana/modules/shadow_campaign/__init__.py
    sana/modules/shadow_campaign/domain.py
    sana/modules/shadow_campaign/manifest.py
    sana/modules/shadow_campaign/policy.py
    sana/modules/shadow_campaign/evaluator.py
    sana/modules/shadow_campaign/ports.py
    tests/test_modules/shadow_campaign/test_domain.py
    tests/test_modules/shadow_campaign/test_manifest.py
    tests/test_modules/shadow_campaign/test_evaluator.py

### 实施

- 定义 CampaignStatus、GateStatus、StopIntent、SchedulingState、ReservationState、ReviewVerdict、ReviewActor、ErrorClass。
- 定义 CampaignProfile、GatePolicy、CostRate、ReviewRubric 的不可变 value objects、版本与 canonical snapshot/hash。
- 实现两个 live profile：docker-smoke-v1 与 shadow-full-v1；阈值不能从 CLI 覆盖。
- 实现 JSONL manifest parser：严格未知字段拒绝、Unicode/长度/数值边界、case ID、6 smoke、40 case、四分层、oracle 窗口、answerability 不变量。
- 实现 allowlisted gold operators，不允许 eval、模板、shell、正则或动态 import。
- 实现稳定 review 抽样、nearest-rank percentile、case-macro coverage、Wilson interval 和固定 gate 优先级。
- 领域包不得 import FastAPI、SQLAlchemy、Docker SDK、HTTP client 或具体存储 adapter。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_modules/shadow_campaign

验收：相同输入得到字节级相同 policy/manifest/decision hash；fatal 永远优先于样本不足。

## 5. 任务 2：实现 0009 schema、约束、RLS 与兼容写入

### 文件

    alembic/versions/0009_shadow_campaign_release_gate.py
    sana/platform/db/models/conversation.py
    sana/platform/db/models/search.py
    sana/platform/db/models/shadow_campaign.py
    sana/platform/db/models/__init__.py
    sana/app/search_operations.py
    tests/test_platform/db/test_schema_metadata.py
    tests/test_platform/db/test_migration_heads.py
    tests/test_platform/db/test_rls.py
    tests/test_platform/db/test_shadow_campaign_schema.py
    tests/test_app/test_search_operations.py

### 实施

- 从 0008_provider_attempt_identity 建立线性 0009。
- conversations 增加 nullable creation_idempotency_key/request_hash 与 tenant/user/key 唯一约束。
- answer_claims 增加 nullable claim_kind/fact_requirement_id；新 Run 写入 FACTUAL/Fact 绑定，历史 NULL 保持兼容。
- 为 fact_requirements 建 tenant/run/id 唯一约束和 AnswerClaim composite deferred FK。
- 新建 shadow_campaigns、shadow_run_results、shadow_manual_reviews，包含设计中全部 identity、state、ledger、review、retention、digest 和 final report 字段。
- FK 全部 tenant-local；Conversation/SearchRun/parent smoke 使用 DEFERRABLE INITIALLY DEFERRED 与 NO ACTION。
- 三表启用并 FORCE RLS；应用角色不含 BYPASSRLS。
- 数据库 CHECK 只保护枚举、非负、字段组合和局部不变量，不用 ceiling CHECK 阻止写入真实违规证据。
- downgrade 只回退 0009，不删除既有业务数据。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_platform/db/test_schema_metadata.py tests/test_platform/db/test_migration_heads.py tests/test_platform/db/test_rls.py tests/test_platform/db/test_shadow_campaign_schema.py tests/test_app/test_search_operations.py
    docker compose -f deployment/docker-compose.yml run --rm migrate

验收：Alembic 单一 head；升级/降级/再升级稳定；跨 tenant Fact、Campaign、Result、Review 访问均拒绝。

## 6. 任务 3：加固通用 Conversation/Message 幂等 API

### 文件

    sana/app/api/routes/conversations.py
    sana/app/api/schemas/conversations.py
    sana/app/api/services.py
    sana/modules/conversation/domain.py
    sana/modules/conversation/ports.py
    sana/platform/db/repositories.py
    sana/platform/db/models/conversation.py
    tests/test_app/api/test_conversations.py
    tests/test_app/api/test_run_idempotency.py
    tests/test_modules/conversation/test_conversation_service.py
    tests/test_platform/db/test_atomic_submission.py

### 实施

- Conversation create 接受可选 Idempotency-Key；无 Header 的普通客户端行为不变。
- 规范化 title 后计算 request hash；PostgreSQL INSERT ON CONFLICT DO NOTHING RETURNING 后 tenant/user scoped 重读。
- 相同 key/hash 返回原 Conversation；相同 key/不同 hash 返回 409。
- Message submit 在校验 owner 的同时 SELECT FOR UPDATE 锁 Conversation，再读取 existing submission。
- existing submission 必须比较规范化 content hash；payload mismatch 返回 409，不能返回旧 receipt。
- 所有 Message/ResponseRun/SearchRun/Step/Outbox/Event 仍在一个事务创建；唯一约束异常不得泄露为 500。
- 日志和错误只含稳定 reason code，不含 Message content。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_app/api/test_conversations.py tests/test_app/api/test_run_idempotency.py tests/test_modules/conversation/test_conversation_service.py tests/test_platform/db/test_atomic_submission.py

验收：两个真实并发 create 返回同一 Conversation；两个真实并发 submit 只生成一套工作流记录。

## 7. 任务 4：实现 Campaign Store、owner authorization 与逐 Result 账本

### 文件

    sana/modules/shadow_campaign/service.py
    sana/platform/db/shadow_campaign.py
    sana/app/shadow_campaign_services.py
    tests/test_platform/db/test_shadow_campaign_store.py
    tests/test_modules/shadow_campaign/test_state_machine.py
    tests/test_modules/shadow_campaign/test_ledger.py

### 实施

- Campaign create 使用 tenant/user/key 与 request hash 幂等创建 Campaign 和全部 Result。
- Full create 锁定 parent smoke，验证 owner、COMPLETED/PASS、decision hash、24 小时与完整 identity。
- 创建 Result 时一次性计算 schedule_ordinal 与 20 个 manual_review_selected 标志。
- 所有命令校验 principal tenant/user 与 Campaign owner；同 tenant 非 owner 同样拒绝。
- lease claim 使用 SKIP LOCKED/乐观版本，只允许合法状态转换。
- 固定 Campaign -> Result 锁顺序；admission 原子写入 ACTIVE 8-call/cost reserve。
- settlement/release 只允许一次；重放为 no-op；未知出站按 possibly-billed charge 封账。
- planned/submitted/collected/failed/skipped/degraded 为缓存计数；事实来源仍是 Result rows。
- pause、abort、fatal、budget、call ceiling 的 stop intent 先持久化，再 drain；SKIPPED 保存稳定 reason。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_modules/shadow_campaign/test_state_machine.py tests/test_modules/shadow_campaign/test_ledger.py tests/test_platform/db/test_shadow_campaign_store.py

验收：并发 admission 不超卖；reservation commit 后崩溃不二次预留；Collector 重放不二次结算；最终没有 ACTIVE reservation。

## 8. 任务 5：实现一致性 Outcome Collector 与 source digest

### 文件

    sana/modules/shadow_campaign/collector.py
    sana/platform/db/shadow_collector.py
    sana/app/shadow_collector.py
    tests/test_modules/shadow_campaign/test_collector.py
    tests/test_platform/db/test_shadow_collector.py

### 实施

- 只有 Run terminal、Step/Attempt/ModelInvocation 封口、outbox 已发布或 reconciliation 完成时可收集。
- 在 tenant-scoped REPEATABLE READ, READ ONLY 事务读取 Run、Fact、Evidence、Claim、Citation、QuerySpec、Invocation、ProviderAttempt、Step/Attempt。
- factual Claim 分母包含全部 FACTUAL；kind NULL、Fact NULL、链路/offset/tenant/run 不一致全部 fail closed。
- 在内存执行 declarative gold assertions，只持久化 assertion ID、状态和 reason code。
- source_snapshot_digest 只含 metric-relevant allowlist，不含文本、URL query、Provider body 或 secret。
- Result 写入和 ACTIVE reservation settlement 在固定锁顺序下幂等完成。
- finalization 前可重算 digest；漂移产生 source_snapshot_mismatch，不能覆盖已 review Result。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_modules/shadow_campaign/test_collector.py tests/test_platform/db/test_shadow_collector.py

验收：跨表并发更新不能形成撕裂 Result；失败/取消 Run 仍可 COLLECTED；未封口数据不能被当成正常样本。

## 9. 任务 6：实现精确 Review Projection 与结构化人工复核

### 文件

    sana/modules/shadow_campaign/review.py
    sana/platform/db/shadow_review.py
    sana/app/shadow_review.py
    tests/test_modules/shadow_campaign/test_review.py
    tests/test_platform/db/test_shadow_review.py

### 实施

- Conversation/Run API 读取 Message/answer；ProjectionReader 只读查询 Claim -> Citation -> VerifiedEvidence -> DocumentVersion/Chunk。
- 所有 projection join 同时约束 tenant 与 run，拒绝 orphan、错 run、非法 offset。
- Repository 只允许 selected Result、正确 rubric、owner Principal、deadline 内写入。
- HUMAN 必须有 reviewer principal；SYSTEM 只允许 expected_answer_missing 或 review_material_unavailable 两类稳定自动记录。
- 候选缺少应有答案写 MAJOR_ERROR；基础设施无法形成 projection 写 UNREVIEWABLE；二者都不能换样。
- 不保存自由文本、prompt、answer、quote、网页正文；reviewer identity 不进入报告。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_modules/shadow_campaign/test_review.py tests/test_platform/db/test_shadow_review.py

验收：20 个预选 unit 不可替换；迟到或非 owner review 拒绝；同 tenant 其他用户无权限。

## 10. 任务 7：实现 Gate、CampaignReportStore 与可恢复最终封账

### 文件

    sana/modules/shadow_campaign/report.py
    sana/platform/storage/campaign_reports.py
    sana/app/shadow_report.py
    sana/platform/telemetry/redaction.py
    tests/test_modules/shadow_campaign/test_report.py
    tests/test_platform/storage/test_campaign_reports.py
    tests/test_app/test_shadow_finalization.py
    tests/test_platform/telemetry/test_redaction.py

### 实施

- Evaluator 使用完整 Result/Review/Policy 输入，输出每条 rule 的 threshold、observed、sample、status、reason code。
- 重算 Result 状态计数、Campaign ledger 与 ModelInvocation 事实；campaign_ledger_mismatch 为硬门。
- canonical JSON 使用稳定 key、UTC RFC3339、decimal string、basis points、禁止 NaN/Infinity。
- CampaignReportStore 使用 tenant/campaign/content digest 作用域、原子 rename、hash verify、路径穿越拒绝和 campaign-artifact URI。
- Markdown 只能由 canonical decision payload 单向生成。
- 三步 finalization：decision_input_hash -> 写 content-addressed artifacts -> 锁 Campaign 重验并原子绑定。
- 并发 report 收敛；artifact 后崩溃可恢复；stale input 拒绝绑定。
- PENDING_REVIEW snapshot 不写 final fields。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_modules/shadow_campaign/test_report.py tests/test_platform/storage/test_campaign_reports.py tests/test_app/test_shadow_finalization.py tests/test_platform/telemetry/test_redaction.py

验收：相同输入的 JSON/decision hash 恒定；报告不含 fixture prompt、答案、query、quote、URL credential、token 或 reviewer identity。

## 11. 任务 8：实现七命令 Runner、API client 与崩溃恢复

### 文件

    sana/app/shadow_runner.py
    sana/app/shadow_api_client.py
    scripts/run_shadow_campaign.py
    tests/test_evals/test_shadow_runner_cli.py
    tests/test_evals/test_shadow_runner_recovery.py

### 实施

- 实现 create/list/resume/pause/abort/review/report。
- create 要求 confirm-live、campaign-key、manifest、profile、api-url；Full 额外要求 parent smoke ID。
- Token 只从 SANA_ACCESS_TOKEN 或交互 getpass 获取；CLI 参数、日志、异常、DB、report 禁止出现。
- 首先调用 /api/v1/me，所有 DB 操作绑定返回的 tenant/user owner。
- 调度并发固定 2；每单元先 admission，再幂等 Conversation，绑定后再幂等 Message。
- 恢复分支严格依据 reservation/conversation/search_run 状态，不生成新 key 或 ID。
- API/Collector transient 最多 3 次，0.5/1/2 秒 jitter；401/403/payload/provenance/invariant 不重试。
- Ctrl-C/SIGTERM 转 PAUSE；abort/fatal/budget/call ceiling 只 drain/finalize。
- list 仅输出 owner 的非敏感摘要；终端 Campaign 不能 resume。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_evals/test_shadow_runner_cli.py tests/test_evals/test_shadow_runner_recovery.py

验收：缺 confirm-live 时零网络；create commit 后丢失输出可 list/resume；四个 receipt 崩溃窗口均无第二个 SearchRun。

## 12. 任务 9：构建 Candidate/Harness/Environment provenance 与专用 Compose

### 文件

    deployment/docker-compose.shadow-eval.yml
    deployment/Dockerfile
    scripts/run_shadow_campaign.ps1
    sana/app/shadow_provenance.py
    tests/test_deployment/test_shadow_eval_compose.py
    tests/test_evals/test_shadow_provenance.py
    docs/operations/shadow-campaign.md

### 实施

- standalone Compose 只包含 Postgres、role provision、Redis、migrate、artifact init、API、dispatcher、worker 和 transient campaign-runner；不含 Streamlit。
- 固定 project 为 sana-shadow-eval；使用独立 DB/Redis/search artifact/report volumes 与 network。
- 仅 API 可选绑定 127.0.0.1；DB/Redis 不暴露到宿主机。
- API/Worker/Dispatcher/Migrate/Campaign Runner 使用同一 immutable image ID；Worker 独占 DeepSeek Key，Runner 独占 Sana token。
- Dockerfile 写 OCI revision label；host launcher 从 clean worktree build，并生成只含 allowlist 的 sanitized provenance attestation。
- 不给 Runner 挂 Docker socket；host launcher负责 docker inspect/compose labels，attestation 以只读文件传入 Runner。
- 记录 candidate commit/image/migration/config、harness commit/fileset digest/schema、environment project/container/volume/network/topology。
- 原始 docker compose config、环境变量值、secret 长度/hash 永不保存。
- preflight 校验 clean、image equality、OCI revision、Alembic head、loopback、空队列、无非 Campaign active Run/outbox、Worker concurrency。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_deployment/test_shadow_eval_compose.py tests/test_evals/test_shadow_provenance.py
    docker compose -p sana-shadow-eval -f deployment/docker-compose.shadow-eval.yml config --quiet

验收：任何共享开发 volume/network/queue、dirty harness、mutable/mismatched image、错误 migration 或 provenance 缺失都在零 Provider 调用时失败。

## 13. 任务 10：建立版本化 40-case manifest、策略与 rubric

### 文件

    evals/shadow/cases-v1.jsonl
    evals/shadow/profiles-v1.json
    evals/shadow/gate-policies-v1.json
    evals/shadow/review-rubric-v1.json
    evals/shadow/cost-rates-v1.json
    tests/test_evals/test_shadow_manifest_v1.py

### 实施

- 40 个 case：FAST/RESEARCH 各 20；每 mode 中英文各 10；每个 case 重复 3 次。
- 四个 mode/locale 分层各至少 5 个 answerable；至少 8 个 no-answer/conflict；至少 6 个 Apex/pollution；至少 16 个稳定 gold case。
- 只有稳定事实使用 deterministic assertions；动态事实必须 manual_required。
- forbidden query terms 仅用于内存计数，不进入报告。
- 所有 snapshot 经过 canonical hash 固定；相同 version/不同 hash 必须失败。
- 测试确保 prompt、oracle 与有效窗口满足 6 小时 active window 和 review 抽样约束。

### 验证

    .\venv\Scripts\python.exe -m pytest -q tests/test_evals/test_shadow_manifest_v1.py tests/test_modules/shadow_campaign/test_manifest.py

验收：manifest 可产生 exactly 120 个稳定调度单元、FAST/RESEARCH 各 60、20 个无重复 review unit。

## 14. 任务 11：离线集成、故障注入与全量回归

### 场景

1. Conversation response、Conversation binding、Message receipt、Run binding 后分别 kill Runner。
2. admission commit 后 kill；Collector settlement commit 前后分别 kill。
3. 两个 Runner 并发 claim/admission/report。
4. STOPPING drain 中分别 kill PAUSE 与 ABORT/FATAL/BUDGET/CALL_CEILING。
5. source table 在 collection/finalization 间发生受控漂移。
6. artifact 写入后、Campaign bind 前 kill。
7. 伪造跨 tenant/run Claim/Citation/Evidence、无 Fact factual Claim 和非法 quote offset。
8. token/key/password/prompt/answer/query/quote 注入日志与异常扫描。

### 验证

    .\venv\Scripts\python.exe -m pytest -q
    .\venv\Scripts\python.exe -m compileall -q sana scripts
    git diff --check
    docker compose -p sana-shadow-eval -f deployment/docker-compose.shadow-eval.yml build
    docker compose -p sana-shadow-eval -f deployment/docker-compose.shadow-eval.yml run --rm migrate

验收：默认测试零公网；无重复 Run、ACTIVE reservation、悬挂 STARTED、账本不平、隐私泄露或多 Alembic head。

## 15. 任务 12：专用 Docker 假 Provider smoke

### 实施

- 在专用 evaluation project 中启动 clean immutable image。
- 使用测试专用 FakeModelGateway/FakeSearchProvider，不能读取宿主机真实 credential。
- 完成 6 个调度单元、pause/resume、一个 receipt 丢失注入、report 重生成。
- 验证 API/Worker/Dispatcher/Migrate/Runner image identity、RLS、队列空闲与资源记录。
- 重建一次完全相同输入，确认 canonical decision hash 稳定。

### 验证

    docker compose -p sana-shadow-eval -f deployment/docker-compose.shadow-eval.yml up -d
    docker compose -p sana-shadow-eval -f deployment/docker-compose.shadow-eval.yml ps
    docker compose -p sana-shadow-eval -f deployment/docker-compose.shadow-eval.yml logs --no-color

验收：Docker 闭环和所有恢复路径通过，日志无 secret/正文；该测试不能冒充 live gate。

## 16. 任务 13：6-run DeepSeek live smoke

### 前置

- 候选/Harness worktree clean，OCI revision 与 0009 head 正确。
- 专用 project 全新且不共享开发数据；队列、active Run、outbox 全空。
- SANA_ACCESS_TOKEN 只注入 Runner；DEEPSEEK_API_KEY 只注入 Worker。
- profile 固定 docker-smoke-v1，模型固定 deepseek-v4-flash，thinking disabled，JSON object。

### 执行

    .\scripts\run_shadow_campaign.ps1 create --confirm-live --campaign-key <stable-key> --manifest evals/shadow/cases-v1.jsonl --profile docker-smoke-v1 --api-url http://api:8000

### 停止条件

- 401/403、无效模型、永久配置错误。
- 任一 Citation/Fact/tenant/预算硬安全违规。
- source/ledger/provenance mismatch、报告隐私违规或永久 STARTED。
- 达到 0.01 美元 stop threshold 或 32-call admission ceiling。

### 验收

- exactly 6 terminal Result；FAST/RESEARCH 各 3；至少一个 intentionally unanswerable。
- 无 SKIPPED、重复 SearchRun、账本不平或 hard deadline breach。
- cost projection 加 30% headroom 后不超过 Full 0.10 美元。
- smoke gate PASS，并保存 candidate/harness/environment identity 与 decision hash。

## 17. 任务 14：120-run Full Campaign、人工 review 与最终报告

### 前置

- 引用 24 小时内 smoke PASS Campaign。
- candidate/harness/environment/manifest/model/config/rate/migration identity 与 smoke 完全相同。
- smoke 成本预测在授权范围内；若超出，停止并向用户申请新的成本授权，不能改 CLI 阈值。

### 执行

    .\scripts\run_shadow_campaign.ps1 create --confirm-live --campaign-key <stable-key> --manifest evals/shadow/cases-v1.jsonl --profile shadow-full-v1 --parent-smoke-campaign-id <uuid> --api-url http://api:8000
    .\scripts\run_shadow_campaign.ps1 review --campaign-id <uuid>
    .\scripts\run_shadow_campaign.ps1 report --campaign-id <uuid>

### 运行规则

- 最大并发 2；每 10 个 Result 做 checkpoint/reconciliation。
- 达到 stop intent 后不再 claim，新请求停止，在途 Run drain，剩余单元明确 SKIPPED。
- 自动样本充分且无 fatal 后完成 exactly 20 个预选 review；不能事后换样。
- 任一 MAJOR_ERROR 为 FAIL；UNREVIEWABLE/超 deadline 为 INSUFFICIENT_SAMPLE。

### 最终交付

- Campaign ID、candidate/harness/environment provenance 与 clean proof。
- planned/submitted/terminal/skipped/failed/degraded、latency、coverage、gold/review、ledger、token/cost。
- 每条 gate 的 threshold/observed/status/reason code。
- canonical JSON/Markdown refs、content hash、decision input hash 与最终 PASS/FAIL/INSUFFICIENT_SAMPLE。
- 明确说明 PASS 仅允许进入 1% durable real-time Shadow 设计，不等于生产切流。

## 18. 提交与回退策略

- 任务 0–10 每个逻辑阶段单独 commit；迁移提交先于行为开关。
- Live Runner 在 0009 存在但未显式执行时不产生任何 Provider 调用。
- 回退应用行为时关闭/不执行 Campaign CLI，不 downgrade 0009，不删除 Campaign 证据。
- 如果 live smoke 或 Full 停止，保留 Campaign/Result/Review/Invocation/report 365 天，不重写 gate。
- 推送前重新运行全量 pytest、compileall、git diff --check、migration head 和 Compose config；只推送任务分支，不改写远程历史。
