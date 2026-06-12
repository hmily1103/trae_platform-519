import unittest
from unittest.mock import MagicMock

from modules.player_stress.core.monitor import PerformanceMonitor


class TestPerformanceMonitor(unittest.TestCase):
    def setUp(self):
        self.adb = MagicMock()
        self.monitor = PerformanceMonitor(
            self.adb,
            "com.test.pkg:media",
            monitor_config={"tv_display_id": 1, "allow_display0_fallback": False},
        )

    def test_tv_surface_fps_is_preferred(self):
        self.monitor._get_fps_from_surfaceflinger = MagicMock(return_value=30.0)
        self.monitor._get_fps_from_gfxinfo = MagicMock(return_value=60.0)

        fps = self.monitor._measure_video_fps(display_id=1, mpp_stats={})

        self.assertEqual(fps, 30.0)
        self.assertEqual(
            self.monitor._last_video_fps_source,
            "surfaceflinger_display_1",
        )
        self.monitor._get_fps_from_gfxinfo.assert_not_called()

    def test_zero_interference_mode_skips_active_fps_collection(self):
        self.adb.is_device_online.return_value = True
        self.adb.get_pid.return_value = 1234
        self.adb.get_memory_info.return_value = {"pss_mb": 100}
        self.adb.get_cpu_usage.return_value = 10.0
        self.adb.get_gfx_info.return_value = {
            "total_frames": 100,
            "janky_frames": 5,
        }
        self.adb.is_audio_active.return_value = False
        self.adb._run_command.return_value = "Display id=0\nDisplay id=1\n"
        self.monitor.rk_monitor.is_supported = False
        self.monitor._disable_fps = True
        self.monitor._measure_video_fps = MagicMock(return_value=30.0)

        snapshot = self.monitor.collect_snapshot()

        self.assertEqual(snapshot["video_fps"], 0.0)
        self.assertEqual(snapshot["video_fps_source"], "none")
        self.assertEqual(snapshot["tv_display_id"], 1)
        self.assertTrue(snapshot["tv_display_verified"])
        self.monitor._measure_video_fps.assert_not_called()

    def test_decode_slowdown_triggers_top_snapshot(self):
        self.adb.is_device_online.return_value = True
        self.adb.get_pid.return_value = 1234
        self.adb.get_memory_info.return_value = {"pss_mb": 100}
        self.adb.get_cpu_usage.return_value = 12.0
        self.adb.get_system_cpu_usage.return_value = 85.0
        self.adb.get_thermal_status.return_value = {
            "available": True,
            "max_temperature_c": 65.0,
            "min_frequency_ratio": 0.9,
            "thermal_throttling": False,
        }
        self.adb.get_gfx_info.return_value = {
            "total_frames": 100,
            "janky_frames": 0,
        }
        self.adb.is_audio_active.return_value = True
        self.adb._run_command.return_value = "Display id=0\nDisplay id=1\n"
        self.adb.get_top_heavy_processes.return_value = "com.noisy.worker(45%)"
        self.monitor.rk_monitor.is_supported = True
        self.monitor.rk_monitor.get_mpp_stats = MagicMock(return_value={
            "active_instances": 1,
            "session_count": 1,
            "total_work_count": 100,
            "work_count_delta": 10,
            "work_count_delta_time_sec": 1.0,
            "decoder_stuck": False,
        })
        self.monitor._measure_video_fps = MagicMock(return_value=0.0)
        self.monitor._expected_stream_fps = 30.0
        self.monitor._expected_stream_fps_ready = True

        snapshot = self.monitor.collect_snapshot()

        self.assertTrue(snapshot["decode_slowdown_detected"])
        self.assertEqual(snapshot["decode_fps_estimate"], 10.0)
        self.assertEqual(snapshot["expected_stream_fps"], 30.0)
        self.assertEqual(snapshot["system_cpu_percent"], 85.0)
        self.assertEqual(snapshot["top_consumers"], "com.noisy.worker(45%)")
        self.adb.get_top_heavy_processes.assert_called_once()


if __name__ == "__main__":
    unittest.main()
