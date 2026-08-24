# -*- coding: utf-8 -*-
"""B2+B3 源码关联 Agent 单元测试。

验证：堆栈解析 / 源码定位 / 片段读取 / LLM 复判 / 降级跳过 / 证据挂接。
全部使用临时源码目录 + 注入替身 LLM，不打真实环境。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from modules.log_monitor.agents.source_agent import (
    SourceCodeAdapter, parse_stack_locations,
)
from modules.log_monitor.agents.source_index import SourceCodeIndex


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


STACK_LOGS = [
    "E/AndroidRuntime: FATAL EXCEPTION: main",
    "    at com.thunder.ktv.PlayerManager.play(PlayerManager.java:88)",
    "    at com.thunder.ktv.player.VideoRenderer.render(VideoRenderer.java:45)",
    "    at java.lang.Thread.run(Thread.java:924)",
]


class TestParseStackLocations(unittest.TestCase):
    """B2: 堆栈→源码定位点解析。"""

    def test_parse_from_problem_location(self):
        locs = parse_stack_locations(
            problem_location="com.thunder.ktv.PlayerManager.play() (PlayerManager.java:88)"
        )
        self.assertEqual(len(locs), 1)
        self.assertEqual(locs[0]["file"], "PlayerManager.java")
        self.assertEqual(locs[0]["line"], 88)
        self.assertEqual(locs[0]["source"], "problem_location")

    def test_parse_from_log_lines(self):
        locs = parse_stack_locations(problem_location="", log_lines=STACK_LOGS)
        # PlayerManager.java:88, VideoRenderer.java:45 (Thread.java 不是 .java? 是的 Thread.java:924)
        files = [(l["file"], l["line"]) for l in locs]
        self.assertIn(("PlayerManager.java", 88), files)
        self.assertIn(("VideoRenderer.java", 45), files)
        self.assertTrue(all(l["source"] == "stack_frame" for l in locs))

    def test_dedup_same_file_line(self):
        locs = parse_stack_locations(
            problem_location="at ...(PlayerManager.java:88)",
            log_lines=["    at com.x.PlayerManager.play(PlayerManager.java:88)"],
        )
        self.assertEqual(len(locs), 1)  # 去重

    def test_no_locations(self):
        self.assertEqual(parse_stack_locations("", []), [])
        self.assertEqual(parse_stack_locations("some text", ["no stack here"]), [])

    def test_kotlin_file_parsed(self):
        locs = parse_stack_locations(
            problem_location="at com.x.FormatHelper.format(FormatHelper.kt:12)"
        )
        self.assertEqual(locs[0]["file"], "FormatHelper.kt")
        self.assertEqual(locs[0]["line"], 12)


class TestSourceCodeAdapterDisabled(unittest.TestCase):
    """源码索引未启用 → skipped。"""

    def test_skipped_when_index_disabled(self):
        idx = SourceCodeIndex(root="")  # 禁用
        agent = SourceCodeAdapter(index=idx)
        alert = {"type": "crash", "message": "NPE"}
        result = agent.run(alert, {"log_lines": STACK_LOGS})
        self.assertEqual(result.status, "skipped")
        self.assertIn("未启用", result.degrade_reason)


class TestSourceCodeAdapterNoStack(unittest.TestCase):
    """无堆栈定位点 → skipped。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="src_agent_test_")
        self.idx = SourceCodeIndex(root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_skipped_when_no_stack(self):
        agent = SourceCodeAdapter(index=self.idx)
        result = agent.run(
            {"type": "keyword", "message": "some error"},
            {"log_lines": ["just a log line, no stack"]},
        )
        self.assertEqual(result.status, "skipped")


class TestSourceCodeAdapterHappyPath(unittest.TestCase):
    """B2 正向路径：有堆栈 + 有源码 → ok + 源码证据。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="src_agent_happy_")
        # 模拟工程
        _write(os.path.join(self.tmpdir, "src", "com", "thunder", "ktv",
                            "PlayerManager.java"),
               "package com.thunder.ktv;\n"
               "public class PlayerManager {\n"
               "    public void play(String url) {\n"
               "        if (url == null) {\n"      # line 4
               "            throw new IllegalArgumentException();\n"  # line 5
               "        }\n"
               "        startPlayback(url);\n"      # line 7
               "    }\n"
               "    private void startPlayback(String url) {\n"
               "        // internal\n"
               "    }\n"
               "}\n")
        self.idx = SourceCodeIndex(root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_source_and_reads_snippet(self):
        agent = SourceCodeAdapter(index=self.idx, llm_caller=None)  # 无 LLM
        result = agent.run(
            {"type": "crash", "message": "NullPointerException"},
            {"log_lines": STACK_LOGS, "root_cause": "url 为 null 导致 NPE"},
        )
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.usable)
        # 应有关联到 PlayerManager.java 的源码证据
        source_ev = [e for e in result.evidence if e["kind"] == "source_code"]
        self.assertTrue(len(source_ev) >= 1)
        self.assertIn("PlayerManager.java", source_ev[0]["desc"])
        # artifacts 应有 snippets
        self.assertIn("snippets", result.artifacts)
        self.assertTrue(len(result.artifacts["snippets"]) >= 1)

    def test_snippet_contains_target_line_marker(self):
        agent = SourceCodeAdapter(index=self.idx)
        result = agent.run(
            {"type": "crash", "message": "NPE"},
            {"log_lines": STACK_LOGS},
        )
        snip = result.artifacts["snippets"][0]
        target_lines = [l for l in snip["lines"] if l["is_target"]]
        self.assertEqual(len(target_lines), 1)
        # 目标行 88 超出文件行数 → clamp 到文件末行
        self.assertLessEqual(target_lines[0]["lineno"], 12)


class TestSourceCodeFileNotFound(unittest.TestCase):
    """有堆栈但源码文件不在索引中 → skipped。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="src_agent_notfound_")
        _write(os.path.join(self.tmpdir, "Other.java"), "public class Other {}")
        self.idx = SourceCodeIndex(root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_skipped_when_file_not_found(self):
        agent = SourceCodeAdapter(index=self.idx)
        result = agent.run(
            {"type": "crash", "message": "NPE"},
            {"log_lines": ["    at com.x.PlayerManager.play(PlayerManager.java:88)"]},
        )
        self.assertEqual(result.status, "skipped")
        self.assertIn("未找到", result.degrade_reason)


class TestLLMReview(unittest.TestCase):
    """B3: LLM 结合源码复判根因。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="src_llm_test_")
        _write(os.path.join(self.tmpdir, "Demo.java"),
               "public class Demo {\n"
               "    void run() {\n"
               "        Object o = null;\n"  # line 3
               "        o.toString();\n"     # line 4
               "    }\n"
               "}\n")
        self.idx = SourceCodeIndex(root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_llm_confirmed(self):
        """LLM 返回 confirmed=True → artifacts.review.confirmed=True。"""
        mock_llm = MagicMock(return_value='{"confirmed": true, "reason": "源码第4行空指针调用佐证NPE", "suggested_fix": "加空判断"}')
        agent = SourceCodeAdapter(index=self.idx, llm_caller=mock_llm)
        result = agent.run(
            {"type": "crash", "message": "NPE at Demo.java:4"},
            {"log_lines": ["    at Demo.run(Demo.java:4)"], "root_cause": "空指针调用"},
        )
        self.assertEqual(result.status, "ok")
        review = result.artifacts.get("review")
        self.assertIsNotNone(review)
        self.assertTrue(review["confirmed"])
        self.assertIn("空指针", review["reason"])
        # summary 应提及复判确认
        self.assertIn("复判确认", result.summary)

    def test_llm_skeptical(self):
        """LLM 返回 confirmed=False → summary 提及存疑。"""
        mock_llm = MagicMock(return_value='{"confirmed": false, "reason": "源码片段未体现NPE风险", "suggested_fix": ""}')
        agent = SourceCodeAdapter(index=self.idx, llm_caller=mock_llm)
        result = agent.run(
            {"type": "crash", "message": "NPE at Demo.java:4"},
            {"log_lines": ["    at Demo.run(Demo.java:4)"], "root_cause": "疑似空指针"},
        )
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.artifacts["review"]["confirmed"])
        self.assertIn("存疑", result.summary)

    def test_llm_failure_does_not_block(self):
        """LLM 复判失败 → 不阻塞，仍返回 ok（源码片段本身有价值）。"""
        mock_llm = MagicMock(side_effect=Exception("LLM timeout"))
        agent = SourceCodeAdapter(index=self.idx, llm_caller=mock_llm)
        result = agent.run(
            {"type": "crash", "message": "NPE at Demo.java:4"},
            {"log_lines": ["    at Demo.run(Demo.java:4)"], "root_cause": "空指针"},
        )
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.artifacts.get("review"))

    def test_llm_markdown_wrapped_json(self):
        """LLM 返回包裹在 markdown 代码块中的 JSON → 能正确解析。"""
        mock_llm = MagicMock(return_value='```json\n{"confirmed": true, "reason": "ok", "suggested_fix": ""}\n```')
        agent = SourceCodeAdapter(index=self.idx, llm_caller=mock_llm)
        result = agent.run(
            {"type": "crash", "message": "NPE"},
            {"log_lines": ["    at Demo.run(Demo.java:4)"], "root_cause": "NPE"},
        )
        self.assertTrue(result.artifacts["review"]["confirmed"])

    def test_no_llm_caller_still_works(self):
        """显式禁用 LLM（llm_caller=None）→ 跳过复判，源码证据仍正常返回。"""
        from modules.log_monitor.agents.source_agent import _NO_LLM
        # 显式传 None（不是 _NO_LLM）→ 禁用 LLM
        agent = SourceCodeAdapter(index=self.idx, llm_caller=None)
        result = agent.run(
            {"type": "crash", "message": "NPE"},
            {"log_lines": ["    at Demo.run(Demo.java:4)"], "root_cause": "NPE"},
        )
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.artifacts.get("review"))
        # 仍有源码证据
        self.assertTrue(any(e["kind"] == "source_code" for e in result.evidence))


class TestRegisterWithPlanner(unittest.TestCase):
    """注册后 Planner 计划中 source_code 应从 skipped 变为 agents。"""

    def test_source_code_registered_and_planned(self):
        from modules.log_monitor.agents.planner import (
            build_plan, clear_registry, AGENT_SOURCE,
            get_registered_agent,
        )
        from modules.log_monitor.agents.adapters import register_builtin_agents
        try:
            clear_registry()
            register_builtin_agents()
            # source_code 应已注册
            self.assertIsNotNone(get_registered_agent(AGENT_SOURCE))
            # crash 计划应包含 source_code 在 agents 中
            plan = build_plan({"type": "crash", "message": "crash", "rule_name": "r"})
            self.assertIn(AGENT_SOURCE, plan.agent_names)
        finally:
            clear_registry()


class TestReadonlyRedline(unittest.TestCase):
    """源码 Agent 只读红线。"""

    def test_readonly_true(self):
        agent = SourceCodeAdapter(index=SourceCodeIndex(root=""))
        self.assertTrue(agent.readonly)


if __name__ == "__main__":
    unittest.main()
