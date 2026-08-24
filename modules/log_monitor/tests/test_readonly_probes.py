#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#26 分类型只读采集模板补强 - 单元测试

验证点：
1. 各类型探针数量补强到位（crash/anr/oom/exception/keyword）
2. 所有探针都能通过 _safe_adb 黑名单护栏（不被自身护栏误拒）
3. 关键新探针存在（logcat -b crash、/data/anr、/proc/<pid>/status、lmkd）
4. 探针中不含危险词（kill/restart 等）——只读安全
5. 采集输出带命令标签，保头+保尾截断
6. 证据链能解析新格式（"$ 命令\n输出"），失败/空输出被过滤
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.log_monitor.selfheal import (
    READONLY_PROBES,
    DANGEROUS_TOKENS,
    SelfHealAgent,
    build_evidence_chain,
)


class TestProbeTemplates(unittest.TestCase):
    """探针模板本身的正确性。"""

    def test_all_types_present(self):
        for t in ("crash", "anr", "exception", "oom", "keyword"):
            self.assertIn(t, READONLY_PROBES)
            self.assertGreaterEqual(len(READONLY_PROBES[t]), 2, f"{t} 探针数量不足")

    def test_probe_counts_enhanced(self):
        # 补强后的最低数量：crash>=4 anr>=5 oom>=5 exception>=2 keyword>=2
        self.assertGreaterEqual(len(READONLY_PROBES["crash"]), 4)
        self.assertGreaterEqual(len(READONLY_PROBES["anr"]), 5)
        self.assertGreaterEqual(len(READONLY_PROBES["oom"]), 5)

    def test_key_new_probes_exist(self):
        crash_all = " ".join(READONLY_PROBES["crash"])
        self.assertIn("logcat -b crash", crash_all, "crash 缺专用缓冲区探针")
        self.assertIn("ps -A", crash_all, "crash 缺进程存活探针")
        anr_all = " ".join(READONLY_PROBES["anr"])
        self.assertIn("/data/anr", anr_all, "anr 缺 traces 探针")
        self.assertIn("cpuinfo", anr_all, "anr 缺 CPU 探针")
        oom_all = " ".join(READONLY_PROBES["oom"])
        self.assertIn("/proc/", oom_all, "oom 缺进程内存明细探针")
        self.assertIn("lmkd", oom_all, "oom 缺低内存信号探针")

    def test_no_dangerous_tokens_in_probes(self):
        """所有探针必须只读——不含黑名单危险词（含 lowmemorykiller 陷阱）。"""
        for t, probes in READONLY_PROBES.items():
            for p in probes:
                low = p.lower()
                for tok in DANGEROUS_TOKENS:
                    self.assertNotIn(
                        tok, low,
                        f"{t} 探针含危险词 '{tok.strip()}': {p}"
                    )

    def test_all_probes_pass_safe_adb_guard(self):
        """每条探针都必须能通过 _safe_adb 护栏（不被误拒）。"""
        agent = SelfHealAgent("dev123", "com.thunder.ktv", mode="collect")
        for t, probes in READONLY_PROBES.items():
            for p in probes:
                cmd = agent._safe_adb(p, "NullPointerException at foo")
                self.assertIsNotNone(cmd, f"{t} 探针被护栏误拒: {p}")
                self.assertIn("adb -s dev123 shell", cmd)


class TestEvidenceCollection(unittest.TestCase):
    """采集输出格式：命令标签 + 保头保尾截断。"""

    def _run_collect(self, stdout_text):
        agent = SelfHealAgent("dev123", "com.thunder.ktv", mode="collect")
        fake = MagicMock()
        fake.stdout = stdout_text
        with patch("modules.log_monitor.selfheal.subprocess.run", return_value=fake):
            return agent._collect_readonly_evidence("exception", "SomeError")

    def test_output_has_command_label(self):
        out = self._run_collect("E/foo: Exception happened")
        self.assertTrue(out, "应有采集结果")
        for ev in out:
            self.assertTrue(ev.startswith("$ "), f"证据缺命令标签: {ev[:50]}")

    def test_empty_output_labeled(self):
        out = self._run_collect("")
        for ev in out:
            self.assertIn("(无输出)", ev)
            self.assertTrue(ev.startswith("$ "))

    def test_truncate_keeps_head_and_tail(self):
        head = "FATAL EXCEPTION: main HEAD_MARKER"
        tail = "TAIL_MARKER at com.x.Y(Z.java:1)"
        big = head + ("\nfiller line" * 300) + "\n" + tail
        out = self._run_collect(big)
        for ev in out:
            self.assertIn("HEAD_MARKER", ev, "截断丢了头部（FATAL 头）")
            self.assertIn("TAIL_MARKER", ev, "截断丢了尾部")
            self.assertIn("(中间截断)", ev)


class TestEvidenceChainParsesNewFormat(unittest.TestCase):
    """证据链解析 #26 新格式。"""

    def test_labeled_probe_parsed(self):
        chain = build_evidence_chain(
            probe_evidence=["$ logcat -b crash -d -t 300\nFATAL EXCEPTION: main\nat com.x.Y(Z.java:1)"],
        )
        probes = [d for d in chain["direct"] if d["kind"] == "probe"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["source"], "$ logcat -b crash -d -t 300")
        self.assertIn("FATAL EXCEPTION", probes[0]["detail"])

    def test_failed_and_empty_filtered(self):
        chain = build_evidence_chain(probe_evidence=[
            "$ cat /data/anr/traces.txt\n采集失败: permission denied",
            "$ dumpsys cpuinfo\n(无输出)",
            "",
        ])
        probes = [d for d in chain["direct"] if d["kind"] == "probe"]
        self.assertEqual(len(probes), 0, "失败/空输出不应进证据链")

    def test_legacy_plain_format_still_works(self):
        """兼容旧格式（无命令标签的裸输出）。"""
        chain = build_evidence_chain(probe_evidence=["some raw adb output"])
        probes = [d for d in chain["direct"] if d["kind"] == "probe"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["source"], "adb 只读命令")


if __name__ == "__main__":
    unittest.main()
