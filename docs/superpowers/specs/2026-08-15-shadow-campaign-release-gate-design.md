# Sana Shadow Campaign 与发布门禁设计

日期：2026-08-15

状态：已通过上线前架构自审，进入实施计划

前置版本：`be42f10 feat: harden DeepSeek search quality pipeline`

## 1. 目标

本阶段建立一个可恢复、可审计、受费用约束的预生产 Shadow Campaign 系统，用不少于 100 次真实 Run 判断当前 DeepSeek 搜索质量管线是否具备进入低比例实时 Shadow 的资格。

系统必须回答五个问题：

1. FAST 与 RESEARCH 是否在各自时延目标内稳定完成。
2. Fact、Evidence、Claim 与 Citation 的安全不变量是否始终成立。
3. 候选是否规划了足够事实，并在稳定 oracle 与独立人工复核中给出正确答案。
4. 模型、检索 Provider、基础设施和内容缺口分别贡献了多少失败或降级。
5. 当前候选版本是否通过版本化、不可手工绕过的发布门禁。

本阶段继续使用同一个 DeepSeek API 和 `deepseek-v4-flash`，不进行生产模型对比。

## 2. 范围边界

### 2.1 本阶段包含

- 40 个不同的版本化 Shadow 用例，每个重复 3 次，共 120 个计划 Run。
- FAST 与 RESEARCH 各 60 个计划样本。
- 可创建、暂停、恢复和汇总的独立 Campaign runner。
- Tenant-scoped Campaign 与 Result 持久化。
- 版本化发布门禁策略与确定性判定器。
- 确定性 gold assertions 和 20 个分层人工 review unit。
- Clean Docker image 与运行配置 provenance preflight。
- 独立本机 Docker Compose evaluation environment，不与开发 UI、数据库、Redis 或队列共享状态。
- JSON 机器报告和 Markdown 人工报告。
- 费用、调用数、并发、失败连续次数和安全违规停止条件。
- Migration、RLS、幂等恢复、脱敏和 Docker 小 Campaign 测试。

### 2.2 本阶段不包含

- 用户请求路径中的实时 Shadow 执行。
- 第二搜索 Provider 的生产接入。
- DeepSeek 与其他生产模型的 paired comparison。
- 正式生产切流。
- S3/MinIO artifact、多主机 Worker、OIDC provisioning 或旧记忆正式导入。

实时 1% Shadow 将在本 Campaign 达标后进入单独规格。当前阶段只为它保留持久化和队列边界，不提前接入用户流量。

## 3. 设计原则

1. **测量不能改变被测搜索路径。** Runner 通过正式 Conversation/Message API 提交请求，通过 PostgreSQL 读取事实，不在进程内复制搜索、验证或合成规则。为恢复安全增加的 Conversation 创建幂等能力是通用 API 契约，不允许为 Campaign 绕过正常路由、预算或 Worker。
2. **安全指标优先于平均质量。** Citation 越权、无证据 COMPLETE、跨租户访问或调用预算越界会立即终止 Campaign。
3. **报告不复制敏感输入。** Prompt 只存在于版本化 manifest 和正常消息存储中，Campaign 表与报告只保存 case ID 和结构化指标。
4. **恢复不能重复收费。** `(campaign_id, case_id, repetition)` 是调度幂等键。每个调度单元拥有一个独立且幂等创建的 Conversation；Conversation ID 持久化后才允许提交 Message。已有 SearchRun 的条目只能收集或重试收集，不能重新提交。
5. **外部计费不宣称 exactly-once。** Provider 请求完成但本地封账前崩溃仍可能产生重复费用，必须记录为 `POSSIBLY_BILLED`。
6. **门禁不可临时放行。** 不提供 `--force-pass`。阈值变更必须产生新的策略版本并创建新 Campaign，不能重写历史判定。
7. **候选与评测工具身份必须不可变。** Candidate commit/image、迁移 head 和运行时安全配置定义被测对象；Harness commit、Runner/Collector digest、manifest、profile、策略和 rubric 定义测量工具。两类身份分开记录，任一项无法验证时禁止发起 live Campaign。
8. **候选不能给自己判正确。** 同一个 DeepSeek 候选模型不得充当最终事实正确性 Judge。自动门禁使用确定性 gold assertions，动态事实使用结构化人工复核。

## 4. 采用的路线

采用分阶段混合路线：

1. 先实现独立批量 Campaign 和发布门禁。
2. 先跑 6 次 Docker 小 Campaign。
3. 小 Campaign 通过后，运行 120 次真实 DeepSeek Campaign。
4. Campaign 达标后，再设计和实现低比例实时 Shadow。

不直接采用实时旁路，因为当前尚无可信的大样本基线、费用边界和持久化恢复能力。不长期停留在纯批量模式，因为固定用例无法覆盖真实请求分布；实时旁路是后续阶段，而不是被取消。

## 5. 总体架构

### 5.1 ShadowCampaignRunner

独立 CLI 负责：

- 校验显式 live 确认、manifest、模型配置、费率版本和费用阈值。
- 创建或恢复 Campaign。
- 按稳定顺序生成 `(case_id, repetition)` 调度单元。
- 以最大并发 2 向现有 API 提交请求。
- 每个 Run 结束后调用 Outcome Collector。
- 每完成 10 个结果执行检查点和停止条件判断。
- Campaign 中断时保留所有已提交 Run，不自动删除或重跑。

现有 `run_live_search_evals.py` 继续保持最多 20 次的 smoke 安全边界。不得通过扩大它的上限实现 Campaign；新系统使用独立的 `run_shadow_campaign.py`。

每个调度单元使用两个确定性 idempotency key：

- Conversation 创建：`shadow-conversation:{campaign_id}:{case_id}:{repetition}`。
- Message submission：`shadow:{campaign_id}:{case_id}:{repetition}`。

Conversation 创建 API 增加可选 `Idempotency-Key`，数据库使用 `(tenant_id, user_id, creation_idempotency_key)` 唯一约束并保存规范化 title 的 request hash；普通客户端不提供该 Header 时保持现有行为。幂等创建使用 PostgreSQL `INSERT ... ON CONFLICT DO NOTHING RETURNING`，未取得新行时在同一 tenant/user scope 重新读取并比较 request hash：相同 key 与相同 hash 返回原 Conversation，相同 key 配不同 title 返回 409。Runner 为每个调度单元创建独立 Conversation，取得 ID 后先把它写入 Result，再提交 Message。这样可同时避免跨 case 上下文污染和崩溃窗口重复计费：

1. Conversation response 丢失时，重放创建请求返回同一 Conversation。
2. Conversation 已绑定但 Message 尚未提交时，恢复进程继续使用同一 Conversation。
3. API 已接受 Message、但 `search_run_id` 尚未写入 Result 时，恢复进程使用同一 Conversation 和 submission key 重放，并取得原 receipt。

Message API 的幂等重放也必须校验规范化 content hash；同一个 `(tenant, conversation, key)` 配不同内容时返回 409，而不是静默返回旧 receipt。为使并发首次提交同样安全，ConversationService 必须先以 `SELECT ... FOR UPDATE` 锁定已验证归属的 Conversation 行，再执行 existing-submission lookup 与整套 Message/ResponseRun/SearchRun/Step/Outbox 创建事务；这样同一 Conversation 的并发提交被串行化，第二个请求必定读到第一个 receipt，而不是把唯一约束异常暴露成 500。Result 保存提交内容 hash，Collector 再与权威 Message 计算值核对，但不保存原文。

任何恢复分支都不得生成第二个 SearchRun。若服务端无法证明 Conversation 或 Message 创建幂等，Campaign 必须停止为 `INFRASTRUCTURE`，不能用新 ID 继续。

### 5.2 ShadowOutcomeCollector

Collector 只从 PostgreSQL 权威表读取：

- SearchRun mode、status、quality、stop reason、usage 和时间。
- required Fact 总数、覆盖数与缺口数。
- factual Claim 和 Citation 数量及可回溯率。
- AnswerClaim kind、Fact 绑定、无 Citation 的 factual Claim 数量和完整证据链有效性。
- QuerySpec 的污染命中数量，但不返回 query 文本。
- ModelInvocation 的角色、状态、调用数、token、错误分类和计费处置。
- ProviderAttempt 的成功、失败和错误类型计数。
- Step/Attempt 的失败阶段和错误码。

Collector 不重新调用 Planner、CoverageEvaluator、CitationValidator 或模型，也不修改 SearchRun。它只能在 SearchRun 已终态、所有 Step/Attempt/ModelInvocation 已封口且相关 outbox 已发布或完成 reconciliation 后开始；所有源表读取必须位于同一个 tenant-scoped `REPEATABLE READ, READ ONLY` 事务，避免跨表撕裂快照。Collector 对 metric-relevant ID、归属、状态、计数、时间与 quote offset 的 canonical allowlist 计算 `source_snapshot_digest`，但排除 prompt、query、answer、quote、网页正文、错误 body 与 secret；digest 随 Result 持久化。最终封账前必须在新的只读一致性快照中重算 digest，源数据漂移时以 `source_snapshot_mismatch` 阻止 PASS，不能静默覆盖已经人工复核的 Result。

Collector 在内存中对最终结构化 Claim 执行 manifest 的 declarative gold assertions，只持久化 assertion ID、pass/fail 和 reason code。允许的首版 operator 固定为 `normalized_contains_all`、`normalized_equals`、`number_in_range`、`set_contains` 和 `source_class_at_least`；禁止任意 Python、shell、模板执行或用户提供的正则表达式。

Citation 安全指标采用 fail-closed 语义：分母是最终答案中的全部 `FACTUAL` Claim，而不是只有 `GROUNDED/VERIFIED/CONFLICTED` 的 Claim。每个 factual Claim 必须绑定 Fact、至少有一条 Citation，且 Citation 必须能追踪到属于同一 tenant/run 的 VerifiedEvidence、DocumentVersion、DocumentChunk 和合法 quote offsets。任一 Claim 缺少 `claim_kind`、factual Claim 缺少 Fact，或 Citation 链缺失均视为违规；没有 factual Claim 时指标为 `NOT_APPLICABLE`，不能伪装成 100%。

### 5.3 ShadowResultStore

SQL adapter 负责 tenant-scoped Campaign、Result、review、检查点和最终 gate report reference。所有写入使用短事务、稳定幂等键和 PostgreSQL RLS；报告正文由 CampaignReportStore 负责。

### 5.4 ReleaseGateEvaluator

纯确定性领域组件接收：

- Campaign metadata。
- 完整 Result 集合。
- 完整结构化 ManualReview 集合。
- 版本化 GatePolicy。

输出 `PASS`、`FAIL` 或 `INSUFFICIENT_SAMPLE`，并为每条规则给出观测值、阈值、样本数和 reason code。Evaluator 不访问网络或数据库，便于使用固定数据做完全可重复的单元测试。

### 5.5 ShadowManualReviewService

动态事实不能由候选模型自评。Full Campaign 创建时即按 GatePolicy 确定性预选 20 个不同的 answerable case/run 并持久化 `manual_review_selected=true`，不能等结果出来后挑样本。独立 review 子命令通过正式 Conversation/Run API 读取原 Message 与回答，并通过 tenant-scoped、只读 `ShadowReviewProjectionReader` 从 PostgreSQL 读取精确的 Claim→Citation→VerifiedEvidence 映射；现有 Evidence API 只有 Fact/Evidence 列表，不能伪装成完整 Citation projection。人工按版本化 rubric 检查后，只把结构化 verdict、严重度和 reason code 写入数据库，不复制 prompt、答案、quote 或网页正文。现有 Principal 没有角色能力，因此首版采用明确的 owner-only authorization：只有认证 Principal 的 `(tenant_id, user_id)` 与 Campaign 创建者完全一致时才可 list/resume/pause/abort/review/report；RLS 仍负责跨 tenant 隔离。报告只公开聚合值，不公开 owner 或 reviewer 身份；正式 reviewer role 属于后续 IAM 设计，不能在本阶段伪装成已有能力。

### 5.6 CampaignReportStore

Campaign report 不复用以 SearchRun 为所有者的 `ArtifactStore` URI。新增独立 `CampaignReportStore` port，使用 tenant/campaign/content-digest 作用域和 `campaign-artifact://` URI；本地 adapter 复用同样的原子写入、哈希校验和 tenant 隔离实现，但不伪造 run ID。

## 6. 数据流

```text
Versioned Manifest + Gate/Cost Policy
                 |
                 v
  Clean-image Provenance Preflight
                 |
                 v
         ShadowCampaignRunner
                 |
                 v
 Idempotent Conversation + Existing Message API
                 |
                 v
    Durable SearchRun / Step / Evidence Pipeline
                 |
                 v
 ShadowOutcomeCollector + Gold Assertions
                 |
                 v
   Campaign Results ---- Structured Manual Reviews
                 \             /
                  \           /
                   v         v
                 ReleaseGateEvaluator
                           |
                           v
               JSON + Markdown Gate Report
```

Runner 崩溃后根据 Campaign ID 恢复。对于已有 `search_run_id` 的调度单元，只等待或收集对应 Run；已有 `conversation_id` 但尚未绑定 Run 的单元只能向该 Conversation 重放 Message；两者都没有的单元先使用确定性 Conversation key 创建或取得同一个 Conversation，持久化绑定后才提交 Message。

## 7. Manifest 设计

Manifest 使用 JSONL，每条用例至少包含：

- `id`：稳定、不可复用的 case ID。
- `prompt`：仅用于提交，不进入 Result 或报告。
- `locale`：`zh-CN` 或 `en`。
- `expected_mode`：FAST 或 RESEARCH。
- `category`：version、background、comparison、multi_fact、conflict、no_answer、provider_resilience 或 pollution_regression。
- `answerability`：answerable 或 intentionally_unanswerable。
- `minimum_required_facts`。
- `gold_assertions`：仅用于稳定事实，采用第 5.2 节 allowlist operator，不包含可执行代码或正则表达式。
- `oracle_type`：`deterministic`、`manual_required` 或 `not_applicable`。
- `valid_from`、`valid_until`：`deterministic` oracle 必填的 assertion 有效窗口；其他 oracle 必须为 null。过期 assertion 令 Campaign preflight 失败。
- `required_source_classes`：需要官方或一手来源时的稳定来源类别，不直接写临时 URL。
- `forbidden_query_terms`：只用于计算污染数量，报告不输出命中词。
- `must_not_complete`：无答案或冲突用例必须为 true。
- `tags`：用于分层统计，不参与业务执行。
- `smoke`：布尔值；全 manifest 必须恰有 6 个 smoke case，FAST/RESEARCH 各 3 个且至少一个 intentionally_unanswerable。

首版包含 40 个不同用例：

- FAST 20 个，RESEARCH 20 个。
- 每种 mode 内中文和英文各 10 个。
- FAST/RESEARCH × 中文/英文四个分层中各至少 5 个 answerable case，确保人工 review 可无重复抽样。
- 每个 case 重复 3 次，产生 FAST 60、RESEARCH 60，共 120 个计划 Run。
- 至少 8 个 intentionally unanswerable/conflict case。
- 至少 6 个 Apex 或对话污染回归 case。
- 至少 16 个 answerable case 提供确定性 gold assertions，并在 FAST/RESEARCH 与中英文四个分层中各不少于 4 个。
- 动态事实或无法安全编码为确定性 predicate 的 answerable case 必须标记 `manual_required`；禁止调用候选 DeepSeek 模型自动生成最终 correctness verdict。

Manifest 文件内容参与 SHA-256 指纹。Campaign 创建后不得替换 manifest；变更用例必须创建新 Campaign。

Case ID 必须匹配 `[a-z0-9][a-z0-9._-]{0,79}`，从而保证两个派生 idempotency key 始终低于 API 的 200 字符上限。

Manifest validator 必须拒绝重复 case ID、未知字段、未知 operator、不一致的 answerability/must_not_complete、无法满足分层抽样的分布，以及不能覆盖 Campaign 6 小时 active wall-clock 窗口的 assertion 有效期。resume 时若 oracle 已过期，原 Campaign 只能以 INSUFFICIENT_SAMPLE/ABORTED 封存；不得原地替换 oracle。

`minimum_required_facts` 不只是报告字段。若候选 Planner 产生的 required Fact 数量小于该值，该 Run 记为 plan completeness failure；coverage 分母使用 `max(required_fact_total, minimum_required_facts)`，防止候选通过少规划 Fact 抬高覆盖率。

## 8. Schema 与迁移

新增线性 Alembic revision `0009_shadow_campaign_release_gate`。该 revision 同时补齐 Conversation 创建幂等字段、AnswerClaim 可度量字段和三张 Campaign 表；不能把这些依赖留给运行时推断。

### 8.1 shadow_campaigns

最少字段：

- tenant ID、campaign ID、created-by user ID、name、creation idempotency key、creation request hash，以及 Full Campaign 必填的 parent smoke Campaign ID/decision hash。
- status：`CREATED/RUNNING/STOPPING/PAUSED/AWAITING_REVIEW/COMPLETED/ABORTED`。
- gate status：`PENDING/PASS/FAIL/INSUFFICIENT_SAMPLE`。
- candidate source commit SHA、candidate `source_dirty=false` 证明、API/Worker/Dispatcher/Migrate 的 Docker image ID、OCI revision label 和 Alembic head。
- harness commit SHA、harness `source_dirty=false` 证明、Runner/Collector/Report generator 文件集合的 SHA-256 和 collector schema version。
- dedicated compose project identity、sanitized compose topology/config hash、container/volume/network identity 和 loopback port map。
- manifest version、hash、case count 和 repetition count。
- candidate provider/model/output format/thinking mode、SearchPolicy、prompt/parser schema、Discovery provider registry 和非敏感 Worker 配置的安全指纹。
- CampaignProfile、GatePolicy、人工 review rubric 与费率的 version、canonical JSON snapshot 和 SHA-256；相同 version 配不同 hash 时 preflight 失败。
- Profile 锁定的 max runs、max concurrency、estimated-cost stop threshold、Provider-call admission ceiling、defense-in-depth structural ceiling 和在途 reserve。
- planned、submitted、collected、failed、skipped、degraded 数量。
- observed Provider calls、possibly-billed call charge、in-flight reserved Provider calls、observed token/cost、possibly-billed cost charge、in-flight reserved cost 和 possibly-billed 数量；数据库 CHECK 只保证所有账本值非负。Admission 使用带 `observed_calls + possibly_billed_call_charge + reserved_calls + 8 <= ceiling` 条件的行锁更新，但 Collector 仍可记录实际超限事实与 violation flag，不能因 ceiling CHECK constraint 丢失违规证据。
- stop intent：`NONE/PAUSE/ABORT/FATAL/BUDGET/CALL_CEILING`，以及 stop reason、created/started/review-deadline/completed time、active wall-clock 和乐观锁 version。
- 自动门禁状态、人工 review 状态、最终 gate report artifact reference 与 canonical decision SHA-256。

### 8.2 shadow_run_results

最少字段：

- tenant ID、result ID、campaign ID、conversation ID、search run ID。
- case ID、repetition、稳定 `schedule_ordinal`、`manual_review_selected`、locale、category 和 expected mode。
- scheduling state：`PENDING/CLAIMED/CONVERSATION_BOUND/SUBMITTED/COLLECTED/FAILED/SKIPPED`。
- claim owner、lease expiry、Conversation/Message submission attempt count、两个确定性 idempotency key 和规范化 submission request hash。
- actual mode、status、quality、stop reason、latency。
- Manifest minimum Fact、Fact total/covered/gap、plan completeness、factual/non-factual Claim、cited factual Claim、完整证据链和 traceability violation 数量。
- gold assertion total/passed/failed/not-applicable 和 oracle version。
- query pollution count。
- model calls、prompt/completion tokens、estimated cost、degraded。
- admission reservation 的 Provider calls/cost、reservation state `NONE/ACTIVE/SETTLED/RELEASED`、settled observed calls/cost、possibly-billed calls/cost charge、reserved/settled/released time 和 budget violation flag。
- provider success/failure count。
- error class、error code、failed phase 和 stable skip reason。
- source terminal time、source snapshot digest、collected time 和 collector schema version。

唯一约束为 `(campaign_id, case_id, repetition)`。Result 在任何 API 调用前以 `conversation_id=NULL, search_run_id=NULL` 预创建并通过短 lease claim。取得幂等 Conversation receipt 后必须先持久化唯一且不可更换的 `conversation_id`，状态转为 `CONVERSATION_BOUND`；取得 Message receipt 后，`search_run_id` 必须唯一且不可更换。lease 过期后可由恢复进程重新 claim，但只能使用记录中的 ID 和稳定 key 重放对应 API。

Campaign 的调用/费用总账与 Result 分账使用固定锁顺序 `Campaign -> Result` 和幂等结算协议：

1. 首次 admission 在一个事务中锁定 Campaign 与 Result，校验 Result 尚无 reservation、Campaign 仍可调度且最坏暴露不越界，然后把 8-call/cost reserve 同时记入 Result 和 Campaign；崩溃恢复复用该 `ACTIVE` reservation，绝不能再次预留。
2. Collector 封账时锁定相同行。只有 `ACTIVE` reservation 可以执行一次 settlement：先从 Campaign 减去该 Result 的 reserve，再加入从权威 ModelInvocation 审计得到的 observed calls/token/cost；对无法排除 Provider 已计费的未知调用，再加入独立的 possibly-billed call/cost charge，最后把 Result 标为 `SETTLED`。重复 Collector 调用是 no-op。
3. 只有能证明 Message 从未接受、SearchRun/ModelInvocation 均不存在的未提交 Result，才可把 reservation 标为 `RELEASED` 并原子退回 reserve。响应丢失或出站状态不明时禁止释放，必须先用稳定 idempotency key reconciliation；最终仍无法证明时按最坏 reserve 结算为 possibly-billed exposure。
4. SKIPPED、FAILED 和停止 drain 同样必须完成上述 settle/release，Campaign 才能封账。实际值即使超过 reserve 或 ceiling 也照实写入并设置 violation，随后触发 fatal gate。

`shadow_run_results`、`shadow_manual_reviews` 与 ModelInvocation/Run 审计是最终报告的事实来源；Campaign 的 planned/submitted/collected/failed/skipped/degraded 计数只是事务内维护的读优化。调用/费用 Campaign 总账是 admission 的串行化账本，但 finalization 必须从 Result 与权威 ModelInvocation 反向重算并与其核对。任何状态计数、reservation 或 settlement 不平，均以 `campaign_ledger_mismatch` 阻止 PASS，而不是自动“修正”历史账本。

Scheduling state 只描述 Harness 进度，不等同于 SearchRun outcome：

- `CLAIMED` 与 `CONVERSATION_BOUND` 的 owner 在 API 操作期间持有并续租短 lease；lease 过期可安全接管。
- `SUBMITTED` 等待 SearchRun terminal 时不长期占有独占 lease；Collector 以新的短 lease 执行幂等收集。
- SearchRun 自身为 FAILED/CANCELLED 但指标已完整收集时，Result 仍是 `COLLECTED`，并在 outcome 字段保存业务失败。
- 只有 Conversation/Message API 或 Collector 在 3 次幂等重试后仍不可恢复，或发生永久 Harness 错误时，Scheduling state 才进入终态 `FAILED`。
- Campaign 因 fatal、费用/调用 ceiling 或显式 abort 停止时，尚未产生 Message submission 的单元进入 `SKIPPED` 并保存稳定 skip reason；它不计入 terminal 样本、latency 或质量分母，但必须进入 planned/skipped 报告。已绑定空 Conversation 的单元也可 SKIPPED，不能删除会话来掩盖轨迹。
- 本规格中的 terminal Result 指 `COLLECTED` 或终态 scheduling `FAILED`，不包括 `SKIPPED`。FAILED 缺少 actual mode/latency 时按失败规则进入分母，不能被丢弃。

Harness transient retry 固定为最多 3 次，使用 0.5/1/2 秒带 jitter backoff；401/403、payload mismatch、provenance mismatch 和 invariant violation 不重试。API response 丢失属于可重放 transient，不得生成新 key。

`conversation_id` 和 `search_run_id` 使用 tenant-scoped、`DEFERRABLE INITIALLY DEFERRED` 的 composite foreign key 与 `ON DELETE NO ACTION`，避免 Campaign 证据仍在保留期内时被单独级联清除，同时允许 tenant 删除事务把 Campaign 与业务数据一起级联删除。首版不自动删除这些引用。

Full Campaign 的 parent smoke reference 同样使用 tenant-scoped deferred foreign key；创建事务锁定并验证 parent 已 COMPLETED/PASS、decision hash 匹配且仍在 24 小时有效期内。父 Campaign 在子 Campaign 保留期内不得单独删除。

### 8.3 shadow_manual_reviews

最少字段：

- tenant ID、review ID、campaign ID、result ID、rubric version。
- correctness verdict：`CORRECT/MINOR_ERROR/MAJOR_ERROR/UNREVIEWABLE`。
- citation relevance、source appropriateness、freshness 和 completeness 的枚举评分。
- stable reason codes、actor type `HUMAN/SYSTEM`、nullable reviewer principal reference、reviewed time。HUMAN 必须有 reviewer。若 answerable Run 因候选结果没有可审答案，SYSTEM 写入 `MAJOR_ERROR/expected_answer_missing`；只有 Harness/基础设施导致 projection 无法形成时才写 `UNREVIEWABLE/review_material_unavailable`。SYSTEM 记录的 reviewer 为 NULL。

唯一约束为 `(result_id, rubric_version)`。Repository 在锁定 Result 后只允许为 `manual_review_selected=true`、属于同一 Campaign/tenant 且尚未超过 review deadline 的 Result 插入；不能用额外 review 替换预选失败样本。表启用并 FORCE RLS。不得保存 reviewer 的自由文本、prompt、答案、quote、网页正文或模型输出；reviewer 身份只用于内部审计，不进入公开报告。

### 8.4 现有表的兼容性补强

- `conversations` 增加 nullable `creation_idempotency_key` 与 `creation_request_hash`，并建立 `(tenant_id, user_id, creation_idempotency_key)` 唯一约束；PostgreSQL 允许普通 Conversation 的 NULL key 重复。重复 key 的 request hash 不一致时 API 返回 409。
- `answer_claims` 增加 nullable `claim_kind` 与 `fact_requirement_id`。CHECK 允许历史 NULL，但非 NULL kind 只能是领域枚举，且 `claim_kind='FACTUAL'` 时 fact ID 必须非 NULL；新 Run 写入必须满足领域不变量，Campaign Collector 遇到 NULL kind 或 factual/null Fact 必须 fail closed。
- `fact_requirements` 增加 `(tenant_id, run_id, id)` 唯一约束；AnswerClaim 的 `(tenant_id, run_id, fact_requirement_id)` 使用 composite foreign key，禁止把其他 tenant/Run 的 Fact 绑定到 Claim。

三张 Campaign 表均启用并 FORCE RLS，使用无 `BYPASSRLS` 的应用角色访问。Result 和 review 不保存 prompt、query、quote、网页正文、模型输出、Authorization 或 API Key。

## 9. Runner 命令与恢复语义

计划提供七个子命令：

```text
run_shadow_campaign.py create
run_shadow_campaign.py list
run_shadow_campaign.py resume --campaign-id <uuid>
run_shadow_campaign.py pause --campaign-id <uuid>
run_shadow_campaign.py abort --campaign-id <uuid>
run_shadow_campaign.py review --campaign-id <uuid>
run_shadow_campaign.py report --campaign-id <uuid>
```

`create` 必须显式提供：

- `--confirm-live`
- `--campaign-key`
- `--manifest`
- `--profile`
- `--api-url`

`shadow-full-v1` 额外要求 `--parent-smoke-campaign-id`；Smoke profile 禁止提供该参数。

Sana access token 只允许从 `SANA_ACCESS_TOKEN` 环境变量或交互式 `getpass` 读取，禁止命令行参数、日志、Campaign 表或报告保存 token。DeepSeek key 只存在于 Worker secret environment；Runner 不读取或复制 Provider key。数据库 URL 中的凭据同样不进入 provenance，只记录脱敏后的 host/database/driver fingerprint。

Campaign 创建本身也必须幂等。`shadow_campaigns` 使用 `(tenant_id, created_by_user_id, creation_idempotency_key)` 唯一约束，creation request hash 覆盖 name、profile、manifest/policy/rate/rubric hash、candidate/harness identity 和 parent smoke decision hash。Campaign key 必须是 1–100 个 `[A-Za-z0-9._-]` 字符且不视为 secret。重放相同 owner/key/hash 返回已有 Campaign：非终态时等价于 resume，终态时只返回摘要且不执行新工作；相同 owner/key 配不同 hash 立即失败。非交互运行必须显式提供 `--campaign-key`；交互模式可以生成 UUID，但必须在任何数据库写入或网络请求前打印给用户。`list` 只列出当前 Principal 自己创建的最近 Campaign 的 ID、key、status、profile、created_at 和非敏感 stop reason，用于恢复丢失的终端输出；同 tenant 的其他用户不能读取或操作该 Campaign。

首版只提供两个版本化 CampaignProfile，CLI 不接受原始阈值覆盖：

- `docker-smoke-v1`：只运行 manifest 中 6 个 `smoke=true` case，每个 1 次；max runs 6、max concurrency 2、Provider-call admission ceiling 32、structural ceiling 48、estimated-cost stop threshold 0.01 美元，使用 `shadow-smoke-gate-v1`。
- `shadow-full-v1`：40 case × 3；max runs 120、max concurrency 2、Provider-call admission ceiling 480、structural ceiling 960、estimated-cost stop threshold 0.10 美元，使用 `shadow-gate-v2`。

Profile 的 canonical snapshot/hash 写入 Campaign。改变费用或调用阈值必须发布新 profile version 并取得相应成本授权；不能通过 CLI 临时修改。CLI 和数据库均校验 `admission ceiling <= structural ceiling = max_runs × RESEARCH.max_llm_calls`。

首版 `--api-url` 只接受 loopback/local Docker endpoint；远程 Campaign 必须等到服务端 build provenance 和 Worker heartbeat 契约另行设计后开放。Campaign active wall-clock 上限为 6 小时，PAUSED 时间不计入 active time，但每次 resume 都必须重新验证 oracle 有效期与 provenance。

Full profile 的 0.10 美元是平台基于版本化费率、已知 token 和 possibly-billed reserve 执行的**费用调度停止阈值**，不是对外部 Provider 账单的绝对保证。Provider 不提供平台可依赖的逐 Campaign 预授权；延迟 usage、费率差异或 Provider 账单语义可能令实际账单略高。

Provider-call 数量采用更强语义：Runner 在每次 admission 时持久化最坏 8 次 in-flight reserve，而 Worker 的 ModelInvocation audit 又在网络出站前执行逐 Run 4/8 原子预算，因此 Full 的 `observed + possibly_billed_charge + reserved <= 480`、Smoke 的 `<= 32` 是平台可执行的 admission ceiling。960/48 只是由 max runs × 8 得出的 defense-in-depth structural ceiling，用于在 Campaign admission 逻辑被绕过时限制最坏损失；达到 structural ceiling 本身已经是严重缺陷，绝不能描述成正常预算。报告必须区分 observed calls、possibly-billed exposure、in-flight reserve、call admission ceiling、structural ceiling、cost stop threshold 和外部账单 caveat。

Runner 在每次 claim/admission 时按固定顺序锁定 Campaign/Result 行，原子检查并更新 remaining Run、Result reservation、Campaign observed/possibly-billed/in-flight 调用与费用账本。每个在途 Run 按 RESEARCH 最坏值预留 8 次调用及 GatePolicy 的 cost reserve；Result 收集后按第 8.2 节协议用权威值幂等结算。`observed + possibly_billed_charge + reserved` 达到 call admission ceiling，或 observed/possibly-billed/reserved estimated cost 达到 stop threshold 后，不再提交新 Run但继续收集已提交 Run。任何 admission 若会令 call ceiling 超限，必须在 API submission 前拒绝。该设计保证最多两个在途 Run 的暴露被显式计入，不宣称 Worker 出站后还能被 Runner 精确中止。

6-run 小 Campaign 用于校准每 mode 的 Run 成本分布。预测费用采用 `60 × max(3 个 FAST 实测成本) + 60 × max(3 个 RESEARCH 实测成本)`，再增加 30% headroom 和 POSSIBLY_BILLED reserve。只有结果不超过 0.10 美元时才自动进入正式 Campaign；否则停止并请求显式成本授权，不能静默提高阈值。

`shadow-full-v1` create 还必须引用一个 24 小时内完成且 PASS 的 smoke Campaign。两者的 candidate/harness commit、Runner/Collector digest、compose environment/config、image、manifest、模型/Worker 配置、collector/safety-invariant version、费率和迁移 head 必须完全相同；SmokeGatePolicy 与 FullGatePolicy 按 profile 分别锁定，不要求名称相同。其他任何候选、harness 或 environment 身份差异都令 smoke 失效并要求重跑。

`pause` 先把 stop intent 写为 PAUSE、Campaign 设置为 STOPPING；Runner 不再 claim 新单元，等待最多两个在途 Run terminal 并完成收集后进入 PAUSED，未提交单元保持 PENDING。进程收到正常中断信号时执行相同可恢复流程。`abort` 写入 ABORT intent，同样先 STOPPING 并收集在途 Run，然后把未提交单元原子标记 SKIPPED、Campaign 置为 ABORTED；它不删除任何数据但不可恢复调度。

进程可能在 STOPPING drain 中被强制杀死，因此 stop intent 必须先于 drain 持久化。`resume` 可接管 PAUSED、RUNNING、STOPPING 或 lease 过期的 Campaign：PAUSE intent 完成 reconciliation 后可恢复 RUNNING；ABORT/FATAL/BUDGET/CALL_CEILING intent 只能继续封账到对应终态，禁止重新 claim 新单元。COMPLETED/ABORTED 不可恢复调度。

Full Campaign 创建 Result 时，使用 `SHA-256(canonical_json([campaign_id, case_id, repetition]))` 稳定排序，从四个 expected-mode/locale 分层各预选 5 个不同的 answerable case，每个 case 只选一个 repetition，共 20 个 review unit；使用结构化编码而不是字符串拼接，避免边界歧义。选中标志随 Result 一次性写入并锁定。没有 fatal safety violation 且自动样本充分时，只有 20 个预选 Run 全部具备可审 projection 才进入 `AWAITING_REVIEW`，review deadline 为 48 小时；候选没有产出应有答案时由 SYSTEM 记录 MAJOR_ERROR 并直接 FAIL，Harness/基础设施使材料不可读时记录 UNREVIEWABLE 并封存为 INSUFFICIENT_SAMPLE，二者都不能换样本。`review` 临时显示 prompt、答案和精确 Citation projection，写库内容仅限结构化评分。全部必需 review 完成后才能产生最终 PASS/FAIL；人工判定 UNREVIEWABLE 或超过 deadline 仍未完成也封存为 INSUFFICIENT_SAMPLE，不能追加迟到 review 改写历史结果。

`report` 可随时生成 immutable snapshot：fatal safety Campaign 可直接生成最终 FAIL；没有 fatal 但自动样本不足时可生成最终 INSUFFICIENT_SAMPLE；自动样本充分但 review 未完成时只能生成 `PENDING_REVIEW` 非最终报告，不能伪装成 gate result。

### 9.1 候选身份 preflight

首版 live Campaign 只支持专用的本机 Docker Compose project，例如 `sana-shadow-eval`。它使用独立 PostgreSQL、Redis、artifact volume、API loopback port 和 eval tenant，不启动 Streamlit，不挂载开发数据库或用户数据；只复用同一个 immutable candidate image 与通过 secret environment 注入的 DeepSeek credential。不得在日常开发 compose project 上运行可用于 gate 的 Campaign。

Runner 在任何可能创建 Conversation、Message 或 Provider 调用的 API 请求前必须：

1. 验证 compose project name、container/volume/network labels 都属于专用 evaluation environment，所有暴露端口只绑定 loopback；基于 service/image ID、command、queue、concurrency、port、volume/network label allowlist 生成 sanitized topology/config hash，secret 字段只保留 presence boolean，禁止保存或哈希原始 `docker compose config` 输出；确认 Smoke 与 Full 使用同一未被重建或改配的 environment identity。
2. 确认用于 image build 的 candidate worktree 对 tracked runtime 文件为 clean，untracked runtime 文件也为空；确认执行 Runner/Collector/Report generator 的 harness worktree 同样 clean，并对实际加载的 harness 文件集合计算 SHA-256。首版要求二者来自同一个 commit，但仍分开记录身份。
3. 读取 API、Worker、Dispatcher 和最近一次成功 Migrate 容器的 immutable image ID，验证四者相同，并验证 OCI `org.opencontainers.image.revision` 等于记录的 candidate commit SHA。
4. 通过只读数据库连接确认 Alembic 单一 head 与预期 revision 一致。
5. 对 SearchPolicy、模型角色配置、thinking/output format、prompt/parser schema、Discovery provider registry 和非敏感 Worker 配置生成 allowlist fingerprint。Secret 只校验“已配置”布尔值，禁止把值、长度或可离线比对的 secret hash 纳入 fingerprint。
6. 确认 PostgreSQL 没有非 Campaign 的 active SearchRun/unpublished step outbox，Celery active/reserved/scheduled 集合为空，目标 Redis queues 深度为零；记录 Worker concurrency、机器/容器资源上限和 Campaign 开始时队列深度。任一检查不可用时禁止把结果用于 latency gate。

任一步无法证明时，在产生 Provider 调用前失败。不得从当前含未提交 runtime 改动的工作区直接 build 正式 Campaign 镜像或运行评测 Harness；应从独立 clean worktree 构建镜像并执行 Runner，再以 image ID 与 harness digest 双重封账。

### 9.2 Campaign 状态机

- `CREATED -> RUNNING`：preflight 和全部调度单元预创建成功。
- `RUNNING -> STOPPING -> PAUSED`：用户 pause 或正常进程中断；收集在途 Run 后暂停。
- `PAUSED -> RUNNING`：provenance、manifest、policy 和 lease 再校验成功。
- `RUNNING -> STOPPING -> COMPLETED`：fatal safety 或费用/调用 ceiling 触发；收集在途 Run、把未提交单元标记 SKIPPED 后封账。
- `RUNNING -> COMPLETED`：全部自动结果已结束，但正式样本不足或预选 review unit 结构上不可审，以 INSUFFICIENT_SAMPLE 封账。
- `RUNNING -> AWAITING_REVIEW -> COMPLETED`：自动样本充分且无 fatal；必需 review 完成后封存质量 gate，或 48 小时 deadline 到达后以 INSUFFICIENT_SAMPLE 封存。
- `CREATED/RUNNING/STOPPING -> ABORTED`：显式 abort、永久配置、provenance 损坏或不可恢复的 Campaign 自身故障；收集在途 Run、标记剩余 SKIPPED，ABORTED 不自动等于候选 FAIL。

`COMPLETED` 与 `ABORTED` 为终态，不能 resume。Gate status 与执行 status 分离：执行完成不代表 PASS，执行中止也不能掩盖已经发生的 fatal FAIL。所有状态转换使用乐观锁；从 STOPPING 开始禁止新 claim。

## 10. 停止条件与错误分类

### 10.1 立即停止

- 任一最终 factual Claim 没有完整可验证 Citation 链；无 factual Claim 时该指标为 N/A。
- Required Fact 无证据却产生 COMPLETE。
- Planner 产生的 required Fact 少于 manifest 的 `minimum_required_facts`。
- Result submission hash 与权威 Message 内容不一致，或同 key 出现 payload mismatch。
- Query 对话污染数量大于 0。
- FAST/RESEARCH 模型调用超过 4/8。
- 跨租户访问、RLS 失败或敏感内容进入报告。
- 401、403、无效模型或其他永久模型配置错误。
- Runner 检测到 terminal Attempt 或超过 reconciliation grace 后仍未封口的孤儿 `STARTED` 模型调用；正常在途调用不属于违规。

### 10.2 受控停止

- 连续 3 个基础设施或 Provider 失败。
- 达到 Run 上限、Provider-call admission ceiling 或 estimated-cost 调度停止阈值。
- 显式 abort 请求。

正常 Ctrl-C/SIGTERM 与 `pause` 是可恢复的暂停语义，不属于终态停止条件，也不把 PENDING 单元标为 SKIPPED。

“连续 3 个”按 `schedule_ordinal` 的连续 terminal prefix 计算，不按并发完成顺序计算。若 ordinal 2 先于 ordinal 1 完成，Runner 必须等待 ordinal 1 terminal 后再推进 streak；这样并发时序不会改变停止判定。任一非基础设施/Provider terminal 结果会重置该 streak。

### 10.3 错误分类

每个 Result 保存一个主分类，同时保存不互斥的稳定 signal flags，避免成功降级 Run 丢失 Provider/基础设施信号。主分类按以下优先级决定：

1. `CANDIDATE_DEFECT`：安全不变量、预算、错误 quality、plan completeness、gold assertion 或确定性业务规则失败。
2. `PERMANENT_CONFIGURATION`：认证、模型名、密钥或不支持的能力配置错误。
3. `INFRASTRUCTURE`：API、PostgreSQL、Redis、Celery、artifact、provenance 或 lease 恢复失败。
4. `PROVIDER_TRANSIENT`：429、5xx、网络和 Provider wall-clock timeout。
5. `CONTENT_GAP`：系统正常结束但未获得足够证据；不是基础设施失败。

若 succeeded Run 经历 Provider timeout 后通过 fallback 完成，它仍可标记 `PROVIDER_TRANSIENT` 主分类并设置 `degraded=true`。Fatal safety signal 永远覆盖其他分类。一个 Result 的多种 signal 都进入各自统计，但只能按上述优先级进入一个主失败分桶，避免分母重复。

错误消息、异常 repr 和 Provider body 不进入 Result；只保存稳定分类和脱敏错误码。

## 11. 发布门禁

正式 Campaign 策略版本为 `shadow-gate-v2`，使用 nearest-rank percentile。比率同时报告 Wilson 95% confidence interval，但首轮阈值仍基于固定观测值；区间用于解释小样本不确定性，不能用于放宽门禁。

Gate status 使用以下不可变优先级：

1. 任一硬安全违规，无论已完成多少样本，最终状态都是 `FAIL`。
2. 没有硬安全违规但 Campaign 必须封存、而样本或人工 review 不充分，最终状态是 `INSUFFICIENT_SAMPLE`；仍在合法 review 窗口内时 gate status 保持 `PENDING`，报告 decision state 为 `PENDING_REVIEW`。
3. 样本充分时再计算质量、正确性、可靠性和时延；任一失败为 `FAIL`，全部通过才是 `PASS`。

`ABORTED` 是 Campaign 执行状态，不替代 gate status；永久配置或基础设施令 Campaign 无法形成有效候选判定时，执行状态可以为 ABORTED，gate status 按以上优先级保持 FAIL 或 INSUFFICIENT_SAMPLE。

### 11.1 Smoke operational gate

`shadow-smoke-gate-v1` 不冒充正式发布门禁，只决定同一候选能否进入 120-run Campaign。它要求：

- 6 个 smoke Result 全部 terminal，expected FAST/RESEARCH 各 3 个且至少一个 intentionally_unanswerable。
- 所有第 11.3 节硬安全门为零违规。
- permanent configuration、infrastructure failure、重复 SearchRun、孤儿 STARTED 和 report privacy violation 均为零。
- 每个 Run 未超过自身 mode hard deadline，smoke Campaign 没有触发费用 stop threshold 或 Provider-call admission ceiling。
- 成本预测按第 9 节公式生成且未超过 full profile 的 0.10 美元授权阈值。

满足才输出 smoke PASS；fatal 仍优先 FAIL，未完成 6 个且无 fatal 时为 INSUFFICIENT_SAMPLE。Smoke PASS 只在 24 小时内对完全相同的 candidate identity 有效。

### 11.2 正式样本充分性

- terminal Result 总数不少于 100。
- 按 actual mode 统计的 FAST 不少于 50，RESEARCH 不少于 50。
- 不同 case ID 不少于 40。
- 至少 8 个不同的 intentionally unanswerable/conflict case ID 有 terminal Result，且该类 terminal Run 总数不少于 20。
- 20 个确定性抽样的人工 review unit 全部完成且 verdict 不为 UNREVIEWABLE。
- 至少 16 个不同 case 的有效 gold assertions 被执行；过期 oracle 不计有效样本并在 preflight 阻止运行。

只有在不存在硬安全违规且 Campaign 需要最终封存时，上述任一不满足才输出 `INSUFFICIENT_SAMPLE`；正常等待人工 review 时保持 PENDING。

### 11.3 硬安全门

- uncited 或 Citation 链不完整的 factual Claim = 0；无 factual Claim 的 Run 记为 N/A 而不是自动通过样本。
- collected source snapshot digest mismatch = 0。
- 无证据 COMPLETE 误报率 = 0。
- plan completeness failure = 0。
- Query pollution count = 0。
- 模型调用预算越界 = 0。
- Campaign `observed + possibly_billed_charge + reserved` Provider-call exposure 超过 profile admission ceiling = 0。
- Campaign/Result/ModelInvocation 状态计数与调用/费用账本 mismatch = 0。
- 永久悬挂 STARTED 模型调用 = 0。
- 跨租户/RLS 违规 = 0。
- idempotent submission payload mismatch = 0。
- critical gold assertion failure 或人工 `MAJOR_ERROR` = 0。

任一不满足即 `FAIL`。

### 11.4 质量与可靠性门

- FAST p95 ≤ 15 秒。
- RESEARCH p95 ≤ 120 秒。
- expected mode 与 actual mode 一致率 ≥ 95%。
- answerable required-Fact coverage 的 case-macro average ≥ 80%，且 FAST/RESEARCH × 中文/英文每个分层 ≥ 70%。
- 非关键 gold assertion pass rate ≥ 95%。
- 人工 review 中 `CORRECT` 比例 ≥ 90%，Citation relevance、source appropriateness 和 freshness 合格率均 ≥ 95%，completeness 合格率 ≥ 90%。
- intentionally unanswerable case 的明确缺口率 = 100%。
- degraded Result 比例 ≤ 10%。
- infrastructure failure 比例 ≤ 1%。

每个 Run 的 coverage 先使用 `covered / max(required_fact_total, minimum_required_facts)` 计算；三个 repetition 先在 case 内平均，再对不同 case 做宏平均，避免重复和 Fact 数量较多的 case 主导质量结论。人工 review 每个 case 只选一个 repetition。时延按实际 mode 使用持久化的 `SearchRun.created_at -> completed_at`；客户端 API round-trip 另行报告，不混入 gate latency。

Gold correctness 同样先对一个 Run 内的 assertions 求比例，再在 repetition 内求 case 平均，最后对不同 case 做宏平均；critical assertion 仍采用任一失败即 FAIL，不参与平均稀释。

失败 Run 仍进入可靠性分母。对 latency，已越过 Run hard deadline 或缺失合法 terminal timestamp 的 Run 以该 mode hard deadline 加 1 毫秒计入，防止快速失败或缺失时间戳改善 p95；其他失败使用真实 terminal latency。没有 SearchRun/actual mode 的 scheduling FAILED Result 不计入 actual-mode 样本充分性，但在 latency 中按 expected mode 分组并使用对应 hard deadline + 1 毫秒。Provider transient 单独报告，同时计入 degraded 或 failure 指标；CONTENT_GAP 不计为基础设施失败，但会影响 coverage。

GatePolicy 是版本化数据，不允许 runner 参数覆盖阈值，并在 Campaign 创建时锁定。策略变更必须创建新的 Campaign；禁止使用新策略覆盖历史 Campaign 的 gate status 或 report artifact。

## 12. 费用与资源控制

- Runner 最大并发固定为 2；CLI 不允许提高首轮上限。
- 每个 SearchRun 继续服从 FAST 4、RESEARCH 8 次模型调用硬预算。
- Full Campaign 的 Provider-call admission ceiling 为 480；Smoke 为 32。`observed + possibly_billed_charge + reserved` 在 Campaign/Result 固定行锁顺序内检查，任何超限 submission 必须被拒绝。
- Full 的 defense-in-depth structural ceiling 为 120 × 8 = 960；Smoke 为 6 × 8 = 48。它来自 max runs 与现有逐 Run ModelInvocation 原子预算，不是正常运行目标。若 observed call 超过 admission ceiling，Campaign 立即 FAIL，即使尚未接近 structural ceiling。
- 每个 claim 按最坏 8 次调用做 in-flight call reserve，最多同时保留两个。费用 reserve 使用 GatePolicy 的保守估算，但费用仍只是调度阈值，不能宣称外部账单 exactly bounded。
- 每完成 10 个 Result 重新汇总 token、费用、POSSIBLY_BILLED 和失败趋势。
- 每次 admission 都检查 observed + possibly-billed charge + reserved；每完成 10 个 Result 做额外趋势与账本 reconciliation。超过估算费用阈值后禁止新提交，已出站调用照常封账。
- 未知 token 不按零成本处理；使用 GatePolicy 的 possibly-billed reserve。
- 报告同时给出 token 事实、估算费用、费率版本和 possibly-billed 数量。

## 13. 隐私、隔离与保留

### 13.1 隐私

- Campaign Result/review 表、JSON 报告和 Markdown 报告不保存 prompt。
- 不保存 query、quote、网页正文、模型响应或 reasoning。
- 不保存被测终端用户身份；只保存 tenant、campaign、case、conversation 和 run reference。人工 review 表可保存受 RLS 保护的 reviewer principal reference 用于内部审计，但报告不得输出。
- TelemetryRedactor 在写 Result 和报告前执行 allowlist 过滤。

### 13.2 保留

- Campaign、`shadow_run_results`、结构化人工 review、对应 Conversation/Message/SearchRun/Step/Attempt 和 ModelInvocation 统一保留 365 天。
- Campaign report 与策略判定至少保留 365 天。
- Campaign 不复制正文到 Result 或报告；原 prompt 和回答只存在于正常 Message 存储中。
- `shadow_run_results` 对 Conversation/SearchRun 使用 deferred NO ACTION reference，因此通用 cleanup 必须跳过 retention 未到期的 Campaign 记录；tenant 全量删除仍可在同一事务中级联完成。

现有 ModelInvocation 通过 `ON DELETE CASCADE` 依赖 SearchRun，因此不能宣称它拥有独立于父 Run 的 365 天保留期。首版本只实现统一的 `retention_until` metadata、安全查询边界和 cleanup exclusion，不启用自动删除。后续删除作业必须按 review -> result -> conversation/run 的依赖顺序经过独立恢复测试后才能启用。

## 14. 报告设计

JSON 与 Markdown 报告包含：

- Campaign ID、candidate/harness commit、两类 clean-worktree 证明、Runner/Collector digest、compose environment identity/sanitized config hash、API/Worker/Dispatcher/Migrate image ID、OCI revision、Alembic head、manifest hash 和运行配置指纹。
- GatePolicy、rubric、费率的 version/hash/snapshot 和 collector schema version。
- 计划、提交、terminal、skipped、失败和降级样本数，并按 stable skip reason 分类。
- FAST/RESEARCH 的 p50、p95、max 和 deadline breach。
- plan completeness、case-macro Fact coverage、明确缺口、factual Claim traceability violation 和 pollution。
- gold assertion 与结构化人工 review 的分层结果，只包含计数、比率、严重度和 reason code。
- observed/possibly-billed/reserved 模型调用暴露、token、estimated cost、POSSIBLY_BILLED，以及 Campaign/Result/ModelInvocation reconciliation 结果。
- Provider、基础设施、内容缺口和候选缺陷分类统计。
- 每条 gate 的阈值、观测值、通过状态和 reason code。
- 最终 `PASS/FAIL/INSUFFICIENT_SAMPLE`。
- Worker concurrency、容器资源、初始队列深度和“controlled baseline, not production load proof”声明。

报告可以列出 case ID 和 Run ID 用于诊断，但不得回显 prompt、答案或抓取内容。报告通过 `CampaignReportStore` 使用 content-addressed storage。

Gate decision hash 只基于 canonical JSON decision payload：UTF-8、key 稳定排序、无额外空白、禁止 NaN/Infinity、时间统一 UTC RFC 3339、金额使用 decimal string、比率使用整数 basis points。`generated_at`、artifact URI 和 Markdown 排版不进入 decision hash。Markdown 必须由该 canonical payload 单向生成；重新生成报告时 JSON decision hash 必须相同，Markdown 另存自己的内容哈希。

最终封账采用可恢复的三步协议：

1. 对稳定排序的 decision-relevant Campaign metadata、Result/Review、profile/policy/rubric/rate snapshot 和 provenance 计算 `decision_input_hash`，再生成 canonical JSON 与 Markdown。输入 allowlist 明确排除 Campaign 乐观锁 version、gate/report fields、final completed_at、artifact URI 和生成时间，避免封账动作反过来改变自身输入。Review 只纳入 selected result、rubric、verdict、评分与 reason codes；reviewer identity 和 reviewed_at 留在审计表但不影响 decision hash。
2. 先把两个 payload 写入 content-addressed CampaignReportStore；进程在此后崩溃只会留下可校验的未绑定 artifact，不会改变 gate。
3. 锁定 Campaign 行，重新计算并比较 `decision_input_hash`、status、version 与 review deadline；完全一致时在一个事务中绑定 JSON/Markdown refs、decision hash、gate status 和 terminal Campaign status。

并发 final report 调用必须收敛到相同 refs/hash；输入发生变化时拒绝绑定并重新生成。PENDING_REVIEW 或运行中的 snapshot 只输出到调用方，不写入 Campaign 的 final report fields。

## 15. 测试策略

### 15.1 单元测试

- nearest-rank p50/p95 边界。
- Fatal safety FAIL 优先于样本不足；无 fatal 时样本不足判定。
- 每条安全、质量和可靠性门禁。
- case-macro、分层 coverage、minimum Fact 分母和三次重复相关性。
- factual Claim 全分母、N/A 与完整 Citation 链 fail-closed。
- gold assertion predicate、有效期和人工 review 抽样稳定性。
- review 48 小时 deadline、迟到写入拒绝和 PENDING/INSUFFICIENT 转换。
- 错误分类优先级。
- observed/possibly-billed/reserved 费用与调用、Run、Smoke 32/48 与 Full 480/960 的 admission/structural 边界。
- smoke/full profile 锁定、candidate identity 匹配和 24 小时有效期。
- 调度单元稳定排序和唯一键。
- Result/review/report allowlist 脱敏。

### 15.2 数据库测试

- `0009` upgrade/downgrade 和单一 Alembic head。
- 三张 Campaign 表 FORCE RLS 和跨租户拒绝。
- Campaign/Result/review 唯一约束和 Conversation 创建幂等唯一约束。
- Campaign create 相同 key/hash 只创建一套 Campaign/Result；相同 key/不同 hash 拒绝。
- 同 tenant 的非 owner Principal 无法 list/resume/pause/abort/review/report Campaign，跨 tenant 同时被 RLS 拒绝。
- AnswerClaim kind/Fact binding 以及跨 tenant/run Fact 绑定拒绝。
- 并发 claim 同一调度单元时只有一个 API submission owner。
- 两个真正并发的 Conversation create 返回同一 ID；两个并发 Message submit 只创建一套 Message/ResponseRun/SearchRun/Step/Outbox。
- 在 Conversation response、Conversation binding、Message receipt、Run binding 四个崩溃窗口恢复时均不产生第二个 SearchRun。
- Conversation/Message 相同 key 配不同 request hash 时返回 409，且零新增 SearchRun。
- Campaign retention reference 阻止父 Conversation/SearchRun 被提前级联删除。
- Result 创建时已锁定 20 个 manual review unit；失败或 UNREVIEWABLE 样本不能被额外 review 替换。

### 15.3 Runner 集成测试

- 未提供 `--confirm-live` 时零网络。
- Token 只能通过 environment/getpass 注入，CLI、异常、日志、provenance 和 report 均不泄漏 access/provider/database secret。
- 超出 max runs/concurrency/call/cost 时在提交前拒绝。
- CLI 不能覆盖 profile 阈值；不匹配或过期的 smoke PASS 不能启动 full Campaign。
- 两个并发 admission 对 Campaign 行加锁且 reserve 不超卖。
- admission commit 后崩溃不会二次 reserve；Collector 重放不会二次 settlement；只有可证明零出站的 Result 才能 release，状态不明按 possibly-billed reserve 封账。
- SKIPPED/FAILED/drain 后 reservation 全部 SETTLED 或 RELEASED；Campaign 总账与 Result/ModelInvocation 重算不一致时无法 PASS。
- dirty worktree、不同 image ID、错误 OCI revision、迁移 head 或配置指纹在零 Provider 调用时拒绝。
- 非专用 compose project、非 loopback port、共享开发 volume/queue 或 Smoke/Full environment identity 变化在零 Provider 调用时拒绝。
- 进程在提交前、提交后、收集前和报告前退出时均可恢复。
- 进程在 Campaign commit 后、ID 输出前退出时，重放 create 或 list+resume 可恢复且零重复 Run。
- 进程在 STOPPING drain 中退出时，PAUSE intent 可恢复调度，ABORT/FATAL/BUDGET/CALL_CEILING intent 只能继续封账且剩余单元为 SKIPPED。
- 连续 3 次 Provider/基础设施失败停止。
- 永久配置错误和安全违规立即停止。
- 报告生成失败不丢失已收集 Result。
- canonical JSON 多次生成 hash 稳定，Markdown 只由 canonical payload 派生。
- finalization 在 artifact 写入后崩溃可恢复；并发 final report 收敛；stale decision_input_hash 不能绑定。
- review 命令不把 prompt、答案或 Citation 内容写入 Campaign 表和报告。
- ReviewProjectionReader 返回精确 Claim→Citation→VerifiedEvidence 关系并拒绝跨 tenant/run 记录。
- Collector 在单一 REPEATABLE READ 快照中读取源表；未封口依赖拒绝收集，finalization 检测源数据漂移且不能基于旧 Result PASS。

### 15.4 Docker 验证

先执行 6 次小 Campaign：

- FAST 3、RESEARCH 3。
- 至少一个 no-answer case。
- 至少覆盖一次 Conversation/Message receipt 丢失恢复注入。
- 验证 migration、RLS、provenance、checkpoint、report 和无 STARTED 调用。
- 用小 Campaign 的实际 token/cost 预测 120-run 成本，并应用 30% headroom；预测高于 0.10 美元时禁止自动启动正式 Campaign。

小 Campaign 未通过时禁止执行 120 次 Campaign。

### 15.5 真实 Campaign

小 Campaign 通过且成本预测在授权范围内后执行 120 个计划 Run。永久错误、安全违规、预算阈值或连续失败触发停止。自动收集后完成 20 个结构化人工 review；fatal safety 可在任意样本量直接产生 FAIL，没有 fatal 时才由样本充分性决定是否为 INSUFFICIENT_SAMPLE。

## 16. 实时 Shadow 的预留边界

后续实时 Shadow 不使用 API 进程内 `asyncio.create_task` 作为可靠交付。目标数据流为：

```text
Primary Run committed
  -> PostgreSQL Outbox with tenant/message reference
  -> dedicated shadow exchange/queue
  -> isolated Shadow Worker concurrency pool
  -> candidate SearchRun
  -> Shadow Result Store
```

Celery 消息不携带 prompt。Shadow Worker 在 tenant-scoped UoW 中按 reference 读取输入。Shadow 不得更新 primary Run、主回答或用户消息。采样率、每日估算费用、并发和 kill switch 将由下一阶段规格定义。

## 17. 验收定义

本阶段实现完成需同时满足：

1. `0009` migration、AnswerClaim/Conversation 兼容补强、三张 Campaign 表、RLS 和 schema tests 通过。
2. Campaign 可创建、停止、恢复、独立绑定 Conversation，并在所有 receipt 崩溃窗口保持 SearchRun 唯一。
3. Candidate/Harness/Environment provenance 能分别证明 clean source、Runner/Collector digest、专用 compose 隔离、相同 immutable image、OCI revision、迁移 head 和安全配置指纹。
4. 6 次 Docker 小 Campaign 通过且无敏感内容、重复 Run 或悬挂调用；120-run 成本预测未超过授权阈值。
5. 正式 Campaign 要么完成合格自动样本与 20 个预选结构化人工 review，要么以可解释的 stop intent、完整账本和 SKIPPED 原因受控封账；后者证明系统正确停止，但候选不得 PASS。
6. Gate report 可重复生成，canonical decision hash 稳定，Campaign report 不滥用 run-scoped artifact URI。
7. Fatal safety 在任何样本量下都产生 FAIL；无 fatal 时只有样本与 review 充分才能产生质量 PASS/FAIL。
8. GatePolicy、rubric 和阈值不能被 CLI 临时覆盖。
9. 全量默认 pytest 不访问真实 DeepSeek。
10. 当前工作区中不属于本阶段的旧搜索/UI 改动保持未提交，正式 Campaign 镜像从独立 clean worktree 构建。

## 18. 后续阶段

若 Campaign 为 PASS，下一阶段依次为：

1. 设计并实现 1% durable real-time Shadow。
2. 在真实分布上同时满足至少 7 天和 1,000 个合格样本；时间与样本门槛不能二选一。
3. 接入第二个真实搜索 Provider 并验证故障切换。
4. 执行多租户并发、取消、Worker crash 和 Redis loss 压测。
5. 单独推进 S3/MinIO、OIDC 和记忆迁移。

Campaign PASS 只允许进入实时 Shadow，不等同于生产切流批准。
