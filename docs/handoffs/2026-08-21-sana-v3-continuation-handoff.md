# Sana v3 重构、迁移与发布门禁交接文档

> 更新时间：2026-08-21（Asia/Shanghai）
> 交接目的：让新的 Codex 任务或另一台电脑仅凭本文件即可恢复上下文并继续开发。
> 当前结论：私有 `sana-v3` 开发仓库已创建；架构迁移主体已完成，Smoke 已通过，最新 Full 被质量门禁正确拒绝；当前正在设计跨运行内容复用能力，尚未获用户对该具体设计的最终批准，尚未开始实现。

## 0. 给接续任务的强制执行摘要

接续任务开始后，请先完整阅读本文件，再阅读第 15 节列出的四份核心设计/计划和两份运维文档。不要从头重做已经完成的架构迁移。

必须遵守以下约束：

1. 当前目标不是“让测试看起来通过”，而是让真实 DeepSeek Live Shadow Full 达到既定发布门禁。
2. 不得降低门禁阈值、删除失败 Campaign、修改历史报告或伪造 20 条人工复核。
3. 不得把 API key、数据库密码、本地访问 token、完整 prompt、answer 或 evidence quote 输出到终端日志、Git、Campaign 报告或聊天。
4. 当前继续使用同一个 DeepSeek API 执行 planner、verifier、synthesizer；用户电脑暂时不能运行本地小模型。生产模型切换测试延后。
5. 用户已授权架构负责人在需要选择时直接采用前沿 AI 系统架构思路做决定，但本轮使用的 brainstorming 技能对“跨运行缓存行为变更”设置了显式设计批准门。因此实施前仍要得到用户一句明确批准。
6. 用户允许大幅改造架构和界面；当前阶段先完成后端质量与发布门禁，不要被 Streamlit/UI 重做分散注意力。
7. 远程仓库 `sana-v3` 是私有开发迁移仓库，不代表生产发布。只有 Full 自动门禁通过、20 条人工复核真实完成、最终报告审计通过后，才可宣称 release-ready。

建议接续任务的第一条回复：

```text
我已完整读取 2026-08-21 Sana v3 交接文档。当前阶段是“Live Full 失败后的跨运行抓取韧性设计审批”，不是重新规划整体架构。最新 Smoke 通过，Full 因 12 个关键金标、72.91% 覆盖和 42/120 降级失败。下一步先确认策略化 Read-through Cache 设计，再写设计文档、实施、重建镜像并重跑 Smoke/Full。
```

## 1. 当前仓库与分支拓扑

### 1.1 本机工作树

| 用途 | 路径 | 分支 | 提交 |
|---|---|---|---|
| 较早的迁移工作树 | `D:\MyProduct\sana_v2` | `codex/search-quality-migration` | `3521bc8` |
| 当前发布门禁工作树 | `D:\MyProduct\sana_v2-shadow-campaign` | `codex/shadow-campaign-release-gate` | 本文提交前为 `25f5d97` |

所有继续开发应在发布门禁工作树或从远程 `sana-v3` 克隆的新工作树中进行。不要在旧工作树上重复或交叉编辑同一分支。

### 1.2 Git 谱系

- 当前分支相对 `sana_v2` 的 `origin/main`：落后 0，领先 80 个提交。
- 差异规模：344 个文件，约 64,518 行新增、35 行删除。
- 当前发布门禁分支完全包含 `codex/search-quality-migration`；二者关系为旧迁移分支领先 0、当前分支再领先 49 个提交。
- 当前迁移保留 v2 的完整 Git 历史，不是一次无历史复制。

### 1.3 远程策略

交接完成时的状态：

- `origin` 指向 `https://github.com/kano51985/sana_v2.git`。
- `v3` 指向私有 `https://github.com/kano51985/sana-v3.git`。
- GitHub CLI 已重新登录 `kano51985`，具有 `repo` 权限。
- GitHub 私有仓库 `kano51985/sana-v3` 已创建。

采用的推送策略：

1. 本机保留 `origin` 指向 v2，使用单独 remote `v3`，避免影响另一个 worktree。
2. 将交接提交推送到 `v3/main` 作为可迁移的开发基线，同时推送 `v3/codex/shadow-campaign-release-gate` 作为继续工作分支。
3. 不创建 release tag，不宣称门禁通过。

回宿舍后的推荐恢复命令：

```powershell
git clone https://github.com/kano51985/sana-v3.git
Set-Location sana-v3
git fetch --all --prune
git switch codex/shadow-campaign-release-gate
git status --short --branch
```

如果默认只拉到了 `main`：

```powershell
git switch --track -c codex/shadow-campaign-release-gate origin/codex/shadow-campaign-release-gate
```

## 2. 为什么这是 v3，而不是继续覆盖 v2

当前代码已经从 v2 的单机脚本/界面应用演进为完整的平台内核，包含：

- 多租户 identity、conversation、message、response run 数据模型。
- PostgreSQL 真相源、Row-Level Security、强制 RLS 和 tenant-scoped Unit of Work。
- Durable SearchRun / SearchStep / StepAttempt 状态机。
- Redis Streams、outbox、Celery、崩溃恢复和 reconciliation。
- FAST / RESEARCH 自动路由、预算与软/硬 deadline。
- direct source、Bing RSS、SearXNG 接口和 circuit breaker。
- HTTP-first 内容抓取、逐跳 SSRF 防护、内容寻址 artifact。
- FactRequirement、EvidenceCandidate、VerifiedEvidence、Claim、Citation 完整证据链。
- DeepSeek planner/verifier/synthesizer 统一模型网关、结构化输出和调用审计。
- Shadow Campaign 调度、预算账本、collector、人工复核、不可变报告和 release gate。
- 独立 Docker Shadow 环境、attestation、镜像绑定、隐私扫描和后置审计。

因此最佳策略是：

- 保留 `sana_v2` 作为迁移源、历史参照和回滚基线。
- 使用私有 `sana-v3` 作为新平台的持续开发仓库。
- 保留完整历史，避免失去 blame、设计决策和迁移提交。
- 暂不把 v3 的 `main` 解释为生产发布；生产放量由 Shadow Gate 决定。

## 3. 阶段总览

| 阶段 | 目标 | 当前状态 | 退出条件 |
|---|---|---|---|
| 0 | 审阅 v2 与原搜索架构计划 | 已完成 | 新平台设计获得确认 |
| 1 | 多租户、持久化、状态机、API 平台底座 | 已完成 | 单元/集成测试与 Docker 基础验证通过 |
| 2 | 搜索、证据、DeepSeek 质量流水线 | 已完成基础架构 | 真实来源质量由后续 Shadow Gate 持续验收 |
| 3 | Shadow Campaign 发布门禁系统 | 已完成 | 离线故障矩阵、不可变报告、审计链通过 |
| 4 | Live 证据质量强化 | 已完成多轮 | Smoke 通过；Full 仍暴露抓取韧性缺口 |
| 5 | Live Smoke / Full Release Gate | 进行中 | Full 自动门禁通过 |
| 6 | 策略化跨运行内容复用 | 等待设计批准 | 设计、计划、实现、测试和新镜像完成 |
| 7 | 20 条真实人工复核 | 未开始 | 用户真实提交 20 条 rubric 评分 |
| 8 | v3 候选发布与远程稳定化 | WIP 仓库可先创建 | Final PASS、审计通过、稳定分支/标签 |
| 9 | 生产模型切换与放量 | 延后 | 独立生产模型评测与分阶段 rollout |

## 4. 阶段 0：v2 审阅与架构设计

### 已完成

- 审阅用户最初位于 `C:\Users\Administrator\Downloads\search-architecture-refactor-plan.md` 的设计思路。
- 选择模块化单体 + ports/adapters + durable orchestration，而不是过早拆微服务。
- 明确 PostgreSQL 是真相源，Redis 只承担队列/事件和可重建缓存。
- 将 Streamlit 降级为 API client，不允许 UI 直接持有核心业务和数据库真相。
- 明确 search planning、discovery、content、evidence、answer、orchestration、platform adapter 边界。

### 核心文档

- `docs/superpowers/specs/2026-08-14-sana-multi-user-search-platform-design.md`
- `docs/superpowers/plans/2026-08-14-sana-multi-user-search-platform-plan.md`

### 状态

完成。不要重新争论“是否改成微服务”；当前规模继续使用模块化单体更利于一致性、调试和迁移。

## 5. 阶段 1：多租户平台底座

### 已完成模块

- `sana/modules/identity`
- `sana/modules/conversation`
- `sana/modules/orchestration`
- `sana/platform/db`
- `sana/platform/events`
- `sana/platform/queue`
- `sana/app/api`
- `sana/clients/streamlit`

### 已完成能力

- tenant/user/conversation/message/search run 完整身份绑定。
- RLS 与 `FORCE ROW LEVEL SECURITY`。
- 原子提交、幂等键、payload hash 冲突检查。
- lease fencing、step attempt、outbox、duplicate delivery 防护。
- SSE resume、API auth、本地开发 bootstrap。
- Mongo/Chroma/user profile 旧数据迁移读取器与 migration ledger。
- API、worker、dispatcher、PostgreSQL、Redis、Streamlit Docker 编排。

### 关键迁移

Alembic 从 `0001_identity_conversation` 演进到 `0012_fetch_run_binding`，当前要求唯一 head。

### 状态

完成并已被后续 Shadow Campaign 重复使用。

## 6. 阶段 2：搜索与 DeepSeek 质量流水线

### 已完成模块

- `sana/modules/search_planning`
- `sana/modules/discovery`
- `sana/modules/content`
- `sana/modules/evidence`
- `sana/modules/answer`
- `sana/modules/model_gateway`
- `sana/platform/search`
- `sana/platform/fetch`
- `sana/platform/models`
- `sana/app/search_operations.py`
- `sana/app/workflow_completion.py`

### 关键行为

- FAST 与 RESEARCH 自动路由；用户提示中的聊天污染词不会进入检索查询。
- Reviewed intent template 为稳定/高风险问题提供经过审阅的 Fact 计划。
- Direct source policy 优先第一方来源。
- Candidate selector 保持 fact-bound 映射并限制候选预算。
- Deterministic verifier 只在显式、可解析的一方页面结构上运行。
- Model verifier 对弱证据、过期 current 页面、无法由单片段证明的“缺失事实” fail closed。
- Claim/Citation 必须绑定 run、fact、document version、chunk 和精确 offset。
- 模型调用通过统一 gateway 记录 reservation、attempt、token、cost 和可能计费状态。

### 核心文档

- `docs/superpowers/specs/2026-08-15-deepseek-quality-stage-design.md`
- `docs/superpowers/plans/2026-08-15-deepseek-quality-stage-plan.md`
- `docs/operations/search-platform.md`
- `docs/pipeline-flow.md`

## 7. 阶段 3：Shadow Campaign 发布门禁

### 已完成能力

- 固定 40 个 case，Full 每个重复 3 次，共 120 次。
- Smoke 固定 6 个代表性 case。
- 版本化 manifest、profile、gate policy、review rubric、cost rate。
- Campaign 创建幂等、parent Smoke 绑定、candidate image/commit/config attestation。
- 固定并发、provider call admission ceiling、structural ceiling、预算 reservation。
- crash-safe runner、fenced collector、pause/resume、abort、drain。
- 20 条确定性人工复核抽样；评分必须由人真实输入。
- JSON/Markdown 内容寻址报告、decision input hash、decision hash。
- 后置审计：唯一性、账本、容器身份、RLS、血缘、报告、隐私。

### 核心文档

- `docs/superpowers/specs/2026-08-15-shadow-campaign-release-gate-design.md`
- `docs/superpowers/plans/2026-08-15-shadow-campaign-release-gate-plan.md`
- `docs/operations/shadow-campaign.md`
- `docs/operations/shadow-campaign-fault-matrix.md`

### 已验证的离线能力

- create response loss、runner crash、reservation settlement、并发 claim。
- artifact/report 并发写入。
- pause drain、resume、abort/fatal/budget/call ceiling。
- source snapshot drift、跨 tenant/run 错链、RLS。
- secret/prompt/answer/query/quote 泄漏扫描。
- worker health probe 进程泄漏、镜像/迁移/network/volume 混用。

## 8. 阶段 4：Live 证据质量强化

当前分支在 `3521bc8` 之后又增加 49 个提交，主要处理真实 Live Campaign 暴露的问题。

### 已完成的代表性修复

- authoritative source routing 与 cost admission。
- direct source 覆盖与中英文 locale routing。
- fast source failover 与 fast hedge verification。
- explicit official value deterministic verification。
- query pollution、plan completeness、fact-bound evidence lineage。
- HTTP 方法属性、201/204 拆分事实、Git object purpose。
- reviewed evidence synthesis、budget pressure fail closed。
- reviewed intent fast path、candidate budget 内 definition ranking。
- source trust scoping。
- 当前版本运行时解析：Python、Rust、Node.js、Git、PostgreSQL、DeepSeek pricing、Apex。
- Apex Bloodhound、地图轮换、ranked changes、community composition。
- 未公开 codename、private weights、全球实例精确总数、universal composition 等证据缺口模板。
- current 页面 freshness gating 与 single excerpt absence 拒绝。

### 当前 HEAD（交接文档提交前）

```text
25f5d97 harden current evidence and answer gaps
aff9d1c scope reviewed plans to trusted direct sources
e5ccafa rank reviewed definitions within candidate budget
84c8927 add reviewed intent evidence fast path
837b10f harden planning and deterministic evidence coverage
cabc5c0 fail closed on weak evidence and defer budget pressure
de47a00 harden reviewed evidence synthesis
25595b7 support split HTTP method facts
```

### 最近一次完整本地测试基线

- `548 passed, 8 skipped`
- Python `compileall` 通过。
- `git diff --check` 通过。
- 8 个 skip 是需要显式宿主 PostgreSQL 测试 URL 的标记测试；相同核心 repository/collector/report 路径已在 Docker PostgreSQL 中覆盖。
- 环境未安装 ruff/black，因此没有声称执行过它们。

## 9. 当前 Docker 与镜像身份

### Docker project

```text
sana-shadow-eval
```

### 当前候选镜像

```text
image id/digest: sha256:79067dc0512266c111d6b39c826bcf5009d8613f6f3a6e3ef12de28a1da56c0e
OCI revision:     25f5d97a041b182ab4e0aa64affc3e95758341df
```

### 当前 attestation

```text
var/shadow-eval/attestation.json
```

### Docker Desktop 注意事项

- Windows 本地账户 `CodexSandboxOffline` 已加入 `docker-users`。
- 加组后必须完全重启 Codex Desktop 和 Docker Desktop，使令牌与 pipe ACL 生效。
- Docker Desktop 应普通启动，不要依赖不安全的 `tcp://localhost:2375`。
- Docker Desktop 重启后，Shadow PostgreSQL/Redis 有时不会自动启动；恢复现有容器即可，不要删除 volume。

恢复命令：

```powershell
docker start sana-shadow-eval-postgres-1 sana-shadow-eval-redis-1
docker restart sana-shadow-eval-api-1 sana-shadow-eval-worker-1 sana-shadow-eval-dispatcher-1
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18000/health/ready
```

预期 health body：

```json
{"status":"ok","checks":{"postgresql":"ok","redis":"ok"}}
```

## 10. 当前门禁证据

### 10.1 最新 Smoke：通过

```text
Campaign ID:        82b92d99-b6d8-4841-bf0c-8e95a2db2e41
Profile:            docker-smoke-v1
Execution class:    LIVE_DEEPSEEK
Runs:               6/6
Failed:             0
Degraded:           0
Automatic gate:     PASS
Gate:               PASS
Decision state:     FINAL_PASS
Decision hash:      dcfc1b846aaac23df629d162e303fc91e8885d2669e826fd051e19b7e4deb84b
```

Smoke 后置审计 7/7：

```text
result_run_uniqueness=PASS (6/6)
ledger_and_idle_state=PASS
image_and_worker_health=PASS
force_rls=PASS
run_local_fetch_lineage=PASS
report_integrity=PASS
privacy_scan=PASS
```

### 10.2 最新 Full：质量门禁失败

```text
Campaign ID:         28b0d1f9-e893-42f2-8ef6-2a06622f5ca5
Parent Smoke:        82b92d99-b6d8-4841-bf0c-8e95a2db2e41
Profile:             shadow-full-v1
Execution class:     LIVE_DEEPSEEK
Runs:                120/120
Failed runs:         0
Degraded runs:       42
Automatic gate:      FAIL
Manual review:       PENDING（自动门禁已失败，不应进行人工评分）
Gate:                FAIL
Decision state:      FINAL_FAIL
Decision input hash: c1c20fa0b9bc5f10ee5647212ef2f59521d09534b716d616c4934b00d7520157
Decision hash:       72559fe6328a97cc572bb3e4788c394c4d0731dd83437b148b63b01c5a494c59
```

完整 Full 后置审计仍为 7/7 PASS。这证明失败是候选质量问题，不是基础设施、账本、血缘、隐私或报告损坏。

### 10.3 Full 质量指标

| 指标 | 观察值 | 门槛 | 结果 |
|---|---:|---:|---|
| Hard safety critical failures | 12 | 0 | FAIL |
| Critical gold | 36/48，75.00% | >=95%，且 critical hard fail 必须 0 | FAIL |
| Coverage macro | 72.91% | >=80% | FAIL |
| FAST / zh-CN coverage | 83.33% | >=70% | PASS |
| FAST / en coverage | 54.16% | >=70% | FAIL |
| RESEARCH / zh-CN coverage | 72.91% | >=70% | PASS |
| RESEARCH / en coverage | 81.25% | >=70% | PASS |
| Mode accuracy | 100% | >=95% | PASS |
| Unanswerable gap | 100% | 100% | PASS |
| Degraded | 42/120，35.00% | <=12/120，10% | FAIL |
| Traceability violations | 0 | 0 | PASS |
| Ledger mismatch | 0 | 0 | PASS |
| Observed provider calls | 0 | 受预算约束 | PASS |
| Possibly billed call charge | 16 | 被账本跟踪 | 仅审计信息 |

错误信号汇总可能在同一 run 上重叠，因此下列总数不能与 42 简单相加：

```text
CANDIDATE_DEFECT:   39
CONTENT_GAP:         8
PROVIDER_TRANSIENT: 19
```

### 10.4 12 个关键金标失败

| Case | Repetition | 失败阶段 | 直接表现 |
|---|---:|---|---|
| fast-en-01-http-get | 1, 2, 3 | FETCH / VERIFY | safe 与 idempotent 两事实均缺失 |
| fast-en-02-dns-port | 1, 2, 3 | FETCH / VERIFY | 53、TCP、UDP 均缺失 |
| fast-zh-02-python-origin | 2, 3 | FETCH | Guido 有证据，1991 缺失 |
| research-en-04-sql-isolation | 3 | FETCH | 四种 isolation 全部缺失 |
| research-zh-01-git-objects | 3 | FETCH | 四对象及用途全缺失 |
| research-zh-02-http-201-204 | 2 | FETCH | 201/204 四事实全缺失 |
| research-zh-04-sqlite-public-domain | 3 | FETCH | public domain 与替代许可两事实缺失 |

这些 case 的其他 repetition 多数能成功，证明 deterministic verifier 和模板本身并非整体失效。

### 10.5 已确认的共因

StepAttempt 证据明确显示：

```text
fetch_network_failure
fetch_deadline_exceeded
```

相同官方 URL 在同一租户、同一 Campaign 的其他 repetition 已经成功抓取并形成 `DocumentVersion`，但失败 repetition 仍从零发起外网请求，未复用仍新鲜、内容摘要已验证的版本。

这造成：

1. 稳定事实受短暂网络抖动影响。
2. 同一 Full Campaign 重复下载相同官方页。
3. Fetch deadline 耗尽后没有安全、可追踪的同租户 fallback。
4. 结果跨 repetition 不稳定，直接击穿 critical gold 和 coverage。

因此下一项架构工作不是继续扩展 reviewed template，而是增加策略化跨运行内容复用。

## 11. 上一个 Full 的对照证据

较早 Full：

```text
Campaign ID:     b13bc54e-4f90-4f8e-ba49-fd57d14e910b
Status:          AWAITING_REVIEW
Runs:            120/120
Failed:          0
Degraded:        28
Gold:            48/48（当时的 interim report）
Coverage macro:  79.17%
FAST/en:         62.50%
```

该 Full 自动指标仍未全部过门槛，且没有完成真实 20 条人工复核，不能作为发布通过证据。它只用于比较：Live 外网抓取导致结果具有明显批次波动。

## 12. 阶段 6：拟议的策略化 Read-through Cache

### 12.1 当前审批状态

已完成问题定位、现有代码边界审查和方案比较。用户尚未对下面的具体方案回复“批准”，因此：

- 尚未创建该功能的正式设计文档。
- 尚未修改 Fetch 行为。
- 尚未写实现计划。
- 尚未产生相关代码提交。

### 12.2 已比较方案

#### 方案 A：只增加 timeout/retry

优点：改动小。
缺点：继续依赖外网、拉长 latency、增加目标站负担、仍无法跨进程/重启复用。
结论：不采用。

#### 方案 B：无条件 cache-first

优点：稳定、简单、便宜。
缺点：current/version/pricing/meta 可能返回过时数据，无法证明 freshness。
结论：不采用。

#### 方案 C：策略化 Read-through Cache（推荐）

读取同租户、同 canonical URL 的最近成功且已提取版本；根据最严格的 Fact freshness 决定 fresh reuse 与 stale-if-error。

建议默认窗口：

| Fact freshness | 直接视为 fresh | 网络故障最大 fallback 年龄 |
|---|---:|---:|
| STABLE | 24 小时 | 30 天 |
| RECENT | 6 小时 | 7 天 |
| CURRENT | 15 分钟 | 2 小时 |

这些值必须版本化、可配置并进入 candidate config hash/attestation，不能作为散落 magic number。

### 12.3 安全边界

1. 只允许同 tenant。
2. 必须 canonical URL hash 精确匹配。
3. 使用最严格的 mapped Fact freshness；CURRENT 优先于 RECENT，RECENT 优先于 STABLE。
4. 读取前仍执行 URL/host SSRF 校验；DNS 重新绑定到私网时 fail closed。
5. artifact 读取必须重新验证 SHA-256。
6. 原始 body 仍需满足当前 max response bytes 和 allowlisted media type。
7. 只允许以下 live 错误 stale-if-error：网络异常、timeout、429、5xx。
8. 禁止对 SSRF、4xx、unsupported media、oversize、empty body、hash corruption 回退。
9. CURRENT 超过 2 小时即使断网也必须报告 evidence gap，不得伪装成当前值。
10. fresh cache reuse 不计 degraded；超过 fresh window 的 stale-if-error 必须标记 degraded 和稳定 reason code。

### 12.4 数据流

推荐新增清晰端口，例如：

```text
ContentSnapshotReader / DocumentReusePort
```

推荐数据流：

```text
FETCH step
  -> 从 plan 计算最严格 freshness
  -> SSRF validate 目标 URL
  -> 查询 tenant-scoped latest reusable fetch/document version
  -> 若在 fresh window：读取并校验 artifact，直接产生 CACHE_FRESH fetch output
  -> 否则执行 live HTTP fetch
       -> live success：产生 LIVE fetch output
       -> eligible transient/deadline failure 且 cache 在 fallback window：CACHE_STALE_IF_ERROR
       -> 其他情况：保留原 TypedError，fail closed
  -> EXTRACT/VERIFY/SYNTHESIZE 沿用既有流水线
```

### 12.5 血缘要求

不得直接把旧 Run 的 evidence 当成新 Run evidence。

每次复用仍应：

- 创建本 Run 独立的 `FetchArtifact`。
- `fetcher` 标记为 `document-cache` 或等价 allowlisted 值。
- `fetch_metadata` 记录：cache policy version、source fetch artifact ID、source run ID、source fetched_at、reuse age、decision、live error code（不得记录原始敏感异常）。
- 重新执行 extract/chunk/select/verify。
- 创建本 Run 的 `DocumentVersionFetch` 绑定。
- collector 继续验证 run-local fetch lineage。
- 不伪造原始 `fetched_at`；复用发生时间放 metadata。

现有 `LocalArtifactStore` 已允许同 tenant 跨 run 读取，并会验证 URI tenant 与 digest；现有 `DocumentVersionFetch` 已能表达新 Run 到旧 content version 的绑定，因此预期不需要数据库迁移。

### 12.6 测试要求

至少覆盖：

1. freshness policy 的 strictest-fact 矩阵。
2. fresh cache 跳过网络。
3. cache 过 fresh window 时 live success 优先。
4. retryable network/deadline/429/5xx 使用 stale-if-error。
5. CURRENT 超 fallback 上限 fail closed。
6. 4xx、SSRF、content type、oversize、empty body 不回退。
7. artifact 缺失/摘要损坏 fail closed。
8. tenant A 无法读取 tenant B cache。
9. canonical URL 不同不得复用。
10. 并发相同 URL 结果幂等。
11. 新 Run `FetchArtifact` 与 `DocumentVersionFetch` 血缘完整。
12. collector/audit 的 run-local lineage 仍通过。
13. cache metadata 不含 secret、prompt、answer、query 或 quote。
14. 现有 HttpContentFetcher、workflow completion、shadow collector 回归测试。
15. 完整单元套件、Docker Smoke、Smoke audit、Full、Full audit。

### 12.7 下一条需要用户确认的问题

```text
是否批准按方案 C（策略化 Read-through Cache）实施，并采用 STABLE 24h/30d、RECENT 6h/7d、CURRENT 15m/2h 的版本化默认窗口？
```

## 13. 批准后的精确工作顺序

### Step 1：正式设计文档

创建：

```text
docs/superpowers/specs/2026-08-21-policy-aware-document-reuse-design.md
```

必须包含：目标、非目标、端口、adapter、策略、数据流、RLS、SSRF、integrity、lineage、telemetry、failure matrix、测试、rollout/rollback。

完成后进行 placeholder、矛盾、歧义和范围自审，并单独提交设计文档。

注意：当前环境提供 brainstorming 技能，但没有列出它要求的 `writing-plans` 技能。接续任务应先检查技能列表；若仍不存在，明确说明缺失并使用同等详细的手工计划作为 fallback。

### Step 2：实现计划

建议拆分：

1. domain policy 与 reuse candidate 类型。
2. tenant-scoped SQL read adapter。
3. artifact integrity 读取。
4. Fetch operation 决策与 output schema v2。
5. completion persistence 与 metadata allowlist。
6. telemetry/error/degraded mapping。
7. unit/integration/security tests。
8. docs 与 Shadow policy snapshot。

### Step 3：实现

预计主要文件：

```text
sana/modules/content/domain.py
sana/modules/content/ports.py
sana/app/search_operations.py
sana/app/workflow_completion.py
sana/app/production_worker.py
sana/platform/db/...（新增小型 adapter，避免继续膨胀大 repository）
sana/platform/db/models/search.py（预期无需 schema 改动）
tests/test_app/test_search_operations.py
tests/test_app/test_workflow_completion.py
tests/test_platform/db/...
tests/test_platform/storage/...
tests/test_platform/security/...
docs/operations/search-platform.md
docs/operations/shadow-campaign.md
```

保持模块边界：policy 属于 content domain，SQL 查询属于 platform adapter，编排决策属于 app operation。

### Step 4：本地验证

```powershell
D:\MyProduct\sana_v2\venv\Scripts\python.exe -m pytest -q
D:\MyProduct\sana_v2\venv\Scripts\python.exe -m compileall sana tests
git diff --check
git status --short
```

不要声称运行未安装的 ruff/black。

### Step 5：镜像与 Smoke

1. 确保 worktree clean。
2. 构建候选镜像，OCI revision 必须等于新 HEAD。
3. 重新生成 `var/shadow-eval/attestation.json`。
4. 使用全新 Campaign key 创建 Smoke。
5. Smoke 必须 `FINAL_PASS`。
6. 运行 `audit_shadow_campaign.ps1`，必须 7/7 PASS。

### Step 6：Full

1. Full 必须绑定新 Smoke ID/hash。
2. 必须使用同一 image/config/commit identity。
3. 收齐 120/120 后检查：
   - hard safety = 0
   - critical gold 不触发 hard fail
   - coverage macro >= 8000 bps
   - 四个 stratum 每个 >= 7000 bps
   - mode accuracy >= 9500 bps
   - unanswerable gap = 10000 bps
   - degraded <= 12
4. Full audit 必须 7/7 PASS。

### Step 7：人工复核

只有自动指标全部通过后才进入：

```powershell
.\scripts\run_shadow_campaign.ps1 review -CampaignId <new-full-campaign-id>
```

必须由用户真实查看 20 条抽样并输入 rubric。AI 不得代替用户假装完成评分。

### Step 8：最终报告与稳定化

- 再次生成/读取 final report。
- 核对 decision hash、JSON/Markdown digest。
- 再跑后置审计。
- 只有 `FINAL_PASS` 才可创建候选 release tag 或把 v3 描述为 release-ready。

## 14. Shadow 命令与密钥安全

### 14.1 原则

- 不把 secret 写入 `.env`、Git 或命令输出。
- 可以通过 PowerShell `Read-Host -MaskInput` 在当前 shell 设置本地环境变量。
- 不在聊天中粘贴 token。
- audit 输出只能包含稳定 ID、hash、计数和 PASS/FAIL。

### 14.2 本地输入

```powershell
$env:DEEPSEEK_API_KEY = Read-Host 'DeepSeek API key' -MaskInput
$env:SANA_ACCESS_TOKEN = Read-Host 'Local Sana token (tenant UUID:user UUID)' -MaskInput
```

数据库 owner/app 密码应使用本机密码管理器或新环境生成，不能复用聊天记录中的值。

### 14.3 从已运行容器恢复当前环境变量但不打印

```powershell
function Get-ContainerEnvValue([string]$Container, [string]$Name) {
    $prefix = "$Name="
    $entry = docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' $Container |
        Where-Object { $_.StartsWith($prefix) } |
        Select-Object -First 1
    if (-not $entry) { throw "Required container environment key is unavailable: $Name" }
    $entry.Substring($prefix.Length)
}

$env:SANA_SHADOW_OWNER_DB_PASSWORD = Get-ContainerEnvValue `
    'sana-shadow-eval-postgres-1' 'POSTGRES_PASSWORD'

$databaseUri = [Uri](Get-ContainerEnvValue `
    'sana-shadow-eval-api-1' 'SANA_DATABASE_URL')
$userInfo = [Uri]::UnescapeDataString($databaseUri.UserInfo)
$separator = $userInfo.IndexOf(':')
if ($separator -lt 0) { throw 'Shadow app database credential is malformed' }
$env:SANA_SHADOW_APP_DB_PASSWORD = $userInfo.Substring($separator + 1)

$env:DEEPSEEK_API_KEY = Get-ContainerEnvValue `
    'sana-shadow-eval-worker-1' 'DEEPSEEK_API_KEY'
```

访问 token 可从当前 Shadow DB 的 tenant/user 身份生成，但命令不得打印 token；将输出直接赋给 `$env:SANA_ACCESS_TOKEN`。

### 14.4 标准脚本

```powershell
.\scripts\run_shadow_campaign.ps1 prepare

.\scripts\run_shadow_campaign.ps1 create `
    -CampaignKey '<new-unique-smoke-key>' `
    -Profile docker-smoke-v1

.\scripts\audit_shadow_campaign.ps1 `
    -CampaignId '<smoke-campaign-id>'

.\scripts\run_shadow_campaign.ps1 create `
    -CampaignKey '<new-unique-full-key>' `
    -Profile shadow-full-v1 `
    -ParentSmokeCampaignId '<smoke-campaign-id>'

.\scripts\audit_shadow_campaign.ps1 `
    -CampaignId '<full-campaign-id>'
```

Campaign key 必须新建，不能复用不同 payload 的旧 key。

## 15. 接续任务必须阅读的文档

按顺序：

1. `docs/superpowers/specs/2026-08-14-sana-multi-user-search-platform-design.md`
2. `docs/superpowers/plans/2026-08-14-sana-multi-user-search-platform-plan.md`
3. `docs/superpowers/specs/2026-08-15-deepseek-quality-stage-design.md`
4. `docs/superpowers/specs/2026-08-15-shadow-campaign-release-gate-design.md`
5. `docs/superpowers/plans/2026-08-15-shadow-campaign-release-gate-plan.md`
6. `docs/operations/search-platform.md`
7. `docs/operations/shadow-campaign.md`
8. `docs/operations/shadow-campaign-fault-matrix.md`
9. 本文件。

如果时间有限，至少阅读本文件、Shadow Campaign design、`search-platform.md` 和 `shadow-campaign.md`。

## 16. 关键文件索引

### 业务/应用

```text
sana/app/search_operations.py
sana/app/workflow_completion.py
sana/app/production_worker.py
sana/app/sql_step_execution.py
sana/app/shadow_runner.py
sana/app/shadow_report.py
```

### 内容与证据

```text
sana/modules/content/domain.py
sana/modules/content/ports.py
sana/modules/content/fetch_strategy.py
sana/modules/evidence/candidate_selector.py
sana/modules/evidence/model_verifier.py
sana/modules/evidence/coverage.py
sana/modules/search_planning/reviewed_templates.py
sana/modules/discovery/official_sources.py
```

### 平台 adapter

```text
sana/platform/fetch/http_fetcher.py
sana/platform/storage/local_artifacts.py
sana/platform/db/models/search.py
sana/platform/db/uow.py
sana/platform/db/shadow_collector.py
sana/platform/db/shadow_report.py
```

### Shadow assets/scripts

```text
evals/shadow/cases-v1.jsonl
evals/shadow/profiles-v1.json
evals/shadow/gate-policies-v1.json
evals/shadow/review-rubric-v1.json
deployment/docker-compose.shadow-eval.yml
scripts/run_shadow_campaign.ps1
scripts/run_shadow_campaign.py
scripts/audit_shadow_campaign.ps1
```

## 17. 不要做的事情

1. 不要通过调低 coverage/degraded/gold 阈值获取 PASS。
2. 不要把 `fetch_network_failure` 简单改名为成功。
3. 不要将旧 Run 的 evidence/citation 直接复制到新 Run。
4. 不要使用跨 tenant cache。
5. 不要让 CURRENT 数据无限期 stale fallback。
6. 不要绕过 SSRF 或 artifact digest 校验。
7. 不要删除失败 Campaign；失败 Campaign 是审计证据。
8. 不要在自动门禁失败时开始 20 条人工复核。
9. 不要在没有新 attestation 的情况下让旧镜像运行新 HEAD。
10. 不要把本地 DeepSeek key/token 提交到新私有仓库；private 不等于 secret store。
11. 不要将 `sana_v2` 的 origin 重写为 v3；当前多 worktree 共享 Git remote 配置。
12. 不要把 WIP `sana-v3/main` 当成 production release。

## 18. 当前剩余任务清单

### 立即任务

- [ ] 用户批准策略化 Read-through Cache 设计。
- [ ] 写正式设计文档并提交。
- [ ] 写详细实现计划。
- [ ] 实现 tenant-scoped reusable content adapter 与 policy。
- [ ] 实现 Fetch output/persistence/telemetry/lineage。
- [ ] 完成单元、集成、安全和 collector 测试。
- [ ] 完整 pytest、compileall、diff-check。

### 门禁任务

- [ ] 构建新镜像并生成新 attestation。
- [ ] 新 Smoke FINAL_PASS。
- [ ] Smoke audit 7/7。
- [ ] 新 Full 自动指标全部达标。
- [ ] Full audit 7/7。
- [ ] 用户真实完成 20 条人工复核。
- [ ] Final report `FINAL_PASS`。

### 远程/发布任务

- [x] 私有 `sana-v3` 创建并推送 WIP 基线。
- [ ] 回宿舍新机器 clone 验证。
- [ ] 门禁通过后整理 `main`、候选 tag 和 release note。
- [ ] 后续独立进行生产模型切换测试。
- [ ] 最后再进入 UI/配置体验重做与生产 rollout。

## 19. 用户偏好与协作方式

- 用户希望助手以“世界级前沿 AI 首席工程架构师”标准做自审和选择。
- 非关键选择无需反复询问，默认选择安全、可审计、可演进的方案。
- 用户允许大幅修改项目架构和界面。
- 用户希望持续推进，不要只给建议而不执行。
- 当技能或安全门禁要求显式批准时，要说明原因并只问一个清晰问题。
- 用户暂时使用同一个 DeepSeek API，不运行本地模型。
- 用户允许本地输入 token，但 secret 不能出现在聊天/Git/日志。
- 用户此前允许提交和推送；当前新需求明确要求建立可在另一地点继续工作的远程私有仓库。

## 20. 最短恢复路径

如果新的任务只有十分钟，执行以下顺序：

1. clone `sana-v3` 并切到 `codex/shadow-campaign-release-gate`。
2. 阅读本文件第 0、3、10、12、13、17、18 节。
3. `git status --short --branch`，确认没有未知改动。
4. 询问用户是否批准第 12 节方案 C；若已明确批准，立即写正式设计文档。
5. 不重跑旧 Full，不进行人工复核，不创建 release tag。
6. 完成 cache 设计/实现后从新镜像、新 Smoke 开始整个门禁链。

---

交接时的架构判断：平台基础、状态机、证据链和发布门禁本身已经达到可继续演进的水平；当前最重要的不足不是“架构推倒重来”，而是让 live content acquisition 具备策略化、租户隔离、完整血缘的跨运行韧性。修复该共因后再用新的 Full 数据决定是否还有 selector/template/source coverage 缺口。
