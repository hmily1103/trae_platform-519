"""
点歌历史存储：JSON 文件持久化，最近 N 条记录
"""
import json
import os
import threading
import time
from typing import Any, Dict, List

_HISTORY_PATH: str = ""
_LOCK = threading.RLock()
_MAX_ENTRIES = 200


def _get_path() -> str:
    global _HISTORY_PATH
    if not _HISTORY_PATH:
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        _HISTORY_PATH = os.path.join(root, "data", "song_order", "history.json")
    return _HISTORY_PATH


def _ensure_dir() -> None:
    p = _get_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)


def add_order(musicno: str, musicname: str, success: bool = True, precision_context: Dict[str, Any] = None) -> None:
    """追加一条点歌记录"""
    with _LOCK:
        _ensure_dir()
        path = _get_path()
        data: Dict[str, Any] = {"entries": []}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        entries: List[Dict[str, Any]] = data.get("entries") or []
        entry = {
            "musicno": musicno,
            "musicname": musicname or "",
            "success": success,
            "ts": time.time(),
        }
        if isinstance(precision_context, dict) and precision_context:
            entry.update({
                "precision_analysis_id": str(precision_context.get("analysis_id") or ""),
                "precision_test_point_id": str(precision_context.get("test_point_id") or ""),
                "precision_execution_id": str(precision_context.get("execution_id") or ""),
                "summary": "点歌请求成功" if success else "点歌请求失败",
                "status": "success" if success else "failed",
            })
        entries.insert(0, entry)
        data["entries"] = entries[:_MAX_ENTRIES]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def list_history(limit: int = 50) -> List[Dict[str, Any]]:
    """获取最近点歌历史"""
    with _LOCK:
        path = _get_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries") or []
            return entries[:limit]
        except Exception:
            return []
