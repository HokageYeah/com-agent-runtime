#!/bin/bash

set -euo pipefail

# 这个脚本不再负责生成旧模板代码。
# 当前定位是“输出本项目推荐目录结构与关键说明”，
# 方便个人开发、团队协作或 LLM 接手时快速对齐工程边界。

echo "当前模板工程: Couple Diary Backend"
echo "下面输出的是推荐目录结构与关键文件说明："
echo

cat <<'EOF'
.
├── alembic/                         # 数据库迁移相关目录
│   ├── env.py                       # Alembic 环境配置与 metadata 入口
│   ├── script.py.mako               # 迁移脚本模板
│   └── versions/                    # 具体迁移版本文件
├── app/                             # 应用主代码目录
│   ├── api/                         # API 路由注册与 endpoints
│   │   ├── api.py                   # 路由统一注册入口
│   │   └── endpoints/
│   │       ├── demo_api.py          # 示例规范接口模块
│   │       └── diary_api.py         # 正式业务模块骨架接口
│   ├── config/                      # 数据库等基础配置封装
│   ├── core/                        # 核心能力：统一响应、全局配置、日志
│   ├── db/                          # SQLAlchemy 连接、Base、metadata
│   ├── decorators/                  # 缓存等通用装饰器
│   ├── middleware/                  # 请求日志、异常处理等中间层逻辑
│   ├── models/
│   │   └── diary_entry.py           # 当前模板默认的正式业务模型样板
│   ├── schemas/
│   │   ├── common_data.py           # 通用响应结构与 PlatformEnum
│   │   └── diary.py                 # diary 业务域 schema 样板
│   ├── scripts/                     # 环境切换、建库、迁移、初始化脚本
│   ├── services/
│   │   ├── demo_service.py          # 示例服务层
│   │   └── diary_service.py         # 正式业务服务骨架
│   └── main.py                      # FastAPI 应用入口
├── tests/                           # 基础回归测试
├── .env.development                 # 开发环境基础模板
├── .env.test                        # 测试环境基础模板
├── .env.production                  # 生产环境基础模板
├── .env.example                     # 环境变量示例文件
├── pyproject.toml                   # Poetry 项目配置与工具链配置
├── poetry.lock                      # Poetry 锁定依赖
├── requirements.txt                 # 已废弃，保留作历史参考
├── run.sh                           # 推荐启动脚本
├── run_app.py                       # 应用启动脚本
└── project_structure.sh             # 当前脚本：输出模板结构说明

关键约定：
1. `demo_api.py` / `demo_service.py` 只用于沉淀示例规范，不承载长期正式业务。
2. `diary_api.py` / `diary_service.py` / `diary_entry.py` 代表当前模板默认的正式业务方向样板。
3. `/api/v1/...` 路由统一通过 `build_api_response_from_request()` 返回标准结构。
4. 新增代码优先使用 `settings.application / server / cors / request_logging / database` 这套配置分组视图。
5. 当前默认持久化模型表为 `diary_entries`，不再保留旧的历史业务模板语义。
EOF

echo
echo "如果你想看更完整的使用说明，请优先阅读 README.md。"
