"""
一键任务（Unified）停止时导出日志监控完整产物：全量 logcat、语音追踪、告警。
写入 reports/unified/artifacts/<unified_run_id>/ 供报告中心下载。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

from shared.unified.report_store import is_safe_unified_id

from .voice_tracker_store import save_voice_session


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _artifacts_base(unified_run_id: str) -> str:
    return os.path.join(_project_root(), "reports", "unified", "artifacts", unified_run_id)


def export_log_monitor_bundle(unified_run_id: str, task_id: str, task_info: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    在仍持有 MONITOR_TASKS[task_id] 时调用，导出后再 stop/delete。
    返回 (写入报告的 summary, artifacts 列表)。
    """
    if not is_safe_unified_id(unified_run_id):
        raise ValueError("invalid unified_run_id")

    device_id = task_info.get("device_id") or ""
    target_package = task_info.get("target_package") or ""
    log_queue = task_info.get("log_queue") or []
    log_queue_lock = task_info.get("log_queue_lock")
    voice_tracker = task_info.get("voice_tracker")
    alert_engine = task_info.get("alert_engine")

    lines: List[str] = []
    if log_queue_lock:
        with log_queue_lock:
            for item in log_queue:
                ln = item.get("log")
                if ln is not None:
                    lines.append(str(ln))
    else:
        for item in log_queue:
            ln = item.get("log")
            if ln is not None:
                lines.append(str(ln))

    full_text = "\n".join(lines)
    if lines and not full_text.endswith("\n"):
        full_text += "\n"

    voice_history: List[Dict[str, Any]] = []
    if voice_tracker is not None:
        try:
            voice_history = list(voice_tracker.get_history())
        except Exception:
            voice_history = []

    try:
        save_voice_session(task_id, device_id, voice_history)
    except Exception:
        pass

    alerts_payload: List[Dict[str, Any]] = []
    if alert_engine is not None:
        try:
            raw_alerts = alert_engine.get_alerts(device_id=device_id or None, limit=10000)
            alerts_payload = [a.to_dict() for a in raw_alerts]
        except Exception:
            alerts_payload = []

    base = _artifacts_base(unified_run_id)
    os.makedirs(base, exist_ok=True)

    log_name = "log_monitor_full.log"
    voice_name = "voice_tracker_full.json"
    alerts_name = "log_monitor_alerts.json"

    log_path = os.path.join(base, log_name)
    with open(log_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(full_text)

    voice_path = os.path.join(base, voice_name)
    with open(voice_path, "w", encoding="utf-8") as f:
        json.dump(voice_history, f, ensure_ascii=False, indent=2)

    alerts_path = os.path.join(base, alerts_name)
    with open(alerts_path, "w", encoding="utf-8") as f:
        json.dump(alerts_payload, f, ensure_ascii=False, indent=2)

    def _href(fn: str) -> str:
        return f"/unified/api/orchestration/{unified_run_id}/artifact/{fn}"

    log_bytes = os.path.getsize(log_path) if os.path.isfile(log_path) else 0
    voice_bytes = os.path.getsize(voice_path) if os.path.isfile(voice_path) else 0
    alerts_bytes = os.path.getsize(alerts_path) if os.path.isfile(alerts_path) else 0

    v_exec = sum(1 for x in voice_history if x.get("status") == "executed")
    v_ign = sum(1 for x in voice_history if x.get("status") == "ignored")
    v_det = sum(1 for x in voice_history if x.get("status") == "detected")

    summary: Dict[str, Any] = {
        "task_id": task_id,
        "device_id": device_id,
        "target_package": target_package,
        "log_line_count": len(lines),
        "voice": {
            "total": len(voice_history),
            "executed": v_exec,
            "ignored": v_ign,
            "pending_detected": v_det,
        },
        "alerts": {"total": len(alerts_payload)},
        "files": {
            "full_log": {
                "label": "完整 Logcat（全文）",
                "filename": log_name,
                "href": _href(log_name),
                "bytes": log_bytes,
            },
            "voice_json": {
                "label": "语音指令追踪（含 raw_logs）",
                "filename": voice_name,
                "href": _href(voice_name),
                "bytes": voice_bytes,
            },
            "alerts_json": {
                "label": "告警明细（全文）",
                "filename": alerts_name,
                "href": _href(alerts_name),
                "bytes": alerts_bytes,
            },
        },
    }

    artifacts: List[Dict[str, Any]] = [
        {
            "type": "file_ref",
            "module": "log_monitor",
            "name": "完整 Logcat",
            "filename": log_name,
            "href": _href(log_name),
        },
        {
            "type": "file_ref",
            "module": "log_monitor",
            "name": "语音追踪 JSON",
            "filename": voice_name,
            "href": _href(voice_name),
        },
        {
            "type": "file_ref",
            "module": "log_monitor",
            "name": "告警明细 JSON",
            "filename": alerts_name,
            "href": _href(alerts_name),
        },
    ]

    return summary, artifacts
