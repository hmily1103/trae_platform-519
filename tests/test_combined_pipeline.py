
import unittest
import threading
import time
from unittest.mock import MagicMock, patch
from modules.combined_test.core.pipeline import run_pipeline, _run_monkey, _run_perf_monitor

class TestCombinedPipeline(unittest.TestCase):

    def setUp(self):
        self.base_url = "http://mock-api"
        self.config = {
            "device_id": "127.0.0.1:5555",
            "package_name": "com.example.app",
            "monkey_events": 100
        }
        self.stop_event = threading.Event()
        self.logs = []
    
    def on_log(self, msg):
        self.logs.append(msg)

    @patch('modules.combined_test.core.pipeline._http_post')
    @patch('modules.combined_test.core.pipeline._http_get')
    def test_monkey_stress_monitor_success(self, mock_get, mock_post):
        """测试 Monkey + 性能 + 日志 组合流程成功场景"""
        # Mock API 响应
        # 1. Log Monitor Start
        # 2. Perf Monitor Start
        # 3. Monkey Start
        # 4. Monkey Status (Running -> Finished)
        # 5. Stop commands
        
        mock_post.side_effect = [
            (True, {'data': {'task_id': 'log_123'}}),  # Log start
            (True, {'data': {'task_id': 'perf_456'}}), # Perf start
            (True, {'data': {'task_id': 'monkey_789'}}), # Monkey start
            (True, {}), # Monkey stop (at end, though finished)
            (True, {}), # Log stop
            (True, {}), # Perf stop
        ]
        
        # Monkey Status 轮询: 第一次 Running, 第二次 Finished
        mock_get.side_effect = [
            (True, {'data': {'devices_status': [{'device_id': '127.0.0.1:5555', 'status': 'running', 'events_executed': 50}]}}),
            (True, {'data': {'devices_status': [{'device_id': '127.0.0.1:5555', 'status': 'finished', 'events_executed': 100}]}}),
        ]

        result = run_pipeline(
            'monkey_stress_monitor',
            self.base_url,
            self.config,
            self.on_log,
            self.stop_event
        )

        self.assertTrue(result['success'])
        self.assertIn('log_monitor_start', result['steps_done'])
        self.assertIn('perf_monitor_start', result['steps_done'])
        self.assertIn('monkey', result['steps_done'])
        self.assertIn('monitor_stop', result['steps_done'])
        
        # 验证是否调用了停止监控接口
        stop_calls = [call for call in mock_post.call_args_list if '/api/stop' in call[0][1]]
        self.assertTrue(any('/log_monitor/api/stop' in c[0][1] for c in stop_calls))
        self.assertTrue(any('/performance_monitor/api/stop' in c[0][1] for c in stop_calls))

    @patch('modules.combined_test.core.pipeline._http_post')
    @patch('modules.combined_test.core.pipeline._http_get')
    def test_reboot_then_monkey_success(self, mock_get, mock_post):
        """测试 Reboot + Monkey 组合流程成功场景"""
        
        # 使用 side_effect 函数来处理不同 URL 的请求，避免顺序依赖
        def post_side_effect(base_url, endpoint, json=None):
            if 'reboot/api/start' in endpoint:
                return True, {'data': {'task_id': 'reboot_1'}}
            if 'monkey/api/start' in endpoint:
                return True, {'data': {'task_id': 'monkey_1'}}
            if 'stop' in endpoint:
                return True, {}
            return True, {}
        
        mock_post.side_effect = post_side_effect
        
        # 状态查询: Reboot Running -> Reboot Done -> Monkey Running -> Monkey Done
        # 注意: 这里依然依赖顺序，因为都是 get status
        mock_get.side_effect = [
            (True, {'data': {'running': True}}), # Reboot running
            (True, {'data': {'running': False}}), # Reboot done
            (True, {'data': {'devices_status': [{'device_id': '127.0.0.1:5555', 'status': 'finished'}]}}), # Monkey done
        ]

        # 临时 Patch time.sleep 以跳过等待
        with patch('time.sleep', return_value=None):
            result = run_pipeline(
                'reboot_then_monkey',
                self.base_url,
                self.config,
                self.on_log,
                self.stop_event
            )

        # 如果失败，打印一下步骤
        if not result['success']:
            print(f"DEBUG: Steps done: {result.get('steps_done')}")
            print(f"DEBUG: Steps failed: {result.get('steps_failed')}")
            print(f"DEBUG: Message: {result.get('message')}")

        self.assertTrue(result['success'])
        self.assertIn('reboot', result['steps_done'])
        self.assertIn('monkey', result['steps_done'])

    @patch('modules.combined_test.core.pipeline._http_post')
    def test_invalid_device_id(self, mock_post):
        """测试无效设备ID的处理"""
        # Case 1: 端口不是数字
        config = {"device_id": "127.0.0.1:abc"} 
        
        ok, stopped = _run_monkey("http://mock", config, self.on_log, self.stop_event)
        
        self.assertFalse(ok)
        # 验证是否捕获了异常并打印了日志
        self.assertTrue(any("设备ID格式错误" in log for log in self.logs))
        # 验证没有调用 start 接口
        mock_post.assert_not_called()

if __name__ == '__main__':
    unittest.main()
