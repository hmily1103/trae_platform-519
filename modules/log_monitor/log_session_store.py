"""日志监控会话的完整 logcat 文本持久化（停止后仍可导出）。"""
from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def full_log_dir() -> str:
    override = os.environ.get("LOG_MONITOR_DATA_DIR", "").strip()
    if override:
        return os.path.join(override, "full_logs")
    return os.path.join(_project_root(), "data", "log_monitor", "full_logs")


def _ensure_dir() -> None:
    os.makedirs(full_log_dir(), exist_ok=True)


def _safe_filename(task_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (task_id or ""))
    return safe or "unknown"


def session_full_log_path(task_id: str) -> str:
    return os.path.join(full_log_dir(), f"{_safe_filename(task_id)}.log")


def save_session_full_log(task_id: str, lines: List[str]) -> str:
    """写入本轮完整日志文本，返回文件路径。"""
    _ensure_dir()
    path = session_full_log_path(task_id)
    body = "\n".join(lines)
    if lines and not body.endswith("\n"):
        body += "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="replace") as f:
        f.write(body)
    os.replace(tmp, path)
    return path


def load_session_full_log_text(task_id: str) -> Optional[str]:
    path = session_full_log_path(task_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def session_full_log_meta(task_id: str) -> Optional[dict]:
    path = session_full_log_path(task_id)
    if not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
        return {
            "path": path,
            "bytes": st.st_size,
            "mtime": st.st_mtime,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        return None
