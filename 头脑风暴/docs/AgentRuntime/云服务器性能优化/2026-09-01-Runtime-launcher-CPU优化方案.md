# AgentRuntime launcher CPU 优化方案

> 日期：2026-09-01  
> 范围：腾讯云单机 production；本文件只给出排查结论和最小优化计划，不修改代码或服务器配置。

## 结论

这次现象不是 `memory_runtime_launcher` 崩溃重启，而是旧 launcher 按当前代码设计正常空转：父进程每隔 5 秒启动一个新的 Python 子进程，子进程完成一次数据库扫描后正常退出。

但这套行为不应继续出现在当前 production 架构中。情侣日记生产链路已经由 `couple-diary-doc` 的 `run_memory_runtime_worker` 发起 Runtime Run；本仓总控计划也明确规定 production 不启用 `app.memory_runtime_launcher` legacy 启动器。因此，当前 production Compose 仍默认启动 launcher，属于部署配置尚未跟上迁移边界，而不是必须保留的运行模式。

最低成本的处理顺序是：

1. 先临时停止 production 的 launcher，保留 Runtime API、Worker、Reconciler。
2. 观察 CPU，并完成一次真实回忆录端到端验收。
3. 验收通过后，永久取消 production 默认启动 launcher；test 暂时保持不变。
4. 只有确认某个环境仍依赖旧业务表时，才用“把轮询间隔调到 60 秒”作为兜底。

不建议现在把旧 launcher 重构成长驻进程。它在 production 的职责已经迁出，重构会增加代码和验证成本，却是在优化一条应该停用的旧链路。

## 证据与判断

| 证据 | 判断 |
|---|---|
| `_launcher-loop` 的 PID 固定，`app.memory_runtime_launcher` 的 PID 每轮变化 | 父进程在反复创建一次性子进程 |
| 每轮均记录初始化、数据库连接成功、四个计数均为 0、数据库关闭 | 子进程正常完成，不是 crash-loop |
| 没有 traceback，容器也没有反复重启记录 | 不是 Docker 重启策略导致的崩溃循环 |
| `run_launcher_loop()` 默认 `interval_seconds=5`，且未读取环境变量 | 5 秒是代码写死的当前行为，服务器无法只改 env 调整 |
| 日志相邻轮次约 8～12 秒 | 等于子进程启动和扫描耗时再加 5 秒 sleep；没有“越来越快”的明确证据 |
| 每轮都重新导入 Python 依赖、初始化日志、连接并关闭数据库 | 即使没有业务，也会产生周期性 CPU、数据库和日志开销 |
| 总控计划明确 production 不启用 legacy launcher，但 Compose 和部署检查仍要求 launcher 运行 | 目标架构与部署实现存在未收口项 |

`ps` 中短命子进程显示 `99% CPU`，表示它在被采样的短生命周期内占满了一个 CPU 核，不能单独证明它长期占满整台服务器。按现有日志估算，每轮活跃约 0.3～2 秒、随后等待 5 秒，它会造成明显的周期性压力，但“整机持续 100%”仍应在停用后通过系统指标复核；如果 CPU 没有显著下降，还需要继续检查其他容器或宿主进程。

## 对 Claude Code 建议的评价

正确的部分：

- 判断“不是崩溃，而是正常执行后退出”正确。
- 判断“反复启动 Python、导入依赖、连接数据库存在固定成本”正确。
- 优先尝试放大轮询间隔，比直接做架构重构更便宜。

需要修正的部分：

- 当前不是“异常退出后无退避地重启”。成功一轮后代码明确 sleep 5 秒；若子进程非零退出，父进程反而会因 `check=True` 失败退出，再由 Docker 处理容器重启。
- 日志没有稳定支持“间隔逐渐变短”或“系统越忙、拉起越快”的推断。
- 长驻进程内部循环在技术上可减少启动成本，但不是本项目当前 production 的首选，因为 legacy launcher 按目标架构本来就不应启用。

## 最小落地计划

### 1. 临时止血与验证

先确认 production launcher 最近没有处理过旧业务事件：

```bash
cd "/usr/HokageYeah/服务端系统/com-agent-runtime"

docker compose \
  -p com-agent-runtime-production \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  --env-file "/usr/HokageYeah/服务端系统/env/runtime-production.env" \
  logs --since=24h launcher \
  | grep '回忆录 Runtime launcher 完成' \
  | grep -v 'delivered=0 repaired=0 orphaned=0 callback_repaired=0'
```

没有输出表示最近 24 小时未看到非零处理记录。然后临时停止 launcher：

```bash
docker compose \
  -p com-agent-runtime-production \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  --env-file "/usr/HokageYeah/服务端系统/env/runtime-production.env" \
  stop launcher
```

停止后至少观察 15 分钟：

```bash
docker compose \
  -p com-agent-runtime-production \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  --env-file "/usr/HokageYeah/服务端系统/env/runtime-production.env" \
  ps -a

docker stats --no-stream
uptime
```

预期结果：

- launcher 为 `Exited`，API、Worker、Reconciler 仍为运行状态。
- 不再出现新的 `python -m app.memory_runtime_launcher` PID。
- CPU 和 load average 明显下降。
- 通过情侣日记生产端创建一份回忆录后，`run_memory_runtime_worker` 能提交 Run，Runtime Worker 能执行，客户端能进入终态并显示结果。

若真实回忆录链路异常，可立即回滚：

```bash
docker compose \
  -p com-agent-runtime-production \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  --env-file "/usr/HokageYeah/服务端系统/env/runtime-production.env" \
  up -d --no-deps launcher
```

注意：临时 `stop` 只用于验证，下次 GitHub Actions 部署仍会把 launcher 拉起。

### 2. 永久收口

验证通过后再实施一个小改动：

- production Compose 默认不启动 launcher，可把它放到仅显式开启的 legacy profile。
- GitHub Actions 的 production 运行服务检查不再强制要求 launcher。
- test Compose 保持当前 launcher 行为，避免影响历史路由审计和现有联调测试。
- 同步修正 `README.md`、`ENV_CONFIG.md`、`VERIFICATION.md` 和 Docker 部署说明中“production 必须运行四个 workload”的旧描述。
- 增加 Compose 配置回归检查：production 默认服务不包含 launcher，test 仍包含 launcher。

这一步不需要改 `memory_runtime_launcher` 的业务实现，也不需要改 Couple Diary 或 Runtime 的公共 API 合同。

### 3. 无法停用时的兜底

只有在验证发现仍有旧调用方依赖 `memory_runtime_launch_events` / `memory_agent_run_refs` 时，才保留 launcher，并做以下最小修改：

- 新增 `RUNTIME_LAUNCHER_INTERVAL_SECONDS`，production 默认 `60`，test 默认 `5`。
- `_launcher-loop` 从环境变量读取并校验为正数。
- 保持现有“一轮一个子进程”的实现，暂不重构数据库生命周期。

从 5 秒调整到 60 秒，可把空轮询和进程启动频率降低约 12 倍；代价是旧链路新增事件最多多等待约 60 秒。当前用户量较少时通常可以接受。

## 暂不采用的方案

- **改成长驻 launcher 并复用连接池：** 能降低单轮固定成本，但涉及信号退出、连接失效恢复、异常隔离和内存长期增长验证，收益不如直接停用旧链路。
- **给容器设置 CPU 上限：** 只能限制影响，不能消除空转；还可能让真正的补偿任务更慢。
- **改用宿主机 cron：** 仍会反复启动 Python，只是把调度位置从容器移到宿主机。
- **引入消息队列或事件驱动调度：** 对当前低流量单机部署属于过度设计。

## 验收标准

- production 默认部署后不存在 `com-agent-runtime-production-launcher-1` 长期容器。
- API、Worker、Reconciler 的健康和运行状态不受影响。
- 一次真实回忆录从创建到客户端展示完整成功。
- 停用前后相同观察窗口内，CPU 峰值频率和 load average 明显下降。
- test 环境部署与现有自动化测试继续通过。
- 若停用后整机仍持续 100%，不再把 launcher 当作唯一根因，继续按 `docker stats` 和 `pidstat` 的累计采样结果定位其他进程。
