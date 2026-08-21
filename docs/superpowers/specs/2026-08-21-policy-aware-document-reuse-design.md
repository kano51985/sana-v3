# Sana 策略化跨运行文档复用设计

日期：2026-08-21

状态：架构自审通过，获用户授权后进入实施

范围：Live content acquisition 的同租户、同 canonical URL、带 freshness 与完整血缘的 read-through reuse；不改变发布门禁阈值、证据验证规则或回答质量定义

## 1. 背景与问题证据

候选 `25f5d97` 的 Live DeepSeek Smoke Campaign 已通过，但随后 Full Campaign `28b0d1f9-e893-42f2-8ef6-2a06622f5ca5` 被质量门禁正确拒绝：

- 120/120 Run 完成，0 failed，但 42 degraded。
- 12 个 critical gold assertion 失败。
- critical gold 为 36/48（75.00%）。
- case-macro coverage 为 72.91%，FAST/en 为 54.16%。
- 后置审计的 Run 唯一性、账本、镜像、RLS、run-local fetch lineage、报告完整性和隐私扫描均通过。

失败 repetition 的 StepAttempt 反复出现 `fetch_network_failure` 与 `fetch_deadline_exceeded`。同一 tenant、同一 Campaign 的其他 repetition 已经成功抓取相同官方 URL、保存原始 FetchArtifact 并形成 DocumentVersion，但后续 Run 仍从零访问公网。结果是稳定事实受瞬时网络抖动影响，相同官方页面被重复下载，且 Fetch deadline 耗尽后没有安全、可追踪的 fallback。

本阶段的目标不是让报告“看起来通过”，而是消除这个已被 Full 数据证明的共同原因，然后使用全新候选镜像重新执行 Smoke 与 Full。

## 2. 架构决策

采用策略化 Read-through Cache：

1. Fetch Step 根据当前 hit 映射的 Fact Requirements 计算最严格 freshness。
2. 在任何缓存读取前重新执行目标 URL 的 SSRF/DNS 校验。
3. 查询同 tenant、同 canonical URL hash 的最近可复用 live FetchArtifact。
4. 缓存年龄位于 fresh window 时，验证并复制原始 body 到当前 Run，跳过公网请求。
5. 超过 fresh window 时优先执行 live HTTP fetch；只有允许的瞬时错误且缓存年龄仍位于 fallback window 时，才执行 stale-if-error。
6. 每次复用都创建当前 Run 独立的 FetchArtifact、重新 Extract/Chunk/Verify，并创建当前 Run 的 DocumentVersionFetch。
7. fresh reuse 不算 degraded；stale-if-error 必须产生稳定降级信号并进入 Shadow Collector。

拒绝以下路线：

- 只增加 timeout/retry：继续依赖公网、增加延迟和目标站负担，仍不能跨 Worker/重启复用。
- 无条件 cache-first：无法证明 current/version/pricing 等事实的新鲜度。
- 直接复用旧 Evidence/Citation：破坏 run-local evidence lineage，禁止采用。

## 3. 目标与非目标

### 3.1 目标

- 降低相同稳定官方来源在重复 Run 间的网络方差。
- 保持 tenant、URL、SSRF、内容完整性和 evidence lineage 的 fail-closed 边界。
- 对 STABLE、RECENT、CURRENT 使用不同、版本化、可配置的窗口。
- 保留原始内容抓取时间，防止复用动作刷新 freshness。
- 让 fresh、live、stale-if-error 决策在数据库、审计和 Shadow 报告中可解释。
- 不引入新的数据库迁移，复用现有 FetchArtifact、DocumentVersion 与 DocumentVersionFetch 表达能力。

### 3.2 非目标

- 不改变 Shadow gate、gold、coverage、degraded 或人工复核阈值。
- 不复用旧 Run 的 VerifiedEvidence、Claim 或 Citation。
- 不提供跨 tenant、模糊 URL、host-only 或语义相似页面缓存。
- 不实现通用 CDN、分布式 HTTP cache、条件请求、浏览器/Katana cache 或后台刷新队列。
- 不让 CURRENT 数据在断网时无限期返回。
- 不以缓存命中掩盖 artifact corruption、SSRF 或不支持的内容。

## 4. 策略模型

### 4.1 默认窗口

策略版本 `document-reuse-v1` 的默认值：

| Fact freshness | Fresh reuse | Stale-if-error 最大年龄 |
| --- | ---: | ---: |
| STABLE | 24 小时 | 30 天 |
| RECENT | 6 小时 | 7 天 |
| CURRENT | 15 分钟 | 2 小时 |

所有窗口必须满足 `0 < fresh_window <= fallback_window`。值由 Worker settings 提供，并在 Shadow Compose 中显式固定，因此进入 rendered Compose config hash 与 attestation。策略版本和实际窗口同时写入每次 cache Fetch 的 allowlisted metadata，不能只依赖进程默认值。

### 4.2 最严格 Fact 规则

严格度固定为：

```text
CURRENT > RECENT > STABLE
```

一个 URL 可能绑定多个 Fact。Fetch Step 只读取 hit 的 `fact_ids`，并在 Plan artifact 中逐一解析 freshness，使用其中最严格值。以下情况禁止缓存并执行 live fetch：

- hit 没有 Fact 绑定；
- Fact ID 不在当前 Plan；
- freshness 值未知；
- Plan/Hit 数据结构不满足 schema。

禁止用所有 Fact 的平均值或最宽松值决定缓存年龄。

### 4.3 年龄语义

缓存年龄以原始 live FetchArtifact 的 `fetched_at` 计算。复用产生的新 FetchArtifact 继续保存该原始抓取时间；当前复用发生时间只写入 `fetch_metadata.reused_at`。因此多次复用不会刷新年龄，也不会形成永久新鲜的复用链。

未来时间戳、naive datetime 或 fallback window 之外的候选不可复用。年龄比较只使用 timezone-aware UTC 语义。

## 5. 领域类型与端口

内容领域新增：

- `DocumentReusePolicy`：版本、三个 freshness window、strictest 与 age assessment。
- `ReuseFreshness`：STABLE、RECENT、CURRENT。
- `ReuseDecision`：MISS、CACHE_FRESH、LIVE、CACHE_STALE_IF_ERROR。
- `ReusableContentSnapshot`：只包含复用所需的稳定 identity、时间、media type、raw body ArtifactRef、digest、HTTP status 和 redirect hops。
- `ReuseAssessment`：freshness、age、fresh/fallback eligibility。

内容端口新增：

```text
ContentSnapshotReader.latest_for_url(tenant_id, canonical_url_hash)
URLSafetyValidator.validate(url)
```

`ContentSnapshotReader` 不返回 ORM entity，不接收 run-global session，也不允许调用方省略 tenant。`URLSafetyValidator` 由现有 SSRFGuard adapter 实现。

## 6. SQL Adapter

新增小型 `SqlContentSnapshotReader`，避免继续扩大通用 repository。查询必须同时满足：

- 所有参与表均为当前 tenant。
- FetchArtifact 的 `url_hash` 与输入 canonical URL hash 精确相等。
- FetchArtifact `status=SUCCEEDED`。
- `fetcher=http`，只选择原始 live fetch，避免 cache-to-cache 链。
- raw storage URI、raw content hash、media type 均存在。
- 存在同一 tenant/run/fetch artifact 的 DocumentVersionFetch。
- 对应 DocumentVersion 存在，证明该 raw body 曾成功 Extract。

结果按 `FetchArtifact.fetched_at DESC, FetchArtifact.id DESC` 稳定排序并只取一个。RLS 是第二道边界；查询本身仍显式包含 tenant predicates。

不需要数据库迁移：

- FetchArtifact 已保存 run、URL hash、fetcher、status、media type、content hash、storage URI、fetched_at 和 JSONB metadata。
- DocumentVersionFetch 已表达 Run-local FetchArtifact 到稳定 DocumentVersion 的绑定。
- LocalArtifactStore 已从 URI 解析 tenant/run/digest，并在读取时重新验证 SHA-256。

## 7. Fetch 数据流

```text
FETCH input
  -> 解析 Plan + hit.fact_ids
  -> 计算 strictest freshness
  -> SSRF/DNS validate 请求 URL
  -> tenant-scoped latest reusable live snapshot query
  -> snapshot 存在时重新 validate 已记录 redirect hops
  -> age <= fresh window
       -> 读取 raw artifact
       -> URI/digest/tenant/size/media/hash 校验
       -> 复制 body 到当前 Run artifact
       -> CACHE_FRESH output
  -> 否则执行 live HTTP fetch
       -> success: LIVE output
       -> eligible transient/deadline/429/5xx
          且 snapshot age <= fallback window
            -> 读取、校验、复制 raw artifact
            -> CACHE_STALE_IF_ERROR output
       -> 其他失败: 保留原 TypedError
  -> EXTRACT
  -> CHUNK / VERIFY / SYNTHESIZE
```

缓存读取使用与 live Fetch 相同的 `FetchRequest.max_response_bytes`。raw body 必须：

- 非空；
- SHA-256 同时匹配 ArtifactRef 和 FetchArtifact.content_hash；
- 长度不超过当前请求上限；
- media type 位于当前 HTTP fetch allowlist；
- request URL 与候选 URL hash 一致；
- 所有已记录 redirect URL 重新通过 SSRF/DNS 校验。

任一校验失败都 fail closed，不转而使用另一个旧候选，也不静默访问公网来掩盖 corruption。

## 8. Stale-if-error 矩阵

允许 stale-if-error：

| Live 失败 | 允许 |
| --- | --- |
| DNS 临时失败（live Fetch 内发生） | 是 |
| 网络连接/读取异常 | 是 |
| Fetch deadline exhausted | 是 |
| HTTP 429 | 是 |
| HTTP 5xx | 是 |

禁止 stale-if-error：

| Live 失败 | 行为 |
| --- | --- |
| URL syntax、credential、port、private/local address、DNS rebinding、redirect SSRF | fail closed |
| HTTP 4xx（429 除外） | 保留 CONTENT error |
| unsupported media type | 保留 CONTENT error |
| oversize、invalid content length | 保留 CONTENT error |
| empty body / empty extracted text | 保留 CONTENT error |
| cache artifact missing、URI/digest mismatch、hash corruption | fail closed |
| unknown/internal error | 保留原错误 |

初次缓存前 URL 安全校验本身失败时不允许复用；系统不能在当前 DNS 无法证明目标仍为公网地址时读取历史内容。

## 9. 输出 schema 与持久化

Fetch output 升级为 `sana.fetch.v2`，保留 v1 读取兼容并新增：

- `fetcher`：`http` 或 `document-cache`。
- `decision`：LIVE、CACHE_FRESH、CACHE_STALE_IF_ERROR。
- `degradation_codes`：fresh/live 为空；stale fallback 固定包含 `fetch_cache_stale_if_error`。
- `cache_metadata`：严格 allowlist。

cache metadata 只允许：

- policy version；
- strictest freshness；
- source fetch artifact ID；
- source run ID；
- source document version ID；
- source fetched_at；
- reused_at；
- reuse age seconds；
- decision；
- live error category/code（仅 stale-if-error）。

禁止保存 secret、Authorization、cookie、prompt、message、query、answer、quote、网页正文、异常 repr 或 Provider body。

WorkflowCompletionCoordinator 持久化当前 Run 的新 FetchArtifact：

- ID 仍由当前 run/step key 稳定派生；
- `fetcher=document-cache`；
- storage URI 指向复制到当前 Run 的 raw body；
- fetched_at 保留 source fetched_at；
- reuse 时间与 source lineage 写入 fetch_metadata；
- response_bytes 写入实际长度。

随后 Extract 重新执行并创建当前 Run 的 DocumentVersionFetch。DocumentVersion 可因 content hash 相同而命中现有唯一记录，但新 Run 的 fetch binding 必须独立存在。

## 10. 降级、Collector 与 telemetry

fresh cache reuse 是正常 read-through 行为，不标记 degraded。

stale-if-error 代表本次 live acquisition 发生瞬时失败，必须：

- 在 Fetch output 写入 `fetch_cache_stale_if_error`；
- 在最终 synthesis 的 pipeline degradation codes 中保留该稳定 code；
- 在 FetchArtifact metadata 保存脱敏 live error category/code；
- 在 Shadow Collector 形成 `PROVIDER_TRANSIENT` signal 和 `degraded=true`；
- 进入 source snapshot digest，但不把 URL、正文或异常文本带入报告。

Collector snapshot 新增 allowlisted SourceFetch projection，并递增 collector schema version。Fresh 命中只用于可观测计数，不改变 error class；stale fallback 产生 provider transient，而不是 candidate defect 或 infrastructure failure。

建议 telemetry 计数：

- `content_fetch_decision_total{decision,freshness,policy_version}`
- `content_reuse_age_seconds{decision,freshness}`
- `content_reuse_validation_failure_total{reason}`

若当前 telemetry adapter 尚无稳定指标 sink，本阶段以 FetchArtifact metadata、Step output 和 Collector digest 作为权威可审计信号，不为单一指标提前引入新基础设施。

## 11. 配置与 attestation

Worker settings 新增：

```text
SANA_WORKER_DOCUMENT_REUSE_ENABLED
SANA_WORKER_DOCUMENT_REUSE_POLICY_VERSION
SANA_WORKER_REUSE_STABLE_FRESH_SECONDS
SANA_WORKER_REUSE_STABLE_FALLBACK_SECONDS
SANA_WORKER_REUSE_RECENT_FRESH_SECONDS
SANA_WORKER_REUSE_RECENT_FALLBACK_SECONDS
SANA_WORKER_REUSE_CURRENT_FRESH_SECONDS
SANA_WORKER_REUSE_CURRENT_FALLBACK_SECONDS
```

生产默认启用；离线 fixture 可使用相同策略但因没有历史 live Fetch 通常 MISS。Shadow Compose 显式固定全部值，使其进入 candidate/environment config hash。相同 Smoke/Full identity 要求这些值完全一致。

功能开关提供一条配置级回滚路径。关闭后不查询 cache，Fetch 行为回到 live-only；数据库中的历史 metadata 和 lineage 继续保留，不做 destructive rollback。

## 12. 并发与幂等

- 同一 Run 的 URL 已在 selection 阶段按 canonical URL 合并。
- 不同 Run 并发 MISS 时允许各自执行 live fetch；系统不引入跨 Run 分布式锁，以免锁服务成为新的 availability dependency。
- Document 与 DocumentVersion 继续依靠现有 `(tenant,url_hash)` 和 `(document_id,content_hash)` 唯一约束收敛。
- 每个 Run 的 FetchArtifact 与 DocumentVersionFetch 使用稳定 ID 和 on-conflict no-op，重复 Step completion 不产生第二条 lineage。
- cache read 没有外部副作用；当前 Run artifact 的内容寻址写入可安全重放。

## 13. 安全与失败边界

1. tenant 是 adapter 必填参数，并同时由 UoW 与 RLS 强制。
2. URL hash 精确匹配；不做 host、prefix、redirect target 或 fuzzy match。
3. SSRF 检查发生在 cache query 前，并覆盖已记录 redirect hops。
4. LocalArtifactStore 校验 URI tenant 与 digest；领域层再比对数据库 raw content hash。
5. CURRENT 超过 2 小时即使断网也返回 evidence gap。
6. source fetched_at 永不被 reuse timestamp 覆盖。
7. 原始 body 仍受当前 size/media policy 约束。
8. cache corruption 不得被静默降级成 MISS。
9. 复用不会抄贝旧 Evidence、Claim、Citation 或 answer。
10. metadata 与报告使用 allowlist，而不是黑名单脱敏。

## 14. 测试策略

### 14.1 领域单元测试

- STABLE/RECENT/CURRENT strictest 矩阵。
- fresh/fallback 边界等于、前一秒、后一秒。
- fallback 小于 fresh、非正窗口、未知 freshness、naive/future timestamp 拒绝。
- eligible live error allowlist 与所有禁止错误。

### 14.2 Fetch operation 测试

- fresh cache 跳过 live fetch。
- 超过 fresh window 时 live success 优先。
- network/deadline/429/5xx 使用 fallback。
- CURRENT 超过 2 小时保留原错误。
- 4xx、SSRF、content type、oversize、empty body 不 fallback。
- source artifact missing/corrupt/digest mismatch fail closed。
- request URL 与 redirect hops 均重新 SSRF validate。
- 当前 Run 得到独立 raw body artifact，source fetched_at 不刷新。
- metadata 不包含 prompt、answer、query、quote 或异常文本。

### 14.3 SQL/安全测试

- 只返回同 tenant、精确 url hash、成功 live fetch 且存在 DocumentVersionFetch 的最新候选。
- tenant A 不能读取 tenant B snapshot。
- cache-produced FetchArtifact 不成为下一次 source。
- 并发相同 URL 的 Document/Version/Run-local binding 收敛。
- FORCE RLS、collector run-local lineage 与现有 composite FK 继续通过。

### 14.4 Coordinator/Collector 测试

- v2 output 持久化 document-cache fetcher、response bytes 和 allowlisted metadata。
- fresh reuse 不 degraded。
- stale-if-error 进入 synthesis degradation code、SourceFetch digest、PROVIDER_TRANSIENT 和 degraded count。
- source digest 不包含 URL、body、prompt、answer、query、quote 或 secret。
- 现有 v1 fixture 仍可读取，现有 live HTTP 路径不回归。

### 14.5 门禁验证

1. 完整 pytest、compileall、diff-check。
2. clean HEAD 构建新 image；OCI revision 必须等于 HEAD。
3. 新 attestation 与全新 Smoke key。
4. Smoke FINAL_PASS 与 7/7 audit。
5. 新 Full 120/120，自动 gate 与 7/7 audit。
6. 只有自动门禁全部通过后，用户本人完成 20 条真实人工复核。

## 15. Rollout 与 rollback

Rollout：

1. 先合并领域 policy、adapter 和离线测试。
2. 在 Worker composition root 启用 reuse，并把策略固定进 Shadow Compose。
3. 从 clean commit 构建新镜像和 attestation。
4. 运行全新 Smoke；不复用旧 Smoke identity。
5. Smoke 通过后运行 Full，比较 gold、coverage、degraded 与 fetch decision 分布。

Rollback：

- 配置关闭 `SANA_WORKER_DOCUMENT_REUSE_ENABLED` 并重启 Worker。
- 不回滚 schema，因为本设计不新增迁移。
- 不删除 cache FetchArtifact、DocumentVersionFetch、失败 Campaign 或报告。
- 若 cache 引入任何 lineage、SSRF、integrity 或 privacy 违规，立即停止 Campaign；不能通过降低门槛继续运行。

## 16. 架构自审结论

本设计保持了现有模块化单体、PostgreSQL 真相源、ports/adapters、run-local evidence lineage 和 fail-closed 安全模型。它复用现有表和 content-addressed artifact 能力，不引入新数据库迁移、分布式锁或后台刷新系统；同时把 freshness、SSRF、完整性、降级和 attestation 变成可测试的显式策略。

剩余主要风险是目标站内容结构变化，而不是缓存语义本身。因为每次复用仍重新 Extract/Verify，结构变化不会被旧 Evidence 静默掩盖；超过 freshness/fallback window 时仍会形成明确 evidence gap。方案可进入实施。
