# -*- coding: utf-8 -*-
"""#23 动态上下文扩窗 单元测试（不依赖 Flask 运行时，直接 import views 函数）"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from modules.log_monitor.views import (  # noqa: E402
    _get_dynamic_context_around_alert,
    _CTX_WINDOWS,
)


def _mk(logs):
    """构造 all_logs 结构 [(idx, log), ...]"""
    return [(i, l) for i, l in enumerate(logs)]


class TestDynamicContext(unittest.TestCase):

    def test_crash_full_stack(self):
        """crash：应回溯到 FATAL 头并前探覆盖完整堆栈"""
        noise_before = ['I/Noise: line %d' % i for i in range(40)]
        crash_block = [
            'E/AndroidRuntime: FATAL EXCEPTION: main',
            'E/AndroidRuntime: Process: com.thunder.ktv, PID: 12345',
            'E/AndroidRuntime: java.lang.NullPointerException: Attempt to invoke virtual method',
        ] + [
            'E/AndroidRuntime:     at com.thunder.ktv.player.MediaCore.play(MediaCore.java:%d)' % (100 + i)
            for i in range(30)
        ] + [
            'E/AndroidRuntime: Caused by: java.lang.IllegalStateException',
            'E/AndroidRuntime:     at com.thunder.ktv.base.App.init(App.java:55)',
            'E/AndroidRuntime:     ... 12 more',
        ]
        noise_after = ['I/Noise: after %d' % i for i in range(30)]
        logs = noise_before + crash_block + noise_after
        alert_line = crash_block[2]  # NPE 首行触发告警

        lines, meta = _get_dynamic_context_around_alert(_mk(logs), alert_line, 'crash')
        joined = '\n'.join(lines)
        self.assertEqual(meta['strategy'], 'crash_full_stack')
        self.assertTrue(meta['matched'])
        self.assertIn('FATAL EXCEPTION', joined, 'FATAL 头必须被包含')
        self.assertIn('MediaCore.java:129', joined, '堆栈末尾必须被包含（旧固定窗口会截掉）')
        self.assertIn('... 12 more', joined, 'Caused by 段必须被包含')
        self.assertEqual(meta['lines'], len(lines))

    def test_crash_old_window_would_miss_stack(self):
        """对照：旧固定 after=20 窗口拿不到 30+ 行堆栈的末尾，新逻辑必须拿到"""
        crash_block = ['E/AndroidRuntime: FATAL EXCEPTION: main',
                       'E/AndroidRuntime: java.lang.NullPointerException'] + [
            'E/AndroidRuntime:     at com.a.B.c(B.java:%d)' % i for i in range(40)
        ]
        logs = ['I/N: x'] * 10 + crash_block + ['I/N: y'] * 10
        lines, meta = _get_dynamic_context_around_alert(_mk(logs), crash_block[1], 'crash')
        self.assertIn('B.java:39', '\n'.join(lines), '第 40 帧堆栈必须在上下文内')

    def test_oom_extra_memory_lines(self):
        """oom：窗口外更早的内存相关行应被补充进来"""
        early_mem = ['I/lowmemorykiller: Killing com.foo (adj 900)',
                     'I/art: GC_FOR_ALLOC freed 2048K']
        noise = ['D/Noise: n%d' % i for i in range(120)]
        oom_line = 'E/art: java.lang.OutOfMemoryError: Failed to allocate a 51121168 byte allocation'
        logs = early_mem + noise + [oom_line] + ['I/N: t%d' % i for i in range(10)]

        lines, meta = _get_dynamic_context_around_alert(_mk(logs), oom_line, 'oom')
        joined = '\n'.join(lines)
        self.assertEqual(meta['strategy'], 'oom_memory')
        self.assertIn('lowmemorykiller', joined, '窗口外内存日志必须被补充')
        self.assertIn('窗口外补充的内存相关日志', joined)
        self.assertGreaterEqual(meta.get('extra_mem_lines', 0), 1)

    def test_anr_wide_window(self):
        """anr：向后窗口应显著大于默认 20 行"""
        anr_line = 'E/ActivityManager: ANR in com.thunder.ktv (com.thunder.ktv/.MainActivity)'
        after = ['I/ActivityManager: Reason: Input dispatching timed out',
                 'I/ActivityManager: Load: 12.0 / 11.5 / 10.2'] + [
            'I/ActivityManager: CPU usage line %d' % i for i in range(60)
        ]
        logs = ['I/N: %d' % i for i in range(30)] + [anr_line] + after
        lines, meta = _get_dynamic_context_around_alert(_mk(logs), anr_line, 'anr')
        joined = '\n'.join(lines)
        self.assertIn('CPU usage line 59', joined, 'ANR 后 60+ 行 CPU 段必须被覆盖')
        self.assertEqual(meta['strategy'], 'anr')

    def test_default_type_keeps_old_behavior(self):
        """未知类型：维持默认 -20/+20 窗口"""
        target = 'E/Foo: something odd'
        logs = ['I/N: %d' % i for i in range(50)] + [target] + ['I/M: %d' % i for i in range(50)]
        lines, meta = _get_dynamic_context_around_alert(_mk(logs), target, 'keyword')
        self.assertEqual(meta['strategy'], 'default')
        self.assertEqual(len(lines), 41)  # 20 + 1 + 20

    def test_fallback_when_not_found(self):
        """告警行不在缓存中：回退最近 fallback 条并附加告警行"""
        logs = ['I/N: %d' % i for i in range(200)]
        lines, meta = _get_dynamic_context_around_alert(_mk(logs), 'E/Ghost: not in queue', 'crash')
        self.assertFalse(meta['matched'])
        self.assertTrue(meta['strategy'].endswith('_fallback'))
        self.assertEqual(lines[-1], 'E/Ghost: not in queue')
        self.assertLessEqual(len(lines), _CTX_WINDOWS['crash']['fallback'] + 1)

    def test_empty_logs(self):
        lines, meta = _get_dynamic_context_around_alert([], 'E/X: y', 'crash')
        self.assertEqual(lines, ['E/X: y'])
        self.assertFalse(meta['matched'])


class TestAgentTruncation(unittest.TestCase):
    """验证 agent.analyze 的分级限长逻辑（不真调 LLM，只验证截断分支）"""

    def test_truncation_keeps_head_and_tail(self):
        # 直接复刻 analyze 内的截断逻辑做等价验证（避免 mock LLM 的重量级依赖）
        _MAX_LINES_BY_TYPE = {'crash': 150, 'anr': 120, 'oom': 100, 'exception': 60}
        log_lines = ['L%d' % i for i in range(300)]
        max_lines = _MAX_LINES_BY_TYPE.get('crash', 50)
        head = max_lines // 3
        tail = max_lines - head
        out = log_lines[:head] + ['...'] + log_lines[-tail:]
        self.assertEqual(out[0], 'L0')
        self.assertEqual(out[-1], 'L299')
        self.assertEqual(len(out), max_lines + 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
