# -*- coding: utf-8 -*-
"""A2 Planner 编排器单元测试。

覆盖：
- 规则表：7 种告警类型 + oom 预留 + 未知类型兜底
- 注册表：注册/注销/只读红线拒绝/类型校验
- build_plan：派出与跳过的划分、跳过原因、空注册表安全降级、to_dict 结构
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from modules.log_monitor.agents.base import BaseAgent  # noqa: E402
from modules.log_monitor.agents import planner  # noqa: E402
from modules.log_monitor.agents.planner import (  # noqa: E402
    AGENT_LOG, AGENT_PROBE, AGENT_HISTORY, AGENT_SOURCE,
    PLAN_RULES, build_plan, register_agent, unregister_agent,
    registered_agent_names, clear_registry,
)


class _StubAgent(BaseAgent):
    display_name = "桩"

    def __init__(self, name):
        self.name = name

    def run(self, alert, context):  # pragma: no cover - 编排测试不执行
        return self.ok("stub")


class _WritableStub(BaseAgent):
    name = "writable_stub"
    readonly = False

    def run(self, alert, context):  # pragma: no cover
        return self.ok("stub")


def _register_all_core():
    for name in (AGENT_LOG, AGENT_PROBE, AGENT_HISTORY):
        register_agent(_StubAgent(name))


class PlannerTestBase(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()


class TestRegistry(PlannerTestBase):
    def test_register_and_list(self):
        _register_all_core()
        self.assertEqual(
            registered_agent_names(),
            sorted([AGENT_LOG, AGENT_PROBE, AGENT_HISTORY]))

    def test_unregister(self):
        _register_all_core()
        unregister_agent(AGENT_PROBE)
        self.assertNotIn(AGENT_PROBE, registered_agent_names())

    def test_readonly_guard_rejects_writable(self):
        with self.assertRaises(ValueError):
            register_agent(_WritableStub())
        self.assertEqual(registered_agent_names(), [])

    def test_non_agent_rejected(self):
        with self.assertRaises(TypeError):
            register_agent(object())  # type: ignore


class TestPlanRules(PlannerTestBase):
    def test_all_seven_alert_types_covered(self):
        """alert_engine 的 7 种类型必须都在规则表中（oom 为预留第 8 种）。"""
        for t in ("keyword", "regex", "exception", "anr", "crash",
                  "level", "frequency"):
            self.assertIn(t, PLAN_RULES, "规则表缺少告警类型: %s" % t)

    def test_crash_plan_dispatches_three_core_agents(self):
        _register_all_core()
        plan = build_plan({"id": "a1", "type": "crash", "rule_name": "Crash监控"})
        self.assertEqual(plan.agent_names, [AGENT_LOG, AGENT_PROBE, AGENT_HISTORY])
        # source_code 未注册 → 进 skipped 且注明未启用
        skipped_names = [s.name for s in plan.skipped]
        self.assertEqual(skipped_names, [AGENT_SOURCE])
        self.assertIn("未启用", plan.skipped[0].reason)

    def test_anr_plan_no_source_agent(self):
        _register_all_core()
        plan = build_plan({"id": "a2", "type": "anr"})
        self.assertEqual(plan.agent_names, [AGENT_LOG, AGENT_PROBE, AGENT_HISTORY])
        self.assertEqual(plan.skipped, [])

    def test_keyword_plan_only_log(self):
        _register_all_core()
        plan = build_plan({"id": "a3", "type": "keyword"})
        self.assertEqual(plan.agent_names, [AGENT_LOG])

    def test_regex_plan_log_and_history(self):
        _register_all_core()
        plan = build_plan({"id": "a4", "type": "regex"})
        self.assertEqual(plan.agent_names, [AGENT_LOG, AGENT_HISTORY])

    def test_unknown_type_falls_back_to_default(self):
        _register_all_core()
        plan = build_plan({"id": "a5", "type": "weird_type"})
        self.assertEqual(plan.agent_names, [AGENT_LOG])
        self.assertIn("默认计划", plan.note)

    def test_empty_type_falls_back(self):
        _register_all_core()
        plan = build_plan({"id": "a6"})
        self.assertEqual(plan.agent_names, [AGENT_LOG])
        self.assertIn("默认计划", plan.note)

    def test_type_case_insensitive(self):
        _register_all_core()
        plan = build_plan({"id": "a7", "type": " CRASH "})
        self.assertEqual(plan.alert_type, "crash")
        self.assertEqual(plan.agent_names, [AGENT_LOG, AGENT_PROBE, AGENT_HISTORY])

    def test_every_entry_has_reason(self):
        """规则表每个条目都必须有非空理由（可审计要求）。"""
        for t, entries in PLAN_RULES.items():
            for name, reason in entries:
                self.assertTrue(reason and reason.strip(),
                                "%s/%s 缺少派出理由" % (t, name))


class TestPlanDegradation(PlannerTestBase):
    def test_empty_registry_all_skipped(self):
        """注册表为空 → 全部进 skipped，计划结构完整（调用方可安全降级 1.0）。"""
        plan = build_plan({"id": "a8", "type": "crash"})
        self.assertEqual(plan.agents, [])
        self.assertEqual(len(plan.skipped), len(PLAN_RULES["crash"]))
        for s in plan.skipped:
            self.assertTrue(s.reason)

    def test_partial_registry(self):
        register_agent(_StubAgent(AGENT_LOG))
        plan = build_plan({"id": "a9", "type": "crash"})
        self.assertEqual(plan.agent_names, [AGENT_LOG])
        skipped = [s.name for s in plan.skipped]
        self.assertEqual(skipped, [AGENT_PROBE, AGENT_HISTORY, AGENT_SOURCE])

    def test_to_dict_structure(self):
        _register_all_core()
        d = build_plan({"id": "a10", "type": "crash", "rule_name": "R"}).to_dict()
        for key in ("alert_id", "alert_type", "rule_name", "agents",
                    "skipped", "created_at", "note"):
            self.assertIn(key, d)
        self.assertEqual(d["alert_id"], "a10")
        self.assertEqual(d["rule_name"], "R")
        for item in d["agents"] + d["skipped"]:
            self.assertIn("name", item)
            self.assertIn("display_name", item)
            self.assertIn("reason", item)


if __name__ == "__main__":
    unittest.main()
