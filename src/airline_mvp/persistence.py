"""Case、Evidence、Trace 和 Handoff 的双后端持久化。

设计映射
--------
- 设计 §18：面试运行档使用 PostgreSQL，SQLite 只保留给隔离测试。
- 设计 §20：人工接管创建过程必须幂等。
- 设计 §23：每个请求都能通过有序 TraceEvent 记录重建。

本模块有意只提供 append/upsert 操作，不提供 ``drop``、``truncate`` 或
破坏性迁移 API。这样既适合安全演示，也符合 Clowder 工作区的
Data Storage Sanctuary 规则。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from .models import (
    CaseStatus,
    EvidenceItem,
    HandoffPacket,
    ServiceResponse,
    ToolCallRecord,
    TraceEvent,
    utc_now,
)


def _json(value: Any) -> str:
    """序列化 Pydantic 模型、枚举和日期时间，同时保留 Unicode 字符。"""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class Database(Protocol):
    """Repository 依赖的最小数据库端口。"""

    backend_name: str

    def connect(self, *, write: bool = False) -> Any: ...

    def lock_trace_sequence(self, connection: Any, case_id: str) -> None: ...


# =============================================================================
# Mock/本地实现：SQLite 是真实嵌入式数据库，不需要额外数据库服务
# =============================================================================


class SQLiteDatabase:
    """轻量、线程安全的连接工厂和仅向前演进的 Schema 管理器。"""

    def __init__(self, path: Path) -> None:
        self.backend_name = "sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._write_lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """打开一个短生命周期连接。

        ``check_same_thread=False`` 允许并行 LangGraph Worker 持久化
        Trace 数据；SQLite 写入仍通过 ``_write_lock`` 串行化。
        """

        lock = self._write_lock if write else _NullLock()
        with lock:
            connection = sqlite3.connect(
                self.path,
                timeout=10,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                yield connection
                if write:
                    connection.commit()
            except Exception:
                if write:
                    connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        """以增量方式创建 MVP 数据表，绝不删除已有记录。"""

        schema = """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            verified_subject_id TEXT,
            locale TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
        );
        CREATE TABLE IF NOT EXISTS cases (
            case_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            status TEXT NOT NULL,
            user_goal TEXT,
            case_summary TEXT,
            plan_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
        );
        CREATE TABLE IF NOT EXISTS tool_calls (
            tool_call_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(case_id)
        );
        CREATE TABLE IF NOT EXISTS evidence_items (
            evidence_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            authority TEXT NOT NULL,
            version TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(case_id)
        );
        CREATE TABLE IF NOT EXISTS service_responses (
            response_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            response_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(case_id, response_version),
            FOREIGN KEY(case_id) REFERENCES cases(case_id)
        );
        CREATE TABLE IF NOT EXISTS handoffs (
            handoff_id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            response_version INTEGER NOT NULL,
            target_queue TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(case_id, reason_code, response_version),
            FOREIGN KEY(case_id) REFERENCES cases(case_id)
        );
        CREATE TABLE IF NOT EXISTS trace_events (
            event_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(case_id, sequence_no)
        );
        CREATE INDEX IF NOT EXISTS idx_trace_case_sequence
            ON trace_events(case_id, sequence_no);
        CREATE INDEX IF NOT EXISTS idx_evidence_case
            ON evidence_items(case_id);
        """
        with self.connect(write=True) as connection:
            connection.executescript(schema)

    def lock_trace_sequence(
        self, _connection: sqlite3.Connection, _case_id: str
    ) -> None:
        """SQLite 写入已由进程内 ``_write_lock`` 串行化，无需额外数据库锁。"""


# =============================================================================
# 真实实现：连接用户在本机或 Docker 中启动的 PostgreSQL
# =============================================================================


def _translate_sqlite_sql_to_postgres(statement: str) -> str:
    """转换本项目使用到的少量 SQLite DB-API 方言。

    Repository 故意只使用一个很小的 SQL 子集，所以无需引入 ORM：

    - ``?`` 参数占位符转换为 psycopg 的 ``%s``；
    - ``INSERT OR IGNORE`` 转换为 PostgreSQL ``ON CONFLICT DO NOTHING``。

    该函数不接收模型或用户动态生成的 SQL。
    """

    translated = statement.replace("?", "%s")
    marker = "INSERT OR IGNORE INTO"
    if marker in translated:
        translated = translated.replace(marker, "INSERT INTO")
        translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return translated


class _PostgreSQLConnectionAdapter:
    """把 psycopg Connection 适配成 Repository 当前使用的 execute 接口。"""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def execute(
        self,
        statement: str,
        parameters: tuple[Any, ...] | list[Any] | None = None,
    ) -> Any:
        translated = _translate_sqlite_sql_to_postgres(statement)
        # psycopg 只在传入 params 时解析 ``%s`` 占位符。无参数查询若仍传入
        # 空元组，SQL 中合法的 LIKE 'prefix%' 会被误判为非法 ``%_`` 占位符。
        if not parameters:
            return self.connection.execute(translated)
        return self.connection.execute(translated, parameters)


class PostgreSQLDatabase:
    """真实 PostgreSQL 连接池和仅向前建表器。

    初始化会立即创建连接池并执行 ``CREATE TABLE/INDEX IF NOT EXISTS``，
    因而选择该后端时可以确认应用确实连到了数据库，而不是伪装成成功后回退
    SQLite。这个类不提供 DROP/TRUNCATE/清库能力。
    """

    backend_name = "postgres"

    def __init__(self, database_url: str, *, pool_size: int = 5) -> None:
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL 后端需要安装可选依赖："
                'python -m pip install -e ".[postgres]"'
            ) from exc

        self.database_url = database_url
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=pool_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        # 在构造阶段验证连接，避免 API 启动成功后到第一个请求才暴露配置错误。
        self.pool.wait(timeout=10)
        self.initialize()

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[_PostgreSQLConnectionAdapter]:
        with self.pool.connection() as connection:
            adapter = _PostgreSQLConnectionAdapter(connection)
            try:
                yield adapter
                if write:
                    connection.commit()
                else:
                    # 只读查询无需保留长事务快照。
                    connection.rollback()
            except Exception:
                connection.rollback()
                raise

    def initialize(self) -> None:
        """创建与 SQLite 相同的应用表；只新增，不删除已有对象。"""

        statements = [
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                verified_subject_id TEXT,
                locale TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                request_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                request_id TEXT NOT NULL,
                status TEXT NOT NULL,
                user_goal TEXT,
                case_summary TEXT,
                plan_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tool_calls (
                tool_call_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                invocation_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                domain TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS evidence_items (
                evidence_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                authority TEXT NOT NULL,
                version TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS service_responses (
                response_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                response_version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(case_id, response_version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS handoffs (
                handoff_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                reason_code TEXT NOT NULL,
                response_version INTEGER NOT NULL,
                target_queue TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(case_id, reason_code, response_version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trace_events (
                event_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(case_id, sequence_no)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_trace_case_sequence
            ON trace_events(case_id, sequence_no)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_evidence_case
            ON evidence_items(case_id)
            """,
        ]
        with self.connect(write=True) as connection:
            for statement in statements:
                connection.execute(statement)

    def lock_trace_sequence(
        self, connection: _PostgreSQLConnectionAdapter, case_id: str
    ) -> None:
        """使用事务级 advisory lock 防止多进程生成重复 Trace sequence。"""

        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (case_id,),
        )

    def close(self) -> None:
        self.pool.close()


class _NullLock:
    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class CaseRepository:
    """供 Graph 边界节点使用的持久化门面，不会直接暴露给模型 Prompt。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start_case(
        self,
        *,
        conversation_id: str,
        case_id: str,
        request_id: str,
        message: str,
        verified_subject_id: str | None,
        locale: str,
    ) -> None:
        now = utc_now().isoformat()
        with self.database.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO conversations(
                    conversation_id, verified_subject_id, locale, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    verified_subject_id=excluded.verified_subject_id,
                    locale=excluded.locale,
                    updated_at=excluded.updated_at
                """,
                (conversation_id, verified_subject_id, locale, now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO messages(
                    message_id, conversation_id, role, content, request_id, created_at
                ) VALUES (?, ?, 'user', ?, ?, ?)
                """,
                (
                    f"msg_{uuid.uuid4().hex[:16]}",
                    conversation_id,
                    message,
                    request_id,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO cases(
                    case_id, conversation_id, request_id, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    case_id,
                    conversation_id,
                    request_id,
                    CaseStatus.NEW.value,
                    now,
                    now,
                ),
            )

    def update_case(
        self,
        *,
        case_id: str,
        status: CaseStatus | str,
        user_goal: str | None = None,
        case_summary: str | None = None,
        plan: Any | None = None,
    ) -> None:
        status_value = status.value if isinstance(status, CaseStatus) else status
        with self.database.connect(write=True) as connection:
            connection.execute(
                """
                UPDATE cases SET
                    status=?,
                    user_goal=COALESCE(?, user_goal),
                    case_summary=COALESCE(?, case_summary),
                    plan_json=COALESCE(?, plan_json),
                    updated_at=?
                WHERE case_id=?
                """,
                (
                    status_value,
                    user_goal,
                    case_summary,
                    _json(plan) if plan is not None else None,
                    utc_now().isoformat(),
                    case_id,
                ),
            )

    def save_tool_calls(self, case_id: str, calls: list[ToolCallRecord]) -> None:
        with self.database.connect(write=True) as connection:
            for call in calls:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO tool_calls(
                        tool_call_id, case_id, invocation_id, task_id, domain,
                        tool_name, arguments_json, status, error_code,
                        started_at, ended_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        call.tool_call_id,
                        case_id,
                        call.invocation_id,
                        call.task_id,
                        call.domain.value,
                        call.tool_name,
                        _json(call.arguments),
                        call.status.value,
                        call.error_code,
                        call.started_at.isoformat(),
                        call.ended_at.isoformat(),
                    ),
                )

    def save_evidence(self, evidence: list[EvidenceItem]) -> None:
        with self.database.connect(write=True) as connection:
            for item in evidence:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence_items(
                        evidence_id, case_id, source_type, source_id, authority,
                        version, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.evidence_id,
                        item.case_id,
                        item.source_type,
                        item.source_id,
                        item.authority,
                        item.version,
                        _json(item),
                        utc_now().isoformat(),
                    ),
                )

    def save_response(
        self, case_id: str, response: ServiceResponse, response_version: int = 1
    ) -> str:
        response_id = f"resp_{uuid.uuid4().hex[:16]}"
        with self.database.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO service_responses(
                    response_id, case_id, response_version, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(case_id, response_version) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (
                    response_id,
                    case_id,
                    response_version,
                    _json(response),
                    utc_now().isoformat(),
                ),
            )
        return response_id

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cases WHERE case_id=?", (case_id,)
            ).fetchone()
        return dict(row) if row else None


class HandoffRepository:
    """质检通过后使用的幂等人工接管队列。

    设计 §20.2 禁止 LLM 直接写入队列记录。Graph 先创建结构化提议，
    再由该确定性 Repository 恰好执行一次状态变更。
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def queue(
        self, packet: HandoffPacket, *, response_version: int = 1
    ) -> HandoffPacket:
        with self.database.connect(write=True) as connection:
            existing = connection.execute(
                """
                SELECT handoff_id, payload_json FROM handoffs
                WHERE case_id=? AND reason_code=? AND response_version=?
                """,
                (packet.case_id, packet.reason_code, response_version),
            ).fetchone()
            if existing:
                persisted = HandoffPacket.model_validate_json(existing["payload_json"])
                return persisted

            handoff_id = f"ho_{uuid.uuid4().hex[:16]}"
            queued = packet.model_copy(
                update={"handoff_id": handoff_id, "status": "queued"}
            )
            connection.execute(
                """
                INSERT INTO handoffs(
                    handoff_id, case_id, reason_code, response_version,
                    target_queue, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    queued.case_id,
                    queued.reason_code,
                    response_version,
                    queued.target_queue,
                    queued.status,
                    _json(queued),
                    utc_now().isoformat(),
                ),
            )
        return queued


class TraceRepository:
    """供可观测性端点使用的有序 Trace 持久化与读取接口。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._sequence_lock = threading.RLock()

    def append(
        self,
        *,
        trace_id: str,
        case_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> TraceEvent:
        with self._sequence_lock, self.database.connect(write=True) as connection:
            self.database.lock_trace_sequence(connection, case_id)
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence_no), 0) AS max_sequence
                FROM trace_events WHERE case_id=?
                """,
                (case_id,),
            ).fetchone()
            sequence_no = int(row["max_sequence"]) + 1
            event = TraceEvent(
                event_id=f"evt_{uuid.uuid4().hex[:16]}",
                trace_id=trace_id,
                case_id=case_id,
                event_type=event_type,
                sequence_no=sequence_no,
                payload=payload or {},
            )
            connection.execute(
                """
                INSERT INTO trace_events(
                    event_id, trace_id, case_id, event_type,
                    sequence_no, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.trace_id,
                    event.case_id,
                    event.event_type,
                    event.sequence_no,
                    _json(event.payload),
                    event.created_at.isoformat(),
                ),
            )
        return event

    def list_for_case(self, case_id: str) -> list[TraceEvent]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM trace_events
                WHERE case_id=? ORDER BY sequence_no ASC
                """,
                (case_id,),
            ).fetchall()
        return [
            TraceEvent(
                event_id=row["event_id"],
                trace_id=row["trace_id"],
                case_id=row["case_id"],
                event_type=row["event_type"],
                sequence_no=row["sequence_no"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
