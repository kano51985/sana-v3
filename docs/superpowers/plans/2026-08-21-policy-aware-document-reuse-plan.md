# Sana 策略化跨运行文档复用实施计划

日期：2026-08-21

状态：架构自审通过，开始实施

对应设计：`docs/superpowers/specs/2026-08-21-policy-aware-document-reuse-design.md`

设计基线：`56bc24c docs: design policy aware document reuse`

## 1. 实施原则

- 使用 failing test -> minimal implementation -> focused regression -> commit 的顺序。
- domain policy、ports、SQL adapter、application orchestration 和 Collector 保持清晰边界。
- 不修改 Shadow gate 阈值、gold assertions、历史 Campaign 或人工 review 数据。
- 不新增数据库迁移；若实现中发现现有 schema 无法安全表达 lineage，停止并升级设计，不临时塞入无约束 JSON。
- 所有 metadata 使用 allowlist；测试和日志不得出现 secret、prompt、answer、query、quote 或网页正文。
- 每个逻辑阶段只暂存明确文件，提交前运行 `git diff --cached --name-only` 与 `git diff --cached --check`。

## 2. 任务 1：内容复用领域策略与端口

### 文件

```text
sana/modules/content/domain.py
sana/modules/content/ports.py
tests/test_modules/content/test_document_reuse_policy.py
```

### 测试先行

- strictest freshness 的单值和全组合矩阵。
- STABLE 24h/30d、RECENT 6h/7d、CURRENT 15m/2h 的边界。
- 等于窗口可用，超过一微秒不可用。
- fallback 小于 fresh、非正窗口、重复/未知 freshness 拒绝。
- naive/future fetched_at fail closed。
- live error allowlist 只接受 network、DNS transient、deadline、429、5xx。

### 实施

- 新增 ReuseFreshness、ReuseDecision、ReuseWindow、ReuseAssessment。
- 新增 ReusableContentSnapshot，字段只包含稳定 identity、raw ArtifactRef 与复用所需 HTTP metadata。
- 新增 DocumentReusePolicy.default() 与 settings 构造路径。
- 新增 ContentSnapshotReader 与 URLSafetyValidator protocols。
- 提供 raw body size/media/hash 的纯校验函数，复用 HTTP allowlisted media type 常量。

### 验证

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_modules/content/test_document_reuse_policy.py tests/test_modules/content
```

### 提交

```text
feat: add policy aware document reuse domain
```

## 3. 任务 2：tenant-scoped SQL snapshot reader

### 文件

```text
sana/platform/db/content_snapshots.py
sana/platform/db/__init__.py
tests/test_platform/db/test_content_snapshots.py
```

### 测试先行

- 同 tenant + 精确 url hash 返回最新成功 live Fetch。
- 不返回其他 tenant、不同 URL、FAILED Fetch、document-cache Fetch。
- 没有 DocumentVersionFetch 或 DocumentVersion 时不返回。
- raw storage URI/digest/media type 缺失时 fail closed。
- fetched_at 相同时使用 ID 稳定打破顺序。
- PostgreSQL RLS 下 tenant A 无法探测 tenant B。

### 实施

- 使用短 tenant-scoped UoW 查询 FetchArtifact -> DocumentVersionFetch -> DocumentVersion。
- 所有 join 同时约束 tenant；run binding 由 DocumentVersionFetch 的 composite FK 保证。
- 将 ORM row 映射为 ReusableContentSnapshot，不向 app 层泄漏 SQLAlchemy 类型。
- 构建 ArtifactRef 时校验 URI digest 与 FetchArtifact.content_hash 一致。

### 验证

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_platform/db/test_content_snapshots.py tests/test_platform/db/test_rls.py
```

### 提交

```text
feat: add tenant scoped reusable content reader
```

## 4. 任务 3：Fetch operation read-through 决策

### 文件

```text
sana/app/search_operations.py
tests/test_app/test_search_operations.py
tests/test_platform/security/test_ssrf.py
```

### 测试先行

- fresh cache 命中时 live fetcher 调用数为 0。
- miss/过 fresh window 时执行 live fetch。
- live success 优先于旧 fallback cache。
- network、DNS、deadline、429、5xx 在 fallback window 内使用 stale cache。
- 4xx、SSRF、unsupported media、oversize、empty body 不 fallback。
- cache artifact missing/corrupt/hash mismatch fail closed。
- request URL 与所有 redirect hop 在读取前重新 validate。
- unmapped Fact ID/unknown freshness 禁止缓存。
- CURRENT 超 2h 保留原 live error。
- fresh/live/stale 输出的 schema、decision、fetcher、degradation codes 和 metadata 正确。
- source fetched_at 保持不变，reused_at 单独记录。
- raw body 被复制到当前 Run artifact。

### 实施

- SearchStepOperations 注入 snapshot reader、URL validator、reuse policy 与 enabled flag。
- 从 plan/hit 解析 strictest freshness。
- 在 cache query 前 validate request URL；候选返回后 validate redirect hops。
- 单一 helper 执行 artifact read、digest/size/media 校验和 current-run copy。
- live Fetch 使用现有 bounded deadline。
- v2 output 保留 v1 读取字段，新增 cache metadata 与稳定 degradation code。

### 验证

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_app/test_search_operations.py tests/test_platform/security/test_ssrf.py
```

### 提交

```text
feat: add read through reuse to fetch operations
```

## 5. 任务 4：Worker settings、composition 与 Shadow config identity

### 文件

```text
sana/app/production_worker.py
deployment/docker-compose.shadow-eval.yml
docs/operations/search-platform.md
docs/operations/shadow-campaign.md
tests/test_app/test_production_worker.py
tests/test_deployment/test_shadow_eval_compose.py
```

### 测试先行

- 默认 policy 值与版本正确。
- 非正窗口、fresh > fallback、未知版本拒绝 Worker 启动。
- disabled 时不查询 snapshot reader。
- production composition 使用同一个 SSRFGuard 执行 cache prevalidation 与 live fetch。
- Shadow Compose 显式包含全部策略值，rendered config hash 随任一窗口变化。
- offline fixture 不读取 live cache 或 secret。

### 实施

- 新增八个 Worker settings 与 validation。
- composition root 创建 SSRFGuard、SqlContentSnapshotReader 与 DocumentReusePolicy。
- 使用轻量 adapter 将 SSRFGuard 暴露为 URLSafetyValidator。
- Shadow worker environment 固定 document-reuse-v1 与默认窗口。
- 运维文档说明策略、回滚和 attestation identity。

### 验证

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_app/test_production_worker.py tests/test_deployment/test_shadow_eval_compose.py
docker compose -p sana-shadow-eval -f deployment/docker-compose.shadow-eval.yml config --quiet
```

### 提交

```text
feat: configure document reuse in production worker
```

## 6. 任务 5：Fetch persistence、run-local lineage 与降级传播

### 文件

```text
sana/app/workflow_completion.py
tests/test_app/test_workflow_completion.py
```

### 测试先行

- LIVE v1/v2 output 继续持久化 `fetcher=http`。
- CACHE_FRESH/CACHE_STALE_IF_ERROR 持久化 `fetcher=document-cache`。
- response_bytes 为当前 raw body 长度。
- cache metadata 只包含 allowlisted keys。
- source fetch/run/version identity、source fetched_at、reused_at 和 age 正确。
- 当前 Run 创建独立 FetchArtifact 与 DocumentVersionFetch。
- fresh cache 不进入 pipeline degradation codes。
- stale fallback 的 `fetch_cache_stale_if_error` 最终进入 answer payload。
- 重复 Step completion 不产生重复 lineage。

### 实施

- `_persist_fetch` 按 output decision 写 fetcher、bytes 与 allowlisted metadata。
- coordinator 汇总成功 Fetch output 中的 degradation codes，并在 verify/synthesize 汇合点保留。
- 保持 DocumentVersion 唯一内容收敛，同时始终写当前 Run 的 DocumentVersionFetch。

### 验证

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_app/test_workflow_completion.py tests/test_app/test_search_operations.py
```

### 提交

```text
feat: persist cache fetch lineage and degradation
```

## 7. 任务 6：Collector projection 与门禁可观测性

### 文件

```text
sana/modules/shadow_campaign/collector.py
sana/platform/db/shadow_collector.py
sana/app/shadow_provenance.py
scripts/run_shadow_campaign.ps1
tests/test_modules/shadow_campaign/test_collector.py
tests/test_platform/db/test_shadow_collector.py
tests/test_evals/test_shadow_provenance.py
```

### 测试先行

- SourceFetch projection 不包含 URL、storage URI、body、prompt、answer、query 或 quote。
- fresh cache 不产生 degraded/provider transient。
- stale-if-error 产生 `fetch_cache_stale_if_error`、PROVIDER_TRANSIENT 和 degraded=true。
- live error category/code 只来自 metadata allowlist。
- malformed/unknown cache metadata fail closed 或成为 candidate defect，不静默忽略。
- source snapshot digest 对 fetch decision/age/source identity 敏感，对敏感文本不可见。
- collector schema version 与 attestation 同步更新。
- 现有 run-local DocumentVersionFetch lineage 继续通过。

### 实施

- 新增 SourceFetch value object 和 RunSourceSnapshot.fetches。
- SQL reader投影 FetchArtifact 的稳定非内容字段与 cache metadata。
- Collector 分类显式识别 stale cache provider transient；fresh 命中只计可观测数据。
- collector schema version 递增并更新 host attestation。

### 验证

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_modules/shadow_campaign/test_collector.py tests/test_platform/db/test_shadow_collector.py tests/test_evals/test_shadow_provenance.py
```

### 提交

```text
feat: audit document reuse in shadow collector
```

## 8. 任务 7：安全、并发与回归收口

### 文件

```text
tests/test_platform/storage/test_local_artifacts.py
tests/test_platform/security/test_ssrf.py
tests/test_platform/db/test_content_snapshots.py
tests/test_platform/db/test_shadow_collector.py
docs/operations/shadow-campaign-fault-matrix.md
```

### 场景

- source artifact 删除、截断、替换和 URI/digest 分歧。
- cache source redirect DNS rebinding。
- tenant A 使用 tenant B ArtifactRef。
- 两个 Run 并发复用同一 DocumentVersion，并各自产生 run-local FetchArtifact/DocumentVersionFetch。
- Step 重试与 completion 重放。
- metadata 注入 credential-like key/value、prompt、answer、query、quote。
- disabled rollback 路径恢复 live-only。

### 验证

```powershell
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m compileall -q sana tests scripts
git diff --check
git status --short
```

### 提交

```text
test: close document reuse security regressions
```

## 9. 任务 8：新镜像、Smoke 与 Full

### 前置

- worktree clean，全部实现提交已完成。
- Docker Desktop 可用。
- OCI revision 等于新 HEAD。
- DeepSeek/API/数据库 secret 只从安全环境或现有容器恢复，不输出值。

### 执行

```powershell
.\scripts\run_shadow_campaign.ps1 prepare
.\scripts\run_shadow_campaign.ps1 create -CampaignKey <new-smoke-key> -Profile docker-smoke-v1
.\scripts\audit_shadow_campaign.ps1 -CampaignId <smoke-id>
.\scripts\run_shadow_campaign.ps1 create -CampaignKey <new-full-key> -Profile shadow-full-v1 -ParentSmokeCampaignId <smoke-id>
.\scripts\audit_shadow_campaign.ps1 -CampaignId <full-id>
```

### 验收

- Smoke 6/6、FINAL_PASS、audit 7/7。
- Full 120/120，hard safety=0，critical gold 无失败。
- coverage macro >=80%，四个 mode/locale stratum >=70%。
- mode accuracy >=95%，unanswerable gap=100%。
- degraded <=12/120，audit 7/7。
- 自动门禁未通过时不进入人工 review，不删除失败 Campaign，不修改阈值。

## 10. 任务 9：人工复核与最终报告

自动 gate 全部通过后，由用户本人执行 exactly 20 个预选 review unit。AI 可以解释 rubric 和诊断 projection，但不得代替用户输入 verdict。

```powershell
.\scripts\run_shadow_campaign.ps1 review -CampaignId <full-id>
.\scripts\run_shadow_campaign.ps1 report -CampaignId <full-id>
.\scripts\audit_shadow_campaign.ps1 -CampaignId <full-id>
```

只有 final report 为 FINAL_PASS、decision/report digest 一致且审计通过后，才讨论候选 tag、main 整理或 release-ready 描述。
