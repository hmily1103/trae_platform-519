import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add project root to path
# current: .../tests/test_monitor_mock.py
# target: .../trae_platform/
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

from modules.player_stress.core.monitor import PerformanceMonitor

class TestMonitorRefactor(unittest.TestCase):
    def setUp(self):
        self.mock_adb = MagicMock()
        self.package_name = "com.test.pkg"
        
        # Mock CollectorManager before importing monitor or initializing
        # Since monitor imports it at top level, we need to patch it where it is imported
        self.patcher = patch('modules.player_stress.core.monitor.CollectorManager')
        self.MockCollectorManager = self.patcher.start()
        
        self.monitor = PerformanceMonitor(self.mock_adb, self.package_name)
        
    def tearDown(self):
        self.patcher.stop()
        
    def test_collect_metrics_calls_collector_manager(self):
        # Setup mock return value for collect_all
        mock_collector_instance = self.MockCollectorManager.return_value
        mock_collector_instance.collect_all.return_value = {
            'video_fps': 30.0,
            'mpp_active': 1,
            'mpp_sessions': 1,
            'mpp_work_count': 100,
            'decoder_stuck': False,
            'tv_stutter_detected': False
        }
        
        # Setup adb mocks to avoid errors in other parts of collect_metrics
        self.mock_adb.get_pid.return_value = 1234
        self.mock_adb.get_memory_info.return_value = {"pss_mb": 100}
        self.mock_adb.get_cpu_usage.return_value = 10.0
        self.mock_adb.get_gfx_info.return_value = {'total_frames': 100, 'janky_frames': 5}
        self.mock_adb.is_device_online.return_value = True
        
        # Mock evaluator
        self.monitor.evaluator = MagicMock()
        
        # Run collect_metrics
        metrics = self.monitor.collect_snapshot()
        
        # Verify collector_manager.collect_all was called
        mock_collector_instance.collect_all.assert_called_once()
        
        # Verify metrics contain data from collector
        self.assertEqual(metrics['video_fps'], 30.0)
        self.assertEqual(metrics['mpp_active'], 1)
        self.assertFalse(metrics['decoder_stuck'])
        
    def test_zero_interference_mode(self):
        # Setup mock return value
        mock_collector_instance = self.MockCollectorManager.return_value
        mock_collector_instance.collect_all.return_value = {
            'video_fps': 30.0,
            'mpp_active': 1
        }
        
        self.mock_adb.get_pid.return_value = 1234
        self.mock_adb.get_memory_info.return_value = {"pss_mb": 100}
        self.mock_adb.get_cpu_usage.return_value = 10.0
        self.mock_adb.get_gfx_info.return_value = {'total_frames': 100, 'janky_frames': 5}
        self.mock_adb.is_device_online.return_value = True
        
        # Enable zero interference mode
        self.monitor._disable_fps = True
        
        # Run collect_metrics
        metrics = self.monitor.collect_snapshot()
        
        # Verify collector_manager.collect_all was called
        mock_collector_instance.collect_all.assert_called_once()
        
        # Verify FPS is 0 despite collector returning 30.0 (because it's overridden in monitor)
        self.assertEqual(metrics['video_fps'], 0.0)
        # Verify MPP data is still there
        self.assertEqual(metrics['mpp_active'], 1)

if __name__ == '__main__':
    unittest.main()
