import unittest
from unittest.mock import MagicMock, patch, ANY
import sys
import os
import time
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

from modules.performance_monitor.service import PerformanceMonitorService
from modules.log_monitor.core.models.analysis_models import PerformanceSnapshot

class TestPerformanceServiceRefactor(unittest.TestCase):
    def setUp(self):
        # Mock dependencies
        self.mock_storage_patcher = patch('modules.performance_monitor.service.PerformanceStorage')
        self.MockStorage = self.mock_storage_patcher.start()
        
        self.mock_baseline_patcher = patch('modules.performance_monitor.service.PerformanceBaseline')
        self.MockBaseline = self.mock_baseline_patcher.start()
        
        self.mock_alert_engine_patcher = patch('modules.performance_monitor.service.PerformanceAlertEngine')
        self.MockAlertEngine = self.mock_alert_engine_patcher.start()
        
        self.mock_adb_controller_patcher = patch('modules.performance_monitor.service.AdbController')
        self.MockAdbController = self.mock_adb_controller_patcher.start()
        
        self.mock_collector_manager_patcher = patch('modules.performance_monitor.service.CollectorManager')
        self.MockCollectorManager = self.mock_collector_manager_patcher.start()
        
        # Initialize service
        self.service = PerformanceMonitorService()
        
    def tearDown(self):
        self.mock_storage_patcher.stop()
        self.mock_baseline_patcher.stop()
        self.mock_alert_engine_patcher.stop()
        self.mock_adb_controller_patcher.stop()
        self.mock_collector_manager_patcher.stop()
        
    def test_start_monitoring_uses_collector_manager(self):
        task_id = "test_task"
        device_id = "test_device"
        package_name = "com.test.pkg"
        
        # Setup mocks
        mock_controller = self.MockAdbController.return_value
        mock_collector_manager = self.MockCollectorManager.return_value
        
        # Setup CollectorManager return value
        mock_collector_manager.collect_all.return_value = {
            'video_fps': 45.0,
            'mpp_active': 1,
            'decoder_stuck': False,
            'tv_stutter_detected': True
        }
        
        # Call start_monitoring
        result = self.service.start_monitoring(task_id, device_id, package_name)
        
        self.assertTrue(result)
        
        # Verify CollectorManager was initialized with controller and package
        self.MockCollectorManager.assert_called_with(mock_controller, package_name)
        
        # Verify controller.start_monitoring was called
        mock_controller.start_monitoring.assert_called()
        
        # Extract the performance_callback passed to controller
        args, kwargs = mock_controller.start_monitoring.call_args
        # Assuming performance_callback is passed as keyword argument or positional
        # Signature: start_monitoring(device_id, log_callback, filter_func, min_log_level, performance_callback, target_package)
        # Based on snippet, it's passed as keyword argument 'performance_callback' or positional
        if 'performance_callback' in kwargs:
            callback = kwargs['performance_callback']
        else:
            # Try positional - 5th argument (index 4) if using full signature, but let's check carefully
            # The snippet showed: performance_callback=performance_callback
            callback = kwargs.get('performance_callback')
            
        self.assertIsNotNone(callback, "performance_callback not found in start_monitoring calls")
        
        # Create a dummy snapshot
        snapshot = PerformanceSnapshot(
            timestamp=datetime.now(),
            total_pss=100,
            gc_count=0,
            cpu_usage=10.0
        )
        
        # Invoke the callback
        callback(snapshot)
        
        # Verify collect_all was called
        mock_collector_manager.collect_all.assert_called_once()
        
        # Verify snapshot was updated with new metrics
        self.assertEqual(snapshot.video_fps, 45.0)
        self.assertEqual(snapshot.mpp_active, 1)
        self.assertTrue(snapshot.tv_stutter_detected)
        self.assertFalse(snapshot.decoder_stuck)
        
        # Verify storage.add_snapshot was called
        self.service.storage.add_snapshot.assert_called()

if __name__ == '__main__':
    unittest.main()
