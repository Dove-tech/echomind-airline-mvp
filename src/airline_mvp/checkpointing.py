"""LangGraph Checkpoint 构建逻辑。

设计映射：设计 §17 和 §18。

默认使用 ``SqliteSaver``，使会话能够在进程重启后恢复。真实部署可以
切换 ``PostgresSaver``；单元测试也可以显式使用 ``MemorySaver``。

显式选择 PostgreSQL 时不会静默回退，否则健康检查可能错误地报告真实
Checkpoint 已启用。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def build_checkpointer(
    path: Path,
    *,
    backend: str = "sqlite",
    postgres_url: str | None = None,
    pool_size: int = 5,
) -> tuple[Any, str]:
    """构建 Mock/本地/真实 Checkpoint 后端。"""

    path.parent.mkdir(parents=True, exist_ok=True)

    # -------------------- 测试 Mock：仅存在于当前 Python 进程 --------------------
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver(), "memory"

    # -------------------- 真实服务：PostgreSQL 持久化 Checkpoint --------------------
    if backend == "postgres":
        if not postgres_url:
            raise ValueError("PostgreSQL Checkpoint 已启用，但连接 URL 为空")
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL Checkpoint 需要安装可选依赖："
                'python -m pip install -e ".[postgres]"'
            ) from exc

        pool = ConnectionPool(
            conninfo=postgres_url,
            min_size=1,
            max_size=pool_size,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=True,
        )
        pool.wait(timeout=10)
        saver = PostgresSaver(pool)
        # setup() 只创建 Checkpoint 自己的表和迁移版本，不删除已有记录。
        saver.setup()
        return saver, "postgres"

    if backend != "sqlite":
        raise ValueError(f"不支持的 Checkpoint backend：{backend}")

    # -------------------- 本地真实实现：嵌入式 SQLite --------------------
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        connection = sqlite3.connect(path, check_same_thread=False)
        return SqliteSaver(connection), "sqlite"
    except ImportError:
        # 仅为兼容最小 Python 环境保留回退；正常安装 pyproject 依赖后不会进入。
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver(), "memory"
