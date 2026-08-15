# Shadow Campaign 隔离运行手册

本手册只用于受控发布门禁。Campaign PASS 仅表示候选版本可以进入后续 1% durable real-time Shadow 设计，不代表可以直接切换生产流量。

## 安全边界

- Compose project 固定为 `sana-shadow-eval`，不会复用开发环境的 PostgreSQL、Redis、network 或 volume。
- 只有 API 映射到宿主机 `127.0.0.1:18000`；PostgreSQL 与 Redis 不发布端口。
- API、migrate、artifact-init、dispatcher、worker、campaign-runner 使用同一不可变 image ID。
- `DEEPSEEK_API_KEY` 只进入 worker；`SANA_ACCESS_TOKEN` 只进入 transient campaign-runner。
- Token 只能来自进程环境变量或 PowerShell 的安全交互输入，不能写入命令行参数、attestation、日志、数据库或报告。
- Runner 不挂载 Docker socket，只读取 host launcher 生成的 sanitized attestation。
- `down` 不删除 evidence volume。需要删除 volume 时必须另行人工确认并明确列出四个 `sana-shadow-eval-*` volume。

## 前置条件

1. 在 clean `codex/shadow-campaign-release-gate` worktree 执行。
2. Docker Desktop/Engine 与 Compose v2 可用。
3. live 模式设置或在安全提示中输入以下值：
   - `SANA_SHADOW_OWNER_DB_PASSWORD`
   - `SANA_SHADOW_APP_DB_PASSWORD`
   - `DEEPSEEK_API_KEY`
   - `SANA_ACCESS_TOKEN`，本地 dev 模式格式为 `<tenant UUID>:<user UUID>`
4. 不要把上述值写入 `.env`、PowerShell history、issue、报告或聊天记录。

离线 fixture 模式不需要、也不会读取 `DEEPSEEK_API_KEY`。launcher 会在 worker 容器内强制覆盖固定的无效哨兵值，宿主机已有的真实 key 不会进入容器。

## 构建与隔离预检

```powershell
.\scripts\run_shadow_campaign.ps1 prepare
```

`prepare` 会拒绝 dirty worktree，并完成以下检查：

- build OCI revision 与 candidate commit 完全一致；
- 所有候选服务 image ID 相同；
- Alembic head 为 `0012_fetch_run_binding`；
- API 只绑定 loopback，DB/Redis 无宿主端口；
- worker concurrency 固定为 2，队列固定为 fast/research/crawl/maintenance；
- 无非 Campaign active SearchRun、未发布 outbox 或初始队列消息；
- network、volume、resource limit 和 config hash 与 sanitized topology 一致；
- attestation 不含 secret、原始 Compose config、环境变量值或 Docker socket。

成功后 attestation 位于 `var/shadow-eval/attestation.json`。它只包含可公开复验的身份与计数。

## 离线 Docker 闭环

在任何 live 调用前，先运行显式标记的离线闭环：

```powershell
.\scripts\run_shadow_campaign.ps1 prepare -OfflineFixture
.\scripts\run_shadow_campaign.ps1 create `
  -OfflineFixture `
  -CampaignKey offline-fixture-<stable-key> `
  -Profile docker-smoke-v1
```

该模式使用专用 `fixture` discovery/fetch/model adapters，产生真实 PostgreSQL、Redis、Celery、Run、Fact、Evidence、Claim、Citation、ledger 与 report 记录，但所有模型结果的 `provider_calls` 和 token 使用量均为 0，且不进行公网访问。

其 attestation schema 为 `shadow-provenance-v2`，`execution_class` 固定为 `OFFLINE_FIXTURE`。CLI confirmation、Compose override 与 attestation 三者不一致时会在 Campaign 写入前失败。离线报告只能证明恢复、持久化、血缘、隐私和容器拓扑；即使其 gate 状态为 PASS，也绝不能作为 DeepSeek live gate 或生产放量依据。

切换到 live 必须重新执行不带 `-OfflineFixture` 的 `prepare`，生成 `execution_class=LIVE_DEEPSEEK` 的新 attestation；同一 attestation 不能跨执行类别复用。

离线 Campaign 完成后执行 fail-closed 审计：

```powershell
.\scripts\audit_shadow_campaign.ps1 `
  -CampaignId <uuid> `
  -OfflineFixture
```

审计器要求当前 worktree clean，且 HEAD、Campaign provenance、attestation 和运行中容器 image ID 完全一致。它同时核对 Result/SearchRun 唯一性、提交次数、Campaign/Result/ModelInvocation 账本、ACTIVE reservation、SearchRun、Outbox、全部业务队列、FORCE RLS、worker health/进程 allowlist 和报告文件哈希。worker 必须有 main+2 prefork 三个 Celery 常驻进程；采样瞬间只额外允许至多一个命令行完全匹配的短生命周期 Redis PING health probe。日志与报告会在进程内扫描数据库口令、当前 token/key 和 manifest prompt；输出只包含 PASS 断言，不回显敏感值。离线模式还强制全部 Provider 调用、token 与费用为零。

`worker` 的 Docker healthcheck 是有界 Redis PING，只证明 worker 进程依赖可达，不单独证明 Celery 消费能力。Celery pidbox/inspect 不作为容器健康条件，因为超时探针可能遗留子进程且在任务后产生假阴性；实际消费能力必须由完成 Campaign、队列归零和上述审计共同证明。

## 费率版本

`deepseek-v4-flash-usd-2026-08-15-v2` 冻结采用 DeepSeek 官方定价页在 2026-08-15 列出的缓存未命中输入价 0.14 美元/百万 token 与输出价 0.28 美元/百万 token。未知出站仍按每个 run 0.001 美元计 possibly-billed charge；预算 admission 独立预留 0.002 美元，以同时覆盖未知出站和同一 run 已确认计费的正常调用，避免把一次瞬态重试误判为 Campaign 级预算事故。来源：<https://api-docs.deepseek.com/quick_start/pricing>。

官方价格或预留语义发生变化时必须新增 cost-rate 版本、重算全部 hash 并重跑 smoke；不得原地修改历史版本或让同一 smoke/full Campaign 使用不同费率身份。

## 评审意图快车道

`reviewed-intents-v1` 只为语义明确、长期稳定的标准问题、公开披露缺口和版本支持表生成原子 Fact，不保存或预填答案。每个 Fact 仍必须经过实时抓取、实体范围内的来源权威判定、候选绑定和确定性验证；网页结构或官方数据变化导致提取失败时必须保留 evidence gap，禁止回退到模板答案。

计划 artifact 的 `planning.strategy` 与 `planning.strategy_version` 会区分 `reviewed_template`、模型规划和启发式降级。PostgreSQL 支持版本使用官方支持表的实时行位次，而不是在模板中固化版本号；模板、直达来源或确定性提取语义变化时必须递增各自版本并重新执行 Smoke/Full 门禁。

## 七个 Runner 命令

创建 6-run smoke：

```powershell
.\scripts\run_shadow_campaign.ps1 create `
  -CampaignKey smoke-<stable-key> `
  -Profile docker-smoke-v1
```

查询 owner Campaign：

```powershell
.\scripts\run_shadow_campaign.ps1 list
```

暂停、恢复或终止：

```powershell
.\scripts\run_shadow_campaign.ps1 pause  -CampaignId <uuid>
.\scripts\run_shadow_campaign.ps1 resume -CampaignId <uuid>
.\scripts\run_shadow_campaign.ps1 abort  -CampaignId <uuid>
```

`pause` 与 `abort` 都会停止新 claim，恢复已持久化的同键在途请求，drain 后再进入 PAUSED/ABORTED。Ctrl-C 或 SIGTERM 会先持久化 PAUSE intent；再次执行 `pause` 可完成无人值守 drain。

`PAUSED` 是可恢复状态，不是报告终态。即使已收集的部分结果存在 hard failure，PAUSE drain 也只能生成内存中的 PENDING snapshot，禁止绑定 JSON、Markdown 或 decision hash。只有 `resume` 完成剩余单元后才能进行最终封账；数据库绑定层会再次拒绝 `status=PAUSED` 或 `stop_intent=PAUSE`。

人工复核与报告：

```powershell
.\scripts\run_shadow_campaign.ps1 review -CampaignId <uuid>
.\scripts\run_shadow_campaign.ps1 report -CampaignId <uuid>
```

Review 终端会临时显示 answer、claim、citation URL 与 quote；数据库只保存固定 verdict/score/reason code，这些临时内容不会进入最终报告。

创建 Full Campaign 必须引用 24 小时内、同 identity 的 smoke PASS：

```powershell
.\scripts\run_shadow_campaign.ps1 create `
  -CampaignKey full-<stable-key> `
  -Profile shadow-full-v1 `
  -ParentSmokeCampaignId <smoke-uuid>
```

## 恢复原则

- create 输出丢失：可以用完全相同的 campaign key/profile/manifest/attestation 直接重放 `create`；CLI 生成的 retention deadline 是首写拥有的运维元数据，不参与 Campaign 身份 hash。也可以先执行 `list`，再按 Campaign ID 执行 `resume`；绝不能换 campaign key。
- Conversation/Message receipt 丢失：Runner 使用持久化 idempotency key 重放，不生成新 Conversation/SearchRun ID。
- 首次不确定 POST 会保留 ACTIVE reservation；一次恢复重放仍失败后，才按 frozen reserve 记 possibly-billed 并封账。
- 如新 Run 只因其他在途 Run 的 ACTIVE reservation 暂时无法准入，Runner 保留并续租当前 lease，等待 Collector 结算后重试；只有 observed/possibly-billed 不可逆总账本身已无法容纳一个完整 Run 时，才进入 BUDGET/CALL_CEILING stop。
- Collector provenance/lineage 永久失败会从 ModelInvocation audit 结算账本，标记 FAILED，并触发 FATAL drain。
- 报告 artifact 写入后绑定失败：再次执行 `report`，content-addressed artifact 与 decision hash 会收敛。

完整故障覆盖、测试映射和 Docker 证据见 `docs/operations/shadow-campaign-fault-matrix.md`。

## 停止容器

```powershell
.\scripts\run_shadow_campaign.ps1 down
```

该命令移除容器与 network，但保留 PostgreSQL、Redis、search artifact 与 Campaign report volume，便于审计和恢复。
