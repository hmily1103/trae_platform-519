"""语音追踪会话持久化（停止监控后仍可回看）。"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def voice_tracker_dir() -> str:
    override = os.environ.get("LOG_MONITOR_DATA_DIR", "").strip()
    if override:
        return os.path.join(override, "voice_tracker")
    return os.path.join(_project_root(), "data", "log_monitor", "voice_tracker")


def _ensure_dir() -> None:
    os.makedirs(voice_tracker_dir(), exist_ok=True)


def _safe_filename(task_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (task_id or ""))
    return safe or "unknown"


def session_path(task_id: str) -> str:
    return os.path.join(voice_tracker_dir(), f"{_safe_filename(task_id)}.json")


def save_voice_session(task_id: str, device_id: str, history: List[Dict[str, Any]]) -> None:
    """将一次监控会话的语音指令历史写入磁盘。"""
    _ensure_dir()
    payload = {
        "task_id": task_id,
        "device_id": device_id or "",
        "saved_at": time.time(),
        "saved_at_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": history,
    }
    path = session_path(task_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_voice_session(task_id: str) -> Optional[Dict[str, Any]]:
    path = session_path(task_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_voice_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    """列出已保存的会话（按保存时间倒序）。"""
    _ensure_dir()
    rows: List[Dict[str, Any]] = []
    base = voice_tracker_dir()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(base, name)
        try:
            st = os.stat(path)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") or []
            rows.append(
                {
                    "task_id": data.get("task_id"),
                    "device_id": data.get("device_id"),
                    "saved_at": data.get("saved_at"),
                    "saved_at_iso": data.get("saved_at_iso"),
                    "command_count": len(items),
                    "mtime": st.st_mtime,
                }
            )
        except Exception:
            continue
    rows.sort(key=lambda x: float(x.get("saved_at") or x.get("mtime") or 0), reverse=True)
    return rows[: max(1, min(limit, 200))]
