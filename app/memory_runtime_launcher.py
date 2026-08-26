"""回忆录 Runtime launcher 的兼容执行入口。

部署脚本历史上使用 ``python -m app.memory_runtime_launcher``。
真实实现已归入 memoir 服务子包，本文件只保持进程入口稳定。
"""

from app.services.memoir.runtime_launcher import main, run_once

__all__ = ["main", "run_once"]


if __name__ == "__main__":
    main()
