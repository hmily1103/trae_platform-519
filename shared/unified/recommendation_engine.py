"""
压测完成后「下次测试方向」推荐引擎。

- 规则引擎：根据 CPU/内存/崩溃/ANR/FPS 等阈值生成 focus_areas、suggestions、next_test_direction
- 阈值从 config/recommendation_rules.json 读取，缺省用默认常量
- 可选 LLM：对规则结果做润色或补充（需配置 call_llm）
"""

from __future__ import annotations

import os
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认阈值（config 缺失时使用）
_DEFAULT_CPU_HIGH_AVG = 80
_DEFAULT_CPU_HIGH_APP = 70
_DEFAULT_MEM_DELTA_MB_LEAK = 50
_DEFAULT_MEM_MAX_MB_HIGH = 400
_DEFAULT_FPS_LOW_AVG = 45
_DEFAULT_JANK_TOTAL_HIGH = 30

_config_cache: Optional[Dict[str, Any]] = None


def _get_thresholds() -> Dict[str, float]:
    """从 config/recommendation_rules.json 读取阈值，缺省用默认值。"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        path = os.path.join(root, "config", "recommendation_rules.json")
        if os.path.exists(path):
            import json
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _config_cache = {
                "cpu_high_avg": float(raw.get("cpu_high_avg", _DEFAULT_CPU_HIGH_AVG)),
                "cpu_high_app": float(raw.get("cpu_high_app", _DEFAULT_CPU_HIGH_APP)),
                "mem_delta_mb_leak": float(raw.get("mem_delta_mb_leak", _DEFAULT_MEM_DELTA_MB_LEAK)),
                "mem_max_mb_high": float(raw.get("mem_max_mb_high", _DEFAULT_MEM_MAX_MB_HIGH)),
                "fps_low_avg": float(raw.get("fps_low_avg", _DEFAULT_FPS_LOW_AVG)),
                "jank_total_high": float(raw.get("jank_total_high", _DEFAULT_JANK_TOTAL_HIGH)),
            }
            return _config_cache
    except Exception as e:
        logger.debug("load recommendation_rules.json: %s", e)
    _config_cache = {
        "cpu_high_avg": _DEFAULT_CPU_HIGH_AVG,
        "cpu_high_app": _DEFAULT_CPU_HIGH_APP,
        "mem_delta_mb_leak": _DEFAULT_MEM_DELTA_MB_LEAK,
        "mem_max_mb_high": _DEFAULT_MEM_MAX_MB_HIGH,
        "fps_low_avg": _DEFAULT_FPS_LOW_AVG,
        "jank_total_high": _DEFAULT_JANK_TOTAL_HIGH,
    }
    return _config_cache


def _get_report(get_report_fn: Callable[[str], Optional[Dict]], unified_id: Optional[str]) -> Optional[Dict]:
    if not get_report_fn or not unified_id:
        return None
    try:
        return get_report_fn(unified_id)
    except Exception as e:
        logger.debug("get_report %s: %s", unified_id, e)
        return None


def collect_metrics_from_orchestration(
    orchestration_report: Dict[str, Any],
    get_report_fn: Callable[[str], Optional[Dict]],
) -> Dict[str, Any]:
    """
    从编排报告及其子报告（monkey、performance_monitor）聚合指标，供规则引擎使用。
    """
    metrics: Dict[str, Any] = {
        "cpu_high": False,
        "memory_high_or_leak": False,
        "crash_or_anr": False,
        "fps_low": False,
        "jank_high": False,
        "monkey": {},
        "performance": {},
    }
    details = orchestration_report.get("details") or {}
    children = details.get("children") or {}

    # Monkey 子报告
    monkey_id = None
    if isinstance(children.get("monkey"), dict):
        monkey_id = children["monkey"].get("unified_report_id")
    if monkey_id:
        monkey_report = _get_report(get_report_fn, monkey_id)
        if monkey_report:
            s = monkey_report.get("summary") or {}
            mem = s.get("mem_mb") or {}
            metrics["monkey"] = {
                "crash_count": s.get("crash_count", 0),
                "anr_count": s.get("anr_count", 0),
                "mem_delta_mb": mem.get("delta"),
                "mem_leak": mem.get("leak"),
                "is_successful": s.get("is_successful"),
            }
            det = monkey_report.get("details") or {}
            perf = det.get("performance") or {}
            metrics["monkey"]["cpu_app"] = perf.get("cpu_app")
            metrics["monkey"]["video_fps"] = perf.get("video_fps")

            th = _get_thresholds()
            if (metrics["monkey"].get("crash_count") or 0) > 0 or (metrics["monkey"].get("anr_count") or 0) > 0:
                metrics["crash_or_anr"] = True
            delta = metrics["monkey"].get("mem_delta_mb")
            if isinstance(delta, (int, float)) and delta >= th["mem_delta_mb_leak"]:
                metrics["memory_high_or_leak"] = True
            if metrics["monkey"].get("mem_leak"):
                metrics["memory_high_or_leak"] = True
            cpu_app = metrics["monkey"].get("cpu_app")
            if isinstance(cpu_app, (int, float)) and cpu_app >= th["cpu_high_app"]:
                metrics["cpu_high"] = True
            fps = metrics["monkey"].get("video_fps")
            if isinstance(fps, (int, float)) and fps > 0 and fps < th["fps_low_avg"]:
                metrics["fps_low"] = True

    # 性能监控子报告
    perf_id = None
    if isinstance(children.get("performance_monitor"), dict):
        perf_id = children["performance_monitor"].get("unified_report_id")
    if perf_id:
        perf_report = _get_report(get_report_fn, perf_id)
        if perf_report:
            s = perf_report.get("summary") or {}
            metrics["performance"] = {
                "cpu_avg": (s.get("cpu") or {}).get("avg"),
                "cpu_max": (s.get("cpu") or {}).get("max"),
                "memory_avg_mb": (s.get("memory") or {}).get("avg_mb"),
                "memory_max_mb": (s.get("memory") or {}).get("max_mb"),
                "fps_avg": (s.get("fps") or {}).get("avg"),
                "fps_min": (s.get("fps") or {}).get("min"),
                "jank_total": (s.get("jank") or {}).get("total"),
            }
            th = _get_thresholds()
            p = metrics["performance"]
            if isinstance(p.get("cpu_avg"), (int, float)) and p["cpu_avg"] >= th["cpu_high_avg"]:
                metrics["cpu_high"] = True
            if isinstance(p.get("cpu_max"), (int, float)) and p["cpu_max"] >= th["cpu_high_avg"]:
                metrics["cpu_high"] = True
            if isinstance(p.get("memory_max_mb"), (int, float)) and p["memory_max_mb"] >= th["mem_max_mb_high"]:
                metrics["memory_high_or_leak"] = True
            if isinstance(p.get("fps_avg"), (int, float)) and p["fps_avg"] > 0 and p["fps_avg"] < th["fps_low_avg"]:
                metrics["fps_low"] = True
            if isinstance(p.get("jank_total"), (int, float)) and p["jank_total"] >= th["jank_total_high"]:
                metrics["jank_high"] = True

    return metrics


def collect_metrics_from_single_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    从单份报告（monkey 或 performance_monitor）聚合指标，供规则引擎使用。
    """
    metrics: Dict[str, Any] = {
        "cpu_high": False,
        "memory_high_or_leak": False,
        "crash_or_anr": False,
        "fps_low": False,
        "jank_high": False,
        "monkey": {},
        "performance": {},
    }
    th = _get_thresholds()
    module = report.get("module")
    summary = report.get("summary") or {}

    if module == "monkey":
        mem = summary.get("mem_mb") or {}
        metrics["monkey"] = {
            "crash_count": summary.get("crash_count", 0),
            "anr_count": summary.get("anr_count", 0),
            "mem_delta_mb": mem.get("delta"),
            "mem_leak": mem.get("leak"),
        }
        det = report.get("details") or {}
        perf = det.get("performance") or {}
        metrics["monkey"]["cpu_app"] = perf.get("cpu_app")
        metrics["monkey"]["video_fps"] = perf.get("video_fps")

        if (metrics["monkey"].get("crash_count") or 0) > 0 or (metrics["monkey"].get("anr_count") or 0) > 0:
            metrics["crash_or_anr"] = True
        delta = metrics["monkey"].get("mem_delta_mb")
        if isinstance(delta, (int, float)) and delta >= th["mem_delta_mb_leak"]:
            metrics["memory_high_or_leak"] = True
        if metrics["monkey"].get("mem_leak"):
            metrics["memory_high_or_leak"] = True
        cpu_app = metrics["monkey"].get("cpu_app")
        if isinstance(cpu_app, (int, float)) and cpu_app >= th["cpu_high_app"]:
            metrics["cpu_high"] = True
        fps = metrics["monkey"].get("video_fps")
        if isinstance(fps, (int, float)) and fps > 0 and fps < th["fps_low_avg"]:
            metrics["fps_low"] = True

    elif module == "performance_monitor":
        metrics["performance"] = {
            "cpu_avg": (summary.get("cpu") or {}).get("avg"),
            "cpu_max": (summary.get("cpu") or {}).get("max"),
            "memory_avg_mb": (summary.get("memory") or {}).get("avg_mb"),
            "memory_max_mb": (summary.get("memory") or {}).get("max_mb"),
            "fps_avg": (summary.get("fps") or {}).get("avg"),
            "fps_min": (summary.get("fps") or {}).get("min"),
            "jank_total": (summary.get("jank") or {}).get("total"),
        }
        p = metrics["performance"]
        if isinstance(p.get("cpu_avg"), (int, float)) and p["cpu_avg"] >= th["cpu_high_avg"]:
            metrics["cpu_high"] = True
        if isinstance(p.get("cpu_max"), (int, float)) and p["cpu_max"] >= th["cpu_high_avg"]:
            metrics["cpu_high"] = True
        if isinstance(p.get("memory_max_mb"), (int, float)) and p["memory_max_mb"] >= th["mem_max_mb_high"]:
            metrics["memory_high_or_leak"] = True
        if isinstance(p.get("fps_avg"), (int, float)) and p["fps_avg"] > 0 and p["fps_avg"] < th["fps_low_avg"]:
            metrics["fps_low"] = True
        if isinstance(p.get("jank_total"), (int, float)) and p["jank_total"] >= th["jank_total_high"]:
            metrics["jank_high"] = True

    return metrics


def _build_recommendation_from_rules(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """根据聚合指标生成推荐（纯规则）。"""
    focus_areas: List[str] = []
    suggestions: List[str] = []
    possible_causes: List[str] = []
    suggested_modules: List[str] = []

    if metrics.get("cpu_high"):
        focus_areas.append("CPU")
        suggestions.append("建议做 CPU/主线程耗时专项：使用 Profiler 或 systrace 排查主线程计算、死循环或频繁 GC。")
        possible_causes.append("主线程计算过多、死循环或后台任务占用 CPU")
        suggested_modules.append("performance_monitor")

    if metrics.get("memory_high_or_leak"):
        focus_areas.append("内存")
        suggestions.append("建议做内存专项：长时间运行 + 前后 PSS 对比，或使用 LeakCanary/Profiler 查泄漏与大对象。")
        possible_causes.append("内存泄漏、大对象未释放或缓存未限制")
        suggested_modules.append("performance_monitor")

    if metrics.get("crash_or_anr"):
        focus_areas.append("稳定性")
        suggestions.append("建议重点看日志与堆栈：复现路径、崩溃栈、ANR 堆栈，并做针对性用例或 Monkey 重跑验证。")
        possible_causes.append("崩溃或 ANR，需结合日志定位代码与场景")
        if "log_monitor" not in suggested_modules:
            suggested_modules.append("log_monitor")

    if metrics.get("fps_low") or metrics.get("jank_high"):
        focus_areas.append("流畅度")
        suggestions.append("建议关注渲染与主线程：过度绘制、主线程耗时、FPS 与 Jank 曲线，可配合 UI 自动化做回归。")
        possible_causes.append("渲染瓶颈或主线程卡顿导致掉帧、卡顿")
        if "performance_monitor" not in suggested_modules:
            suggested_modules.append("performance_monitor")

    if not focus_areas:
        focus_areas.append("整体")
        suggestions.append("本次指标未见明显异常，建议按计划做常规回归或延长 Monkey 时长观察稳定性。")
        possible_causes.append("无突出异常")
        next_test_direction = "建议按计划做常规回归或延长 Monkey 时长观察。"
    else:
        next_test_direction = f"下次测试建议优先关注：{'、'.join(focus_areas)}。可针对性增加专项测试或日志分析后再做 Monkey 回归验证。"

    priority = "high" if (metrics.get("crash_or_anr") or metrics.get("memory_high_or_leak")) else "medium"
    if not focus_areas or focus_areas == ["整体"]:
        priority = "low"

    return {
        "focus_areas": focus_areas,
        "priority": priority,
        "suggestions": suggestions,
        "possible_causes": possible_causes,
        "next_test_direction": next_test_direction,
        "suggested_modules": list(dict.fromkeys(suggested_modules)),
        "metrics_summary": {
            "cpu_high": metrics.get("cpu_high"),
            "memory_high_or_leak": metrics.get("memory_high_or_leak"),
            "crash_or_anr": metrics.get("crash_or_anr"),
            "fps_low": metrics.get("fps_low"),
            "jank_high": metrics.get("jank_high"),
        },
    }


def generate_next_test_recommendation(
    orchestration_report: Dict[str, Any],
    get_report_fn: Callable[[str], Optional[Dict]],
    use_llm: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    根据编排报告及子报告生成「下次测试方向」推荐。

    :param orchestration_report: 编排报告（含 details.children）
    :param get_report_fn: 根据 unified_id 获取报告的函数，如 store.get_report
    :param use_llm: 是否用 LLM 润色 next_test_direction（需配置 call_llm）
    :return: 推荐 dict，失败返回 None
    """
    if orchestration_report.get("module") != "unified" or orchestration_report.get("kind") != "orchestration":
        return None
    try:
        metrics = collect_metrics_from_orchestration(orchestration_report, get_report_fn)
        rec = _build_recommendation_from_rules(metrics)

        if use_llm:
            try:
                from utils.llm_client import call_llm
            except Exception:
                call_llm = None
            if call_llm:
                try:
                    import json
                    context = json.dumps(
                        {"metrics_summary": rec.get("metrics_summary"), "focus_areas": rec.get("focus_areas"), "suggestions": rec.get("suggestions")},
                        ensure_ascii=False,
                    )
                    prompt = (
                        "根据以下压测结果摘要，用一两句中文总结「下次测试方向」（例如：优先做内存专项还是 CPU 专项，是否先看日志再回归）。不要重复列表，只输出结论句。\n\n"
                        + context
                    )
                    llm_text = call_llm([{"role": "user", "content": prompt}], timeout=30)
                    if llm_text and isinstance(llm_text, str) and llm_text.strip():
                        rec["next_test_direction"] = llm_text.strip()
                except Exception as e:
                    logger.warning("Recommendation LLM fallback: %s", e)

        return rec
    except Exception as e:
        logger.exception("generate_next_test_recommendation: %s", e)
        return None


def generate_single_report_recommendation(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    根据单份报告（monkey 或 performance_monitor）生成「下次测试方向」推荐。
    用于仅跑单模块时的自动推荐。
    """
    module = report.get("module")
    if module not in ("monkey", "performance_monitor"):
        return None
    try:
        metrics = collect_metrics_from_single_report(report)
        return _build_recommendation_from_rules(metrics)
    except Exception as e:
        logger.exception("generate_single_report_recommendation: %s", e)
        return None
