# -*- coding: utf-8 -*-
"""API 压测报告存储"""
import json
import os
import uuid
import time
from typing import Dict, Any, List, Optional


def _get_reports_dir(app_root: Optional[str] = None) -> str:
    if app_root:
        base = app_root
    else:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    path = os.path.join(base, "logs", "api_stress_reports")
    os.makedirs(path, exist_ok=True)
    return path


def save_report(
    config: Dict[str, Any],
    results: Dict[str, Any],
    end_reason: str = "completed",
    app_root: Optional[str] = None,
) -> str:
    """保存报告，返回 report_id"""
    report_id = str(uuid.uuid4())[:8] + "_" + str(int(time.time()))
    report = {
        "report_id": report_id,
        "created_at": time.time(),
        "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "end_reason": end_reason,  # "completed" | "stopped"
        "config": config,
        "results": results,
    }
    path = os.path.join(_get_reports_dir(app_root), f"{report_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report_id


def load_report(report_id: str, app_root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """加载单条报告"""
    path = os.path.join(_get_reports_dir(app_root), f"{report_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_reports(limit: int = 50, app_root: Optional[str] = None) -> List[Dict[str, Any]]:
    """列表报告，按时间倒序"""
    dir_path = _get_reports_dir(app_root)
    files = [f for f in os.listdir(dir_path) if f.endswith(".json")]
    reports = []
    for f in sorted(files, reverse=True)[:limit]:
        try:
            with open(os.path.join(dir_path, f), "r", encoding="utf-8") as fp:
                reports.append(json.load(fp))
        except Exception:
            pass
    return sorted(reports, key=lambda x: x.get("created_at", 0), reverse=True)
