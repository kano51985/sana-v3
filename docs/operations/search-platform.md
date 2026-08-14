# Sana 搜索平台运行手册

## 当前发布状态

新 Streamlit API 客户端已成为 `start.bat` 和 `start-streamlit.ps1` 的默认入口，旧界面只允许通过 `-Legacy` 或 `SANA_UI_MODE=legacy` 显式启动。这个默认入口变更不等同于生产切流批准：只有本文的切流门槛全部达标后，才可把真实多用户流量接入新 Worker。

PostgreSQL 是 Run、Step、Attempt、证据与记忆的唯一事实源。Redis 只承担 Celery broker 和 SSE 加速；它不能成为恢复依据。API、Outbox Dispatcher、Celery Worker 和 Streamlit 是四个独立进程。

## 必要配置

本地默认值仅供开发：

| 变量 | 用途 | 本地默认值 |
| --- | --- | --- |
| `SANA_DATABASE_URL` | PostgreSQL async SQLAlchemy URL | `postgresql+asyncpg://sana:sana@localhost:5432/sana` |
| `SANA_REDIS_URL` | SSE Redis Stream | `redis://localhost:6379/1` |
| `SANA_CELERY_BROKER_URL` | Celery broker | `redis://localhost:6379/0` |
| `SANA_API_URL` | Streamlit 访问的 API 地址 | `http://localhost:8000` |
| `SANA_AUTH_MODE` | `dev` 或 `oidc` | `dev` |
| `SANA_STEP_HANDLER_FACTORY` | Worker 处理器工厂，格式 `module:function` | 无，必须显式配置 |
| `SANA_ARTIFACT_ROOT` | 跨 Step 的 content-addressed artifact 根目录 | `var/artifacts` |

生产环境必须使用 OIDC，并设置 issuer、audience 与 JWKS URL。`SanaSettings` 会拒绝在 `SANA_ENVIRONMENT=production` 时启用开发认证。模型密钥只能通过进程环境或外部秘密管理器注入，不能放进 Streamlit、数据库配置面板、镜像或仓库。

## 外部 PostgreSQL/Redis 启动

没有 Docker 时，先把上述 URL 指向外部服务，然后分别运行：

```powershell
.\start-api.ps1
python -m sana.app.bootstrap_dev
.\start-worker.ps1 -Role dispatcher
$env:SANA_STEP_HANDLER_FACTORY = "your_package.worker:create_handler"
.\start-worker.ps1 -Role worker -Queue all
.\start-streamlit.ps1
```

`bootstrap_dev` 只允许在本地开发认证模式运行，会幂等创建开发租户与用户并打印临时 Bearer token。OIDC 用户与租户必须由正式身份供应流程预配，不使用该命令。

可以用 `-SkipMigrations` 禁止 API 入口自动迁移；生产建议由独立发布作业执行 `python -m alembic upgrade head`，成功后再滚动 API。

## Docker Compose

基础设施、迁移、API 和 Streamlit：

```powershell
docker compose -f deployment/docker-compose.yml up --build
```

开发身份初始化：

```powershell
docker compose -f deployment/docker-compose.yml --profile dev-bootstrap run --rm bootstrap-dev
```

只有提供了真实 `SANA_STEP_HANDLER_FACTORY` 才能启用 Worker profile：

```powershell
docker compose -f deployment/docker-compose.yml --profile workers up --build
```

默认 compose 密码只适用于本地。生产必须同时设置 PostgreSQL 密码和完整 URL，使用平台秘密注入，并限制 5432/6379 不对公网暴露。

## 健康与就绪

- `GET /health/live` 只证明 API 进程仍在运行。
- `GET /health/ready` 同时探测 PostgreSQL 和 Redis；任一不可用返回 503。
- Worker 缺少处理器工厂时直接退出，禁止产生“空 Worker 已健康”的假象。
- Dispatcher 每轮先枚举 ACTIVE tenant，再在各自 `app.tenant_id` RLS 事务中领取 Outbox。
- 同一控制进程扫描 PostgreSQL 中超出 grace period 的 READY、到期 RETRY_WAIT 和 lease 过期 RUNNING Step；Redis 丢失任务后会按稳定 task ID 重投。
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

1. 已提供 `DurableStepExecutor`、PostgreSQL lease/Attempt finalization 与重试边界，但尚无生产 completion coordinator/`SANA_STEP_HANDLER_FACTORY` 将真实 planner、provider、fetch、extract、verify、synthesize 全部装配并生成后继 Step。
2. 已提供带 tenant/hash 校验的本地共享卷 artifact adapter；多主机 Worker 上线前仍需接入 S3/MinIO 或经过验证的共享文件系统，不能让每台 Worker 使用独立本地目录。
3. 当前机器没有可用 PostgreSQL，所以新 Reconciler、RLS、迁移、真实多用户、Worker crash 和记忆正式导入未在本机执行；Redis 服务可用不代表这些验收通过。
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
