# Shadow Campaign 故障注入与离线门禁矩阵

日期：2026-08-15

范围：任务 11 离线故障注入与任务 12 专用 Docker Fake Provider smoke。本文只证明可恢复性、隔离、账本、血缘、隐私和报告收敛；不证明 DeepSeek live 质量，也不能授权生产放量。

## 准入结论

- 默认回归：`398 passed, 8 skipped`。8 个跳过项是需要显式宿主 PostgreSQL 测试 URL 的标记测试；相同 repository/collector/report 路径已在独立 Docker PostgreSQL 闭环中执行。
- Python 字节码、`git diff --check`、Compose config、迁移、API/dispatcher/worker health 均通过。
- 已验证 image：commit `3823f7e`，image ID `sha256:7d00da8343e1086ed8bb8acebaddaaa708170d055e7e741d59b4644c99bcd077`，attestation schema `shadow-provenance-v2`，execution class `OFFLINE_FIXTURE`。
- PAUSE/RESUME 最终 Campaign `1807ac6e-04af-407e-ba0b-000c49567cb5`：6 submitted、6 collected、6 个唯一 SearchRun、最大 submission attempt 1、0 Provider calls、0 token、0 cost、0 ACTIVE reservation。
- 最终 JSON decision hash `b7d0e52ddc23c3cbc071d3acc1529dcc00e186bfeb85806c9a2478348d575bce`；Markdown hash `3ff2ee050a3af25976671869d652f30123c6f652d50dd80bf4ed00f7ff02bbe6`。
- gate 为 `FAIL` 是离线 fixture 的 source/gold 语义结果，不是基础设施失败；离线 PASS/FAIL 都不得充当 live gate。

## 场景矩阵

| 故障场景 | 自动化覆盖 | Docker/持久化证据 | 验收结果 |
|---|---|---|---|
| Conversation response、Conversation binding、Message receipt、Run binding 后 Runner 进程丢失 | `tests/test_evals/test_shadow_runner_recovery.py::test_four_receipt_crash_windows_never_create_a_second_search_run` | Campaign `ea43a8a0-ac55-4b86-951f-bdc14fce1f39` 在 2 个 receipt 已提交、0 collected 时 kill runner；`resume` 后 6/6，6 个唯一 SearchRun，max attempt 1 | PASS：不创建第二个 Conversation/SearchRun |
| create commit 后响应丢失 | Campaign service idempotency test；CLI recovery tests | 同 key 的 create request hash 已移除客户端 wall-clock retention identity；首写 deadline 保留 | PASS：同 payload/key 可直接重放，payload 变化仍 409/invariant conflict |
| admission commit 后崩溃、ACTIVE reservation 恢复与 settlement/release 重放 | `tests/test_platform/db/test_shadow_campaign_repository.py::test_campaign_create_retry_and_lifecycle_are_atomic`；`tests/test_modules/shadow_campaign/test_budget.py` | 最终 isolated DB 全局 ACTIVE reservation=0；Campaign 与 Result/Invocation 账本反向核对为 0/0/0 | PASS：一次预留、一次结算或释放，不超卖 |
| 两个/多个 Runner 并发 claim | repository 的并发 `claim_next`/in-flight fencing；scheduler tests | 初次 Docker smoke 暴露 SUBMITTED 未计入 in-flight，修复后固定并发 2；后续 Campaign 均 6/6 | PASS：SUBMITTED+ACTIVE 与有效 lease 共同计入并发 |
| 并发 artifact/report 写入及 bind 前崩溃 | `tests/test_app/test_shadow_finalization.py::test_stale_input_rebuilds_and_artifact_writes_converge`、`test_concurrent_finalizers_and_later_reads_share_one_binding`；`tests/test_platform/storage/test_campaign_reports.py` | Campaign `531633a0-9fda-445c-8c46-84dd92463e80` 重跑 report 返回 duplicate=true，JSON/Markdown hash 不变 | PASS：content-addressed 写入、hash verify、并发绑定收敛 |
| PAUSE drain 中 kill，随后 resume | lifecycle/report/DB defensive binding tests；runner cancellation test | Campaign `1807ac6e-04af-407e-ba0b-000c49567cb5`：`RUNNING 2/0` kill → `STOPPING|PAUSE|2/0` → `PAUSED|2/2`、4 pending、gate PENDING、final fields UNBOUND → resume 6/6 | PASS：PAUSED 永不提前终结，剩余单元可恢复 |
| ABORT/FATAL/BUDGET/CALL_CEILING stop ordering | `tests/test_modules/shadow_campaign/test_lifecycle.py`、`test_budget.py`；`tests/test_platform/db/test_shadow_runner.py`、repository integration | stop intent 在 drain 前持久化；所有剩余单元 terminal/明确 skipped，最终无 ACTIVE reservation | PASS：停止新 claim 后 drain，硬停止不可回退为 PAUSE |
| collection/finalization 间 source 漂移 | `tests/test_platform/db/test_shadow_collector.py::test_collector_is_fenced_atomic_idempotent_and_rls_scoped`；`tests/test_modules/shadow_campaign/test_report.py::test_ledger_or_source_drift_is_a_fatal_gate_failure` | source snapshot digest 重验；stale input 重新构建，review 后漂移 fail closed | PASS：不覆盖已审结果，不用撕裂快照生成 PASS |
| PAUSED partial snapshot 同时含 hard source failure | `tests/test_modules/shadow_campaign/test_report.py::test_paused_campaign_with_hard_failure_still_remains_resumable`；`tests/test_platform/db/test_shadow_report.py::test_report_gateway_defensively_rejects_paused_final_binding` | 反例 Campaign `e1cfafbe-c207-41e3-b8c4-a883a499db03` 保留为不可改写审计证据；修复后当前 PAUSE Campaign final fields 保持 UNBOUND | PASS：PAUSE 优先于 hard-failure finalization guard |
| 跨 tenant/run Claim/Citation/Evidence、无 Fact factual Claim、非法 offset | collector/review PostgreSQL integration；`tests/test_modules/shadow_campaign/test_collector.py::test_invalid_citation_chain_is_a_fail_closed_candidate_defect`；schema/RLS tests | 四张 shadow 表 `relrowsecurity=true` 且 `relforcerowsecurity=true`；projection join 同时绑定 tenant/run | PASS：错链 fail closed，跨 tenant 不可见 |
| token/key/password/prompt/answer/query/quote 泄漏 | CLI serializer/redaction/provenance/privacy guard tests；post-Campaign auditor | API/dispatcher/worker logs 与 JSON/Markdown artifacts 扫描当前口令、token/key 和 40 个 manifest prompt，未命中；CLI bytes 只输出 byte length/hash | PASS：只输出稳定 code、计数、hash 和 PASS 断言 |
| worker health probe 超时/进程泄漏 | Compose structural test；post-Campaign auditor | Celery inspect 探针已替换为有界 direct-exec Redis PING；常驻进程严格为 main+2 prefork，采样时只允许至多一个命令行完全匹配的短生命周期探针 | PASS：无未知或累积 probe；Campaign/queue 审计另证实际消费能力 |
| 镜像/迁移/网络/volume 混用 | provenance/Compose tests；post-Campaign auditor | API/dispatcher/worker 与 attestation image ID 一致；唯一 head `0011_document_fetch_lineage`；DB/Redis 无宿主端口；独立 network/四个 volume | PASS：identity 或 topology 任一不一致均在 Provider 调用前失败 |

## 已发现并关闭的问题

1. 调度器最初只计算 claim lease，未把 `SUBMITTED + ACTIVE reservation` 算作在途，可能突破并发 2 并提前触发 call ceiling。现由数据库原子 claim 同时计算两类在途状态。
2. CLI 最初直接 JSON 序列化 datetime/Decimal/report bytes；现使用 allowlisted serializer，bytes 只输出长度和 SHA-256。
3. Windows volume 上并发 `os.replace` 可能产生 sharing race；现使用 striped lock、fsynced temp、atomic link/create-if-absent 与最终 hash verify。
4. Celery inspect healthcheck 会产生超时子进程或假阴性；现使用不派生子进程的 direct-exec Redis PING，并由 Campaign 审计证明消费能力。
5. PAUSED partial Campaign 遇到 hard source failure 时曾被提前绑定为 final FAIL；现 report 先执行 PAUSE guard，数据库 final binder 再做独立防御。
6. create request hash 曾包含客户端生成的 retention timestamp，破坏相同 key 的进程级重放；现 retention 为首写拥有的固定运维元数据，不参与 Campaign 身份。

## 重跑命令

```powershell
.\scripts\run_shadow_campaign.ps1 prepare -OfflineFixture
.\scripts\run_shadow_campaign.ps1 create `
  -OfflineFixture `
  -CampaignKey offline-fixture-<stable-key> `
  -Profile docker-smoke-v1
.\scripts\audit_shadow_campaign.ps1 `
  -CampaignId <uuid> `
  -OfflineFixture
```

审计器必须在 clean worktree、相同 HEAD 与相同 attestation 下运行。任何失败都阻断 live smoke；不得删除或改写失败 Campaign 来获得干净结果。
