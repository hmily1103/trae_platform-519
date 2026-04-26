"""
Unified 编排回调：当 Monkey 完成时，若该次运行包含性能监控，则自动停止性能监控、
写入性能报告并标记为已完成，使编排整体变为 finished 并生成编排报告。
"""

from __future__ import annotations

import time
from typing import Any, Dict

try:
    from shared.unified.orchestrator import get_run, set_child, run_has_performance_monitor
except Exception:
    get_run = None
    set_child = None
    run_has_performance_monitor = None

try:
    from shared.unified.report_store import get_unified_report_store
except Exception:
    get_unified_report_store = None


def on_unified_monkey_finished(run_id: str) -> None:
    """
    一键任务中 Monkey 完成后调用：若该 run 包含 performance_monitor，
    则停止性能监控、将 session 写入 unified 报告，并标记 performance_monitor 为已结束，
    这样下次 status 轮询时 overall 会变为 finished，编排报告会一起生成。
    """
    if not run_id or not get_run or not set_child:
        return
    run = get_run(run_id)
    if not run:
        return
    if run_has_performance_monitor and not run_has_performance_monitor(run_id):
        return
    children = run.get("children") or {}
    perf_child = children.get("performance_monitor")
    if not perf_child or not isinstance(perf_child, dict) or perf_child.get("error"):
        return

    task_id = perf_child.get("task_id")
    session_id = perf_child.get("session_id")
    if not task_id:
        return

    try:
        from modules.performance_monitor.views import PERFORMANCE_SERVICE, PERFORMANCE_SERVICE_LOCK
    except Exception:
        return

    # 先停止性能监控（会 end_session，但 session 数据仍在 storage）
    with PERFORMANCE_SERVICE_LOCK:
        ok = PERFORMANCE_SERVICE.stop_monitoring(task_id)
    if not ok:
        # 任务可能已被停止，仍标记为已由编排结束
        pass

    # 从 storage 取 session 与统计，写入 unified 报告
    perf_unified_id = None
    if get_unified_report_store:
        try:
            session = PERFORMANCE_SERVICE.storage.get_session(session_id) if session_id else None
            stats = PERFORMANCE_SERVICE.storage.get_statistics(session_id) if session_id else {}
            metadata = (session or {}).get("metadata", {})
            device_id = metadata.get("device_id") or (run.get("request") or {}).get("device_id")
            package_name = metadata.get("package_name") or (run.get("request") or {}).get("package_name")

            perf_unified_id = f"perf_{run_id}_{task_id}"
            summary = {
                "session_id": session_id,
                "snapshot_count": stats.get("snapshot_count", 0),
                "fps": stats.get("fps", {}),
                "cpu": stats.get("cpu", {}),
                "memory": stats.get("memory", {}),
                "perceptual_stall": stats.get("perceptual_stall", {}),
            }
            get_unified_report_store().save_report(
                unified_id=perf_unified_id,
                module="performance_monitor",
                kind="performance",
                status="finished",
                summary=summary,
                details={"session_id": session_id, "statistics": stats, "metadata": metadata},
                device_id=device_id,
                package_name=package_name,
                legacy_id=session_id,
                started_at=metadata.get("start_time"),
                finished_at=metadata.get("end_time") or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                raw={"session": session, "statistics": stats},
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Unified: save performance report after monkey finished: %s", e)

    # 更新 child，使 _get_perf_child_status 返回 finished
    updated = dict(perf_child)
    updated["finished_by_unified"] = True
    if perf_unified_id:
        updated["unified_report_id"] = perf_unified_id
    set_child(run_id, "performance_monitor", updated)
