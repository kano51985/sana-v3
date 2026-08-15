# Sana Shadow Campaign 与发布门禁设计

日期：2026-08-15

状态：书面规格已自审，等待用户最终确认

前置版本：`be42f10 feat: harden DeepSeek search quality pipeline`

## 1. 目标

本阶段建立一个可恢复、可审计、受费用约束的预生产 Shadow Campaign 系统，用不少于 100 次真实 Run 判断当前 DeepSeek 搜索质量管线是否具备进入低比例实时 Shadow 的资格。

系统必须回答四个问题：

1. FAST 与 RESEARCH 是否在各自时延目标内稳定完成。
2. Fact、Evidence、Claim 与 Citation 的安全不变量是否始终成立。
3. 模型、检索 Provider、基础设施和内容缺口分别贡献了多少失败或降级。
4. 当前候选版本是否通过版本化、不可手工绕过的发布门禁。

本阶段继续使用同一个 DeepSeek API 和 `deepseek-v4-flash`，不进行生产模型对比。

## 2. 范围边界

### 2.1 本阶段包含

- 40 个不同的版本化 Shadow 用例，每个重复 3 次，共 120 个计划 Run。
- FAST 与 RESEARCH 各 60 个计划样本。
- 可创建、暂停、恢复和汇总的独立 Campaign runner。
- Tenant-scoped Campaign 与 Result 持久化。
- 版本化发布门禁策略与确定性判定器。
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

1. **测量不能改变被测系统。** Runner 通过正式 API 提交请求，通过 PostgreSQL 读取事实，不在进程内复制搜索业务规则。
2. **安全指标优先于平均质量。** Citation 越权、无证据 COMPLETE、跨租户访问或调用预算越界会立即终止 Campaign。
3. **报告不复制敏感输入。** Prompt 只存在于版本化 manifest 和正常消息存储中，Campaign 表与报告只保存 case ID 和结构化指标。
4. **恢复不能重复收费。** `(campaign_id, case_id, repetition)` 是调度幂等键；已有 SearchRun 的条目只能收集或重试收集，不能重新提交。
5. **外部计费不宣称 exactly-once。** Provider 请求完成但本地封账前崩溃仍可能产生重复费用，必须记录为 `POSSIBLY_BILLED`。
6. **门禁不可临时放行。** 不提供 `--force-pass`。阈值变更必须产生新的策略版本并重新计算报告。

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

每个 API submission 使用确定性 idempotency key：`shadow:{campaign_id}:{case_id}:{repetition}`。如果进程在 API 已接受请求、但 `search_run_id` 尚未写入 Result 前崩溃，恢复流程使用同一 key 重放 submission，并取得原 receipt；禁止生成第二个 SearchRun。

### 5.2 ShadowOutcomeCollector

Collector 只从 PostgreSQL 权威表读取：

- SearchRun mode、status、quality、stop reason、usage 和时间。
- required Fact 总数、覆盖数与缺口数。
- factual Claim 和 Citation 数量及可回溯率。
- QuerySpec 的污染命中数量，但不返回 query 文本。
- ModelInvocation 的角色、状态、调用数、token、错误分类和计费处置。
- ProviderAttempt 的成功、失败和错误类型计数。
- Step/Attempt 的失败阶段和错误码。

Collector 不重新调用 Planner、CoverageEvaluator、CitationValidator 或模型，也不修改 SearchRun。

### 5.3 ShadowResultStore

SQL adapter 负责 tenant-scoped Campaign、Result、检查点和最终 gate report。所有写入使用短事务、稳定幂等键和 PostgreSQL RLS。

### 5.4 ReleaseGateEvaluator

纯确定性领域组件接收：

- Campaign metadata。
- 完整 Result 集合。
- 版本化 GatePolicy。

输出 `PASS`、`FAIL` 或 `INSUFFICIENT_SAMPLE`，并为每条规则给出观测值、阈值、样本数和 reason code。Evaluator 不访问网络或数据库，便于使用固定数据做完全可重复的单元测试。

## 6. 数据流

```text
Versioned Manifest + Gate Policy + Cost Policy
                 |
                 v
         ShadowCampaignRunner
                 |
                 v
          Existing Sana API
                 |
                 v
    Durable SearchRun / Step / Evidence Pipeline
                 |
                 v
       ShadowOutcomeCollector
                 |
                 v
 shadow_campaigns + shadow_run_results
                 |
                 v
       ReleaseGateEvaluator
                 |
                 v
       JSON + Markdown Gate Report
```

Runner 崩溃后根据 Campaign ID 恢复。对于已有 `search_run_id` 的调度单元，只等待或收集对应 Run；尚未绑定 Run 的单元使用其确定性 idempotency key 请求 API，API 返回已有或新建的唯一 receipt。

## 7. Manifest 设计

Manifest 使用 JSONL，每条用例至少包含：

- `id`：稳定、不可复用的 case ID。
- `prompt`：仅用于提交，不进入 Result 或报告。
- `locale`：`zh-CN` 或 `en`。
- `expected_mode`：FAST 或 RESEARCH。
- `category`：version、background、comparison、multi_fact、conflict、no_answer、provider_resilience 或 pollution_regression。
- `answerability`：answerable 或 intentionally_unanswerable。
- `minimum_required_facts`。
- `forbidden_query_terms`：只用于计算污染数量，报告不输出命中词。
- `must_not_complete`：无答案或冲突用例必须为 true。
- `tags`：用于分层统计，不参与业务执行。

首版包含 40 个不同用例：

- FAST 20 个，RESEARCH 20 个。
- 每种 mode 内中文和英文各 10 个。
- 每个 case 重复 3 次，产生 FAST 60、RESEARCH 60，共 120 个计划 Run。
- 至少 8 个 intentionally unanswerable/conflict case。
- 至少 6 个 Apex 或对话污染回归 case。

Manifest 文件内容参与 SHA-256 指纹。Campaign 创建后不得替换 manifest；变更用例必须创建新 Campaign。

## 8. Schema 与迁移

新增线性 Alembic revision `0009_shadow_campaign_release_gate`。

### 8.1 shadow_campaigns

最少字段：

- tenant ID、campaign ID、name。
- status：`CREATED/RUNNING/STOPPING/PAUSED/COMPLETED/ABORTED`。
- gate status：`PENDING/PASS/FAIL/INSUFFICIENT_SAMPLE`。
- source commit SHA。
- manifest version、hash、case count 和 repetition count。
- candidate provider/model/output format/thinking mode 的安全配置指纹。
- GatePolicy 和费率版本。
- max runs、max concurrency、estimated-cost stop threshold、max provider calls。
- planned、submitted、completed、failed、degraded 数量。
- observed token、estimated cost 和 possibly-billed 数量。
- stop reason、created/started/completed time 和乐观锁 version。
- 最终 gate report artifact reference 与 SHA-256。

### 8.2 shadow_run_results

最少字段：

- tenant ID、result ID、campaign ID、search run ID。
- case ID、repetition、locale、category 和 expected mode。
- scheduling state：`PENDING/CLAIMED/SUBMITTED/COLLECTED/FAILED`。
- claim owner、lease expiry、submission attempt count 和确定性 idempotency key。
- actual mode、status、quality、stop reason、latency。
- Fact total/covered/gap、Claim/Citation 和 traceability。
- query pollution count。
- model calls、prompt/completion tokens、estimated cost、degraded。
- provider success/failure count。
- error class、error code 和 failed phase。
- collected time 和 collector schema version。

唯一约束为 `(campaign_id, case_id, repetition)`。Result 在 submission 前以 `search_run_id=NULL` 预创建并通过短 lease claim；取得 receipt 后 `search_run_id` 必须唯一且不可更换。CLAIMED lease 过期后可由恢复进程重新 claim，并用相同 idempotency key 取得原 receipt。

两张表均启用并 FORCE RLS，使用无 `BYPASSRLS` 的应用角色访问。Result 不保存 prompt、query、quote、网页正文、模型输出、Authorization 或 API Key。

## 9. Runner 命令与恢复语义

计划提供三个子命令：

```text
run_shadow_campaign.py create
run_shadow_campaign.py resume --campaign-id <uuid>
run_shadow_campaign.py pause --campaign-id <uuid>
run_shadow_campaign.py report --campaign-id <uuid>
```

`create` 必须显式提供：

- `--confirm-live`
- `--manifest`
- `--gate-policy`
- `--max-runs`
- `--max-provider-calls`
- `--estimated-cost-stop-usd`
- `--api-url`

首轮固定：max runs 120、max concurrency 2、最大 Provider 调用 480、estimated-cost stop threshold 0.10 美元。

Campaign 的 0.10 美元是平台基于版本化费率、已知 token 和 possibly-billed reserve 执行的**调度停止阈值**，不是对外部 Provider 账单的绝对保证。Provider 不提供平台可依赖的逐 Campaign 预授权；崩溃窗口和延迟 usage 可能令实际账单略高。真正的硬边界是 120 Run、480 次 Provider 调用和 Provider 账户侧额度。报告必须明确这一限制，不能把估算阈值描述成外部账单硬上限。

Runner 在每次提交前检查剩余 Run、调用和估算费用。POSSIBLY_BILLED 调用按策略中的保守 reserve 计费。达到任一阈值后不再提交新 Run，但继续收集已提交 Run。

`pause` 先把 Campaign 设置为 STOPPING；Runner 不再 claim 新单元，等待最多两个在途 Run terminal 并完成收集后进入 PAUSED。进程收到正常中断信号时执行相同流程。`resume` 只能恢复 PAUSED、RUNNING 或因进程丢失而 lease 过期的 Campaign；COMPLETED/ABORTED 不可恢复。

## 10. 停止条件与错误分类

### 10.1 立即停止

- Citation traceability 小于 100%。
- Required Fact 无证据却产生 COMPLETE。
- Query 对话污染数量大于 0。
- FAST/RESEARCH 模型调用超过 4/8。
- 跨租户访问、RLS 失败或敏感内容进入报告。
- 401、403、无效模型或其他永久模型配置错误。
- Runner 检测到 terminal Attempt 或超过 reconciliation grace 后仍未封口的孤儿 `STARTED` 模型调用；正常在途调用不属于违规。

### 10.2 受控停止

- 连续 3 个基础设施或 Provider 失败。
- 达到 Run、Provider call 或 estimated-cost 阈值。
- 用户中断或显式 stop 请求。

### 10.3 错误分类

每个失败最多归入一个主分类：

1. `CANDIDATE_DEFECT`：安全不变量、预算、错误 quality 或确定性业务规则失败。
2. `PROVIDER_TRANSIENT`：429、5xx、网络和 Provider wall-clock timeout。
3. `INFRASTRUCTURE`：API、PostgreSQL、Redis、Celery、artifact 或 lease 恢复失败。
4. `CONTENT_GAP`：系统正常结束但未获得足够证据；不是基础设施失败。
5. `PERMANENT_CONFIGURATION`：认证、模型名、密钥或不支持的能力配置错误。

错误消息、异常 repr 和 Provider body 不进入 Result；只保存稳定分类和脱敏错误码。

## 11. 发布门禁

首版策略版本为 `shadow-gate-v1`，使用 nearest-rank percentile。

### 11.1 样本充分性

- terminal Result 总数不少于 100。
- FAST 不少于 50，RESEARCH 不少于 50。
- 不同 case ID 不少于 40。
- intentionally unanswerable/conflict case 不少于 8。

不满足时只能输出 `INSUFFICIENT_SAMPLE`。

### 11.2 硬安全门

- Citation traceability = 100%。
- 无证据 COMPLETE 误报率 = 0。
- Query pollution count = 0。
- 模型调用预算越界 = 0。
- 永久悬挂 STARTED 模型调用 = 0。
- 跨租户/RLS 违规 = 0。

任一不满足即 `FAIL`。

### 11.3 质量与可靠性门

- FAST p95 ≤ 15 秒。
- RESEARCH p95 ≤ 120 秒。
- answerable required-Fact coverage ≥ 80%。
- intentionally unanswerable case 的明确缺口率 = 100%。
- degraded Result 比例 ≤ 10%。
- infrastructure failure 比例 ≤ 1%。

失败 Run 仍进入时延和可靠性分母，不能通过删除慢失败样本改善 p95。Provider transient 单独报告，同时计入 degraded 或 failure 指标；CONTENT_GAP 不计为基础设施失败，但会影响 coverage。

GatePolicy 是版本化数据，不允许 runner 参数覆盖阈值，并在 Campaign 创建时锁定。策略变更必须创建新的 Campaign；禁止使用新策略覆盖历史 Campaign 的 gate status 或 report artifact。

## 12. 费用与资源控制

- Runner 最大并发固定为 2；CLI 不允许提高首轮上限。
- 每个 SearchRun 继续服从 FAST 4、RESEARCH 8 次模型调用硬预算。
- Campaign 额外限制 Provider 调用总数为 480。
- 每完成 10 个 Result 重新汇总 token、费用、POSSIBLY_BILLED 和失败趋势。
- 超过估算费用阈值后禁止新提交，已出站调用照常封账。
- 未知 token 不按零成本处理；使用 GatePolicy 的 possibly-billed reserve。
- 报告同时给出 token 事实、估算费用、费率版本和 possibly-billed 数量。

## 13. 隐私、隔离与保留

### 13.1 隐私

- Campaign DB、JSON 报告和 Markdown 报告不保存 prompt。
- 不保存 query、quote、网页正文、模型响应或 reasoning。
- 不保存用户身份；只保存 tenant、campaign、case 和 run reference。
- TelemetryRedactor 在写 Result 和报告前执行 allowlist 过滤。

### 13.2 保留

- `shadow_run_results` 明细默认保留 90 天。
- Campaign 聚合报告与策略判定保留 365 天。
- ModelInvocation 计费审计独立保留 365 天。
- 正常 SearchRun/artifact 的清理由其自身策略控制，Campaign 不复制正文以延长保留。

首版本只实现可配置 retention metadata 和安全查询边界；自动删除作业必须在外键、审计和恢复策略经过独立验证后启用，避免提前破坏证据链。

## 14. 报告设计

JSON 与 Markdown 报告包含：

- Campaign ID、commit SHA、manifest hash、模型配置指纹。
- GatePolicy、费率和 collector schema version。
- 计划、提交、terminal、失败和降级样本数。
- FAST/RESEARCH 的 p50、p95、max 和 deadline breach。
- Fact coverage、明确缺口、Citation traceability 和 pollution。
- 模型调用、token、estimated cost、POSSIBLY_BILLED。
- Provider、基础设施、内容缺口和候选缺陷分类统计。
- 每条 gate 的阈值、观测值、通过状态和 reason code。
- 最终 `PASS/FAIL/INSUFFICIENT_SAMPLE`。

报告可以列出 case ID 和 Run ID 用于诊断，但不得回显 prompt 或抓取内容。报告 artifact 使用 content-addressed storage 并把 SHA-256 保存到 Campaign。

## 15. 测试策略

### 15.1 单元测试

- nearest-rank p50/p95 边界。
- 样本不足判定。
- 每条安全、质量和可靠性门禁。
- 错误分类优先级。
- 费用、Run 和 Provider-call admission。
- 调度单元稳定排序和唯一键。
- Result/report allowlist 脱敏。

### 15.2 数据库测试

- `0009` upgrade/downgrade 和单一 Alembic head。
- 两表 FORCE RLS 和跨租户拒绝。
- Campaign/Result 唯一约束。
- 并发 claim 同一调度单元时只有一个 API submission owner。
- 恢复时已有 Run 不产生第二次提交。

### 15.3 Runner 集成测试

- 未提供 `--confirm-live` 时零网络。
- 超出 max runs/concurrency/call/cost 时在提交前拒绝。
- 进程在提交前、提交后、收集前和报告前退出时均可恢复。
- 连续 3 次 Provider/基础设施失败停止。
- 永久配置错误和安全违规立即停止。
- 报告生成失败不丢失已收集 Result。

### 15.4 Docker 验证

先执行 6 次小 Campaign：

- FAST 3、RESEARCH 3。
- 至少一个 no-answer case。
- 验证 migration、RLS、checkpoint、report 和无 STARTED 调用。

小 Campaign 未通过时禁止执行 120 次 Campaign。

### 15.5 真实 Campaign

小 Campaign 通过后执行 120 个计划 Run。永久错误、安全违规、预算阈值或连续失败触发停止。最终报告只能在满足样本充分性后给出 PASS/FAIL，否则为 INSUFFICIENT_SAMPLE。

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

1. `0009` migration、RLS 和 schema tests 通过。
2. Campaign 可创建、停止、恢复和幂等收集。
3. 6 次 Docker 小 Campaign 通过且无敏感内容或悬挂调用。
4. 120 次 Campaign 在既定调用和估算费用阈值内完成，或以可解释的停止原因结束。
5. Gate report 可重复生成且 hash 稳定。
6. 只有样本充分时才能产生 PASS/FAIL。
7. GatePolicy 不能被 CLI 临时覆盖。
8. 全量默认 pytest 不访问真实 DeepSeek。
9. 当前工作区中不属于本阶段的旧搜索/UI 改动保持未提交。

## 18. 后续阶段

若 Campaign 为 PASS，下一阶段依次为：

1. 设计并实现 1% durable real-time Shadow。
2. 在真实分布上持续观测至少 7 天或 1,000 个合格样本。
3. 接入第二个真实搜索 Provider 并验证故障切换。
4. 执行多租户并发、取消、Worker crash 和 Redis loss 压测。
5. 单独推进 S3/MinIO、OIDC 和记忆迁移。

Campaign PASS 只允许进入实时 Shadow，不等同于生产切流批准。
