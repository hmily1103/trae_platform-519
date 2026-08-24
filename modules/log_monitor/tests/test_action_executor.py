#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#29 告警 action 只读动作执行链路 - 单元测试。

覆盖：
- validate_shell_action：白名单放行、黑名单拒绝、重定向拒绝、空命令、管道过滤器放行
- execute_action：screenshot 截图落盘（含 PNG 魔数校验）、shell 白名单执行、
  shell 危险命令拒绝、custom_shell 一律拒绝、none/空返回 None、缺设备返回 failed
- action_result_to_str：JSON 序列化
- build_evidence_chain 直接证据⑥：动作产物挂进证据链（仅 status=ok 时）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.log_monitor import action_executor as ax
from modules.log_monitor.selfheal import build_evidence_chain


PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200  # 足够长且以 PNG 魔数开头


class TestValidateShellAction(unittest.TestCase):
    """只读 shell 动作护栏校验。"""

    def test_empty_rejected(self):
        self.assertIsNotNone(ax.validate_shell_action(""))
        self.assertIsNotNone(ax.validate_shell_action("   "))

    def test_whitelist_pass(self):
        self.assertIsNone(ax.validate_shell_action("dumpsys meminfo com.thunder.ktv"))
        self.assertIsNone(ax.validate_shell_action("cat /proc/1/status"))

    def test_blacklist_rejected(self):
        # "kill" 在 DANGEROUS_TOKENS 内
        self.assertIsNotNone(ax.validate_shell_action("ps -A; kill -9 1"))
        # " rm " 在黑名单内
        self.assertIsNotNone(ax.validate_shell_action("rm -rf /tmp/x"))

    def test_redirect_rejected(self):
        # 重定向写真实文件 → 拒绝（存在副作用）
        self.assertIsNotNone(ax.validate_shell_action("dumpsys > /tmp/out.txt"))
        self.assertIsNotNone(ax.validate_shell_action("dumpsys meminfo >> /data/local/tmp/x"))

    def test_dev_null_redirect_allowed(self):
        # 重定向到 /dev/null 只是丢弃输出，无副作用，按设计放行
        self.assertIsNone(ax.validate_shell_action("dumpsys activity >/dev/null"))
        self.assertIsNone(ax.validate_shell_action("dumpsys meminfo 2>/dev/null"))

    def test_non_whitelist_first_token_rejected(self):
        self.assertIsNotNone(ax.validate_shell_action("ping 1.1.1.1"))

    def test_pipe_filters_allowed(self):
        # 首段白名单 + 过滤器 grep/head 应放行
        self.assertIsNone(
            ax.validate_shell_action("logcat -d -t 500 | grep Activity | head -20")
        )

    def test_empty_pipe_segment_rejected(self):
        self.assertIsNotNone(ax.validate_shell_action("logcat -d | | head"))


class TestExecuteAction(unittest.TestCase):
    """execute_action 各动作分支。"""

    def setUp(self):
        # 截图产物写入临时目录，避免污染仓库 static
        self._tmp = tempfile.mkdtemp(prefix="alert_cap_")
        self._orig_capture_dir = ax.CAPTURE_DIR
        ax.CAPTURE_DIR = self._tmp

    def tearDown(self):
        ax.CAPTURE_DIR = self._orig_capture_dir
        for f in os.listdir(self._tmp):
            try:
                os.remove(os.path.join(self._tmp, f))
            except OSError:
                pass
        try:
            os.rmdir(self._tmp)
        except OSError:
            pass

    def test_none_and_empty_return_none(self):
        self.assertIsNone(ax.execute_action("", "dev1", "a1"))
        self.assertIsNone(ax.execute_action("none", "dev1", "a1"))
        self.assertIsNone(ax.execute_action("NONE", "dev1", "a1"))

    def test_missing_device_failed(self):
        res = ax.execute_action("screenshot", "", "a1")
        self.assertEqual(res["status"], "failed")
        self.assertIn("缺少设备", res["summary"])

    def test_screenshot_ok(self):
        fake = MagicMock()
        fake.stdout = PNG_HEAD
        fake.stderr = b""
        with patch("modules.log_monitor.action_executor.subprocess.run", return_value=fake):
            res = ax.execute_action("screenshot", "dev1", "alert_42")
        self.assertEqual(res["type"], "screenshot")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["artifact"].startswith(ax.CAPTURE_URL_PREFIX))
        # 真实落盘校验
        fname = os.path.basename(res["artifact"])
        self.assertTrue(os.path.exists(os.path.join(self._tmp, fname)))

    def test_screenshot_invalid_output_failed(self):
        fake = MagicMock()
        fake.stdout = b"this is not a png"
        fake.stderr = b"error"
        with patch("modules.log_monitor.action_executor.subprocess.run", return_value=fake):
            res = ax.execute_action("screenshot", "dev1", "alert_43")
        self.assertEqual(res["status"], "failed")
        self.assertIn("截图无效", res["summary"])

    def test_shell_whitelist_executes(self):
        fake = MagicMock()
        fake.stdout = "com.thunder.ktv (pid 1234)"
        fake.stderr = b""
        with patch("modules.log_monitor.action_executor.subprocess.run", return_value=fake):
            res = ax.execute_action(
                "shell:dumpsys meminfo {package}", "dev1", "a1", package="com.thunder.ktv"
            )
        self.assertEqual(res["status"], "ok")
        self.assertIn("com.thunder.ktv", res["command"])
        self.assertIn("pid 1234", res["output"])
        self.assertIn("dumpsys meminfo", res["command"])

    def test_shell_dangerous_refused(self):
        res = ax.execute_action("shell:kill -9 1", "dev1", "a1")
        self.assertEqual(res["status"], "refused")
        self.assertIn("危险命令", res["summary"])

    def test_shell_rm_refused(self):
        res = ax.execute_action("shell:rm -rf /data/local/tmp", "dev1", "a1")
        self.assertEqual(res["status"], "refused")

    def test_shell_output_truncated(self):
        fake = MagicMock()
        fake.stdout = "HEAD" + ("\nfiller line" * 300) + "\nTAIL"
        fake.stderr = b""
        with patch("modules.log_monitor.action_executor.subprocess.run", return_value=fake):
            res = ax.execute_action("shell:logcat -d", "dev1", "a1")
        self.assertEqual(res["status"], "ok")
        self.assertIn("(中间截断)", res["output"])
        self.assertIn("HEAD", res["output"])
        self.assertIn("TAIL", res["output"])

    def test_custom_shell_refused(self):
        res = ax.execute_action("custom_shell:ps -A", "dev1", "a1")
        self.assertEqual(res["status"], "refused")
        self.assertIn("安全策略禁止", res["summary"])

    def test_unknown_action_skipped(self):
        res = ax.execute_action("weird:something", "dev1", "a1")
        self.assertEqual(res["status"], "skipped")


class TestActionResultToStr(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(ax.action_result_to_str(None), "")

    def test_dict_serialized(self):
        s = ax.action_result_to_str({"type": "screenshot", "status": "ok"})
        self.assertTrue(s.startswith("{"))
        self.assertIn("screenshot", s)


class TestEvidenceChainActionArtifact(unittest.TestCase):
    """直接证据⑥：动作产物挂进证据链（仅 status=ok 时）。"""

    def test_screenshot_artifact_added(self):
        action_result = {
            "type": "screenshot", "status": "ok",
            "summary": "崩溃现场截图已留存（120KB）",
            "artifact": "/static/alert_captures/alert_1_120000.png",
            "command": "adb -s dev1 exec-out screencap -p",
        }
        chain = build_evidence_chain(action_result=action_result)
        arts = [d for d in chain["direct"] if d["kind"] == "action_artifact"]
        self.assertEqual(len(arts), 1)
        self.assertIn("alert_1_120000.png", arts[0]["detail"])
        self.assertEqual(arts[0]["label"], "截图取证")

    def test_shell_output_added(self):
        action_result = {
            "type": "shell", "status": "ok",
            "summary": "只读命令采集完成",
            "output": "pid 1234",
            "command": "dumpsys meminfo",
        }
        chain = build_evidence_chain(action_result=action_result)
        arts = [d for d in chain["direct"] if d["kind"] == "action_artifact"]
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["label"], "告警动作采集")
        self.assertIn("pid 1234", arts[0]["detail"])

    def test_refused_action_not_added(self):
        action_result = {
            "type": "custom_shell", "status": "refused",
            "summary": "已拒绝", "command": "custom_shell:ps",
        }
        chain = build_evidence_chain(action_result=action_result)
        arts = [d for d in chain["direct"] if d["kind"] == "action_artifact"]
        self.assertEqual(len(arts), 0)

    def test_no_action_result_no_artifact(self):
        chain = build_evidence_chain()
        arts = [d for d in chain["direct"] if d["kind"] == "action_artifact"]
        self.assertEqual(len(arts), 0)


if __name__ == "__main__":
    unittest.main()
