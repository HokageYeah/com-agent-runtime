# AgentRuntime

独立的公共 AgentRuntime 服务。当前完成版本化契约、健康检查、能力发现、配置和审计接口骨架；业务工作流、数据库模型和 Worker 将在后续任务实现。

本地检查：`python -m pytest tests -q`、`ruff check .`、`mypy app`。

Worker 骨架入口：`python -m app.worker --once`（目前只校验配置，不执行任务）。
