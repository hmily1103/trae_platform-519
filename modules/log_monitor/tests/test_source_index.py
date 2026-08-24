# -*- coding: utf-8 -*-
"""B1 源码索引器单元测试。

全部测试使用 tempfile 创建临时源码目录，不依赖真实工程。
验证：索引构建 / 类名查找 / 片段读取 / 路径穿越防护 / 跳过目录 / 禁用降级。
"""
import os
import shutil
import tempfile
import unittest

from modules.log_monitor.agents.source_index import (
    SourceCodeIndex,
    get_index,
    reset_index,
    _SKIP_DIRS,
)


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class TestSourceIndexDisabled(unittest.TestCase):
    """未配置源码目录 → 全部降级跳过。"""

    def test_not_enabled_when_no_root(self):
        idx = SourceCodeIndex(root="")
        self.assertFalse(idx.is_enabled())

    def test_not_enabled_when_root_not_exist(self):
        idx = SourceCodeIndex(root="/this/path/does/not/exist/12345")
        self.assertFalse(idx.is_enabled())

    def test_find_files_empty_when_disabled(self):
        idx = SourceCodeIndex(root="")
        self.assertEqual(idx.find_files("PlayerManager"), [])

    def test_read_snippet_none_when_disabled(self):
        idx = SourceCodeIndex(root="")
        self.assertIsNone(idx.read_snippet("any.java", 10))

    def test_status_disabled(self):
        idx = SourceCodeIndex(root="")
        st = idx.status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["file_count"], 0)


class TestSourceIndexBuild(unittest.TestCase):
    """索引构建：类名提取 / 文件名索引 / 跳过目录。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="source_index_test_")
        # 模拟一个 Android 机顶盒 App 工程
        _write(os.path.join(self.tmpdir, "src", "com", "thunder", "ktv",
                            "PlayerManager.java"), '''\
package com.thunder.ktv;

import android.content.Context;

public class PlayerManager {
    private Context mContext;

    public void play(String url) {
        if (url == null) {
            throw new IllegalArgumentException("url is null");
        }
        // play logic
        startPlayback(url);
    }

    private void startPlayback(String url) {
        // ...
    }
}
''')
        _write(os.path.join(self.tmpdir, "src", "com", "thunder", "ktv",
                            "player", "VideoRenderer.java"), '''\
package com.thunder.ktv.player;

public class VideoRenderer {
    public void renderFrame(byte[] data) {
        // render
    }
}
''')
        # Kotlin 文件
        _write(os.path.join(self.tmpdir, "src", "com", "thunder", "ktv",
                            "utils", "FormatHelper.kt"), '''\
package com.thunder.ktv.utils

object FormatHelper {
    fun formatTime(ms: Long): String {
        return "$ms ms"
    }
}
''')
        # interface
        _write(os.path.join(self.tmpdir, "src", "com", "thunder", "ktv",
                            "IPlayerCallback.java"), '''\
package com.thunder.ktv;

public interface IPlayerCallback {
    void onCompleted();
}
''')
        # 应被跳过的目录
        _write(os.path.join(self.tmpdir, "build", "intermediates",
                            "PlayerManager.java"), "// should be skipped")
        _write(os.path.join(self.tmpdir, ".git", "PlayerManager.java"), "// skip")
        # 非 Java/Kotlin 文件
        _write(os.path.join(self.tmpdir, "src", "README.md"), "# README")

        self.idx = SourceCodeIndex(root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_is_enabled(self):
        self.assertTrue(self.idx.is_enabled())

    def test_find_by_class_name(self):
        hits = self.idx.find_files("PlayerManager")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].endswith("PlayerManager.java"))
        self.assertIn("src", hits[0])

    def test_find_by_class_name_kotlin_object(self):
        hits = self.idx.find_files("FormatHelper")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].endswith(".kt"))

    def test_find_by_class_name_interface(self):
        hits = self.idx.find_files("IPlayerCallback")
        self.assertEqual(len(hits), 1)

    def test_find_by_filename(self):
        hits = self.idx.find_files("VideoRenderer.java")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].endswith("VideoRenderer.java"))

    def test_find_by_filename_with_path(self):
        hits = self.idx.find_files("com/thunder/ktv/PlayerManager.java")
        self.assertEqual(len(hits), 1)

    def test_find_nonexistent(self):
        self.assertEqual(self.idx.find_files("NoSuchClass"), [])

    def test_skip_dirs_not_indexed(self):
        """.git / build 目录下的文件不应进索引。"""
        hits = self.idx.find_files("PlayerManager")
        # 只应有 1 个（src 下的），build/.git 下的不算
        self.assertEqual(len(hits), 1)
        self.assertNotIn("build", hits[0])
        self.assertNotIn(".git", hits[0])

    def test_non_source_files_not_indexed(self):
        self.assertEqual(self.idx.find_files("README"), [])

    def test_status(self):
        st = self.idx.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["file_count"], 4)  # 4 个 java/kt 文件
        self.assertEqual(st["class_count"], 4)  # PlayerManager, VideoRenderer, FormatHelper, IPlayerCallback
        self.assertEqual(st["scan_errors"], 0)
        self.assertGreater(st["build_ms"], -1)  # 非负
        self.assertEqual(st["root"], self.tmpdir)

    def test_status_idempotent(self):
        """多次调用 status 不重复构建。"""
        st1 = self.idx.status()
        st2 = self.idx.status()
        self.assertEqual(st1["file_count"], st2["file_count"])


class TestReadSnippet(unittest.TestCase):
    """源码片段读取：正确行号 / target 标记 / 截断指示 / 路径安全。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="source_snippet_test_")
        self.java_file = os.path.join(self.tmpdir, "src", "Demo.java")
        lines = []
        for i in range(1, 51):
            lines.append("    // line %d" % i)
        _write(self.java_file, "\n".join(lines) + "\n")
        self.idx = SourceCodeIndex(root=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_correct_lines(self):
        snip = self.idx.read_snippet(self.java_file, 25, context=3)
        self.assertIsNotNone(snip)
        self.assertEqual(snip["start_line"], 22)
        self.assertEqual(snip["end_line"], 28)
        self.assertEqual(len(snip["lines"]), 7)
        # target 行标记
        target_lines = [l for l in snip["lines"] if l["is_target"]]
        self.assertEqual(len(target_lines), 1)
        self.assertEqual(target_lines[0]["lineno"], 25)

    def test_read_clamps_oversized_line(self):
        """target_line 超出文件总行数 → clamp 到末行。"""
        snip = self.idx.read_snippet(self.java_file, 999, context=5)
        self.assertIsNotNone(snip)
        self.assertEqual(snip["target_line"], 50)  # 50 行
        self.assertEqual(snip["end_line"], 50)

    def test_read_clamps_undersized_line(self):
        """target_line < 1 → clamp 到第 1 行。"""
        snip = self.idx.read_snippet(self.java_file, -5, context=3)
        self.assertIsNotNone(snip)
        self.assertEqual(snip["target_line"], 1)
        self.assertEqual(snip["start_line"], 1)

    def test_truncated_indicator(self):
        """中间行读取 → truncated=True（前有行、后有行）。"""
        snip = self.idx.read_snippet(self.java_file, 25, context=5)
        self.assertTrue(snip["truncated"])

    def test_not_truncated_at_start(self):
        """从第 1 行开始读 → start=1，前无截断。"""
        snip = self.idx.read_snippet(self.java_file, 3, context=5)
        self.assertEqual(snip["start_line"], 1)

    def test_read_nonexistent_file(self):
        self.assertIsNone(self.idx.read_snippet(
            os.path.join(self.tmpdir, "nope.java"), 10))

    def test_path_traversal_rejected(self):
        """路径穿越攻击：试图读取 root 外的文件 → None。"""
        # 构造一个 root 外的文件
        outside = os.path.join(tempfile.gettempdir(), "outside_test_%d.java" % os.getpid())
        try:
            _write(outside, "// secret")
            # 用 ../ 尝试穿越
            malicious = os.path.join(self.tmpdir, "..", os.path.basename(outside))
            self.assertIsNone(self.idx.read_snippet(malicious, 1))
        finally:
            if os.path.exists(outside):
                os.remove(outside)

    def test_relative_path_resolved(self):
        """传相对路径 → 以 root 为基解析。"""
        rel = os.path.join("src", "Demo.java")
        snip = self.idx.read_snippet(rel, 10, context=2)
        self.assertIsNotNone(snip)


class TestFileTooLarge(unittest.TestCase):
    """大文件跳过：超过 max_file_kb 的文件不进索引、不可读片段。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="source_large_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_large_file_skipped_in_index(self):
        big_file = os.path.join(self.tmpdir, "Big.java")
        with open(big_file, "w", encoding="utf-8") as fh:
            fh.write("public class Big {\n")
            fh.write("    // " + "x" * (600 * 1024) + "\n")  # ~600KB > 512KB
            fh.write("}\n")
        idx = SourceCodeIndex(root=self.tmpdir, max_file_kb=1)  # 上限 1KB
        self.assertEqual(idx.find_files("Big"), [])

    def test_large_file_snippet_none(self):
        big_file = os.path.join(self.tmpdir, "Big.java")
        with open(big_file, "w", encoding="utf-8") as fh:
            fh.write("public class Big {\n")
            fh.write("    // " + "x" * (600 * 1024) + "\n")
            fh.write("}\n")
        idx = SourceCodeIndex(root=self.tmpdir, max_file_kb=1)
        self.assertIsNone(idx.read_snippet(big_file, 1))


class TestSingleton(unittest.TestCase):
    """单例管理：get_index / reset_index。"""

    def test_get_index_returns_same(self):
        a = get_index()
        b = get_index()
        self.assertIs(a, b)

    def test_reset_index_changes_instance(self):
        original = get_index()
        new = reset_index(root="")
        self.assertIsNot(original, new)
        self.assertFalse(new.is_enabled())
        # 恢复全局状态
        reset_index(root="")


class TestPlannerIntegration(unittest.TestCase):
    """Planner 计划中 source_code Agent 的 skipped 行为验证。"""

    def test_source_code_skipped_when_not_registered(self):
        """未注册 source_code Agent → build_plan 将其放入 plan.skipped。"""
        from modules.log_monitor.agents.planner import build_plan, AGENT_SOURCE
        alert = {
            "type": "crash",
            "message": "NullPointerException in PlayerManager",
            "rule_name": "crash_rule",
            "timestamp": "2026-07-29T10:00:00",
        }
        plan = build_plan(alert)
        skipped_names = [a.name for a in plan.skipped]
        self.assertIn(AGENT_SOURCE, skipped_names)
        # source_code 未注册 → get_registered_agent 返回 None
        from modules.log_monitor.agents.planner import get_registered_agent
        self.assertIsNone(get_registered_agent(AGENT_SOURCE))

    def test_source_code_status_in_plan_reason(self):
        """Planner 计划中 source_code 的 skip reason 应提及源码关联。"""
        from modules.log_monitor.agents.planner import build_plan, AGENT_SOURCE
        alert = {"type": "crash", "message": "crash", "rule_name": "r", "timestamp": ""}
        plan = build_plan(alert)
        source_entry = next(a for a in plan.skipped if a.name == AGENT_SOURCE)
        self.assertIn("源码", source_entry.reason)


if __name__ == "__main__":
    unittest.main()
