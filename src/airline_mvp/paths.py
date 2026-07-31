"""独立工程的文件系统路径定义。

代码不会修改任何 Runtime 配置文件。环境变量可以覆盖数据目录，
而版本库内的 Fixture 路径始终保持确定性。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 路径常量会在模块导入时确定，因此需要先加载项目级 .env。
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _configured_path(name: str, default: Path) -> Path:
    """空环境变量按未配置处理，避免 ``Path("")`` 意外指向当前目录。"""

    value = os.getenv(name, "").strip()
    return Path(value) if value else default


DATA_ROOT = _configured_path("AIRLINE_MVP_DATA_DIR", PROJECT_ROOT / "data")
FIXTURE_ROOT = DATA_ROOT / "fixtures" / "airline_mvp"
KNOWLEDGE_ROOT = DATA_ROOT / "knowledge" / "airline_mvp"
RUNTIME_ROOT = _configured_path(
    "AIRLINE_MVP_RUNTIME_DIR",
    PROJECT_ROOT / ".runtime",
)


def ensure_runtime_dirs() -> None:
    """只创建当前工程内部的 Runtime 目录。

    设计 §6 和 §21 在该新工程内使用 SQLite/Chroma Runtime。本函数绝不会
    修改 PostgreSQL、Clowder、EchoMind、Redis 或任何已有持久化数据库。
    """

    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
