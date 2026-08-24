#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#25 证据链 + 三信号置信度 单元测试

覆盖：
- build_evidence_chain：直接证据/模型推断/历史引用 的分类正确性
- problem_location 有堆栈佐证 → 直接证据；无佐证 → 模型推断
- _assess_confidence 三信号分级：high（历史命中，口径不变）/ medium（堆栈+上下文）/ low
- handle_alert 在 LLM 全失败时不 NameError（回归修复验证）
"""

import unittest
from unittest.mock import patch, MagicMock

from modules.log_monitor.selfheal import (
    SelfHealAgent, build_evidence_chain,
)

STACK_LOGS = [
    "07-29 10:00:01.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
    "07-29 10:00:01.001  1234  1234 E AndroidRuntime: java.lang.NullPointerException: null obj",
    "07-29 10:00:01.002  1234  1234 E AndroidRuntime: \tat com.thunder.ktv.PlayerManager.play(PlayerManager.java:88)",
    "07-29 10:00:01.003  1234  1234 E AndroidRuntime: \tat com.thunder.ktv.MainActivity.onClick(MainActivity.java:120)",
] + [f"07-29 10:00:02.{i:03d}  1234  1234 I Other: filler line {i}" for i in range(10)]


class TestBuildEvidenceChain(unittest.TestCase):
    def test_stack_backed_location_is_direct(self):
        """问题定位能在堆栈中找到对应 文件:行号 → 直接证据"""
        ec = build_evidence_chain(
            root_cause="空指针",
            problem_location="com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)",
            impact="播放功能不可用",
            trigger_line=STACK_LOGS[0],
            log_lines=STACK_LOGS,
        )
        kinds_direct = [e["kind"] for e in ec["direct"]]
        self.assertIn("trigger_log", kinds_direct)
        self.assertIn("stack_frames", kinds_direct)
        self.assertIn("problem_location", kinds_direct)
        # 根因/影响始终是推断
        kinds_inferred = [e["kind"] for e in ec["inferred"]]
        self.assertIn("root_cause", kinds_inferred)
        self.assertIn("impact", kinds_inferred)
        self.assertNotIn("problem_location", kinds_inferred)

    def test_unbacked_location_is_inferred(self):
        """日志无堆栈时，问题定位标记为模型推断"""
        ec = build_evidence_chain(
            root_cause="疑似初始化顺序问题",
            problem_location="com.thunder.ktv.SomeClass.someMethod() (SomeClass.java:42)",
            trigger_line="E Something: error happened",
            log_lines=["E Something: error happened", "I Other: no stack here"],
        )
        kinds_inferred = [e["kind"] for e in ec["inferred"]]
        self.assertIn("problem_location", kinds_inferred)
        self.assertNotIn("problem_location", [e["kind"] for e in ec["direct"]])

    def test_context_meta_and_device_and_probe(self):
        ec = build_evidence_chain(
            trigger_line="x",
            log_lines=["x"],
            context_meta={"strategy": "crash_full_stack", "lines": 43, "range": [35, 78]},
            device_context={"model": "TS_KTV_X9", "android_version": "14", "apk_version": ""},
            probe_evidence=["some probe output", "采集失败: timeout", "(无输出)"],
        )
        kinds = [e["kind"] for e in ec["direct"]]
        self.assertIn("context_window", kinds)
        self.assertIn("device_context", kinds)
        # 采集失败/无输出的探针不进证据链
        probes = [e for e in ec["direct"] if e["kind"] == "probe"]
        self.assertEqual(len(probes), 1)

    def test_historical_references(self):
        ec = build_evidence_chain(
            trigger_line="x", log_lines=["x"],
            historical=[{"id": "case_1", "rule_name": "NPE规则", "root_cause": "空指针",
                         "_score": 3, "resolved": True}],
        )
        self.assertEqual(len(ec["references"]), 1)
        self.assertIn("case_1", ec["references"][0]["source"])
        self.assertIn("已解决", ec["references"][0]["source"])

    def test_failed_root_cause_not_in_inferred(self):
        """分析失败的根因不作为推断证据"""
        ec = build_evidence_chain(root_cause="分析失败: timeout", log_lines=["x"])
        self.assertNotIn("root_cause", [e["kind"] for e in ec["inferred"]])

    def test_empty_input(self):
        ec = build_evidence_chain()
        self.assertEqual(ec, {"direct": [], "inferred": [], "references": []})


class TestConfidenceThreeSignals(unittest.TestCase):
    def setUp(self):
        self.agent = SelfHealAgent("dummy_device", "com.thunder.ktv", mode="observe")

    def test_high_on_history_hit_unchanged(self):
        """历史已解决案例命中 → high（自动关单口径与旧版一致）"""
        conf, reason, bd = self.agent._assess_confidence(
            "空指针", [{"id": "c1", "rule_name": "NPE", "_score": 4, "resolved": True}],
            rule_name="NPE", problem_location="", log_lines=["one line"],
        )
        self.assertEqual(conf, "high")
        self.assertIn("命中已知已解决案例", reason)
        self.assertEqual(bd["history"], 100)
        self.assertEqual(bd["score"], 40)  # 仅历史命中（其他维度 0）

    def test_medium_on_stack_and_context(self):
        """无历史命中，但命中堆栈 + 上下文充足 → medium"""
        conf, reason, bd = self.agent._assess_confidence(
            "空指针", [], rule_name="NPE",
            problem_location="com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)",
            log_lines=STACK_LOGS,
        )
        self.assertEqual(conf, "medium")
        self.assertIn("命中堆栈=是", reason)
        self.assertIn("充足", reason)
        self.assertEqual(bd["stack"], 100)
        self.assertEqual(bd["context"], 100)
        self.assertEqual(bd["history"], 0)
        # 综合分 = 30(堆栈) + 0(历史) + 15(上下文) + 0(探针) = 45
        self.assertEqual(bd["score"], 45)

    def test_low_on_sparse_context(self):
        """无堆栈、上下文不足 → low"""
        conf, reason, bd = self.agent._assess_confidence(
            "疑似问题", [], rule_name="",
            problem_location="", log_lines=["only one line"],
        )
        self.assertEqual(conf, "low")
        self.assertIn("命中堆栈=否", reason)
        self.assertEqual(bd["stack"], 0)
        self.assertEqual(bd["context"], 0)

    def test_low_on_invalid_root_cause(self):
        conf, reason, bd = self.agent._assess_confidence(
            "分析失败: boom", [], problem_location="x", log_lines=STACK_LOGS,
        )
        self.assertEqual(conf, "low")
        self.assertIn("根因无效", reason)
        self.assertEqual(bd["score"], 0)  # 根因无效则整项为 0

    def test_unresolved_history_not_high(self):
        """历史案例未解决 → 不能 high（即便重合度高）"""
        conf, _, bd = self.agent._assess_confidence(
            "空指针", [{"id": "c1", "rule_name": "NPE", "_score": 9, "resolved": False}],
            rule_name="NPE", problem_location="", log_lines=["x"],
        )
        self.assertNotEqual(conf, "high")
        self.assertEqual(bd["history"], 0)


class TestConfidenceScoreBreakdown(unittest.TestCase):
    """#30 分项打分 0-100 + 权重 + 明细。自动关单口径不放宽。"""

    def setUp(self):
        self.agent = SelfHealAgent("dummy_device", "com.thunder.ktv", mode="observe")

    def test_weights_constant(self):
        _, _, bd = self.agent._assess_confidence("空指针", [], rule_name="NPE",
                                                log_lines=["x"])
        self.assertEqual(bd["weights"], {"stack": 30, "history": 40, "context": 15, "probe": 15})

    def test_all_dimensions_present(self):
        _, _, bd = self.agent._assess_confidence("空指针", [], rule_name="NPE",
                                                log_lines=STACK_LOGS)
        for k in ("score", "stack", "history", "context", "probe", "notes"):
            self.assertIn(k, bd)
        for dim in ("stack", "history", "context", "probe"):
            self.assertIn(dim, bd["notes"])
            self.assertTrue(0 <= bd[dim] <= 100)

    def test_probe_full_score_when_useful(self):
        _, _, bd = self.agent._assess_confidence(
            "空指针", [], rule_name="NPE",
            problem_location="com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)",
            log_lines=STACK_LOGS,
            evidence=["$ logcat -d -t 300\nFATAL EXCEPTION: main", "$ dumpsys meminfo\n(无输出)"],
        )
        self.assertEqual(bd["probe"], 100)
        self.assertEqual(bd["stack"], 100)
        # 30 + 0 + 15 + 15 = 60
        self.assertEqual(bd["score"], 60)

    def test_probe_none_when_no_evidence(self):
        """observe 模式无探针采集 → probe=0，且注明原因"""
        _, _, bd = self.agent._assess_confidence("空指针", [], rule_name="NPE",
                                                log_lines=STACK_LOGS)
        self.assertEqual(bd["probe"], 0)
        self.assertIn("未启用", bd["notes"]["probe"])

    def test_probe_failed_only_partial(self):
        _, _, bd = self.agent._assess_confidence(
            "空指针", [], rule_name="NPE", log_lines=STACK_LOGS,
            evidence=["$ cat /proc/1/status\n采集失败: permission denied"],
        )
        self.assertEqual(bd["probe"], 40)

    def test_history_partial_overlap_scored_60(self):
        """已解决但重合度不足 → 历史给 60（部分支持），仍非 high"""
        conf, _, bd = self.agent._assess_confidence(
            "空指针", [{"id": "c2", "rule_name": "OTHER", "_score": 1, "resolved": True}],
            rule_name="NPE", problem_location="", log_lines=["x"],
        )
        self.assertNotEqual(conf, "high")
        self.assertEqual(bd["history"], 60)

    def test_context_partial_score(self):
        """5-9 行上下文 → context=60"""
        _, _, bd = self.agent._assess_confidence(
            "空指针", [], rule_name="NPE",
            problem_location="com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)",
            log_lines=["at com.x.Y(Z.java:1)"] * 6,
        )
        self.assertEqual(bd["context"], 60)


class TestHandleAlertEvidenceChain(unittest.TestCase):
    """handle_alert 集成：证据链落在返回结果里；LLM 全失败不 NameError"""

    def _make_agent(self):
        agent = SelfHealAgent("dummy_device", "com.thunder.ktv", mode="observe")
        # 不真实执行 adb
        agent._collect_device_context = MagicMock(return_value={"model": "TS_KTV_X9"})
        return agent

    @patch("modules.log_monitor.selfheal.get_knowledge_base")
    def test_result_contains_evidence_chain(self, mock_kb):
        mock_kb.return_value.search_similar.return_value = []
        agent = self._make_agent()
        fake = MagicMock()
        fake.root_cause = "空指针"
        fake.suggestions = ["加判空"]
        fake.problem_location = "com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)"
        fake.impact = "播放不可用"
        fake.investigation_path = ["步骤1"]
        fake.suggested_patch = ""
        agent._agent = MagicMock()
        agent._agent.analyze.return_value = fake

        result = agent.handle_alert(
            {"type": "crash", "severity": "medium", "rule_name": "NPE", "log_line": STACK_LOGS[0]},
            STACK_LOGS,
            context_meta={"strategy": "crash_full_stack", "lines": len(STACK_LOGS), "range": [0, 13]},
        )
        ec = result.get("evidence_chain")
        self.assertIsInstance(ec, dict)
        self.assertTrue(ec["direct"])   # 触发行/堆栈/窗口/设备
        self.assertTrue(ec["inferred"])  # 根因/影响
        self.assertEqual(result.get("context_meta", {}).get("strategy"), "crash_full_stack")
        # 无历史命中但堆栈+上下文充足 → medium
        self.assertEqual(result.get("confidence"), "medium")
        # #30 分项打分字段落地
        self.assertIn("confidence_breakdown", result)
        bd = result["confidence_breakdown"]
        self.assertIsInstance(bd, dict)
        self.assertEqual(bd["stack"], 100)
        self.assertEqual(bd["history"], 0)
        self.assertEqual(bd["score"], 45)  # 30+0+15+0
        self.assertIn("confidence_score", result)
        self.assertEqual(result["confidence_score"], 45)

    @patch("modules.log_monitor.selfheal.get_knowledge_base")
    def test_llm_total_failure_no_name_error(self, mock_kb):
        """回归：LLM 重试全失败时，L3/L4 字段应为空值而非 NameError"""
        mock_kb.return_value.search_similar.return_value = []
        agent = self._make_agent()
        agent._agent = MagicMock()
        agent._agent.analyze.side_effect = RuntimeError("llm down")

        result = agent.handle_alert(
            {"type": "crash", "severity": "medium", "rule_name": "NPE", "log_line": "x"},
            ["x"],
        )
        self.assertTrue(result["root_cause"].startswith("分析失败"))
        self.assertEqual(result["problem_location"], "")
        self.assertEqual(result["suggested_patch"], "")
        self.assertEqual(result["status"], "NEEDS_HUMAN")
        self.assertEqual(result["confidence"], "low")
        # #30：根因无效时分项打分整项为 0
        self.assertEqual(result.get("confidence_score"), 0)
        self.assertIsInstance(result.get("confidence_breakdown"), dict)


if __name__ == "__main__":
    unittest.main()
