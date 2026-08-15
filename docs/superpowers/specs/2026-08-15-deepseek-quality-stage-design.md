# Sana DeepSeek 质量闭环阶段设计

日期：2026-08-15

状态：设计内容已确认，架构自审通过，可进入实施计划

范围：在现有持久化搜索 Worker 中，以同一 DeepSeek API Key 和 `deepseek-v4-flash` 接入 Planner、批量 Verifier、Synthesizer，并建立模型调用预算、脱敏审计和真实质量验收。

## 1. 目标

本阶段把本地 heuristic 搜索闭环升级为真实模型驱动的质量闭环，但不改变 PostgreSQL 权威状态、Celery 调度、多租户隔离和证据不可跳级原则。

完成后：

- Planner、Verifier、Synthesizer 均通过统一 Model Gateway 调用 DeepSeek V4 Flash。
- FAST 正常路径使用 3 次模型调用，最多 4 次；RESEARCH 最多 8 次。
- 模型只能提出规划、证据判断和答案 Claim，不能创建网页、引文、offset 或 Citation 事实。
- 所有最终事实仍须通过确定性 Evidence、Claim 和 Citation 校验。
- 模型 Provider 和模型名保持角色级配置，后续生产模型切换不需要改工作流。
- 默认测试不读取真实密钥、不访问真实模型 API。

## 2. 已确认决策

1. 当前使用现有 `DEEPSEEK_API_KEY`，密钥只进入 Worker 进程。
2. Planner、Verifier、Synthesizer 统一使用 `deepseek-v4-flash`。
3. 使用 OpenAI-compatible Chat Completions 接口和固定官方 base URL。
4. 三个角色显式使用非思考模式，以满足 FAST 时延目标。
5. 结构化调用启用 JSON Output，非法输出最多修复一次。
6. Verify 从“每个文档单独调用”改为所有 Extract 完成后的单一批量 Step。
7. 确定性证据定位、Claim 约束和 Citation 校验继续拥有最终否决权。
8. 本轮真实验收最多执行 20 个 Run；遇到认证、余额或永久模型配置错误立即停止。
9. 本阶段不提交设计或实施计划文档；用户已授权架构自审通过后进入实施计划。
10. 20 个 live Run 只作为功能和时延冒烟样本，不能单独证明生产 p95；最终生产 SLO 仍需更大规模 Shadow 数据。
11. 不宣称外部模型调用 exactly-once。Worker 在请求完成但结果落库前崩溃时，Provider 可能产生重复费用；平台必须准确标记 `POSSIBLY_BILLED`，但只能保证数据库副作用和答案幂等。

## 3. 不采用的方案

### 3.1 每个文档独立调用 Verifier

该方案保持现有 VERIFY fan-out，但 4 个文档会令正常路径达到 Planner 1 次、Verifier 4 次、Synthesizer 1 次，超过 FAST 的 4 次调用上限。并行判断还会增加跨调用结论不一致和重复 token 消耗。

### 3.2 将 Verifier 与 Synthesizer 合并为单次大模型调用

该方案调用次数更少，但会模糊证据裁决与答案表达的边界，使 Verifier 无法独立评测，也提高未经验证文本直接进入回答的风险。

### 3.3 仅让 DeepSeek 执行 Planner

该方案改动最小，但不能改善目前 lexical verifier 和引用模板的质量，不满足本阶段的质量闭环目标。

## 4. 总体架构

`ProductionWorker` 创建一个 DeepSeek Provider，并通过一个 Model Gateway 为三个角色建立独立 RoleConfig。官方 base URL 固定为 `https://api.deepseek.com`，adapter 负责追加 `/chat/completions`：

- `PLANNER`：标准化请求和 Fact Requirements。
- `VERIFIER`：在受限候选集合内判定 supports/contradicts。
- `SYNTHESIZER`：只根据已接受 Evidence 生成结构化 Proposed Claims。

角色默认使用同一模型，但配置键彼此独立。后续可只把 Verifier 或 Synthesizer 切换至其他生产模型。

工作流调整为：

```text
ROUTE
  -> PLAN (DeepSeek)
  -> DISCOVERY fan-out
  -> SELECT
  -> FETCH fan-out
  -> EXTRACT fan-out
  -> VERIFY fan-in (DeepSeek)
  -> SYNTHESIZE (DeepSeek)
  -> deterministic claim/citation gate
  -> persisted answer
```

PostgreSQL 仍是 Run、Step、Attempt、Fact、Evidence、Claim、Citation 和模型调用审计的唯一事实源。Redis/Celery 只负责投递。

生产执行路径必须复用现有 `EvidenceBuilder`、`EvidenceVerifier`、`CoverageEvaluator`、`ClaimSynthesizer` 和 `CitationValidator`，不能在 `SearchStepOperations` 中继续维护另一套手写 coverage/claim/citation 规则。Artifact payload 只负责跨 Step 传输，领域服务负责所有不变量。

## 5. Model Gateway 扩展

### 5.1 请求能力

ModelRequest 增加与 Provider 无关的结构化输出和推理模式选项：

- `output_format=text|json_object`
- `thinking_mode=provider_default|enabled|disabled`

DeepSeek adapter 将其映射为：

```json
{
  "response_format": {"type": "json_object"},
  "thinking": {"type": "disabled"}
}
```

其他 Provider 可按能力映射或显式拒绝不支持的组合，不能静默忽略安全相关选项。

### 5.2 结构修复

JSON parser 第一次失败时允许一次修复调用。修复调用：

- 使用相同角色和模型。
- 强制非思考和 JSON Output。
- 使用 temperature 0。
- 计入同一 Run 的调用和 token 预算。
- 不把模型隐藏推理内容拼入下一轮。

### 5.3 Provider 错误

Provider 继续输出统一 TypedError：

- 429、网络错误、5xx：`TRANSIENT`。
- 401、403、无效模型：`PERMANENT`。
- 空内容、无效响应结构：`MODEL_OUTPUT`。
- deadline 或调用/token 超限：`BUDGET`。

所有 retry 必须服从 absolute deadline，不能在 Gateway 内无限等待。

## 6. Planner 数据流

Planner 输入只包含：

- 当前用户请求。
- 经过授权、用于指代消解的简短上下文摘要；当前阶段默认不传会话全文。

Planner 输出 NormalizedIntent：entity、aliases、locale、Fact Requirements、完整来源和比较需求。IntentParser 校验事实数量、枚举值和必填字段。

Query Compiler 继续确定性生成查询，执行长度限制、实体约束、locale/freshness 约束、签名去重和对话污染过滤。模型不能直接提供最终 URL 或绕过 Query Compiler。

## 7. 批量 Verifier 数据流

### 7.1 Fan-in 调度

每个成功的 EXTRACT 不再立即创建自己的 VERIFY。Completion Coordinator 等待全部 EXTRACT 进入 terminal 状态后，创建唯一 `verify` Step。其输入 artifact 包含 plan reference 和所有成功 extract references。

如果没有可用 Extract，跳过模型 Verifier，直接创建 SYNTHESIZE，并把所有 required facts 标记为缺失。

### 7.2 候选选择

模型调用前执行确定性候选选择：

- 每个 Fact 最多 3 个候选 Chunk。
- 整个 Run 最多 12 个候选。
- 每个候选最多提供 600 字符。
- 排序使用实体、subject、fact-type 关键词、来源权威度和 freshness 的确定性分数。
- 在相关性门槛满足时优先来源多样性；同一 registrable domain 不占据同一 Fact 的多个席位，除非不存在其他合格来源。
- 输入携带稳定 candidate、fact、document、document version、chunk ID 和原始 offset。

全文不发送给模型；未进入候选集合的内容不能被模型引用。

### 7.3 模型输出

Verifier JSON 对每个判断只能返回：

- `fact_id`
- `candidate_id`
- `support_type`：supports 或 contradicts
- `quote`
- `confidence`
- `reason_codes`

模型不能提供 URL、document version ID、最终 Evidence ID 或 Citation。

### 7.4 确定性校验

模型结果必须通过以下检查：

1. fact/candidate ID 存在于本次输入。
2. quote 是指定候选文本的精确连续子串。
3. quote 可重新映射到 Chunk 和 DocumentVersion 的准确 offset。
4. DocumentVersion hash、tenant 和 run 匹配。
5. confidence 范围有效，support type 和 reason code 在允许集合内。

任一检查失败时，该判断为 REJECTED；系统不能尝试“修正”模型引文。通过后才创建 EvidenceCandidate 和 VerifiedEvidence。

来源身份和 authority 由系统策略确定，模型无权声明。`source_identity` 使用离线 Public Suffix List 解析后的 registrable domain；`OFFICIAL` 只来自版本化 allowlist，未命中时为 `UNKNOWN` 或 `INDEPENDENT`。Evidence persistence 必须保存 `source_identity` 和 `authority`，使 `CoverageEvaluator` 能在真实生产路径中区分官方来源、两个独立来源和同一发布者重复内容。

## 8. Synthesizer 数据流

Synthesizer 输入只包含：

- 用户语言和规范化 Fact 描述。
- ACCEPTED Evidence 的稳定 ID、精确 quote、来源标题和 authority。
- 每个 Fact 的 Coverage 状态、冲突和缺失原因。

模型输出 Proposed Claims：

- `claim_key`
- `text`
- `fact_id`
- `evidence_ids`
- `kind`：factual、uncertainty 或 commentary

模型生成的 URL、序号、HTML 或 Markdown citation 标记全部忽略。ClaimSynthesizer 只保留属于相应 Fact coverage 的 Evidence。CitationValidator 再次验证 tenant/run/fact/evidence 映射，重新计算 support，并为合法 Evidence 生成 URL、quote、offset 和序号。

AnswerQuality 和 StopReason 由确定性 coverage policy 决定，不由模型直接决定。无证据 Fact 不得产生受支持的 factual claim。

数据库 Citation 需要持久化领域 Citation 的完整不可变快照：document version、chunk、quote、start/end offset、rendered URL 和 ordinal。读取时仍通过 VerifiedEvidence 关系复核，而不是只相信快照字段。

## 9. 调用预算

正常路径：

| 角色 | FAST | RESEARCH |
| --- | ---: | ---: |
| Planner | 1 | 1 |
| Batch Verifier | 1 | 1 |
| Synthesizer | 1 | 1 |
| 正常合计 | 3 | 3 |
| Run 硬上限 | 4 | 8 |

结构修复和 retry 都消耗调用预算。Gateway 在发送请求前通过 AuditSink 的数据库事务预留调用，在收到响应后记录 prompt/completion tokens。预算耗尽时不再发出网络请求。

`model_invocations` 是实际 Provider 调用的唯一计数来源。AuditSink.start 必须锁定 SearchRun，验证当前 Step/Attempt 仍可执行，原子检查 4/8 上限、递增 SearchRun `llm_call_count` 并插入 STARTED，然后才允许网络出站。失败、retry、repair 和可能已计费的崩溃调用均占用预算；REUSED 不占用新调用。`StepBudgetCost` 不再重复累计模型调用，避免成功路径和 audit 双重记账。

SearchRun usage 增加 prompt/completion token 计数，但 token 只在 Provider 返回可验证 usage 后累加。调用硬上限由 PostgreSQL 原子执行；进程内 ModelCallBudget 继续限制单次 generate/repair 的局部调用与 token，不能替代数据库预算。

现有 Step 只拿到 Run hard deadline，phase_seconds 主要是事后统计，无法保护合成时间。实施时必须增加 StepType→BudgetPhase 映射：非 SYNTHESIZE Step 的执行 deadline 不晚于 `BudgetGuard.non_synthesis_deadline`，SYNTHESIZE 使用 hard deadline。每个模型请求再取 role timeout、Step deadline 和 Run 剩余时间的最小值。到达 admission deadline 后不再启动模型请求，直接执行相应失败或降级策略。

FAST 15 秒是验收门而不是设计假设。三次串行模型调用若无法满足门槛，本阶段必须报告失败并根据真实 trace 调整检索量、role timeout 或降级点，不能通过延长 FAST hard deadline 伪造达标。

## 10. 脱敏模型调用审计

新增 tenant-scoped `model_invocations` 审计表和 `ModelInvocationAuditSink` port。每个实际 HTTP 调用在出站前先以独立短事务锁定 Run、校验当前 Step/Attempt、预留 Run 调用预算并写入 STARTED，响应或错误后再封账。最少包含：

- tenant、run、step、attempt、trace ID。
- role、provider、model、thinking mode、output format。
- 调用序号、是否 retry/repair、started/completed time、latency、status。
- prompt tokens、completion tokens、请求/响应字符数。
- typed error category/code。
- prompt template/parser schema version。
- logical call key、是否真实调用 Provider、可选的 reused-from invocation ID。

禁止持久化：

- API Key 或 Authorization header。
- 原始 prompt、用户消息或网页正文。
- 原始模型响应或隐藏 reasoning content。
- Provider raw payload。
- 原始 request/response hash；对低熵或可猜测内容直接散列仍可能泄漏相等性和被字典反推。

`StepExecutionContext` 增加 attempt ID/number 和 trace ID，用于构造模型调用上下文。成功、HTTP 错误和解析错误都在 AuditSink 封账；认证/网络失败无 token usage 时记录为 0。若 Worker 崩溃，租约回收同时把该 Attempt 下仍为 STARTED 的调用标记为 `ABANDONED/POSSIBLY_BILLED`。

审计 ID/幂等键由 tenant、step、attempt、role 和调用序号确定。新的 Step Attempt 会产生新的模型调用记录，因为 Provider 不提供可依赖的 exactly-once 幂等契约。平台只保证记录不重复覆盖、业务写入幂等，并明确展示可能重复计费的崩溃窗口。

为缩小重复计费窗口，成功的结构化模型内容先写入 tenant-scoped content-addressed artifact，再把 artifact reference 与 COMPLETED 审计一起封账。新的 Attempt 若发现 provider、model、thinking/output format、prompt template version、parser schema version和 Step input refs 完全相同的 COMPLETED logical call，可校验 artifact hash 后复用，并记录 REUSED；它不消耗新的 provider call budget。原始 Provider payload 和 reasoning content 不进入 artifact。若进程在 Provider 返回后、artifact/audit 封账前崩溃，仍只能标记 `POSSIBLY_BILLED` 并重新调用。

模型价格随时间变化，数据库只把 Provider 返回的 token 数作为事实。估算费用由带生效时间和版本号的费率配置计算，不能把当前网页价格写死为历史账单事实。

## 11. 降级与错误处理

### 11.1 Planner

Planner 在 retry/repair 后仍失败时，Run 以 `INFRASTRUCTURE_FAILURE` 结束。不静默回退 heuristic，以免真实模型验收被伪装为成功。

### 11.2 Verifier

Verifier 最终失败时启用现有确定性 lexical verifier，并写入：

- `verifier_version=deterministic-fallback-v1`
- degraded reason 和原模型错误码
- AnswerQuality 最高为 PARTIAL

降级不能接受模型之前输出但未通过结构和精确 span 校验的内容。

### 11.3 Synthesizer

Synthesizer 最终失败时，使用确定性 Evidence renderer 输出已核验 quote 和明确缺失项。AnswerQuality 仍由 coverage 决定，审计记录 `degraded_synthesis=true`。

### 11.4 永久配置错误

密钥缺失、401/403 或无效模型名立即失败，不继续进行 live eval。日志只能出现稳定错误码，不能打印密钥或请求 header。

### 11.5 Worker 崩溃

沿用现有 lease、Attempt 和 Reconciler。模型输出只有在 Step 成功事务中成为后继输入；崩溃后的重试使用新的 Attempt 和审计幂等键。旧 Attempt 的未封账调用标记为 `POSSIBLY_BILLED`。最终 Answer、Claim 和 Citation 继续使用稳定 ID 防止重复业务副作用，但不承诺 Provider 不会重复收费。

## 12. Schema 与迁移

新增线性 Alembic revision，接在当前 merge head 之后：

- 创建 `model_invocations` 及 tenant/run/step/attempt 外键、唯一调用序号、状态和时间索引。
- 对 `model_invocations` 启用并 FORCE RLS。
- 为 `evidence_candidates` 增加 `source_identity`、`source_authority`。
- 为 `citations` 增加 document version、chunk、quote、start/end offset。
- 对现有行使用保守值回填：authority 为 UNKNOWN，source identity 从已持久化 Document host 推导；无法可靠推导时保持 UNKNOWN。
- 先添加可空列并回填，再设置 NOT NULL，避免升级期间破坏已有数据。

迁移必须支持 downgrade，只删除本阶段新增列和表，不删除已有 Evidence、Claim 或 Citation 数据。

## 13. 配置

新增或明确以下 Worker 配置：

| 变量 | 本阶段默认值 |
| --- | --- |
| `SANA_WORKER_MODEL_PIPELINE_ENABLED` | `false`；live eval 时显式设为 `true` |
| `SANA_WORKER_PLANNER_PROVIDER` | `deepseek` |
| `SANA_WORKER_PLANNER_MODEL` | `deepseek-v4-flash` |
| `SANA_WORKER_VERIFIER_PROVIDER` | `deepseek` |
| `SANA_WORKER_VERIFIER_MODEL` | `deepseek-v4-flash` |
| `SANA_WORKER_SYNTHESIZER_PROVIDER` | `deepseek` |
| `SANA_WORKER_SYNTHESIZER_MODEL` | `deepseek-v4-flash` |
| `SANA_WORKER_MODEL_THINKING` | `disabled` |

`DEEPSEEK_API_KEY` 继续由 SecretProvider 从 Worker 环境读取。API 和 Streamlit 不接收该变量。生产环境必须拒绝空模型名、heuristic planner 和不受支持的 thinking/output-format 组合。

功能开关关闭时保持当前确定性管线，便于一条配置完成回滚。开关开启但模型配置无效时 Worker 必须拒绝启动，不能静默切回确定性模式。当前阶段通过 Compose/Worker 环境显式开启，不把本地测试选择固化为最终生产默认。

## 14. 测试设计

### 14.1 默认测试

所有默认测试使用 FakeModelGateway 或 mock HTTP transport，不读取真实用户环境密钥。覆盖：

- V4 Flash payload、非思考模式和 JSON Output。
- 429/5xx retry、401/403 permanent failure、timeout 和空响应。
- JSON repair 的调用/token 统计。
- Planner 污染过滤。
- Extract fan-in 只生成一个 VERIFY。
- FAST/RESEARCH 调用预算。
- Verifier 伪造 quote、offset、fact ID、document version 和跨 tenant ID 时全部拒绝。
- Synthesizer 非法 evidence mapping 被移除。
- 模型失败降级和 AnswerQuality 上限。
- 重复投递、Worker crash 和审计幂等。
- Worker 在模型响应前后被强杀时，STARTED 调用分别进入 ABANDONED/POSSIBLY_BILLED，业务答案仍保持单一。
- 审计不包含 secret、prompt、正文和 Provider raw payload。
- 新表/列的 migration、RLS、upgrade/downgrade 和已有数据回填。
- 生产 SearchStepOperations 确实调用领域 Coverage/Claim/Citation 服务，不保留第二套规则。

### 14.2 Live eval

Live eval 必须通过显式标志运行，最多 20 个 Run。覆盖：

- FAST 单事实。
- RESEARCH 多事实。
- 中文和英文。
- 无答案。
- Provider 部分失败。
- Apex 对话污染回归。

认证、余额、模型名等永久错误出现后立即停止剩余用例。

20 个 live Run 只报告 observed p50、p95、max 和超时数，作为冒烟证据。它们不能关闭生产 p95 阻断项；生产切换需由至少 100 个有代表性的 Shadow Run 或持续观测窗口证明 SLO。

## 15. 验收门

本阶段完成必须同时满足：

- Query 对话污染率为 0。
- Citation 可回溯率为 100%。
- Required Fact 无证据时 COMPLETE 误报率为 0。
- 所有 Run 的模型调用数不超各自预算。
- 本轮 live sample 不得越过 FAST 15 秒或 RESEARCH 120 秒 hard deadline；同时输出 observed p95，但不把小样本 p95表述为生产 SLO。
- Apex 各 Fact 有可回溯结论或明确缺口，不用泛化文本冒充答案。
- API Key、原始 prompt、网页全文和 reasoning content 不进入日志或审计表。
- 默认全量 pytest 不访问真实 DeepSeek API。
- Docker 中 Planner、Verifier、Synthesizer 可使用同一 Key 完成真实闭环。
- 模型功能开关关闭后，无需回滚数据库即可恢复当前确定性管线。

生产切流门仍要求足够规模的 Shadow 数据证明 FAST p95 ≤ 15 秒、RESEARCH p95 ≤ 120 秒。

## 16. 发布与回滚

1. 先迁移 schema，但保持模型功能开关关闭。
2. 运行默认测试和 Docker migration/RLS 验证。
3. 在单 Worker 上显式开启 DeepSeek V4 Flash，执行不超过 20 个 live Run。
4. 若出现永久配置错误、证据越权、预算越界或 hard deadline 超时，立即关闭开关并保留审计记录。
5. 回滚只需关闭功能开关并重启 Worker；不回滚 schema，不删除模型审计或已生成 Evidence。

## 17. 本阶段不包含

- 本地小模型部署。
- DeepSeek V4 Pro 或其他生产模型质量对比。
- S3/MinIO artifact 切换。
- 正式 OIDC provisioning。
- 旧记忆正式导入。
- 最终生产流量切换。

这些事项保留为后续独立阶段，不阻塞当前 DeepSeek V4 Flash 质量闭环。

## 18. 参考资料

- DeepSeek 当前模型与定价：https://api-docs.deepseek.com/zh-cn/quick_start/pricing
- DeepSeek 思考模式：https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
- DeepSeek JSON Output：https://api-docs.deepseek.com/guides/json_mode
- DeepSeek Chat Completions：https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/
