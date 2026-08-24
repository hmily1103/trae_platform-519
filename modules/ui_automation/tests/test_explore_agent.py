"""ExploreAgent / CaseValidator 单测（不依赖真机 / LLM）。"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from modules.ui_automation.agents.case_validator import CaseValidator
from modules.ui_automation.agents.device_tools import DeviceToolkit, element_brief, goal_keywords
from modules.ui_automation.agents.explore_agent import ExploreAgent, ExploreCancelled
from modules.ui_automation.models import RecordingSession, UIAction, UISelector


def _fake_el(**kwargs):
    el = MagicMock()
    el.resource_id = kwargs.get("resource_id", "")
    el.text = kwargs.get("text", "")
    el.content_desc = kwargs.get("content_desc", "")
    el.class_name = kwargs.get("class_name", "android.widget.Button")
    el.bounds = kwargs.get(
        "bounds",
        {"center_x": 100, "center_y": 200, "left": 0, "top": 0, "right": 200, "bottom": 400},
    )
    el.clickable = kwargs.get("clickable", True)
    el.focusable = kwargs.get("focusable", False)
    el.scrollable = kwargs.get("scrollable", False)
    return el


class TestDeviceToolsHelpers(unittest.TestCase):
    def test_element_brief(self):
        el = _fake_el(resource_id="com.app:id/search", text="搜索")
        b = element_brief(3, el)
        self.assertEqual(b["i"], 3)
        self.assertEqual(b["id"], "com.app:id/search")
        self.assertEqual(b["text"], "搜索")
        self.assertTrue(b["clickable"])

    def test_goal_keywords(self):
        keys = goal_keywords("打开点歌页并搜索周杰伦")
        self.assertTrue(any("周杰伦" in k or k == "周杰伦" for k in keys) or "周杰伦" in "".join(keys))
        self.assertTrue(any("点歌" in k for k in keys))


class TestVerifyActionSuccess(unittest.TestCase):
    def test_input_visible(self):
        tools = DeviceToolkit(MagicMock())
        tools.dump_ui = MagicMock(
            return_value={
                "ok": True,
                "fingerprint": "fp2",
                "texts": ["搜索", "周杰伦"],
            }
        )
        ok, reason = tools.verify_action_success(
            action_type="input",
            goal="输入周杰伦",
            before_fp="fp1",
            input_value="周杰伦",
            settle_ms=0,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "input_text_visible")

    def test_click_ui_changed(self):
        tools = DeviceToolkit(MagicMock())
        tools.dump_ui = MagicMock(
            return_value={"ok": True, "fingerprint": "fp2", "texts": ["新页面"]}
        )
        ok, reason = tools.verify_action_success(
            action_type="click",
            goal="点击确定",
            before_fp="fp1",
            settle_ms=0,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ui_changed")


class TestAssertSelector(unittest.TestCase):
    def test_text_contains(self):
        tools = DeviceToolkit(MagicMock())
        tools.dump_ui = MagicMock(
            return_value={"ok": True, "candidates": [], "texts": ["搜索结果", "周杰伦"]}
        )
        ok, used, err = tools.assert_selector(None, "text_contains:周杰伦")
        self.assertTrue(ok)
        self.assertEqual(used, "text_contains")
        self.assertEqual(err, "")


class TestEnsureAssertions(unittest.TestCase):
    def test_appends_assertion(self):
        steps = ExploreAgent._ensure_assertion_steps(
            ["打开点歌", "搜索周杰伦"], "打开点歌搜索周杰伦"
        )
        self.assertTrue(any("断言" in s for s in steps))

    def test_keeps_existing(self):
        planned = ["打开", "断言：出现结果"]
        self.assertEqual(
            ExploreAgent._ensure_assertion_steps(planned, "打开并验证"),
            planned,
        )


class TestExploreAgentBuild(unittest.TestCase):
    def setUp(self):
        self.storage = MagicMock()
        self.agent = ExploreAgent(self.storage)

    def test_build_click_from_element_index(self):
        tools = MagicMock()
        tools.build_selector.return_value = UISelector(strategy="resource_id", value="id/search")
        el = _fake_el(resource_id="id/search", text="搜索", bounds={"center_x": 10, "center_y": 20})
        decision = {
            "action_type": "click",
            "element_index": 0,
            "description": "点击搜索",
            "wait_after": 500,
        }
        built = self.agent._build_action_from_decision(tools, MagicMock(), [el], decision, "点击搜索")
        self.assertTrue(built["ok"])
        self.assertEqual(built["action_type"], "click")
        self.assertEqual(built["coordinates"]["x"], 10)
        self.assertEqual(built["element_index"], 0)
        self.assertFalse(built.get("weak_locator"))

    def test_build_assertion_from_goal(self):
        tools = MagicMock()
        tools.build_selector.return_value = UISelector(strategy="text", value="结果")
        el = _fake_el(text="结果", bounds={"center_x": 1, "center_y": 2})
        built = self.agent._build_action_from_decision(
            tools,
            MagicMock(),
            [el],
            {"action_type": "click", "element_index": 0},
            "断言：界面出现结果",
        )
        self.assertTrue(built["ok"])
        self.assertEqual(built["action_type"], "assertion")

    def test_build_fails_without_target(self):
        tools = MagicMock()
        built = self.agent._build_action_from_decision(
            tools, MagicMock(), [], {"action_type": "click"}, "点什么"
        )
        self.assertFalse(built["ok"])


class TestExploreAgentRetryFlow(unittest.TestCase):
    def test_resolve_retries_then_succeeds(self):
        storage = MagicMock()
        storage.get_screenshot_path.return_value = "/tmp/s.png"
        storage.get_ui_tree_path.return_value = "/tmp/u.xml"
        storage.save_recording.return_value = True
        agent = ExploreAgent(storage)

        tools = MagicMock()
        el = _fake_el(resource_id="id/ok", text="确定", bounds={"center_x": 50, "center_y": 60})
        dump_ok = {
            "ok": True,
            "xml": "<node/>",
            "parser": MagicMock(),
            "candidates": [el],
            "briefs": [{"i": 0, "text": "确定"}],
            "fingerprint": "fp1",
        }
        tools.dump_ui.return_value = dump_ok
        tools.build_selector.return_value = UISelector(strategy="text", value="确定")
        tools.verify_action_success.side_effect = [
            (False, "界面未见变化"),
            (True, "ui_changed"),
        ]
        tools.controller = MagicMock()
        tools.invalidate_dump_cache = MagicMock()
        tools.preview_jpeg_b64 = MagicMock(return_value=None)

        session = MagicMock()
        session.actions = []
        session.package_name = ""
        session.id = "ai_test"

        budget = {"left": 20}

        with patch.object(
            agent._explorer,
            "_llm_pick_action",
            return_value={
                "action_type": "click",
                "element_index": 0,
                "description": "点确定",
                "wait_after": 100,
            },
        ), patch.object(
            agent._explorer,
            "_execute_action",
            return_value=(True, ""),
        ):
            result = agent._resolve_step_with_retries(
                tools=tools,
                session=session,
                step_num=1,
                goal="点确定",
                case_context="点确定",
                execute=True,
                budget=budget,
                progress_callback=None,
            )

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(len(result.get("attempts") or []), 2)
        self.assertEqual(len(session.actions), 1)
        self.assertEqual(session.actions[0].status, "completed")

    def test_stop_check_raises(self):
        storage = MagicMock()
        agent = ExploreAgent(storage)
        tools = MagicMock()
        session = MagicMock()
        session.actions = []
        session.id = "ai_test"
        session.package_name = ""

        def boom():
            raise ExploreCancelled("用户已取消探索")

        with self.assertRaises(ExploreCancelled):
            agent._resolve_step_with_retries(
                tools=tools,
                session=session,
                step_num=1,
                goal="点确定",
                case_context="点确定",
                execute=True,
                budget={"left": 10},
                progress_callback=None,
                stop_check=boom,
            )


class TestCaseValidator(unittest.TestCase):
    def test_regression_ready_with_strong(self):
        tools = MagicMock()
        tools.launch_app = MagicMock()
        tools.act_by_strategy = MagicMock(return_value=(True, "resource_id", ""))
        # MagicMock 禁止 assert* 属性名，用 configure_mock
        tools.configure_mock(**{"assert_selector": MagicMock(return_value=(True, "text", ""))})

        session = RecordingSession(
            id="c1",
            device_id="d1",
            package_name="",
            created_at=datetime.now(),
            name="t",
            actions=[
                UIAction(
                    step_num=1,
                    action_type="click",
                    selector=UISelector(strategy="resource_id", value="id/ok"),
                    status="completed",
                    wait_after=0,
                ),
                UIAction(
                    step_num=2,
                    action_type="assertion",
                    selector=UISelector(strategy="text", value="成功"),
                    value="exists:成功",
                    status="completed",
                    wait_after=0,
                ),
            ],
        )
        result = CaseValidator(tools=tools).validate(session, relaunch=False, require_strong=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["regression_ready"])

    def test_weak_blocks_regression(self):
        tools = MagicMock()
        tools.act_by_strategy.return_value = (True, "coordinates", "")
        session = RecordingSession(
            id="c2",
            device_id="d1",
            package_name="",
            created_at=datetime.now(),
            actions=[
                UIAction(
                    step_num=1,
                    action_type="click",
                    selector=UISelector(strategy="coordinates", value="10,20"),
                    coordinates={"x": 10, "y": 20},
                    status="completed",
                    wait_after=0,
                )
            ],
        )
        result = CaseValidator(tools=tools).validate(session, relaunch=False, require_strong=True)
        self.assertFalse(result["regression_ready"])
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
