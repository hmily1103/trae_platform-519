# -*- coding: utf-8 -*-
"""A4 并行执行器（agents/executor.py）单元测试。

覆盖：
- 正常并行：多 Agent 全 ok、确实并行（总耗时 < 串行耗时）
- 单 Agent 超时：不阻塞其余 Agent
- 单 Agent 异常：降级为 failed，不阻塞
- 整体预算护栏：预算耗尽 → 未完成者降级 timeout，budget_exceeded=True
- 跳过条目留痕：plan.skipped 原样进 findings
- 注册表漂移防御：计划派出后 Agent 被移除 → failed 留痕
- 永不抛出 & 序列化：混合场景下 execute_plan 稳定返回，可 JSON 化
- 降级判定：usable_findings / degraded_findings / all_degraded 口径
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from modules.log_monitor.agents.base import AgentFinding, BaseAgent  # noqa: E402
from modules.log_monitor.agents.planner import (  # noqa: E402
    DiagnosisPlan, PlannedAgent, clear_registry, register_agent,
)
from modules.log_monitor.agents.executor import (  # noqa: E402
    PlanExecution, execute_plan,
)


# ---- 受控替身 Agent ----
class FastOkAgent(BaseAgent):
    name = "fast_ok"
    display_name = "秒回"

    def run(self, alert, context):
        return self.ok("fast done", evidence=[{"kind": "t", "desc": "d"}],
                       artifacts={"k": "v"})


class FastOk2Agent(FastOkAgent):
    name = "fast_ok2"
    display_name = "秒回2"


class SleepAgent(BaseAgent):
    name = "sleeper"
    display_name = "慢速"
    sleep_seconds = 0.6

    def run(self, alert, context):
        time.sleep(self.sleep_seconds)
        return self.ok("slept %.1fs" % self.sleep_seconds)


class Sleep2Agent(SleepAgent):
    name = "sleeper2"
    display_name = "慢速2"


class BoomAgent(BaseAgent):
    name = "boom"
    display_name = "必炸"

    def run(self, alert, context):
        raise RuntimeError("boom!")


class HangAgent(BaseAgent):
    name = "hang"
    display_name = "挂死"

    def run(self, alert, context):
        time.sleep(30)
        return self.ok("never reach")


def _plan_of(*agents):
    """用替身 Agent 直接构造计划并注册（绕开 PLAN_RULES，聚焦执行器行为）。"""
    plan = DiagnosisPlan(alert_id="a1", alert_type="crash")
    for a in agents:
        register_agent(a)
        plan.agents.append(PlannedAgent(a.name, a.display_name, "test"))
    return plan


ALERT = {"id": "a1", "type": "crash", "rule_name": "R", "message": "m"}


class TestExecuteBasic(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def test_all_ok(self):
        plan = _plan_of(FastOkAgent(), FastOk2Agent())
        ex = execute_plan(plan, ALERT, {})
        self.assertIsInstance(ex, PlanExecution)
        self.assertEqual(len(ex.findings), 2)
        self.assertTrue(all(f.status == "ok" for f in ex.findings.values()))
        self.assertFalse(ex.budget_exceeded)
        self.assertFalse(ex.all_degraded)
        self.assertEqual(len(ex.usable_findings), 2)

    def test_actually_parallel(self):
        """两个各睡 0.6s 的 Agent 并行跑，总耗时应远小于串行 1.2s。"""
        plan = _plan_of(SleepAgent(), Sleep2Agent())
        start = time.time()
        ex = execute_plan(plan, ALERT, {}, agent_timeout=5, total_budget=10)
        elapsed = time.time() - start
        self.assertTrue(all(f.status == "ok" for f in ex.findings.values()))
        self.assertLess(elapsed, 1.1, "并行执行总耗时不应接近串行耗时")

    def test_empty_plan(self):
        plan = DiagnosisPlan(alert_id="a1", alert_type="keyword")
        ex = execute_plan(plan, ALERT, {})
        self.assertEqual(ex.findings, {})
        self.assertFalse(ex.budget_exceeded)

    def test_elapsed_recorded(self):
        plan = _plan_of(FastOkAgent())
        ex = execute_plan(plan, ALERT, {})
        self.assertGreaterEqual(ex.elapsed_ms, 0)
        self.assertEqual(ex.findings["fast_ok"].artifacts.get("k"), "v")


class TestDegrade(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def test_single_agent_timeout_not_blocking(self):
        """挂死 Agent 单体超时降级，秒回 Agent 不受影响。"""
        plan = _plan_of(HangAgent(), FastOkAgent())
        ex = execute_plan(plan, ALERT, {}, agent_timeout=0.3, total_budget=5)
        self.assertEqual(ex.findings["hang"].status, "timeout")
        self.assertEqual(ex.findings["fast_ok"].status, "ok")
        self.assertFalse(ex.budget_exceeded, "单体超时不应记为整体预算超限")

    def test_failure_not_blocking(self):
        plan = _plan_of(BoomAgent(), FastOkAgent())
        ex = execute_plan(plan, ALERT, {})
        self.assertEqual(ex.findings["boom"].status, "failed")
        self.assertIn("boom!", ex.findings["boom"].error)
        self.assertEqual(ex.findings["fast_ok"].status, "ok")
        self.assertEqual(len(ex.degraded_findings), 1)

    def test_total_budget_exceeded(self):
        """整体预算耗尽：未完成者降级 timeout 并明示预算原因。"""
        plan = _plan_of(SleepAgent(), Sleep2Agent())
        ex = execute_plan(plan, ALERT, {}, agent_timeout=5, total_budget=0.15)
        self.assertTrue(ex.budget_exceeded)
        budget_hits = [f for f in ex.findings.values()
                       if f.status == "timeout" and "预算" in f.degrade_reason]
        self.assertGreaterEqual(len(budget_hits), 1)

    def test_all_degraded_flag(self):
        """派出的全军覆没 → all_degraded=True（调用方退回 1.0 链路）。"""
        plan = _plan_of(BoomAgent(), HangAgent())
        ex = execute_plan(plan, ALERT, {}, agent_timeout=0.2, total_budget=5)
        self.assertTrue(ex.all_degraded)
        self.assertEqual(ex.usable_findings, [])

    def test_all_degraded_ignores_skipped(self):
        """只有 skipped、没有派出 → 不算全军覆没。"""
        plan = DiagnosisPlan(alert_id="a1", alert_type="crash")
        plan.skipped.append(PlannedAgent("source_code", "源码关联", "未启用"))
        ex = execute_plan(plan, ALERT, {})
        self.assertFalse(ex.all_degraded)

    def test_unregistered_after_plan(self):
        """计划派出后 Agent 被移除 → failed 留痕，不抛出。"""
        plan = _plan_of(FastOkAgent())
        clear_registry()  # 模拟注册表漂移
        ex = execute_plan(plan, ALERT, {})
        self.assertEqual(ex.findings["fast_ok"].status, "failed")
        self.assertIn("注册表", ex.findings["fast_ok"].error)


class TestSkippedAndAudit(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def test_skipped_entries_preserved(self):
        plan = _plan_of(FastOkAgent())
        plan.skipped.append(PlannedAgent(
            "source_code", "源码关联", "源码关联未启用（未配置源码目录）"))
        ex = execute_plan(plan, ALERT, {})
        self.assertEqual(ex.findings["source_code"].status, "skipped")
        self.assertIn("未启用", ex.findings["source_code"].degrade_reason)
        # skipped 不进证据链
        self.assertEqual(len(ex.usable_findings), 1)

    def test_never_raises_mixed(self):
        """混合场景（ok/炸/挂/跳过/漂移）下 execute_plan 永不抛出。"""
        plan = _plan_of(FastOkAgent(), BoomAgent(), HangAgent())
        plan.skipped.append(PlannedAgent("source_code", "源码关联", "未启用"))
        plan.agents.append(PlannedAgent("ghost", "幽灵", "test"))  # 从未注册
        try:
            ex = execute_plan(plan, ALERT, {}, agent_timeout=0.3, total_budget=5)
        except Exception as exc:  # noqa: BLE001
            self.fail("execute_plan 不应抛出异常: %r" % exc)
        self.assertEqual(len(ex.findings), 5)
        self.assertEqual(ex.findings["fast_ok"].status, "ok")
        self.assertEqual(ex.findings["boom"].status, "failed")
        self.assertEqual(ex.findings["hang"].status, "timeout")
        self.assertEqual(ex.findings["source_code"].status, "skipped")
        self.assertEqual(ex.findings["ghost"].status, "failed")

    def test_to_dict_json_serializable(self):
        plan = _plan_of(FastOkAgent(), BoomAgent())
        ex = execute_plan(plan, ALERT, {})
        payload = ex.to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("fast_ok", text)
        round_trip = json.loads(ex.to_json())
        self.assertEqual(round_trip["plan"]["alert_type"], "crash")
        self.assertIn("budget_exceeded", round_trip)

    def test_finding_keys_match_plan(self):
        plan = _plan_of(FastOkAgent(), FastOk2Agent())
        plan.skipped.append(PlannedAgent("source_code", "源码关联", "未启用"))
        ex = execute_plan(plan, ALERT, {})
        self.assertEqual(
            set(ex.findings.keys()),
            {"fast_ok", "fast_ok2", "source_code"},
        )


if __name__ == "__main__":
    unittest.main()
