"""
Unified orchestration registry (in-memory) and helpers.

Design goals:
- Additive: does not change existing module behavior
- Lightweight: no external deps, no scheduler
- Best-effort: can aggregate status by querying module in-memory state
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional


_LOCK = threading.RLock()
_RUNS: Dict[str, Dict[str, Any]] = {}


def new_run_id(prefix: str = "unified") -> str:
    return f"{prefix}_{int(time.time())}_{str(uuid.uuid4())[:8]}"


def create_run(request_payload: Dict[str, Any]) -> str:
    run_id = new_run_id()
    with _LOCK:
        _RUNS[run_id] = {
            "run_id": run_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "status": "running",
            "request": request_payload,
            "children": {},  # module -> child metadata
            "errors": [],
        }
    return run_id


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        r = _RUNS.get(run_id)
        return dict(r) if r else None


def update_run(run_id: str, **fields: Any) -> None:
    with _LOCK:
        if run_id not in _RUNS:
            return
        _RUNS[run_id].update(fields)
        _RUNS[run_id]["updated_at"] = time.time()


def set_child(run_id: str, module: str, child: Dict[str, Any]) -> None:
    with _LOCK:
        if run_id not in _RUNS:
            return
        _RUNS[run_id]["children"][module] = child
        _RUNS[run_id]["updated_at"] = time.time()


def add_error(run_id: str, module: str, error: str) -> None:
    with _LOCK:
        if run_id not in _RUNS:
            return
        _RUNS[run_id]["errors"].append({"module": module, "error": error, "ts": time.time()})
        _RUNS[run_id]["updated_at"] = time.time()


def remove_run(run_id: str) -> bool:
    """移除运行记录（用于清理已失效/不存在的任务）"""
    with _LOCK:
        if run_id in _RUNS:
            del _RUNS[run_id]
            return True
        return False


def run_has_performance_monitor(run_id: str) -> bool:
    """判断该 run 是否包含 performance_monitor 子任务（用于 Monkey 完成时是否联动结束性能监控）"""
    r = get_run(run_id)
    if not r:
        return False
    children = r.get("children") or {}
    return "performance_monitor" in children and isinstance(children.get("performance_monitor"), dict)


def list_runs(limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出运行记录，用于「运行中任务」展示。status 可选：running / finished，不传则全部。"""
    with _LOCK:
        runs = list(_RUNS.values())
    if status:
        status_lower = str(status).strip().lower()
        runs = [r for r in runs if str(r.get("status", "")).lower() == status_lower]
    runs.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    out = []
    for r in runs[:limit]:
        out.append({
            "run_id": r.get("run_id"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "modules": list((r.get("children") or {}).keys()),
        })
    return out

