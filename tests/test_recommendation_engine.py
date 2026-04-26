"""
推荐引擎单测：规则输出、单报告推荐。
"""
import pytest

# 测试 _build_recommendation_from_rules 与 单报告推荐
from shared.unified.recommendation_engine import (
    _build_recommendation_from_rules,
    collect_metrics_from_single_report,
    generate_single_report_recommendation,
    _get_thresholds,
)


class TestGetThresholds:
    """阈值从配置或默认值加载"""

    def test_returns_dict_with_expected_keys(self):
        th = _get_thresholds()
        assert "cpu_high_avg" in th
        assert "mem_delta_mb_leak" in th
        assert "fps_low_avg" in th
        assert th["cpu_high_avg"] >= 0
        assert th["mem_delta_mb_leak"] >= 0


class TestBuildRecommendationFromRules:
    """规则引擎：给定 metrics 应产出对应 focus_areas 与 suggestions"""

    def test_cpu_high_yields_cpu_focus(self):
        metrics = {"cpu_high": True, "memory_high_or_leak": False, "crash_or_anr": False, "fps_low": False, "jank_high": False}
        rec = _build_recommendation_from_rules(metrics)
        assert "CPU" in rec["focus_areas"]
        assert "performance_monitor" in rec["suggested_modules"]
        assert "主线程" in rec["next_test_direction"] or "CPU" in rec["next_test_direction"]

    def test_memory_high_yields_memory_focus(self):
        metrics = {"cpu_high": False, "memory_high_or_leak": True, "crash_or_anr": False, "fps_low": False, "jank_high": False}
        rec = _build_recommendation_from_rules(metrics)
        assert "内存" in rec["focus_areas"]
        assert any("内存" in s for s in rec["suggestions"])

    def test_crash_or_anr_yields_stability_focus(self):
        metrics = {"cpu_high": False, "memory_high_or_leak": False, "crash_or_anr": True, "fps_low": False, "jank_high": False}
        rec = _build_recommendation_from_rules(metrics)
        assert "稳定性" in rec["focus_areas"]
        assert "log_monitor" in rec["suggested_modules"]

    def test_all_clean_yields_overall_low_priority(self):
        metrics = {"cpu_high": False, "memory_high_or_leak": False, "crash_or_anr": False, "fps_low": False, "jank_high": False}
        rec = _build_recommendation_from_rules(metrics)
        assert "整体" in rec["focus_areas"]
        assert rec["priority"] == "low"
        assert "常规回归" in rec["next_test_direction"] or "观察" in rec["next_test_direction"]


class TestCollectMetricsFromSingleReport:
    """单份报告指标聚合"""

    def test_monkey_crash_sets_crash_or_anr(self):
        report = {"module": "monkey", "summary": {"crash_count": 1, "anr_count": 0, "mem_mb": {}}, "details": {"performance": {}}}
        metrics = collect_metrics_from_single_report(report)
        assert metrics["crash_or_anr"] is True

    def test_monkey_mem_delta_above_threshold_sets_memory_high(self):
        report = {
            "module": "monkey",
            "summary": {"crash_count": 0, "anr_count": 0, "mem_mb": {"delta": 60}},
            "details": {"performance": {}},
        }
        metrics = collect_metrics_from_single_report(report)
        assert metrics["memory_high_or_leak"] is True

    def test_performance_high_cpu_sets_cpu_high(self):
        report = {
            "module": "performance_monitor",
            "summary": {"cpu": {"avg": 85, "max": 90}, "memory": {}, "fps": {}, "jank": {}},
        }
        metrics = collect_metrics_from_single_report(report)
        assert metrics["cpu_high"] is True


class TestGenerateSingleReportRecommendation:
    """单报告推荐入口"""

    def test_monkey_report_with_crash_returns_recommendation(self):
        report = {
            "module": "monkey",
            "summary": {"crash_count": 1, "anr_count": 0, "mem_mb": {}},
            "details": {"performance": {}},
        }
        rec = generate_single_report_recommendation(report)
        assert rec is not None
        assert "稳定性" in rec["focus_areas"]
        assert rec["suggestions"]
        assert rec["next_test_direction"]

    def test_performance_report_high_memory_returns_recommendation(self):
        report = {
            "module": "performance_monitor",
            "summary": {
                "cpu": {"avg": 30},
                "memory": {"max_mb": 500},
                "fps": {"avg": 50},
                "jank": {"total": 0},
            },
        }
        rec = generate_single_report_recommendation(report)
        assert rec is not None
        assert "内存" in rec["focus_areas"]

    def test_other_module_returns_none(self):
        report = {"module": "log_monitor", "summary": {}}
        rec = generate_single_report_recommendation(report)
        assert rec is None
