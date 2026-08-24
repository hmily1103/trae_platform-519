# -*- coding: utf-8 -*-
"""A3 存量能力适配器测试（依赖全注入，不打真实 LLM / adb / 知识库文件）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from modules.log_monitor.agents.adapters import (  # noqa: E402
    LogAnalysisAdapter, DeviceProbeAdapter, HistoryAdapter,
    register_builtin_agents,
)
from modules.log_monitor.agents.base import run_with_guard  # noqa: E402
from modules.log_monitor.agents.planner import (  # noqa: E402
    build_plan, clear_registry, registered_agent_names,
)


ALERT = {
    "id": "a-1", "type": "crash", "rule_name": "崩溃监控",
    "severity": "critical",
    "message": "FATAL EXCEPTION: main java.lang.NullPointerException",
}


# ---- 替身 ----
class FakeAnalysisResult:
    def to_dict(self):
        return {
            "root_cause": "PlayerManager 空指针：mSession 未判空",
            "suggestions": ["增加判空", "复现路径回归"],
            "problem_location": "com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)",
            "impact": "点歌播放不可用",
        }


class FakeAnalyzer:
    def __init__(self):
        self.calls = []

    def analyze(self, log_lines, alert_context=None):
        self.calls.append((list(log_lines), dict(alert_context or {})))
        return FakeAnalysisResult()


class FakeAnalyzerNoLocation:
    def analyze(self, log_lines, alert_context=None):
        class R:
            def to_dict(self):
                return {"root_cause": "", "suggestions": [], "problem_location": ""}
        return R()


class FakeKB:
    def __init__(self, cases=None):
        self._cases = cases if cases is not None else []
        self.calls = []

    def search_similar(self, alert_type, query_text, top_k=3):
        self.calls.append((alert_type, query_text, top_k))
        return list(self._cases)


def fake_executor_ok(action, device_id, alert_id="", package=""):
    return {"type": "screenshot", "status": "ok",
            "summary": "截图成功", "artifact": "/static/alert_captures/x.png",
            "output": "", "command": "screencap", "executed_at": "t"}


def fake_executor_refused(action, device_id, alert_id="", package=""):
    return {"type": "custom_shell", "status": "refused",
            "summary": "自由命令一律拒绝", "artifact": "",
            "output": "custom_shell 违反只读红线", "command": action,
            "executed_at": "t"}


# ---------------------------------------------------------------------------
class TestLogAnalysisAdapter(unittest.TestCase):
    def test_ok_with_stack_evidence(self):
        fake = FakeAnalyzer()
        f = LogAnalysisAdapter(analyzer=fake).run(
            ALERT, {"log_lines": ["at com.x.Y.z(Y.java:1)"]})
        self.assertTrue(f.usable)
        self.assertIn("空指针", f.summary)
        self.assertEqual(f.artifacts["problem_location"],
                         "com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)")
        kinds = [e["kind"] for e in f.evidence]
        self.assertIn("stack_location", kinds)

    def test_input_translation(self):
        """alert 字段 + context 的 device_context/historical_cases 正确透传。"""
        fake = FakeAnalyzer()
        LogAnalysisAdapter(analyzer=fake).run(ALERT, {
            "log_lines": ["l1"],
            "device_context": {"model": "rk3566"},
            "historical_cases": [{"root_cause": "旧案"}],
        })
        _, ctx = fake.calls[0]
        self.assertEqual(ctx["rule_name"], "崩溃监控")
        self.assertEqual(ctx["type"], "crash")
        self.assertEqual(ctx["device_context"], {"model": "rk3566"})
        self.assertEqual(len(ctx["historical_cases"]), 1)

    def test_no_location_no_stack_evidence(self):
        f = LogAnalysisAdapter(analyzer=FakeAnalyzerNoLocation()).run(
            ALERT, {"log_lines": []})
        self.assertTrue(f.usable)
        self.assertEqual(f.evidence, [])
        self.assertIn("未给出明确根因", f.summary)

    def test_analyzer_exception_degrades_via_guard(self):
        """LLM 调用抛异常 → guard 收敛为 failed，不外抛。"""
        class Boom:
            def analyze(self, *a, **kw):
                raise RuntimeError("llm down")
        f = run_with_guard(LogAnalysisAdapter(analyzer=Boom()), ALERT, {})
        self.assertEqual(f.status, "failed")
        self.assertIn("llm down", f.error)


class TestDeviceProbeAdapter(unittest.TestCase):
    def test_reuse_existing_action_result_no_rerun(self):
        """context 已带 action_result → 直接复用，绝不二次执行。"""
        called = []

        def spy_executor(*a, **kw):
            called.append(1)
            return fake_executor_ok(*a, **kw)

        ar = fake_executor_ok("screenshot", "d1")
        f = DeviceProbeAdapter(executor=spy_executor).run(
            ALERT, {"action_result": ar, "action": "screenshot",
                    "device_id": "d1"})
        self.assertEqual(called, [])  # 未重跑
        self.assertTrue(f.usable)
        self.assertEqual(f.evidence[0]["kind"], "action_artifact")
        self.assertEqual(f.artifacts["action_result"], ar)

    def test_execute_when_no_prior_result(self):
        f = DeviceProbeAdapter(executor=fake_executor_ok).run(
            ALERT, {"action": "screenshot", "device_id": "d1"})
        self.assertTrue(f.usable)
        self.assertIn("/static/alert_captures/x.png",
                      str(f.evidence[0]["detail"]))

    def test_skipped_without_action(self):
        f = DeviceProbeAdapter(executor=fake_executor_ok).run(
            ALERT, {"action": "none", "device_id": "d1"})
        self.assertEqual(f.status, "skipped")
        self.assertIn("未配置", f.degrade_reason)

    def test_skipped_without_device(self):
        f = DeviceProbeAdapter(executor=fake_executor_ok).run(
            ALERT, {"action": "screenshot", "device_id": ""})
        self.assertEqual(f.status, "skipped")

    def test_refused_action_is_failed_not_usable(self):
        """custom_shell 被拒 → failed 留痕，产物不进证据链。"""
        f = DeviceProbeAdapter(executor=fake_executor_refused).run(
            ALERT, {"action": "custom_shell", "device_id": "d1"})
        self.assertEqual(f.status, "failed")
        self.assertFalse(f.usable)
        self.assertIn("refused", f.degrade_reason)
        # 审计留痕：拒绝详情仍在 artifacts 里
        self.assertEqual(f.artifacts["action_result"]["status"], "refused")


class TestHistoryAdapter(unittest.TestCase):
    def test_hit_cases(self):
        kb = FakeKB(cases=[
            {"id": "c1", "_score": 4.0, "resolved": True, "root_cause": "旧根因A"},
            {"id": "c2", "_score": 1.2, "resolved": False, "root_cause": "旧根因B"},
        ])
        f = HistoryAdapter(kb=kb).run(ALERT, {})
        self.assertTrue(f.usable)
        self.assertIn("2 条", f.summary)
        self.assertEqual(len(f.evidence), 2)
        self.assertEqual(f.evidence[0]["kind"], "history_case")
        self.assertIn("已解决=是", f.evidence[0]["desc"])
        self.assertEqual(len(f.artifacts["cases"]), 2)

    def test_no_hit_is_still_ok(self):
        """未命中也是有效结论（ok），而不是 failed。"""
        f = HistoryAdapter(kb=FakeKB()).run(ALERT, {})
        self.assertTrue(f.usable)
        self.assertIn("未检索到", f.summary)
        self.assertEqual(f.artifacts["cases"], [])

    def test_query_composition(self):
        kb = FakeKB()
        HistoryAdapter(kb=kb).run(ALERT, {})
        atype, query, top_k = kb.calls[0]
        self.assertEqual(atype, "crash")
        self.assertIn("NullPointerException", query)
        self.assertIn("崩溃监控", query)
        self.assertEqual(top_k, 3)


class TestRegistration(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def test_register_builtin_agents(self):
        names = register_builtin_agents()
        self.assertEqual(sorted(names),
                         ["device_probe", "history", "log_analysis", "source_code"])
        self.assertEqual(registered_agent_names(),
                         ["device_probe", "history", "log_analysis", "source_code"])

    def test_plan_dispatches_after_registration(self):
        """注册后 crash 计划真正派得动人：四个 Agent 全部派出。"""
        register_builtin_agents()
        plan = build_plan(ALERT)
        self.assertEqual(sorted(plan.agent_names),
                         ["device_probe", "history", "log_analysis", "source_code"])
        self.assertEqual(plan.skipped, [])

    def test_register_is_idempotent(self):
        register_builtin_agents()
        register_builtin_agents()
        self.assertEqual(len(registered_agent_names()), 4)


class TestAdapterReadonlyFlag(unittest.TestCase):
    def test_all_adapters_readonly(self):
        from modules.log_monitor.agents.source_agent import SourceCodeAdapter
        for cls in (LogAnalysisAdapter, DeviceProbeAdapter, HistoryAdapter, SourceCodeAdapter):
            self.assertTrue(cls.readonly, "%s 必须 readonly=True" % cls.__name__)


if __name__ == "__main__":
    unittest.main()
