"""
Unified report storage (backward-compatible additive layer).

Goals:
- Provide a single place to persist "normalized" run reports across modules
- Avoid breaking existing module-specific report formats and endpoints
- Keep dependencies minimal (JSON on disk)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

# 允许的 unified_id：字母数字、下划线、连字符，长度 1~256，防止路径穿越
_SAFE_UNIFIED_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,256}$")


def _dedupe_artifacts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for a in items:
        key = (
            a.get("type"),
            a.get("href") or a.get("path") or "",
            a.get("filename") or "",
            a.get("name") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def is_safe_unified_id(unified_id: Any) -> bool:
    """校验 unified_id 不含路径成分，避免路径穿越。"""
    if not unified_id or not isinstance(unified_id, str):
        return False
    s = unified_id.strip()
    if not s or ".." in s or "/" in s or "\\" in s:
        return False
    return bool(_SAFE_UNIFIED_ID_RE.match(s))


class UnifiedReportStore:
    """
    A lightweight JSON store:
    - Index file: reports/unified/index.json  (list of summaries)
    - Detail file: reports/unified/<unified_id>.json  (full payload)
    """

    def __init__(self, base_dir: Optional[str] = None):
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self.base_dir = base_dir or os.path.join(root, "reports", "unified")
        self._index_path = os.path.join(self.base_dir, "index.json")
        self._lock = threading.RLock()
        os.makedirs(self.base_dir, exist_ok=True)

        # Ensure index exists
        if not os.path.exists(self._index_path):
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump({"reports": []}, f, ensure_ascii=False, indent=2)

    def _now(self) -> float:
        return time.time()

    def _read_index(self) -> Dict[str, Any]:
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                return json.load(f) or {"reports": []}
        except Exception:
            return {"reports": []}

    def _write_index(self, data: Dict[str, Any]) -> None:
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save_report(
        self,
        unified_id: str,
        module: str,
        kind: str,
        status: str,
        summary: Dict[str, Any],
        details: Optional[Dict[str, Any]] = None,
        *,
        device_id: Optional[str] = None,
        package_name: Optional[str] = None,
        legacy_id: Optional[str] = None,
        runtime_id: Optional[str] = None,
        started_at: Optional[Any] = None,
        finished_at: Optional[Any] = None,
        artifacts: Optional[List[Dict[str, Any]]] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Persist a normalized report and update index.
        - unified_id should be stable (e.g. module + job_id or report_id)
        """
        if not is_safe_unified_id(unified_id):
            raise ValueError("unified_id contains invalid characters or path components")
        payload = {
            "unified_id": unified_id,
            "module": module,
            "kind": kind,
            "status": status,
            "device_id": device_id,
            "package_name": package_name,
            "legacy_id": legacy_id,
            "runtime_id": runtime_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "summary": summary or {},
            "details": details or {},
            "artifacts": artifacts or [],
            "raw": raw or {},
            "updated_at": self._now(),
        }

        detail_path = os.path.join(self.base_dir, f"{unified_id}.json")
        with self._lock:
            with open(detail_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            idx = self._read_index()
            reports: List[Dict[str, Any]] = idx.get("reports") or []

            # Upsert summary row
            row = {
                "unified_id": unified_id,
                "module": module,
                "kind": kind,
                "status": status,
                "device_id": device_id,
                "package_name": package_name,
                "legacy_id": legacy_id,
                "runtime_id": runtime_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "summary": summary or {},
                "updated_at": payload["updated_at"],
            }

            replaced = False
            for i, r in enumerate(reports):
                if r.get("unified_id") == unified_id:
                    reports[i] = row
                    replaced = True
                    break
            if not replaced:
                reports.append(row)

            # Sort by updated_at desc
            reports.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
            idx["reports"] = reports
            self._write_index(idx)

        # Stream Bus：报告保存后推送事件，供 Dashboard 等订阅
        try:
            from shared.core.stream_bus import get_stream_bus
            get_stream_bus().publish(module, "report", {"unified_id": unified_id, "status": status, "kind": kind})
        except Exception:
            pass

        # 单模块报告（monkey / performance_monitor）完成后异步生成「下次测试方向」推荐
        if module in ("monkey", "performance_monitor") and str(status or "").lower() in ("finished", "completed", "success"):
            if os.environ.get("ENABLE_NEXT_TEST_RECOMMENDATION", "1").strip().lower() in ("1", "true", "yes"):
                store_ref = self
                uid = unified_id

                def _single_report_recommendation_task():
                    try:
                        from shared.unified.recommendation_engine import generate_single_report_recommendation
                        report = store_ref.get_report(uid)
                        if report:
                            rec = generate_single_report_recommendation(report)
                            if rec:
                                store_ref.update_report_details(uid, {"recommendations": rec})
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning("single-report recommendation: %s", e)

                t = threading.Thread(target=_single_report_recommendation_task, daemon=True)
                t.start()

        return unified_id

    def cleanup_stale_running_reports(self, max_age_hours: int = 24) -> int:
        """
        Scan index for reports marked as 'running' that are older than max_age_hours.
        Mark them as 'unknown' (or 'stopped').
        Returns count of updated reports.
        """
        count = 0
        now = self._now()
        with self._lock:
            idx = self._read_index()
            reports = idx.get("reports") or []
            changed = False
            
            for r in reports:
                status = str(r.get("status", "")).lower()
                if status == "running":
                    updated_at = r.get("updated_at") or r.get("started_at") or 0
                    if (now - updated_at) > (max_age_hours * 3600):
                        r["status"] = "unknown"
                        # Also update detail file if exists
                        uid = r.get("unified_id")
                        if uid:
                            self._mark_report_stopped(uid, "unknown", "Stale report auto-cleanup")
                        changed = True
                        count += 1
            
            if changed:
                self._write_index(idx)
        return count

    def _mark_report_stopped(self, unified_id: str, status: str, reason: str):
        detail_path = os.path.join(self.base_dir, f"{unified_id}.json")
        try:
            if os.path.exists(detail_path):
                with open(detail_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                payload["status"] = status
                payload["updated_at"] = self._now()
                # Add note to details
                details = payload.get("details") or {}
                details["stop_reason"] = reason
                payload["details"] = details
                with open(detail_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def list_reports(
        self,
        *,
        module: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List reports with optional filters. keyword matches unified_id, legacy_id, device_id, package_name."""
        with self._lock:
            idx = self._read_index()
            reports: List[Dict[str, Any]] = idx.get("reports") or []
            if module:
                reports = [r for r in reports if r.get("module") == module]
            if kind:
                reports = [r for r in reports if r.get("kind") == kind]
            if status:
                status_lower = str(status).strip().lower()
                reports = [r for r in reports if str(r.get("status", "")).lower() == status_lower]
            if keyword:
                kw = str(keyword).strip().lower()
                if kw:
                    def _matches(r):
                        hay = " ".join(
                            str(x or "") for x in [
                                r.get("unified_id"),
                                r.get("legacy_id"),
                                r.get("device_id"),
                                r.get("package_name"),
                                r.get("module"),
                                r.get("kind"),
                            ]
                        ).lower()
                        return kw in hay
                    reports = [r for r in reports if _matches(r)]
            limit = max(0, min(int(limit), 500))
            return reports[:limit]

    def get_report(self, unified_id: str) -> Optional[Dict[str, Any]]:
        if not is_safe_unified_id(unified_id):
            return None
        detail_path = os.path.join(self.base_dir, f"{unified_id}.json")
        try:
            with open(detail_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def update_report_details(self, unified_id: str, details_merge: Dict[str, Any]) -> bool:
        """
        将 details_merge 合并到已有报告的 details 中并写回。
        不修改 index，仅更新详情文件。用于追加 recommendations 等。
        """
        if not is_safe_unified_id(unified_id) or not details_merge:
            return False
        detail_path = os.path.join(self.base_dir, f"{unified_id}.json")
        with self._lock:
            try:
                with open(detail_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                return False
            details = payload.get("details") or {}
            for k, v in details_merge.items():
                details[k] = v
            payload["details"] = details
            payload["updated_at"] = self._now()
            try:
                with open(detail_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                return False
        return True

    def extend_report(
        self,
        unified_id: str,
        *,
        details_update: Optional[Dict[str, Any]] = None,
        artifacts_extend: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """向已有报告追加 details 字段并合并 artifacts（用于编排任务导出日志等）。"""
        if not is_safe_unified_id(unified_id):
            return False
        if not details_update and not artifacts_extend:
            return False
        detail_path = os.path.join(self.base_dir, f"{unified_id}.json")
        with self._lock:
            try:
                with open(detail_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                return False
            if details_update:
                d = payload.get("details") or {}
                for k, v in details_update.items():
                    d[k] = v
                payload["details"] = d
            if artifacts_extend:
                arts = list(payload.get("artifacts") or [])
                arts.extend(artifacts_extend)
                payload["artifacts"] = _dedupe_artifacts(arts)
            payload["updated_at"] = self._now()
            try:
                with open(detail_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            except Exception:
                return False

            idx = self._read_index()
            reports: List[Dict[str, Any]] = idx.get("reports") or []
            for i, r in enumerate(reports):
                if r.get("unified_id") == unified_id:
                    reports[i] = {**r, "updated_at": payload["updated_at"]}
                    break
            reports.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
            idx["reports"] = reports
            self._write_index(idx)
        return True


_STORE: Optional[UnifiedReportStore] = None
_STORE_LOCK = threading.RLock()


def get_unified_report_store() -> UnifiedReportStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = UnifiedReportStore()
        return _STORE

