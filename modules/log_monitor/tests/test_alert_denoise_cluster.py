# -*- coding: utf-8 -*-
"""#24 日志去噪 + 同事件聚类 单元测试"""
import os
import sys
import unittest
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from modules.log_monitor.alert_engine import (  # noqa: E402
    AlertEngine,
    REPEAT_SUPPRESS_THRESHOLD,
)

DEV = '192.168.1.140:8787'


class TestDenoise(unittest.TestCase):
    """① 去噪"""

    def setUp(self):
        self.engine = AlertEngine()

    def test_noise_line_skipped(self):
        """系统噪音行（BufferQueue/chatty 等）不触发任何告警"""
        noise_lines = [
            'W/BufferQueueProducer: [SurfaceView] dequeueBuffer: BufferQueue has been abandoned',
            'I/chatty: uid=1000(system) identical 37 lines',
            'D/OpenGLRenderer: endAllActiveAnimators',
            'E/wpa_supplicant: wlan0: CTRL-EVENT-SCAN-FAILED ret=-16',
        ]
        for line in noise_lines:
            self.assertEqual(self.engine.check_log(line, DEV), [], f'噪音行不应告警: {line}')

    def test_strong_signal_never_filtered(self):
        """含强异常信号的行即使 tag 在噪音名单也绝不过滤（宁可多报不可漏报）"""
        line = 'E/AudioFlinger: FATAL EXCEPTION in audio thread'
        alerts = self.engine.check_log(line, DEV)
        self.assertEqual(len(alerts), 1, '强信号行必须告警')

    def test_normal_error_still_alerts(self):
        """非噪音的正常异常行仍正常告警"""
        alerts = self.engine.check_log(
            'E/AndroidRuntime: java.lang.NullPointerException: xxx', DEV)
        self.assertEqual(len(alerts), 1)


class TestRepeatSuppression(unittest.TestCase):
    """② 重复刷屏抑制"""

    def setUp(self):
        self.engine = AlertEngine()

    def test_repeated_same_line_suppressed(self):
        """同签名（仅时间戳/数字不同）告警超过阈值后只计数不新建"""
        created = 0
        for i in range(REPEAT_SUPPRESS_THRESHOLD + 5):
            # 数字部分不同，但归一化签名相同；用非崩溃家族关键词避免被聚类合并
            line = f'07-29 10:00:{i:02d}.123  111  222 E MediaPlayer: setDataSource failed: status=0x{80000000 + i:x}'
            # MediaPlayer 行默认规则不命中，改用 keyword 规则命中的 NPE 行会被聚类；
            # 这里直接用 level 无法命中——改为构造命中 rule_npe 且间隔>聚类窗口
            alerts = self.engine.check_log(
                f'E/TestApp: NullPointerException at step {i} code 0x{1000 + i:x}', DEV)
            if alerts:
                created += 1
                # 拉开时间避免聚类合并干扰本测试
                self.engine.alert_history[-1].timestamp -= timedelta(seconds=10 + i)
                self.engine.alert_history[-1].last_seen = None
        self.assertLessEqual(created, REPEAT_SUPPRESS_THRESHOLD,
                             f'超过阈值({REPEAT_SUPPRESS_THRESHOLD})后不应再新建告警，实际新建 {created}')
        self.assertGreater(self.engine.suppressed_count, 0, '应有被抑制的计数')
        # 被抑制的次数应累加到既有告警的 occurrence_count
        total_occ = sum(a.occurrence_count for a in self.engine.alert_history)
        self.assertGreater(total_occ, created, 'occurrence_count 应累计被抑制的命中')


class TestClusterMergeInPlace(unittest.TestCase):
    """③ 同事件聚类：原地合并，保 id / self_heal"""

    def setUp(self):
        self.engine = AlertEngine()

    def test_crash_event_merges_to_one(self):
        """FATAL 行 + NPE 行 = 同一次崩溃 → 只产生 1 条告警，id 不变"""
        a1 = self.engine.check_log('E/AndroidRuntime: FATAL EXCEPTION: main', DEV)
        self.assertEqual(len(a1), 1)
        first_id = a1[0].id
        # 模拟已回写自愈结果
        self.engine.alert_history[-1].self_heal = {'status': 'NOTIFY_ONLY'}

        a2 = self.engine.check_log(
            'E/AndroidRuntime: java.lang.NullPointerException: Attempt to invoke', DEV)
        self.assertEqual(a2, [], '窗口内同事件不应返回新告警（避免下游重复分析）')
        self.assertEqual(len(self.engine.alert_history), 1, '历史里只应有 1 条')
        rec = self.engine.alert_history[-1]
        self.assertEqual(rec.id, first_id, '合并必须保持原 id')
        self.assertEqual(rec.self_heal, {'status': 'NOTIFY_ONLY'}, '合并不得丢失 self_heal')
        self.assertEqual(rec.occurrence_count, 2)
        self.assertIn('NullPointerException', '\n'.join(rec.related_lines))
        self.assertIn('/', rec.rule_name, '规则名应并集展示')

    def test_stack_lines_absorbed(self):
        """FATAL 后的堆栈行（at .../Caused by）被吸收进 related_lines，不新建告警"""
        self.engine.check_log('E/AndroidRuntime: FATAL EXCEPTION: main', DEV)
        stack = [
            'E/AndroidRuntime:     at com.thunder.ktv.MediaCore.play(MediaCore.java:120)',
            'E/AndroidRuntime:     at com.thunder.ktv.App.start(App.java:33)',
            'E/AndroidRuntime: Caused by: java.lang.IllegalStateException',
            'E/AndroidRuntime:     ... 12 more',
        ]
        for line in stack:
            self.assertEqual(self.engine.check_log(line, DEV), [], f'堆栈行不应新建告警: {line}')
        rec = self.engine.alert_history[-1]
        self.assertEqual(len(self.engine.alert_history), 1)
        self.assertGreaterEqual(len(rec.related_lines), 3, '堆栈行应进 related_lines')
        self.assertIn('MediaCore.java:120', '\n'.join(rec.related_lines))

    def test_different_device_not_merged(self):
        """不同设备的崩溃不合并"""
        self.engine.check_log('E/AndroidRuntime: FATAL EXCEPTION: main', DEV)
        a2 = self.engine.check_log('E/AndroidRuntime: FATAL EXCEPTION: main', 'other_device')
        self.assertEqual(len(a2), 1, '不同设备应各自成条')
        self.assertEqual(len(self.engine.alert_history), 2)

    def test_outside_window_not_merged(self):
        """超出时间窗的崩溃不合并"""
        self.engine.check_log('E/AndroidRuntime: FATAL EXCEPTION: main', DEV)
        # 手动把上一条时间拨回 10 秒前
        self.engine.alert_history[-1].timestamp = datetime.now() - timedelta(seconds=10)
        self.engine.alert_history[-1].last_seen = None
        a2 = self.engine.check_log(
            'E/TestApp: java.lang.OutOfMemoryError: Failed to allocate', DEV)
        self.assertEqual(len(a2), 1, '超窗应新建告警')
        self.assertEqual(len(self.engine.alert_history), 2)

    def test_to_dict_contains_new_fields(self):
        self.engine.check_log('E/AndroidRuntime: FATAL EXCEPTION: main', DEV)
        d = self.engine.alert_history[-1].to_dict()
        for key in ('occurrence_count', 'related_lines', 'last_seen'):
            self.assertIn(key, d)


if __name__ == '__main__':
    unittest.main(verbosity=2)
