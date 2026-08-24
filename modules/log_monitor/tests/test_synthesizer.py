# -*- coding: utf-8 -*-
"""C1 Synthesizer 综合裁决器单元测试。

验证：多 Agent 结果合并 / 证据链扩展 / 置信度多源升级 / 降级安全 / 兼容格式。
全部使用注入替身 Agent，不打真实 LLM / adb / 知识库。
"""
import unittest
from unittest.mock import MagicMock

from modules.log_monitor.agents.base import AgentFinding, STATUS_OK, STATUS_FAILED, STATUS_SKIPPED, STATUS_TIMEOUT
from modules.log_monitor.agents.planner import DiagnosisPlan, PlannedAgent, build_plan
from modules.log_monitor.agents.executor import PlanExecution
from modules.log_monitor.agents.synthesizer import synthesize, _assess_confidence_v2


def _ok_finding(name, summary="ok", evidence=None, artifacts=None):
    return AgentFinding(
        agent_name=name, status=STATUS_OK, summary=summary,
        evidence=evidence or [], artifacts=artifacts or {},
        duration_ms=10,
    )


def _degraded_finding(name, status=STATUS_FAILED, reason="boom"):
    return AgentFinding(
        agent_name=name, status=status, summary="degraded",
        error=reason, degrade_reason=reason, duration_ms=5,
    )


def _skipped_finding(name, reason="skipped"):
    return AgentFinding(
        agent_name=name, status=STATUS_SKIPPED, summary="skipped",
        degrade_reason=reason, duration_ms=0,
    )


def _make_execution(findings, plan=None, all_degraded=False):
    """构造一个 PlanExecution 替身。"""
    execution = MagicMock(spec=PlanExecution)
    execution.findings = findings
    execution.usable_findings = [f for f in findings if f.status == STATUS_OK]
    execution.all_degraded = all_degraded
    execution.budget_exceeded = False
    execution.elapsed_ms = 100
    execution.plan = plan or DiagnosisPlan(alert_id="a1", alert_type="crash")
    execution.to_dict.return_value = {
        "findings": [{"agent_name": f.agent_name, "status": f.status} for f in findings],
        "elapsed_ms": 100,
    }
    return execution


STACK_LOGS = [
    "E/AndroidRuntime: FATAL EXCEPTION: main",
    "    at com.thunder.ktv.PlayerManager.play(PlayerManager.java:88)",
]


class TestSynthesizeDegrade(unittest.TestCase):
    """降级安全：all_degraded / 无日志分析 → None。"""

    def test_all_degraded_returns_none(self):
        execution = _make_execution(
            [_degraded_finding("log_analysis"), _degraded_finding("device_probe")],
            all_degraded=True,
        )
        result = synthesize(execution, {"type": "crash"}, {})
        self.assertIsNone(result)

    def test_no_log_analysis_returns_none(self):
        execution = _make_execution(
            [_ok_finding("device_probe"), _ok_finding("history")],
        )
        result = synthesize(execution, {"type": "crash"}, {})
        self.assertIsNone(result)


class TestSynthesizeHappyPath(unittest.TestCase):
    """正向路径：多 Agent 结果合并 → 兼容 dict。"""

    def setUp(self):
        self.log_finding = _ok_finding(
            "log_analysis",
            summary="空指针",
            artifacts={
                "root_cause": "url 为 null 导致 NullPointerException",
                "suggestions": ["添加空判断"],
                "problem_location": "PlayerManager.java:88",
                "impact": "播放功能不可用",
                "investigation_path": ["检查 url 参数"],
                "suggested_patch": "",
            },
            evidence=[{"kind": "stack_location", "desc": "堆栈定位", "detail": "PlayerManager.java:88"}],
        )
        self.history_finding = _ok_finding(
            "history",
            artifacts={"cases": [{"id": "c1", "root_cause": "NPE", "_score": 5, "resolved": True, "rule_name": "crash_rule"}]},
        )
        self.probe_finding = _ok_finding(
            "device_probe",
            artifacts={"action_result": {"status": "ok", "type": "screenshot", "artifact": "/tmp/x.png"}},
            evidence=[{"kind": "action_artifact", "desc": "截图", "detail": "/tmp/x.png"}],
        )
        self.source_finding = _ok_finding(
            "source_code",
            artifacts={
                "snippets": [{"file": "/src/PlayerManager.java", "target_line": 88, "lines": []}],
                "review": {"confirmed": True, "reason": "源码佐证", "suggested_fix": "加空判断"},
            },
            evidence=[{"kind": "source_code", "desc": "源码片段", "detail": "if (url == null)..."}],
        )

    def test_returns_compatible_dict(self):
        execution = _make_execution([self.log_finding, self.history_finding, self.probe_finding, self.source_finding])
        alert = {"type": "crash", "message": "NPE", "rule_name": "crash_rule", "severity": "medium", "id": "a1"}
        ctx = {"log_lines": STACK_LOGS, "device_id": "dev1"}
        result = synthesize(execution, alert, ctx)

        self.assertIsNotNone(result)
        # 兼容 1.0 的 key 全在
        for key in ("root_cause", "suggestions", "problem_location", "impact",
                     "confidence", "confidence_score", "confidence_breakdown",
                     "evidence_chain", "status", "auto_closeable", "agent_name"):
            self.assertIn(key, result, "缺少兼容 key: %s" % key)

    def test_root_cause_from_log_analysis(self):
        execution = _make_execution([self.log_finding, self.history_finding])
        result = synthesize(execution, {"type": "crash", "rule_name": "crash_rule"}, {"log_lines": STACK_LOGS})
        self.assertIn("NullPointerException", result["root_cause"])

    def test_history_hit_gives_high_confidence(self):
        execution = _make_execution([self.log_finding, self.history_finding])
        result = synthesize(execution, {"type": "crash", "rule_name": "crash_rule", "severity": "medium"}, {"log_lines": STACK_LOGS})
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["status"], "AUTO_RESOLVED")

    def test_source_evidence_in_chain(self):
        execution = _make_execution([self.log_finding, self.source_finding])
        result = synthesize(execution, {"type": "crash", "rule_name": "r", "severity": "medium"}, {"log_lines": STACK_LOGS})
        chain = result["evidence_chain"]
        source_evs = [e for e in chain["direct"] if e.get("kind") == "source_code"]
        self.assertTrue(len(source_evs) >= 1)

    def test_degraded_agents_in_inferred(self):
        execution = _make_execution([self.log_finding, _degraded_finding("device_probe", reason="adb timeout")])
        result = synthesize(execution, {"type": "crash", "rule_name": "r", "severity": "medium"}, {"log_lines": STACK_LOGS})
        chain = result["evidence_chain"]
        degraded_evs = [e for e in chain["inferred"] if e.get("kind") == "agent_degraded"]
        self.assertTrue(len(degraded_evs) >= 1)
        self.assertIn("device_probe", degraded_evs[0]["label"])

    def test_llm_review_fix_appended(self):
        execution = _make_execution([self.log_finding, self.source_finding])
        result = synthesize(execution, {"type": "crash", "rule_name": "r", "severity": "medium"}, {"log_lines": STACK_LOGS})
        self.assertIn("加空判断", result["suggested_patch"])

    def test_agent_v2_flag(self):
        execution = _make_execution([self.log_finding])
        result = synthesize(execution, {"type": "crash", "rule_name": "r"}, {"log_lines": STACK_LOGS})
        self.assertTrue(result.get("agent_v2"))
        self.assertIn("plan", result)
        self.assertIn("execution", result)


class TestConfidenceV2SourceBoost(unittest.TestCase):
    """置信度多源升级：源码佐证提升堆栈维。"""

    def test_source_confirmed_boosts_to_100(self):
        """无堆栈帧但有 problem_location + 源码 + LLM 确认 → stack=100。"""
        _, _, breakdown = _assess_confidence_v2(
            "NPE", [], "r",
            problem_location="PlayerManager.java:88",
            log_lines=["no stack frame here"],
            context_meta=None, probe_evidence=[],
            source_info={"snippets": [{"file": "x"}], "review": {"confirmed": True}},
        )
        self.assertEqual(breakdown["stack"], 100)
        self.assertIn("LLM 复判确认", breakdown["notes"]["stack"])

    def test_source_without_llm_boosts_to_75(self):
        """有源码片段但无 LLM 复判 → stack=75。"""
        _, _, breakdown = _assess_confidence_v2(
            "NPE", [], "r",
            problem_location="PlayerManager.java:88",
            log_lines=["no stack"],
            context_meta=None, probe_evidence=[],
            source_info={"snippets": [{"file": "x"}], "review": None},
        )
        self.assertEqual(breakdown["stack"], 75)

    def test_no_source_stays_50(self):
        """无源码佐证 → stack=50（与 1.0 一致）。"""
        _, _, breakdown = _assess_confidence_v2(
            "NPE", [], "r",
            problem_location="PlayerManager.java:88",
            log_lines=["no stack"],
            context_meta=None, probe_evidence=[],
            source_info={"snippets": [], "review": None},
        )
        self.assertEqual(breakdown["stack"], 50)

    def test_invalid_root_cause_zeros_all(self):
        _, _, breakdown = _assess_confidence_v2(
            "分析失败: boom", [], "r",
            problem_location="x.java:1",
            log_lines=STACK_LOGS,
            context_meta=None, probe_evidence=[],
            source_info={"snippets": [{"file": "x"}], "review": {"confirmed": True}},
        )
        self.assertEqual(breakdown["score"], 0)
        self.assertEqual(breakdown["stack"], 0)

    def test_auto_close_unchanged(self):
        """源码佐证不改变自动关单口径（仍只有历史命中才 high）。"""
        level, _, _ = _assess_confidence_v2(
            "NPE", [], "r",
            problem_location="PlayerManager.java:88",
            log_lines=["no stack"],
            context_meta={"lines": 20}, probe_evidence=[],
            source_info={"snippets": [{"file": "x"}], "review": {"confirmed": True}},
        )
        # 无历史命中 → 不能 high（即使源码+LLM 双确认）
        self.assertNotEqual(level, "high")
        # 但可达 medium（源码+LLM+上下文充足）
        self.assertEqual(level, "medium")


class TestSynthesizeSeverity(unittest.TestCase):
    """高危告警 → NEEDS_HUMAN（与 1.0 一致）。"""

    def test_high_severity_needs_human(self):
        log_f = _ok_finding("log_analysis", artifacts={
            "root_cause": "crash", "suggestions": [], "problem_location": "",
        })
        execution = _make_execution([log_f])
        result = synthesize(execution, {"type": "crash", "severity": "high", "rule_name": "r"}, {"log_lines": []})
        self.assertEqual(result["status"], "NEEDS_HUMAN")
        self.assertFalse(result["auto_closeable"])


if __name__ == "__main__":
    unittest.main()
