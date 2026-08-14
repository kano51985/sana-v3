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
| `SANA_WORKER_PLANNER_PROVIDER` | `heuristic`、`deepseek`、`openai` 或 `local` | `heuristic` |
| `SANA_WORKER_PLANNER_MODEL` | 模型型 planner 的显式模型名 | 空 |
| `SANA_WORKER_DISCOVERY_PROVIDERS` | 逗号分隔的 `bing_rss`/`searxng` | `bing_rss` |
| `SANA_WORKER_SEARXNG_URL` | 启用 `searxng` 时的服务地址 | 空 |
| `SANA_WORKER_MAX_SELECTED_HITS` | 每个 Run 最多抓取的候选数 | `4` |

生产环境必须使用 OIDC，并设置 issuer、audience 与 JWKS URL。`SanaSettings` 会拒绝在 `SANA_ENVIRONMENT=production` 时启用开发认证；`ProductionWorkerSettings` 也会拒绝生产环境使用离线 heuristic planner。模型密钥只能通过 Worker 进程环境或外部秘密管理器注入，不能放进 Streamlit、数据库配置面板、镜像或仓库。离线 heuristic 只供本地连通性和恢复验证，它会在证据不足时返回 `PARTIAL`，不会把猜测包装成完整答案。

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
- `fast`、`research`、`crawl`、`maintenance` 使用各自独立的 direct exchange 与 routing key。
- Celery 消息只传 `tenant_id`、`step_id` 与 trace context，不传 prompt、正文或密钥。

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

## 尚未解除的生产阻断项

截至 2026-08-14，本仓库仍有以下阻断项，因此不能宣称最终切流完成：

1. 已提供带 tenant/hash 校验的本地共享卷 artifact adapter；多主机 Worker 上线前仍需接入 S3/MinIO 或经过验证的共享文件系统，不能让每台 Worker 使用独立本地目录。
2. 内置 completion coordinator 已在本机 Docker 完成真实 planner、Bing RSS discovery、HTTP fetch、extract、verify、synthesize 的 FAST/RESEARCH 闭环；也已在两个并行 FETCH 期间 SIGKILL Worker，验证 6 秒租约回收、旧 Attempt `lease_expired` 封账、attempt 2 续跑、重复消息吸收以及单一助手回复。生产切流前仍需用获批的模型型 planner/provider 完成质量与时延验收。
3. 本机 Docker 已验证迁移、非绕过应用角色、RLS、双租户 API、Outbox 发布、Redis 清空后的 READY Step 重投及 Worker crash/lease 恢复；尚未执行记忆正式导入与召回抽样。
4. 生产 OIDC tenant/user provisioning 仍需外部身份管理流程。

这些阻断项必须通过实现和真实集成环境证据关闭，不能通过降低就绪条件或改文档绕过。

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
