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
3. 设置或在安全提示中输入以下值：
   - `SANA_SHADOW_OWNER_DB_PASSWORD`
   - `SANA_SHADOW_APP_DB_PASSWORD`
   - `DEEPSEEK_API_KEY`
   - `SANA_ACCESS_TOKEN`，本地 dev 模式格式为 `<tenant UUID>:<user UUID>`
4. 不要把上述值写入 `.env`、PowerShell history、issue、报告或聊天记录。

## 构建与隔离预检

```powershell
.\scripts\run_shadow_campaign.ps1 prepare
```

`prepare` 会拒绝 dirty worktree，并完成以下检查：

- build OCI revision 与 candidate commit 完全一致；
- 所有候选服务 image ID 相同；
- Alembic head 为 `0010_shadow_collector_audit`；
- API 只绑定 loopback，DB/Redis 无宿主端口；
- worker concurrency 固定为 2，队列固定为 fast/research/crawl/maintenance；
- 无非 Campaign active SearchRun、未发布 outbox 或初始队列消息；
- network、volume、resource limit 和 config hash 与 sanitized topology 一致；
- attestation 不含 secret、原始 Compose config、环境变量值或 Docker socket。

成功后 attestation 位于 `var/shadow-eval/attestation.json`。它只包含可公开复验的身份与计数。

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

- create 输出丢失：先执行 `list`，再按 Campaign ID 执行 `resume`；不要换 campaign key。
- Conversation/Message receipt 丢失：Runner 使用持久化 idempotency key 重放，不生成新 Conversation/SearchRun ID。
- 首次不确定 POST 会保留 ACTIVE reservation；一次恢复重放仍失败后，才按 frozen reserve 记 possibly-billed 并封账。
- Collector provenance/lineage 永久失败会从 ModelInvocation audit 结算账本，标记 FAILED，并触发 FATAL drain。
- 报告 artifact 写入后绑定失败：再次执行 `report`，content-addressed artifact 与 decision hash 会收敛。

## 停止容器

```powershell
.\scripts\run_shadow_campaign.ps1 down
```

该命令移除容器与 network，但保留 PostgreSQL、Redis、search artifact 与 Campaign report volume，便于审计和恢复。
