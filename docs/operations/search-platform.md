# Sana 搜索平台运行手册

## 当前发布状态

新 Streamlit API 客户端已成为 `start.bat` 和 `start-streamlit.ps1` 的默认入口，旧界面只允许通过 `-Legacy` 或 `SANA_UI_MODE=legacy` 显式启动。这个默认入口变更不等同于生产切流批准：只有本文的切流门槛全部达标后，才可把真实多用户流量接入新 Worker。

PostgreSQL 是 Run、Step、Attempt、证据与记忆的唯一事实源。Redis 只承担 Celery broker 和 SSE 加速；它不能成为恢复依据。API、Outbox Dispatcher、Celery Worker 和 Streamlit 是四个独立进程。

## 必要配置

本地默认值仅供开发：

| 变量 | 用途 | 本地默认值 |
| --- | --- | --- |
| `SANA_DATABASE_URL` | 无 RLS 绕过权限的 PostgreSQL 应用 URL | `postgresql+asyncpg://sana_app:sana-app@localhost:5432/sana` |
| `SANA_REDIS_URL` | SSE Redis Stream | `redis://localhost:6379/1` |
| `SANA_CELERY_BROKER_URL` | Celery broker | `redis://localhost:6379/0` |
| `SANA_API_URL` | Streamlit 访问的 API 地址 | `http://localhost:8000` |
| `SANA_AUTH_MODE` | `dev` 或 `oidc` | `dev` |
| `SANA_STEP_HANDLER_FACTORY` | Worker 处理器工厂，格式 `module:function` | `sana.app.production_worker:create_handler` |
| `SANA_ARTIFACT_ROOT` | 跨 Step 的 content-addressed artifact 根目录 | `var/artifacts` |
| `SANA_WORKER_MODEL_PIPELINE_ENABLED` | 启用 Planner/Verifier/Synthesizer 模型质量闭环 | `false` |
| `SANA_WORKER_PLANNER_PROVIDER` | Planner provider | `deepseek` |
| `SANA_WORKER_PLANNER_MODEL` | Planner 模型 | `deepseek-v4-flash` |
| `SANA_WORKER_VERIFIER_PROVIDER` | Verifier provider | `deepseek` |
| `SANA_WORKER_VERIFIER_MODEL` | Verifier 模型 | `deepseek-v4-flash` |
| `SANA_WORKER_SYNTHESIZER_PROVIDER` | Synthesizer provider | `deepseek` |
| `SANA_WORKER_SYNTHESIZER_MODEL` | Synthesizer 模型 | `deepseek-v4-flash` |
| `SANA_WORKER_DEEPSEEK_BASE_URL` | DeepSeek 官方 API 根地址 | `https://api.deepseek.com` |
| `SANA_WORKER_MODEL_THINKING` | DeepSeek 思考模式 | `disabled` |
| `SANA_WORKER_MODEL_OUTPUT_FORMAT` | 结构化输出格式 | `json_object` |
| `SANA_WORKER_LIVE_EVAL_MAX_RUNS` | 单轮真实评测硬上限 | `20` |
| `SANA_WORKER_DISCOVERY_PROVIDERS` | 逗号分隔的 `direct`/`bing_rss`/`searxng` | `direct,bing_rss` |
| `SANA_WORKER_SEARXNG_URL` | 启用 `searxng` 时的服务地址 | 空 |
| `SANA_WORKER_MAX_SELECTED_HITS` | 每个 Run 最多抓取的候选数 | `4` |

生产环境必须使用 OIDC，并设置 issuer、audience 与 JWKS URL。`SanaSettings` 会拒绝在 `SANA_ENVIRONMENT=production` 时启用开发认证。模型密钥只能通过 Worker 进程环境或外部秘密管理器注入，不能放进 API、Streamlit、数据库配置面板、镜像或仓库。功能开关关闭时，Worker 保持 deterministic/heuristic 管线用于回滚、连通性和恢复验证；开启时，三个角色任一配置缺失、Provider 不一致或密钥缺失都会 fail closed，不会静默降级启动。

当前质量管线由同一 DeepSeek API 承担 Planner、Verifier 和 Synthesizer，默认使用 `deepseek-v4-flash`、关闭 thinking 并要求 JSON object。Model Gateway 对外部调用施加独立的总墙钟超时，并在 Step deadline 前保留 2 秒用于审计封账、本地降级和事务提交；Planner 失败时转入启发式计划，Verifier/Synthesizer 失败时转入确定性证据约束路径。所有降级都会进入最终 `degraded`、`degradation_codes` 与 `stop_reason`，不能伪装成完整模型结果。

`direct` 不是任意 URL 访问能力。它只读取版本化、代码审查可见的稳定入口注册表；模型不能生成 direct URL 或提升其权威等级。`direct-sources-v1` 覆盖 Python、DeepSeek、Apex Legends、HTTP/IANA、Git/kernel.org、Rust、OpenAI Developers，以及明确标为独立来源的 Apex 当前 meta 分析入口。`source-authority-v4` 仍单独决定 OFFICIAL/INDEPENDENT：默认按实体限定 registrable domain；当官方工件发布在共享域时，只允许审核过的 URL 前缀，不会把整个共享域提升为官方。生产扩展必须通过配置变更、真实 Worker 网络探测和 SSRF 测试加入。

`search-v5` 在 RESEARCH 模式做有界集合选择：只要仍有能覆盖未覆盖 Fact 的 curated direct + official URL，就先完成这层覆盖；之后按未覆盖增益和 Fact 映射特异性处理其余来源，再考虑 authority 与发布方多样性。这避免泛化搜索主页因重复映射多个 Query 而挤掉精确 direct 文档，也不把域名权威误当成页面相关性。证据候选按 Fact 轮询分配 8 个全局槽位，避免末尾 Fact 饥饿并约束 Verifier 成本；Fact key 中的数字、对象名等强锚点用于过滤 chunk 和定位 quote。除非原请求明确包含“可选/if available”等标记，Fact 的 required 状态由系统强制为 true，模型无权把用户要求静默降级为可选。

## 外部 PostgreSQL/Redis 启动

没有 Docker 时，先把上述 URL 指向外部服务，然后分别运行：

```powershell
.\start-api.ps1
python -m sana.app.bootstrap_dev
.\start-worker.ps1 -Role dispatcher
.\start-worker.ps1 -Role worker -Queue all
.\start-streamlit.ps1
```

`bootstrap_dev` 只允许在本地开发认证模式运行，会幂等创建开发租户与用户并打印临时 Bearer token。OIDC 用户与租户必须由正式身份供应流程预配，不使用该命令。

可以用 `-SkipMigrations` 禁止 API 入口自动迁移；生产建议由独立发布作业执行 `alembic upgrade head`，成功后再滚动 API。不要在项目根目录使用 `python -m alembic`，因为仓库内的同名迁移目录会遮蔽已安装的 Alembic 包。

数据库迁移使用对象所有者 `sana`，运行时服务使用无 `SUPERUSER`、无 `BYPASSRLS` 的 `sana_app`。Compose 会先运行幂等的 `provision-db-role` 作业，再执行迁移；不要把管理角色的连接串传给 API、dispatcher 或 worker，否则 PostgreSQL 超级用户会绕过 RLS。非本地环境必须覆盖 `POSTGRES_PASSWORD` 与 `POSTGRES_APP_PASSWORD`。

容器运行时不安装旧版 ChromaDB/MongoDB 适配器。执行旧界面回滚时安装 `.[legacy]`，执行记忆导入时安装 `.[migration]`；两类工具都不应进入 API/Worker 的生产镜像。Dockerfile 先安装独立的运行依赖层，再复制源码，因此普通代码变更不会重新下载全部 wheel。可通过构建参数 `PIP_INDEX_URL` 临时选择组织批准的软件源。

## Docker Compose

基础设施、迁移、API 和 Streamlit：

```powershell
docker compose -f deployment/docker-compose.yml up --build
```

开发身份初始化：

```powershell
docker compose -f deployment/docker-compose.yml --profile dev-bootstrap run --rm bootstrap-dev
```

Worker profile 默认装配内置的持久化搜索处理器。首次启动和每次升级时，`artifact-init` 会以一次性 root 作业修复共享 artifact 卷所有权；常驻 API、Dispatcher、Worker 和 Streamlit 都以非 root `sana` 用户运行：

```powershell
docker compose -f deployment/docker-compose.yml --profile workers up --build
```

默认 compose 密码只适用于本地。生产必须同时设置 PostgreSQL 密码和完整 URL，使用平台秘密注入，并限制 5432/6379 不对公网暴露。

## 健康与就绪

- `GET /health/live` 只证明 API 进程仍在运行。
- `GET /health/ready` 同时探测 PostgreSQL 和 Redis；任一不可用返回 503。
- Compose 和 `start-worker.ps1` 默认使用内置生产处理器；直接调用底层 Worker 且既未注入工厂也未使用入口默认值时会退出，禁止产生“空 Worker 已健康”的假象。
- Dispatcher 每轮先枚举 ACTIVE tenant，再在各自 `app.tenant_id` RLS 事务中领取 Outbox。
- 同一控制进程扫描 PostgreSQL 中超出 grace period 的 READY、到期 RETRY_WAIT 和 lease 过期 RUNNING Step；Redis 丢失任务后会按稳定 task ID 重投，并用数据库 `updated_at` 开启下一段重投冷却，避免 Worker 离线时形成毫秒级投递风暴。
- Reconciler 还会封口已完成/已过期 Attempt 留下的 `STARTED` 模型调用，写为 `ABANDONED/POSSIBLY_BILLED`；告警和成本系统不得把这类记录当作零成本成功。
- `fast`、`research`、`crawl`、`maintenance` 使用各自独立的 direct exchange 与 routing key。
- Celery 消息只传 `tenant_id`、`step_id` 与 trace context，不传 prompt、正文或密钥。

当前数据库 head 为 `0012_fetch_run_binding`。`provider_attempts` 按 `(query_spec_id, provider, attempt_no)` 唯一，允许 Direct 与 Bing 对同一 Query 并发落库；`document_version_fetches` 通过 tenant/run 复合外键把内容稳定的 DocumentVersion 绑定到本次成功 FetchArtifact，防止历史抓取内容冒充当前 Run 证据。`model_invocations`、抓取血缘、证据与引用表均强制 PostgreSQL RLS。引用必须保存本 Run 抓取绑定、document version、chunk、原文 quote 与精确 offset，缺任一项都不能进入用户答案。

## 切流门槛

生产切流必须同时满足：

- Shadow Eval 的 FAST p95 不高于 15 秒，RESEARCH p95 不高于 120 秒。
- Query 对话污染率为 0，Citation 可回溯率为 100%。
- Required Fact 无证据时，COMPLETE 误报率为 0。
- 跨租户 API、Repository 和 PostgreSQL RLS 测试零失败。
- Worker 崩溃、重复投递和 Redis 清空后，Run 能从 PostgreSQL 恢复。
- 多用户、FAST、RESEARCH、自动升级、取消、Apex、记忆召回均通过真实服务验收。
- 用户记忆已完成 dry-run、正式导入与抽样核验。

在门槛达标前，Shadow 只记录脱敏结构化指标，不影响用户可见答案。

## 回滚

回滚窗口内禁止删除 MongoDB、Chroma 或 `user_profile.json` 原数据，也禁止删除旧 `WebSearchNode`、Mongo search repository 和旧 Web 配置。

UI 回滚：

```powershell
.\start-streamlit.ps1 -Legacy
```

或临时设置：

```powershell
$env:SANA_UI_MODE = "legacy"
.\start-streamlit.ps1
```

API/Worker 回滚必须先停止新流量，再停止 Dispatcher，等待运行中的外部调用结束或取消，最后回到旧入口。不要回滚数据库迁移来恢复旧界面；旧界面在回滚窗口内仍使用自己的原数据。数据清理必须是窗口结束后的独立变更，并带备份清单与抽样核验记录。

模型质量管线可以独立回滚，无需降级数据库：

```powershell
$env:SANA_WORKER_MODEL_PIPELINE_ENABLED = "false"
docker compose -f deployment/docker-compose.yml --profile workers up -d --force-recreate worker
```

## 尚未解除的生产阻断项

截至 2026-08-15，本仓库仍有以下阻断项，因此不能宣称最终切流完成：

1. 已提供带 tenant/hash 校验的本地共享卷 artifact adapter；多主机 Worker 上线前仍需接入 S3/MinIO 或经过验证的共享文件系统，不能让每台 Worker 使用独立本地目录。
2. 内置 completion coordinator 已在本机 Docker 完成 DeepSeek Planner/Verifier/Synthesizer、Direct/Bing discovery、HTTP fetch、extract、单一 fan-in Verify 与受约束 synthesize 的 FAST/RESEARCH 闭环；也已验证 Worker 强杀后的租约回收、重复消息吸收、孤儿模型调用封口和单一助手回复。当前仅是受控小样本 smoke，不是生产 SLO 证明；仍需扩大真实流量样本，统计 FAST/RESEARCH p95、Fact 覆盖率、Provider 失败率与降级率。
3. 本机 Docker 已验证迁移、非绕过应用角色、RLS、双租户 API、Outbox 发布、Redis 清空后的 READY Step 重投及 Worker crash/lease 恢复；尚未执行记忆正式导入与召回抽样。
4. 生产 OIDC tenant/user provisioning 仍需外部身份管理流程。

这些阻断项必须通过实现和真实集成环境证据关闭，不能通过降低就绪条件或改文档绕过。

## 2026-08-15 受控验证摘要

- 三例 DeepSeek smoke（2 FAST + 1 RESEARCH）全部在调用预算内完成：FAST 为 11.7–12.3 秒、最多 3 次模型调用；RESEARCH 为 13.7 秒、3 次模型调用；引用回溯率为 100%。结果以 PARTIAL 为主，暴露的是真实证据覆盖不足，不能据此宣布质量门槛达标。
- 慢响应演练中，Verifier 与 Synthesizer 均触发 Gateway 总墙钟超时；运行仍在 14.0 秒内以确定性路径成功结束，覆盖 1/1 Fact、生成 3 条完整引用，并明确标记降级。模型审计记录为 `FAILED/POSSIBLY_BILLED`，无遗留 `STARTED`。
- 模型管线关闭回滚实测为 11.7 秒、0 次模型调用、覆盖 1/1 Fact、2 条完整引用；验证后已恢复 DeepSeek 管线。
- 以上均为本地 Docker、单机、有限样本结果，不能替代持续压测、生产网络和真实多租户验收。

## 发布验收记录模板

每次候选发布记录 commit、环境、操作者、开始/结束时间，并逐项附日志或指标链接：

- [ ] 全量 `python -m pytest -q`
- [ ] `python scripts/run_search_evals.py --fixtures evals/search_cases.jsonl`
- [ ] PostgreSQL/Redis integration markers
- [ ] 两 tenant、两 user 隔离
- [ ] FAST / RESEARCH / 自动升级 / 取消
- [ ] Worker 强杀与 lease 恢复
- [ ] Redis 清空与 Reconciler 恢复
- [ ] Apex 多事实证据闭环
- [ ] 记忆导入、召回与抽样核验
- [ ] 旧 UI 回滚
