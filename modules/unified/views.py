"""
Unified reporting API (read-only, additive).
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

from flask import Blueprint, request, render_template

from utils.response import success_response, error_response

try:
    from shared.unified.report_store import get_unified_report_store, is_safe_unified_id
except Exception:
    get_unified_report_store = None
    is_safe_unified_id = lambda x: False

try:
    from shared.unified.recommendation_engine import generate_next_test_recommendation
except Exception:
    generate_next_test_recommendation = None

try:
    from utils.llm_client import call_llm
except Exception:
    call_llm = None

try:
    from shared.unified.orchestrator import create_run, get_run, set_child, add_error, update_run, remove_run
except Exception:
    create_run = None
    get_run = None
    set_child = None
    add_error = None
    update_run = None
    remove_run = None


unified_bp = Blueprint("unified", __name__, template_folder="templates")
_START_LOCK = threading.RLock()


def _safe_get_json() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}

@unified_bp.route("/api/health", methods=["GET"])
def api_unified_health():
    """Simple health check for the additive unified module."""
    return success_response(
        data={
            "ok": True,
            "has_store": bool(get_unified_report_store),
            "has_orchestrator": bool(create_run and get_run and set_child),
        },
        message="unified module ready",
    )

@unified_bp.route("/", methods=["GET"])
def unified_index_page():
    return render_template("unified_index.html")

@unified_bp.route("/reports", methods=["GET"])
def unified_reports_page():
    return render_template("unified_reports.html")


@unified_bp.route("/report/<unified_id>", methods=["GET"])
def unified_report_detail_page(unified_id: str):
    return render_template("unified_report_detail.html", unified_id=unified_id)


@unified_bp.route("/api/start", methods=["POST"])
def api_unified_start():
    """
    One-click start multiple module tasks (additive).
    """
    data = _safe_get_json()
    return _start_unified_run(data)


def _start_unified_run(data: Dict[str, Any]):
    """内部实现：根据传入 data 启动一键任务，返回统一的 JSON 响应。"""
    if not (create_run and set_child and add_error):
        return error_response(message="Unified orchestrator not available", status_code=500)

    modules = data.get("modules") or []
    if isinstance(modules, str):
        modules = [modules]
    modules = [m for m in modules if isinstance(m, str)]
    if not modules:
        return error_response(message="modules 不能为空", error="modules required", status_code=400)

    device_id = data.get("device_id")
    package_name = data.get("package_name")

    with _START_LOCK:
        run_id = create_run(data)
        children: Dict[str, Any] = {}

        # 设备资源锁：需要 device 的模块先尝试占用
        try:
            from core.device import get_device_manager
            dm = get_device_manager()
            devices_to_acquire = []
            if device_id and any(m in modules for m in ("performance_monitor", "log_monitor", "ui_automation_suite")):
                devices_to_acquire.append(device_id)
            if "monkey" in modules:
                monkey_cfg = (data.get("monkey") or {})
                ip = monkey_cfg.get("ip")
                if ip:
                    port = int(monkey_cfg.get("port") or 8787)
                    devices_to_acquire.append(f"{ip}:{port}")
            for dev in set(devices_to_acquire):
                if dm.is_device_locked(dev):
                    owner = dm.get_device_owner(dev)
                    raise RuntimeError(f"设备 {dev} 已被任务占用: {owner}")
                if not dm.acquire_device(dev, run_id):
                    raise RuntimeError(f"设备 {dev} 占用失败")
        except ImportError:
            pass  # DeviceManager 不可用时跳过锁

        # Monkey
        if "monkey" in modules:
            try:
                from modules.monkey import views as monkey_views

                monkey_cfg = (data.get("monkey") or {})
                ip = monkey_cfg.get("ip")
                port = int(monkey_cfg.get("port") or 8787)
                if not ip:
                    raise ValueError("monkey.ip required")

                device_key = f"{ip}:{port}"
                with monkey_views.monkey_tests_lock:
                    if device_key in monkey_views.monkey_tests:
                        raise RuntimeError(f"device already running monkey: {device_key}")

                if not monkey_views.check_device_connection(ip, port):
                    if not monkey_views.connect_adb_device(ip, port):
                        raise RuntimeError(f"device not connected: {device_key}")

                tr = monkey_views.MonkeyTestResult(ip, port)
                tr.package_name = monkey_cfg.get("package_name") or package_name or tr.package_name
                ec = monkey_cfg.get("events_count")
                if ec is not None and ec != "":
                    try:
                        tr.events_planned = int(ec)
                    except (ValueError, TypeError):
                        pass
                throttle = int(monkey_cfg.get("throttle") or 1000)
                timeout = int(monkey_cfg.get("timeout") or 3600)
                tr.start_time = monkey_views.datetime.now()
                tr.status = monkey_views.MonkeyTestResult.STATUS_RUNNING
                tr.unified_run_id = run_id  # 用于 Monkey 完成后回调：同时结束性能监控并生成报告

                t = threading.Thread(
                    target=monkey_views.run_monkey_test_background,
                    args=(tr, tr.events_planned, throttle, timeout),
                    daemon=True,
                )
                with monkey_views.monkey_tests_lock:
                    monkey_views.monkey_tests[device_key] = tr
                with monkey_views.device_threads_lock:
                    monkey_views.DEVICE_THREADS[device_key] = t
                t.start()

                child = {
                    "module": "monkey",
                    "device_key": device_key,
                    "report_id": tr.report_id,
                    "runtime_id": getattr(tr, "runtime_id", None),
                    "unified_report_id": f"monkey_{tr.report_id}",
                }
                set_child(run_id, "monkey", child)
                children["monkey"] = child
            except Exception as e:
                add_error(run_id, "monkey", str(e))
                children["monkey"] = {"error": str(e)}

        # Performance monitor
        if "performance_monitor" in modules:
            try:
                from modules.performance_monitor.views import PERFORMANCE_SERVICE, PERFORMANCE_SERVICE_LOCK

                perf_cfg = (data.get("performance_monitor") or {})
                task_id = perf_cfg.get("task_id") or f"perf_{int(time.time())}"
                polling_interval = float(perf_cfg.get("polling_interval", 3.0))
                monitor_type = perf_cfg.get("monitor_type", "video")
                description = perf_cfg.get("description", "")

                if not device_id or not package_name:
                    raise ValueError("device_id and package_name required for performance_monitor")

                with PERFORMANCE_SERVICE_LOCK:
                    ok = PERFORMANCE_SERVICE.start_monitoring(
                        task_id=task_id,
                        device_id=device_id,
                        package_name=package_name,
                        description=description,
                        polling_interval=polling_interval,
                        monitor_type=monitor_type,
                    )
                    if not ok:
                        raise RuntimeError("performance_monitor start_monitoring failed (task exists?)")
                    info = PERFORMANCE_SERVICE.get_task_info(task_id) or {}

                child = {"module": "performance_monitor", "task_id": task_id, "session_id": info.get("session_id")}
                set_child(run_id, "performance_monitor", child)
                children["performance_monitor"] = child
            except Exception as e:
                add_error(run_id, "performance_monitor", str(e))
                children["performance_monitor"] = {"error": str(e)}

        # Log monitor
        if "log_monitor" in modules:
            try:
                from modules.log_monitor import views as log_views

                log_cfg = (data.get("log_monitor") or {})
                task_id = log_cfg.get("task_id") or f"log_monitor_{int(time.time())}"
                min_log_level = log_cfg.get("min_log_level", "Verbose")
                target_package = log_cfg.get("target_package") or package_name or "com.thunder.ktv"

                if not device_id:
                    raise ValueError("device_id required for log_monitor")

                with log_views.MONITOR_TASKS_LOCK:
                    if task_id in log_views.MONITOR_TASKS:
                        raise RuntimeError("log_monitor task already running")

                controller = log_views.AdbController()
                alert_engine = log_views.AlertEngine()

                log_queue = []
                log_queue_lock = threading.Lock()
                alert_queue = []
                alert_queue_lock = threading.Lock()

                def log_callback(log_line, analysis_result):
                    with log_queue_lock:
                        log_queue.append(
                            {
                                "log": log_line,
                                "analysis": analysis_result[0] if analysis_result else None,
                                "timestamp": time.time(),
                            }
                        )
                    alerts = alert_engine.check_log(log_line, device_id, target_package)
                    if alerts:
                        with alert_queue_lock:
                            for alert in alerts:
                                alert_queue.append(alert.to_dict())

                controller.start_monitoring(
                    device_id=device_id,
                    log_callback=log_callback,
                    min_log_level=min_log_level,
                    target_package=target_package,
                )

                with log_views.MONITOR_TASKS_LOCK:
                    log_views.MONITOR_TASKS[task_id] = {
                        "controller": controller,
                        "device_id": device_id,
                        "start_time": time.time(),
                        "log_queue": log_queue,
                        "log_queue_lock": log_queue_lock,
                        "alert_queue": alert_queue,
                        "alert_queue_lock": alert_queue_lock,
                        "alert_engine": alert_engine,
                        "target_package": target_package,
                    }

                child = {"module": "log_monitor", "task_id": task_id}
                set_child(run_id, "log_monitor", child)
                children["log_monitor"] = child
            except Exception as e:
                add_error(run_id, "log_monitor", str(e))
                children["log_monitor"] = {"error": str(e)}

        # UI automation suite
        if "ui_automation_suite" in modules:
            try:
                from modules.ui_automation.views import UI_AUTOMATION_SERVICE

                ui_cfg = (data.get("ui_automation_suite") or {})
                suite_id = ui_cfg.get("suite_id")
                ui_device_id = ui_cfg.get("device_id") or device_id
                if not suite_id or not ui_device_id:
                    raise ValueError("ui_automation_suite.suite_id and device_id required")

                job_id = UI_AUTOMATION_SERVICE.run_suite(suite_id, ui_device_id)
                if not job_id:
                    raise RuntimeError("ui_automation_suite run_suite failed")

                child = {
                    "module": "ui_automation",
                    "kind": "suite",
                    "suite_id": suite_id,
                    "job_id": job_id,
                    "unified_report_id": f"ui_automation_suite_{job_id}",
                }
                set_child(run_id, "ui_automation_suite", child)
                children["ui_automation_suite"] = child
            except Exception as e:
                add_error(run_id, "ui_automation_suite", str(e))
                children["ui_automation_suite"] = {"error": str(e)}

        # Server stress (ARM)
        if "server_stress" in modules:
            try:
                from modules.server_stress.views import get_stress_manager

                ss_cfg = (data.get("server_stress") or {})
                server_id = ss_cfg.get("server_id")
                if not server_id:
                    raise ValueError("server_stress.server_id required")

                cpu_cores = int(ss_cfg.get("cpu_cores", 0))
                cpu_load = int(ss_cfg.get("cpu_load", 80))
                timeout = int(ss_cfg.get("timeout", 60))

                sm = get_stress_manager()
                success, msg = sm.start_stress(server_id, cpu_cores, cpu_load, timeout)
                if not success:
                    raise RuntimeError(msg)

                job = sm.active_stress_jobs.get(server_id, {})
                runtime_id = job.get("runtime_id")
                child = {
                    "module": "server_stress",
                    "server_id": server_id,
                    "runtime_id": runtime_id,
                    "unified_report_id": f"server_stress_{run_id}_{server_id}",
                }
                set_child(run_id, "server_stress", child)
                children["server_stress"] = child
            except Exception as e:
                add_error(run_id, "server_stress", str(e))
                children["server_stress"] = {"error": str(e)}

        # Persist orchestration row (best-effort)
        if get_unified_report_store:
            try:
                get_unified_report_store().save_report(
                    unified_id=run_id,
                    module="unified",
                    kind="orchestration",
                    status="running",
                    summary={"modules": modules},
                    details={"children": children},
                    device_id=device_id,
                    package_name=package_name,
                    legacy_id=run_id,
                    started_at=time.time(),
                    raw={"request": data},
                )
            except Exception:
                pass

        return success_response(data={"run_id": run_id, "children": children}, message="unified run started")


@unified_bp.route("/api/linked_stress/start", methods=["POST"])
def api_unified_linked_stress_start():
    """
    联动压测场景：
    - 服务器压测（server_stress）
    - 设备侧 Monkey 或播放器压测
    - 可选挂性能监控 / 日志监控

    这里主要做参数整理与校验，实际启动仍复用 _start_unified_run，
    避免影响现有 /unified/api/start 行为。
    """
    data = _safe_get_json()
    mode = (data.get("mode") or "monkey").lower()
    server_cfg = data.get("server") or {}
    device_cfg = data.get("device") or {}
    observers = data.get("observers") or {}

    server_id = server_cfg.get("server_id")
    device_id = device_cfg.get("device_id")

    # 基本必填校验
    if not server_id or not device_id:
        return error_response(
            message="server.server_id 和 device.device_id 不能为空",
            error="server/device required",
            status_code=400,
        )

    # 组装 modules 列表
    modules: list[str] = ["server_stress"]
    if mode == "monkey":
        modules.append("monkey")
    elif mode == "player":
        # 预留：未来如果 unified 支持 player_stress，可在此启动
        modules.append("player_stress")

    if observers.get("performance_monitor"):
        modules.append("performance_monitor")
    if observers.get("log_monitor"):
        modules.append("log_monitor")

    # 转换为 unified_start 能理解的 payload
    unified_payload: Dict[str, Any] = {
        "modules": modules,
        "device_id": device_id,
        "package_name": device_cfg.get("package_name"),
        "server_stress": server_cfg,
    }

    if "monkey" in modules:
        unified_payload["monkey"] = device_cfg.get("monkey") or {}

    # 观测模块目前使用各自默认配置，后续如需细化可在 observers 字段扩展

    return _start_unified_run(unified_payload)


def _get_monkey_child_status(child: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from modules.monkey import views as monkey_views
        device_key = child.get("device_key")
        with monkey_views.monkey_tests_lock:
            tr = monkey_views.monkey_tests.get(device_key)
            if tr:
                return {"status": "running", "test_info": tr.to_dict()}
        rid = child.get("report_id")
        with monkey_views.reports_lock:
            found = next((r for r in monkey_views.REPORTS if r.get("report_id") == rid), None)
        if found:
            return {"status": "finished", "report": found}
        return {"status": "unknown"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_perf_child_status(child: Dict[str, Any]) -> Dict[str, Any]:
    try:
        # 若由一键任务在 Monkey 完成后已结束性能监控，则视为 finished
        if child.get("finished_by_unified"):
            return {"status": "finished", "report": {"unified_report_id": child.get("unified_report_id")}}
        from modules.performance_monitor.views import PERFORMANCE_SERVICE, PERFORMANCE_SERVICE_LOCK
        task_id = child.get("task_id")
        with PERFORMANCE_SERVICE_LOCK:
            info = PERFORMANCE_SERVICE.get_task_info(task_id)
            if info:
                return {"status": "running", "task": {"task_id": task_id, "session_id": info.get("session_id")}}
        # 任务已不存在且曾保存过编排报告 ID，说明已由 Monkey 完成回调结束
        if child.get("unified_report_id"):
            return {"status": "finished", "report": {"unified_report_id": child.get("unified_report_id")}}
        return {"status": "stopped_or_missing"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_log_child_status(child: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from modules.log_monitor import views as log_views
        task_id = child.get("task_id")
        with log_views.MONITOR_TASKS_LOCK:
            info = log_views.MONITOR_TASKS.get(task_id)
            if info:
                return {"status": "running", "task": {"task_id": task_id, "device_id": info.get("device_id")}}
        return {"status": "stopped_or_missing"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_ui_suite_child_status(child: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from modules.ui_automation.views import UI_AUTOMATION_SERVICE
        job_id = child.get("job_id")
        status = UI_AUTOMATION_SERVICE.get_suite_job_status(job_id)
        if status:
            s = status.get("status")
            if s in ("completed", "failed", "error", "stopped"):
                return {"status": "finished", "job": status}
            return {"status": "running", "job": status}
        return {"status": "unknown"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _get_server_stress_child_status(child: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from modules.server_stress.views import get_stress_manager
        server_id = child.get("server_id")
        sm = get_stress_manager()
        job = sm.active_stress_jobs.get(server_id) if server_id else None
        if job and job.get("status") == "running":
            return {"status": "running", "job": {"server_id": server_id, "runtime_id": job.get("runtime_id")}}
        return {"status": "finished"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _build_orchestration_artifacts(children: Dict[str, Any]) -> list:
    artifacts = []
    for key, child in (children or {}).items():
        if not isinstance(child, dict):
            continue
        unified_id = child.get("unified_report_id")
        if unified_id:
            artifacts.append(
                {
                    "type": "unified_report",
                    "module": key,
                    "unified_id": unified_id,
                    "path": f"/unified/api/reports/{unified_id}",
                }
            )

        # Also expose legacy identifiers for convenience
        for legacy_field in ("report_id", "job_id", "task_id", "session_id", "runtime_id", "server_id"):
            if child.get(legacy_field):
                artifacts.append(
                    {
                        "type": "legacy_ref",
                        "module": key,
                        "name": legacy_field,
                        "value": child.get(legacy_field),
                    }
                )
    return artifacts


def _summarize_child_statuses(statuses: Dict[str, Any]) -> Dict[str, Any]:
    summary = {"children": {}}
    for k, v in (statuses or {}).items():
        if not isinstance(v, dict):
            summary["children"][k] = {"status": "unknown"}
            continue
        entry = {"status": v.get("status")}
        # Pull a small set of high-signal fields per module
        if k == "monkey":
            report = v.get("report") or {}
            test_info = v.get("test_info") or {}
            src = report or test_info
            if isinstance(src, dict):
                entry.update(
                    {
                        "monkey_status": src.get("status"),
                        "events_executed": src.get("events_executed"),
                        "events_planned": src.get("events_planned"),
                        "crash_count": src.get("crash_count"),
                        "anr_count": src.get("anr_count"),
                        "is_successful": src.get("is_successful"),
                    }
                )
        elif k == "ui_automation_suite":
            job = v.get("job") or {}
            if isinstance(job, dict):
                entry.update(
                    {
                        "suite_status": job.get("status"),
                        "pass_rate": job.get("pass_rate"),
                        "total_cases": job.get("total_cases"),
                        "current_case_index": job.get("current_case_index"),
                    }
                )
        elif k == "performance_monitor":
            task = v.get("task") or {}
            if isinstance(task, dict):
                entry.update({"task_id": task.get("task_id"), "session_id": task.get("session_id")})
        elif k == "log_monitor":
            task = v.get("task") or {}
            if isinstance(task, dict):
                entry.update({"task_id": task.get("task_id"), "device_id": task.get("device_id")})
        elif k == "server_stress":
            job = v.get("job") or {}
            if isinstance(job, dict):
                entry.update({"server_id": job.get("server_id"), "runtime_id": job.get("runtime_id")})

        if v.get("error"):
            entry["error"] = v.get("error")

        summary["children"][k] = entry
    return summary


@unified_bp.route("/api/status/<run_id>", methods=["GET"])
def api_unified_status(run_id: str):
    if not (get_run and update_run):
        return error_response(message="Unified orchestrator not available", status_code=500)
    run = get_run(run_id)
    if not run:
        return error_response(message="Run not found", error="not found", status_code=404)

    children = run.get("children") or {}
    statuses: Dict[str, Any] = {}

    if "monkey" in children:
        statuses["monkey"] = _get_monkey_child_status(children["monkey"])
    if "performance_monitor" in children:
        statuses["performance_monitor"] = _get_perf_child_status(children["performance_monitor"])
    if "log_monitor" in children:
        statuses["log_monitor"] = _get_log_child_status(children["log_monitor"])
    if "ui_automation_suite" in children:
        statuses["ui_automation_suite"] = _get_ui_suite_child_status(children["ui_automation_suite"])
    if "server_stress" in children:
        statuses["server_stress"] = _get_server_stress_child_status(children["server_stress"])

    overall = "running"
    terminal = True
    for s in statuses.values():
        st = s.get("status")
        if st in ("running", "unknown"):
            terminal = False
    if terminal and statuses:
        overall = "finished"

    update_run(run_id, status=overall)

    # 任务结束时释放设备资源锁
    if overall == "finished":
        try:
            from core.device import get_device_manager
            dm = get_device_manager()
            req = run.get("request", {}) or {}
            if req.get("device_id"):
                dm.release_device(req["device_id"], run_id)
            for key, child in (children or {}).items():
                if isinstance(child, dict):
                    dk = child.get("device_key") or child.get("device_id")
                    if dk:
                        dm.release_device(dk, run_id)
        except Exception:
            pass

    if get_unified_report_store:
        try:
            artifacts = _build_orchestration_artifacts(children)
            child_summary = _summarize_child_statuses(statuses)
            store = get_unified_report_store()
            store.save_report(
                unified_id=run_id,
                module="unified",
                kind="orchestration",
                status=overall,
                summary={
                    "modules": list(children.keys()),
                    "overall_status": overall,
                    **child_summary,
                },
                details={
                    "children": children,
                    "statuses": statuses,
                    "errors": run.get("errors") or [],
                },
                device_id=run.get("request", {}).get("device_id"),
                package_name=run.get("request", {}).get("package_name"),
                legacy_id=run_id,
                started_at=run.get("created_at"),
                finished_at=time.time() if overall == "finished" else None,
                artifacts=artifacts,
            )
            # 压测完成后自动生成「下次测试方向」推荐（仅编排任务且 overall==finished）
            if overall == "finished" and generate_next_test_recommendation and os.environ.get("ENABLE_NEXT_TEST_RECOMMENDATION", "1").strip() in ("1", "true", "yes"):
                try:
                    report = store.get_report(run_id)
                    if report:
                        use_llm = os.environ.get("RECOMMENDATION_USE_LLM", "0").strip().lower() in ("1", "true", "yes")
                        rec = generate_next_test_recommendation(report, store.get_report, use_llm=use_llm)
                        if rec and store.update_report_details(run_id, {"recommendations": rec}):
                            pass  # 已写入 details.recommendations
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("Unified: next-test recommendation failed: %s", e)
        except Exception:
            pass

    return success_response(data={"run": run, "overall_status": overall, "statuses": statuses})


@unified_bp.route("/api/stop/<run_id>", methods=["POST"])
def api_unified_stop(run_id: str):
    if not (get_run and update_run):
        return error_response(message="Unified orchestrator not available", status_code=500)
    run = get_run(run_id)
    if not run:
        return error_response(message="Run not found", error="not found", status_code=404)

    children = run.get("children") or {}
    stop_results: Dict[str, Any] = {}

    if "monkey" in children:
        try:
            from modules.monkey import views as monkey_views
            device_key = children["monkey"].get("device_key")
            with monkey_views.monkey_tests_lock:
                tr = monkey_views.monkey_tests.get(device_key)
                if tr:
                    tr.status = monkey_views.MonkeyTestResult.STATUS_STOPPED
                    tr.status_reason = "Unified stop"
                    if getattr(tr, "monkey_process", None):
                        try:
                            tr.monkey_process.terminate()
                        except Exception:
                            pass
            if device_key:
                monkey_views.kill_monkey_process(device_key)
            stop_results["monkey"] = {"stopped": True}
        except Exception as e:
            stop_results["monkey"] = {"stopped": False, "error": str(e)}

    if "performance_monitor" in children:
        try:
            from modules.performance_monitor.views import PERFORMANCE_SERVICE, PERFORMANCE_SERVICE_LOCK
            task_id = children["performance_monitor"].get("task_id")
            with PERFORMANCE_SERVICE_LOCK:
                ok = PERFORMANCE_SERVICE.stop_monitoring(task_id)
            stop_results["performance_monitor"] = {"stopped": bool(ok)}
        except Exception as e:
            stop_results["performance_monitor"] = {"stopped": False, "error": str(e)}

    if "log_monitor" in children:
        try:
            from modules.log_monitor import views as log_views
            task_id = children["log_monitor"].get("task_id")
            with log_views.MONITOR_TASKS_LOCK:
                info = log_views.MONITOR_TASKS.get(task_id)
                if info:
                    info["controller"].stop_monitoring()
                    del log_views.MONITOR_TASKS[task_id]
            stop_results["log_monitor"] = {"stopped": True}
        except Exception as e:
            stop_results["log_monitor"] = {"stopped": False, "error": str(e)}

    if "ui_automation_suite" in children:
        try:
            from modules.ui_automation.views import UI_AUTOMATION_SERVICE
            job_id = children["ui_automation_suite"].get("job_id")
            ok = UI_AUTOMATION_SERVICE.stop_suite_job(job_id)
            stop_results["ui_automation_suite"] = {"stopped": bool(ok)}
        except Exception as e:
            stop_results["ui_automation_suite"] = {"stopped": False, "error": str(e)}

    if "server_stress" in children:
        try:
            from modules.server_stress.views import get_stress_manager
            server_id = children["server_stress"].get("server_id")
            sm = get_stress_manager()
            ok, _ = sm.stop_stress(server_id)
            stop_results["server_stress"] = {"stopped": bool(ok)}
        except Exception as e:
            stop_results["server_stress"] = {"stopped": False, "error": str(e)}

    update_run(run_id, status="stopped")

    # 释放设备资源锁
    try:
        from core.device import get_device_manager
        dm = get_device_manager()
        run = get_run(run_id)
        req = run.get("request", {}) or {}
        dev_id = req.get("device_id")
        if dev_id:
            dm.release_device(dev_id, run_id)
        for key, child in (children or {}).items():
            if isinstance(child, dict):
                dk = child.get("device_key") or child.get("device_id")
                if dk:
                    dm.release_device(dk, run_id)
    except Exception:
        pass

    return success_response(data={"run_id": run_id, "stop_results": stop_results}, message="unified run stop issued")


@unified_bp.route("/api/runs/<run_id>", methods=["DELETE"])
def api_unified_delete_run(run_id: str):
    """移除运行记录（清理已失效/不存在的任务）"""
    if not remove_run:
        return error_response(message="Orchestrator not available", status_code=500)
    removed = remove_run(run_id)
    return success_response(data={"run_id": run_id, "removed": removed}, message="已移除" if removed else "记录不存在")


@unified_bp.route("/api/reports", methods=["GET"])
def api_list_unified_reports():
    if not get_unified_report_store:
        return error_response(message="Unified report store not available", status_code=500)
    try:
        # Trigger stale cleanup on list (lightweight enough)
        store = get_unified_report_store()
        store.cleanup_stale_running_reports(max_age_hours=24)
        
        module = request.args.get("module")
        kind = request.args.get("kind")
        status = request.args.get("status")
        keyword = request.args.get("keyword") or request.args.get("kw")
        limit = request.args.get("limit", default=100, type=int)
        if limit is None:
            limit = 100
        reports = store.list_reports(
            module=module, kind=kind, status=status, keyword=keyword, limit=limit
        )
        return success_response(data={"reports": reports})
    except Exception as e:
        return error_response(message="Failed to list reports", error=str(e), status_code=500)


def _report_context_for_llm(report: Dict[str, Any], max_chars: int = 8000) -> str:
    """从统一报告构建给 LLM 的文本摘要（控制长度避免超 token）。"""
    parts = []
    parts.append(f"报告ID: {report.get('unified_id', '')}")
    parts.append(f"模块: {report.get('module', '')}  类型: {report.get('kind', '')}  状态: {report.get('status', '')}")
    parts.append(f"设备: {report.get('device_id', '')}  包名: {report.get('package_name', '')}")
    parts.append(f"开始: {report.get('started_at')}  结束: {report.get('finished_at')}")
    if report.get("summary"):
        parts.append("摘要: " + json.dumps(report["summary"], ensure_ascii=False))
    details = report.get("details") or {}
    if details:
        if report.get("module") == "unified" and details.get("statuses"):
            for k, v in details["statuses"].items():
                err = v.get("error")
                sub = f"  [{k}] 状态={v.get('status')}"
                if err:
                    sub += f" 错误={err}"
                if v.get("report"):
                    sub += " " + json.dumps(v["report"], ensure_ascii=False)[:500]
                parts.append(sub)
        else:
            parts.append("详情: " + json.dumps(details, ensure_ascii=False)[:3000])
    raw = report.get("raw") or {}
    if raw:
        parts.append("原始片段: " + json.dumps(raw, ensure_ascii=False)[:2000])
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(已截断)"
    return text


@unified_bp.route("/api/reports/<unified_id>", methods=["GET"])
def api_get_unified_report(unified_id: str):
    if not get_unified_report_store:
        return error_response(message="Unified report store not available", status_code=500)
    if not is_safe_unified_id(unified_id):
        return error_response(message="Invalid report id", error="invalid unified_id", status_code=400)
    try:
        report = get_unified_report_store().get_report(unified_id)
        if not report:
            return error_response(message="Report not found", error="not found", status_code=404)
        return success_response(data={"report": report})
    except Exception as e:
        return error_response(message="Failed to get report", error=str(e), status_code=500)


def _build_fallback_analysis(report: Dict[str, Any]) -> str:
    """LLM 不可用时的规则摘要，便于用户仍能看到结论与建议。"""
    parts = []
    s = report.get("summary") or {}
    mod = report.get("module")
    status = report.get("status")
    parts.append(f"【规则摘要】模块: {mod}，状态: {status}")

    if report.get("details", {}).get("recommendations"):
        rec = report["details"]["recommendations"]
        parts.append("\n下次测试方向: " + (rec.get("next_test_direction") or ""))
        if rec.get("focus_areas"):
            parts.append("关注维度: " + "、".join(rec["focus_areas"]))
        if rec.get("suggestions"):
            parts.append("建议: " + "; ".join(rec["suggestions"][:3]))
        return "\n".join(parts)

    if mod == "monkey":
        crash = s.get("crash_count", 0)
        anr = s.get("anr_count", 0)
        mem = s.get("mem_mb") or {}
        parts.append(f"崩溃: {crash}，ANR: {anr}，内存变化: {mem.get('delta')} MB")
        if crash or anr:
            parts.append("建议: 查看日志与堆栈，复现后针对性修复。")
        elif isinstance(mem.get("delta"), (int, float)) and mem["delta"] >= 50:
            parts.append("建议: 关注内存泄漏，可做长时间运行或 Profiler 分析。")
        else:
            parts.append("建议: 按计划做常规回归。")
        return "\n".join(parts)

    if mod == "performance_monitor":
        cpu = (s.get("cpu") or {}).get("avg")
        mem = (s.get("memory") or {}).get("max_mb")
        fps = (s.get("fps") or {}).get("avg")
        parts.append(f"CPU 均值: {cpu}%，内存峰值: {mem} MB，FPS 均值: {fps}")
        if isinstance(cpu, (int, float)) and cpu >= 80:
            parts.append("建议: 排查主线程耗时与 CPU 占用。")
        elif isinstance(mem, (int, float)) and mem >= 400:
            parts.append("建议: 关注内存与泄漏。")
        else:
            parts.append("建议: 指标在可接受范围，可继续观察。")
        return "\n".join(parts)

    if mod == "unified" and report.get("kind") == "orchestration":
        children = (report.get("details") or {}).get("statuses") or {}
        parts.append("子任务: " + ", ".join(f"{k}={v.get('status')}" for k, v in children.items()))
        parts.append("建议: 查看各子报告详情与「下次测试方向」卡片。")
        return "\n".join(parts)

    parts.append("摘要: " + json.dumps(s, ensure_ascii=False)[:500])
    return "\n".join(parts)


@unified_bp.route("/api/reports/<unified_id>/analyze", methods=["POST"])
def api_analyze_report(unified_id: str):
    """对报告做智能分析：优先 LLM；失败时返回规则摘要（from_llm=False, fallback=True）。"""
    if not get_unified_report_store:
        return error_response(message="Unified report store not available", status_code=500)
    if not is_safe_unified_id(unified_id):
        return error_response(message="Invalid report id", error="invalid unified_id", status_code=400)
    try:
        report = get_unified_report_store().get_report(unified_id)
        if not report:
            return error_response(message="Report not found", error="not found", status_code=404)
    except Exception as e:
        return error_response(message="Failed to get report", error=str(e), status_code=500)

    context = _report_context_for_llm(report)
    system_prompt = (
        "你是测试报告分析助手。根据下面给出的报告摘要（来自自动化测试平台），用中文简洁回答：\n"
        "1) 简要结论：本次任务整体是通过、失败还是存在异常；\n"
        "2) 可能原因：若有失败、崩溃、ANR 或异常，请分析最可能的原因；\n"
        "3) 建议：下一步可采取的操作（如重跑、查看日志、检查某模块、联系开发等）。\n"
        "回答要简短、分条、便于阅读，不要重复报告原文。"
    )

    if call_llm:
        try:
            result = call_llm(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": context},
                ],
                timeout=90,
            )
            text = (result or "").strip()
            if text:
                return success_response(data={"analysis": text, "from_llm": True, "fallback": False})
        except Exception as e:
            import logging
            logging.getLogger(__name__).info("analyze LLM failed, using fallback: %s", e)

    fallback = _build_fallback_analysis(report)
    return success_response(
        data={"analysis": fallback, "from_llm": False, "fallback": True},
        message="本次为规则摘要，LLM 暂不可用或超时",
    )


@unified_bp.route("/api/reports/<unified_id>/interpret_crash", methods=["POST"])
def api_interpret_crash(unified_id: str):
    """Monkey 报告崩溃/ANR 解读：根据 error_details、status_reason 用 LLM 给出可能原因与排查建议。"""
    if not get_unified_report_store:
        return error_response(message="Unified report store not available", status_code=500)
    if not is_safe_unified_id(unified_id):
        return error_response(message="Invalid report id", error="invalid unified_id", status_code=400)
    try:
        report = get_unified_report_store().get_report(unified_id)
        if not report:
            return error_response(message="Report not found", error="not found", status_code=404)
    except Exception as e:
        return error_response(message="Failed to get report", error=str(e), status_code=500)

    if report.get("module") != "monkey":
        return error_response(message="仅支持 Monkey 报告", error="module must be monkey", status_code=400)
    summary = report.get("summary") or {}
    crash_count = summary.get("crash_count") or 0
    anr_count = summary.get("anr_count") or 0
    if crash_count == 0 and anr_count == 0:
        return error_response(message="该报告无崩溃/ANR 记录", error="no crash or anr", status_code=400)

    details = report.get("details") or {}
    error_details = details.get("error_details") or []
    status_reason = details.get("status_reason") or ""
    raw_monkey = (report.get("raw") or {}).get("monkey") or {}
    monkey_output_snippet = (raw_monkey.get("monkey_output") or "")[-4000:]  # 末尾 4000 字符常含堆栈

    parts = [
        f"崩溃次数: {crash_count}，ANR 次数: {anr_count}",
        f"状态原因: {status_reason}",
    ]
    if error_details:
        parts.append("错误/崩溃片段:\n" + "\n".join(str(x) for x in error_details[:30]))
    if monkey_output_snippet:
        parts.append("Monkey 输出末尾片段:\n" + monkey_output_snippet)
    context = "\n\n".join(parts)[:8000]

    if not call_llm:
        return error_response(
            message="LLM 未配置，无法解读",
            error="请配置 llm_config.json 与 API Key",
            status_code=503,
        )
    try:
        prompt = (
            "以下是一份 Android Monkey 测试中的崩溃/ANR 信息。请用简短中文回答：\n"
            "1) 可能原因：根据堆栈或 NOT RESPONDING 信息，推断最可能的原因（如空指针、主线程阻塞、某 SDK 等）；\n"
            "2) 排查建议：建议重点查看的代码模块、日志关键字或复现步骤。\n"
            "回答分条、简洁，不要大段复制原文。\n\n"
            "---\n\n" + context
        )
        result = call_llm([{"role": "user", "content": prompt}], timeout=60)
        text = (result or "").strip()
        if not text:
            return error_response(message="LLM 未返回内容", status_code=500)
        return success_response(data={"interpretation": text})
    except Exception as e:
        return error_response(message="解读失败", error=str(e), status_code=500)


@unified_bp.route("/api/reports/<unified_id>/interpret", methods=["POST"])
def api_interpret_report(unified_id: str):
    """报告通俗解读：用 2～3 段人话解释报告内容、关键指标与建议，适合非技术人员。"""
    if not get_unified_report_store:
        return error_response(message="Unified report store not available", status_code=500)
    if not is_safe_unified_id(unified_id):
        return error_response(message="Invalid report id", error="invalid unified_id", status_code=400)
    try:
        report = get_unified_report_store().get_report(unified_id)
        if not report:
            return error_response(message="Report not found", error="not found", status_code=404)
    except Exception as e:
        return error_response(message="Failed to get report", error=str(e), status_code=500)

    context = _report_context_for_llm(report, max_chars=6000)

    if not call_llm:
        return error_response(
            message="LLM 未配置，无法解读",
            error="请配置 llm_config.json 与 API Key",
            status_code=503,
        )
    try:
        prompt = (
            "请用 2～3 段通俗中文解释下面这份测试报告，面向非技术人员：\n"
            "1) 这份报告在说什么（任务类型、结果概况）；\n"
            "2) 关键指标或结论（用通俗语言，避免堆砌数字）；\n"
            "3) 可能的风险或建议关注点。\n"
            "语气简洁、易懂，不要大段复制原文。\n\n"
            "---\n\n" + context
        )
        result = call_llm([{"role": "user", "content": prompt}], timeout=60)
        text = (result or "").strip()
        if not text:
            return error_response(message="LLM 未返回内容", status_code=500)
        return success_response(data={"interpretation": text})
    except Exception as e:
        return error_response(message="解读失败", error=str(e), status_code=500)

