"""
Runtime 持久化存储：SQLite
- 服务重启后恢复 Runtime 状态
- 支持历史记录回溯
"""
import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from .model import RuntimeObject, RuntimeStatus


class RuntimeStorage:
    """SQLite 持久化层"""

    def __init__(self, db_path: Optional[str] = None):
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        data_dir = os.environ.get("RUNTIME_DATA_DIR") or os.path.join(root, "data", "runtime")
        os.makedirs(data_dir, exist_ok=True)
        self._path = db_path or os.path.join(data_dir, "runtimes.db")
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self._path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS runtimes (
                        runtime_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        module TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        started_at REAL,
                        ended_at REAL,
                        context TEXT,
                        result TEXT,
                        owner TEXT DEFAULT 'system'
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtimes_module ON runtimes(module)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtimes_status ON runtimes(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtimes_created ON runtimes(created_at DESC)")
                conn.commit()
            finally:
                conn.close()

    def save(self, r: RuntimeObject) -> None:
        with self._lock:
            conn = sqlite3.connect(self._path)
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO runtimes
                    (runtime_id, name, module, status, created_at, started_at, ended_at, context, result, owner)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.runtime_id,
                        r.name,
                        r.module,
                        r.status.value,
                        r.created_at,
                        r.started_at,
                        r.ended_at,
                        json.dumps(r.context, ensure_ascii=False),
                        json.dumps(r.result, ensure_ascii=False),
                        r.owner,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def load(self, runtime_id: str) -> Optional[RuntimeObject]:
        with self._lock:
            conn = sqlite3.connect(self._path)
            try:
                row = conn.execute(
                    "SELECT runtime_id, name, module, status, created_at, started_at, ended_at, context, result, owner FROM runtimes WHERE runtime_id = ?",
                    (runtime_id,),
                ).fetchone()
                if not row:
                    return None
                return self._row_to_runtime(row)
            finally:
                conn.close()

    def list_all(
        self,
        module: Optional[str] = None,
        status: Optional[RuntimeStatus] = None,
        limit: int = 100,
    ) -> List[RuntimeObject]:
        with self._lock:
            conn = sqlite3.connect(self._path)
            try:
                sql = "SELECT runtime_id, name, module, status, created_at, started_at, ended_at, context, result, owner FROM runtimes WHERE 1=1"
                params: List[Any] = []
                if module:
                    sql += " AND module = ?"
                    params.append(module)
                if status:
                    sql += " AND status = ?"
                    params.append(status.value)
                sql += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [self._row_to_runtime(row) for row in rows]
            finally:
                conn.close()

    def _row_to_runtime(self, row) -> RuntimeObject:
        ctx = {}
        res = {}
        try:
            if row[7]:
                ctx = json.loads(row[7])
        except Exception:
            pass
        try:
            if row[8]:
                res = json.loads(row[8])
        except Exception:
            pass
        return RuntimeObject(
            runtime_id=row[0],
            name=row[1],
            module=row[2],
            status=RuntimeStatus(row[3]),
            created_at=row[4],
            started_at=row[5],
            ended_at=row[6],
            context=ctx,
            result=res,
            owner=row[9] or "system",
        )


_STORAGE: Optional[RuntimeStorage] = None
_STORAGE_LOCK = threading.Lock()


def get_runtime_storage() -> RuntimeStorage:
    global _STORAGE
    with _STORAGE_LOCK:
        if _STORAGE is None:
            _STORAGE = RuntimeStorage()
        return _STORAGE
