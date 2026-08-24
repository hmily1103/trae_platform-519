# -*- coding: utf-8 -*-
"""A1 Agent 协议层单元测试。

覆盖：
- AgentFinding 结构与 to_dict / 状态校验 / usable 判定
- BaseAgent 便捷构造（ok / skipped）
- run_with_guard：正常 / 异常降级 / 超时降级 / 非法返回值 / 只读红线拦截
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from modules.log_monitor.agents.base import (  # noqa: E402
    AgentFinding, BaseAgent, run_with_guard,
    STATUS_OK, STATUS_FAILED, STATUS_TIMEOUT, STATUS_SKIPPED,
)


# ---- 测试用 Agent 桩 ----

class _OkAgent(BaseAgent):
    name = "ok_agent"
    display_name = "正常 Agent"

    def run(self, alert, context):
        return self.ok(
            "分析完成",
            evidence=[{"kind": "test", "desc": "证据1", "detail": "detail"}],
            artifacts={"root_cause": "空指针"},
        )


class _BoomAgent(BaseAgent):
    name = "boom_agent"
    display_name = "异常 Agent"

    def run(self, alert, context):
        raise RuntimeError("boom")


class _SlowAgent(BaseAgent):
    name = "slow_agent"
    display_name = "慢 Agent"

    def run(self, alert, context):
        time.sleep(5)
        return self.ok("不该到这里")


class _BadReturnAgent(BaseAgent):
    name = "bad_return_agent"
    display_name = "坏返回 Agent"

    def run(self, alert, context):
        return {"not": "a finding"}


class _WritableAgent(BaseAgent):
    name = "writable_agent"
    display_name = "越权 Agent"
    readonly = False  # 违反只读红线

    def run(self, alert, context):
        return self.ok("不该被执行")


class _SkipAgent(BaseAgent):
    name = "skip_agent"
    display_name = "跳过 Agent"

    def run(self, alert, context):
        return self.skipped("源码关联未启用")


_ALERT = {"id": "a1", "type": "crash", "rule_name": "NPE", "message": "boom"}
_CTX = {"device_id": "dummy", "package": "com.thunder.ktv", "log_lines": []}


class TestAgentFinding(unittest.TestCase):
    def test_to_dict_roundtrip(self):
        f = AgentFinding(agent_name="x", status=STATUS_OK, summary="s",
                         evidence=[{"kind": "k"}], artifacts={"a": 1})
        d = f.to_dict()
        self.assertEqual(d["agent_name"], "x")
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["evidence"], [{"kind": "k"}])
        self.assertEqual(d["artifacts"], {"a": 1})
        self.assertIn("started_at", d)
        self.assertIn("duration_ms", d)

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            AgentFinding(agent_name="x", status="weird")

    def test_usable_only_when_ok(self):
        self.assertTrue(AgentFinding(agent_name="x", status=STATUS_OK).usable)
        for st in (STATUS_FAILED, STATUS_TIMEOUT, STATUS_SKIPPED):
            self.assertFalse(AgentFinding(agent_name="x", status=st).usable)


class TestBaseAgentHelpers(unittest.TestCase):
    def test_ok_helper(self):
        f = _OkAgent().ok("done", artifacts={"k": "v"})
        self.assertEqual(f.agent_name, "ok_agent")
        self.assertEqual(f.status, STATUS_OK)
        self.assertEqual(f.artifacts, {"k": "v"})

    def test_skipped_helper(self):
        f = _SkipAgent().run(_ALERT, _CTX)
        self.assertEqual(f.status, STATUS_SKIPPED)
        self.assertIn("未启用", f.degrade_reason)
        self.assertFalse(f.usable)

    def test_readonly_default_true(self):
        self.assertTrue(_OkAgent.readonly)
        self.assertTrue(BaseAgent.readonly)


class TestRunWithGuard(unittest.TestCase):
    def test_normal_run(self):
        f = run_with_guard(_OkAgent(), _ALERT, _CTX, timeout=5)
        self.assertEqual(f.status, STATUS_OK)
        self.assertEqual(f.summary, "分析完成")
        self.assertEqual(f.artifacts.get("root_cause"), "空指针")
        self.assertGreaterEqual(f.duration_ms, 0)

    def test_exception_degrades_to_failed(self):
        f = run_with_guard(_BoomAgent(), _ALERT, _CTX, timeout=5)
        self.assertEqual(f.status, STATUS_FAILED)
        self.assertIn("RuntimeError", f.error)
        self.assertIn("降级", f.degrade_reason)

    def test_timeout_degrades(self):
        f = run_with_guard(_SlowAgent(), _ALERT, _CTX, timeout=0.5)
        self.assertEqual(f.status, STATUS_TIMEOUT)
        self.assertIn("超时", f.summary)
        self.assertIn("降级", f.degrade_reason)

    def test_bad_return_degrades_to_failed(self):
        f = run_with_guard(_BadReturnAgent(), _ALERT, _CTX, timeout=5)
        self.assertEqual(f.status, STATUS_FAILED)
        self.assertIn("AgentFinding", f.error)

    def test_readonly_guard_blocks_writable_agent(self):
        f = run_with_guard(_WritableAgent(), _ALERT, _CTX, timeout=5)
        self.assertEqual(f.status, STATUS_FAILED)
        self.assertIn("只读", f.error + f.degrade_reason)

    def test_non_agent_object_rejected(self):
        f = run_with_guard(object(), _ALERT, _CTX, timeout=5)  # type: ignore
        self.assertEqual(f.status, STATUS_FAILED)
        self.assertIn("协议", f.error + f.degrade_reason)

    def test_guard_never_raises(self):
        # 协议层承诺：任何输入都不抛异常
        for agent in (_OkAgent(), _BoomAgent(), _BadReturnAgent(),
                      _WritableAgent(), _SkipAgent()):
            try:
                f = run_with_guard(agent, _ALERT, _CTX, timeout=2)
            except Exception as exc:  # pragma: no cover
                self.fail("run_with_guard 抛出了异常: %r" % exc)
            self.assertIsInstance(f, AgentFinding)


if __name__ == "__main__":
    unittest.main()
