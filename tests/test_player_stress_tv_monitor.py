import unittest
import threading
from unittest.mock import MagicMock

from modules.player_stress.core.adb_manager import AdbManager
from modules.player_stress.core.evaluator import PlayStateEvaluator
from modules.player_stress.core.monitor import PerformanceMonitor
from modules.player_stress.core.root_cause_analyzer import RootCauseAnalyzer
from modules.player_stress.core.runner import TestRunner


class TvDisplayMonitorTests(unittest.TestCase):
    def test_cpu_evidence_parses_top_process_count_and_load(self):
        adb = object.__new__(AdbManager)
        adb._run_command = MagicMock(side_effect=[
            "cpu  100 0 50 850 0 0 0 0 0 0",
            (
                "Tasks: 120 total\n"
                "PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS\n"
                "101 u0_a1 20 0 0 0 0 R 45.0 1.0 0:01 com.noisy.worker\n"
                "102 u0_a2 20 0 0 0 0 S 8.0 1.0 0:01 com.test.player\n"
            ),
            "2.50 1.50 0.75 1/120 1234",
            "121",
        ])

        evidence = adb.get_cpu_evidence(limit=5)

        self.assertEqual(evidence["process_count"], 121)
        self.assertEqual(evidence["load_average"], [2.5, 1.5, 0.75])
        self.assertEqual(
            evidence["top_processes"][0]["name"],
            "com.noisy.worker",
        )
        self.assertEqual(
            evidence["top_processes"][0]["cpu_percent"],
            45.0,
        )

    def test_adb_command_stops_before_retry_when_cancelled(self):
        adb = object.__new__(AdbManager)
        adb.device_id = "test-device"
        adb.dm = MagicMock()
        adb._cpu_stat_lock = threading.Lock()
        adb._previous_cpu_stat = None
        adb._cancel_event = threading.Event()
        adb._cancel_event.set()

        output = adb._run_command(["shell", "dumpsys", "cpuinfo"])

        self.assertEqual(output, "Error: command cancelled")
        adb.dm.run_adb_command.assert_not_called()

    def test_top_parser_handles_combined_state_cpu_header_and_duplicates(self):
        output = (
            "Tasks: 308 total\n"
            "PID USER PR NI VIRT RES SHR S[%CPU] %MEM TIME+ ARGS\n"
            "101 root 20 0 0 0 0 S 5.2 0.1 0:01 sh /system/bin/init.thunder.sh\n"
            "102 root 20 0 0 0 0 S 2.6 0.1 0:01 sh /system/bin/init.thunder.sh\n"
            "103 system 20 0 0 0 0 R 57.8 1.0 0:02 composer\n"
        )

        processes = AdbManager._parse_top_processes(output)

        init_process = next(
            process
            for process in processes
            if process["name"] == "/system/bin/init.thunder.sh"
        )
        self.assertEqual(init_process["instance_count"], 2)
        self.assertEqual(init_process["cpu_percent"], 7.8)
        self.assertEqual(processes[0]["name"], "composer")

    def test_top_parser_aggregates_any_repeated_shell_script(self):
        output = (
            "PID USER PR NI VIRT RES SHR S[%CPU] %MEM TIME+ ARGS\n"
            "201 root 20 0 0 0 0 S 4.0 0.1 0:01 sh /vendor/bin/worker.sh alpha\n"
            "202 root 20 0 0 0 0 S 6.0 0.1 0:01 /system/bin/sh /vendor/bin/worker.sh alpha\n"
        )

        processes = AdbManager._parse_top_processes(output)

        self.assertEqual(processes[0]["name"], "/vendor/bin/worker.sh alpha")
        self.assertEqual(processes[0]["instance_count"], 2)
        self.assertEqual(processes[0]["cpu_percent"], 10.0)

    def test_system_cpu_usage_uses_proc_stat_delta(self):
        adb = object.__new__(AdbManager)
        adb._run_command = MagicMock(side_effect=[
            "cpu  100 0 50 850 0 0 0 0 0 0",
            "cpu  160 0 70 870 0 0 0 0 0 0",
        ])

        self.assertEqual(adb.get_system_cpu_usage(), 0.0)
        self.assertEqual(adb.get_system_cpu_usage(), 80.0)

    def test_device_identity_reads_firmware_and_ip(self):
        adb = object.__new__(AdbManager)
        adb.device_id = "192.168.16.105:8787"
        adb._run_command = MagicMock(
            return_value="eng.thunder.20260609.203352"
        )

        self.assertEqual(adb.get_device_ip(), "192.168.16.105")
        self.assertEqual(
            adb.get_firmware_incremental(),
            "eng.thunder.20260609.203352",
        )

    def test_firmware_fallback_parses_full_getprop_output(self):
        adb = object.__new__(AdbManager)
        adb.device_id = "device-1"
        adb._run_command = MagicMock(side_effect=[
            "Error: unsupported",
            (
                "[ro.build.type]: [eng]\n"
                "[ro.build.version.incremental]: "
                "[eng.thunder.20260609.203352]\n"
            ),
        ])

        self.assertEqual(
            adb.get_firmware_incremental(),
            "eng.thunder.20260609.203352",
        )

    def test_thermal_status_detects_high_temperature(self):
        adb = object.__new__(AdbManager)
        adb._run_command = MagicMock(side_effect=[
            "/sys/class/thermal/thermal_zone0/temp=82500",
            (
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq=600000\n"
                "/sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq=800000"
            ),
            (
                "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq=2000000\n"
                "/sys/devices/system/cpu/cpu1/cpufreq/cpuinfo_max_freq=2000000"
            ),
        ])

        status = adb.get_thermal_status()

        self.assertTrue(status["available"])
        self.assertTrue(status["thermal_throttling"])
        self.assertEqual(status["max_temperature_c"], 82.5)
        self.assertEqual(status["min_frequency_ratio"], 0.3)

    def test_configured_display_one_is_verified(self):
        adb = MagicMock()
        adb._run_command.return_value = "Display id=0\nDisplay id=1\n"
        monitor = PerformanceMonitor(
            adb,
            "com.test.player:media",
            monitor_config={"tv_display_id": 1},
        )

        self.assertEqual(monitor._detect_tv_display_id(), 1)
        self.assertTrue(monitor._tv_display_verified)
        self.assertEqual(
            monitor._tv_display_verification_reason,
            "configured_display_found",
        )

    def test_tv_fps_does_not_fallback_to_gfxinfo(self):
        adb = MagicMock()
        monitor = PerformanceMonitor(
            adb,
            "com.test.player:media",
            monitor_config={"tv_display_id": 1, "allow_display0_fallback": False},
        )
        monitor._get_fps_from_surfaceflinger = MagicMock(return_value=0.0)
        monitor._get_fps_from_gfxinfo = MagicMock(return_value=60.0)

        fps = monitor._measure_video_fps(display_id=1, mpp_stats={})

        self.assertEqual(fps, 0.0)
        monitor._get_fps_from_gfxinfo.assert_not_called()
        self.assertEqual(monitor._last_video_fps_source, "none")

    def test_unscoped_surface_list_is_not_claimed_as_display_one(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Error: unknown option --display-id",
            "SurfaceView[com.test.player/com.test.MainActivity]",
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        fps = monitor._get_fps_from_surfaceflinger(display_id=1)

        self.assertEqual(fps, 0.0)
        self.assertEqual(adb._run_command.call_count, 2)

    def test_unscoped_surface_list_uses_target_root_section(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Display 0: no identification data\nDisplay 1: no identification data",
            (
                "Root#0\n"
                "SurfaceView - #1\n"
                "Root#1\n"
                "Background for -SurfaceView - #4\n"
                "SurfaceView - #4\n"
                "#2\n"
                "SurfaceView - #5\n"
                "Root#2\n"
                "SurfaceView - #9\n"
            ),
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        candidates = monitor._find_tv_surface_candidates(1)

        self.assertEqual(
            candidates,
            ["SurfaceView - #4", "SurfaceView - #5", "#2"],
        )

    def test_tv_freeze_event_is_counted(self):
        adb = MagicMock()
        monitor = PerformanceMonitor(adb, "com.test.player:media")
        monitor._tv_display_id = 1
        monitor.history.append({"pss_mb": 0, "cpu_percent": 0})

        monitor.report_event("TV_FREEZE", "Display 1画面静止5秒")
        summary = monitor.get_summary()

        self.assertEqual(summary["tv_freeze_count"], 1)
        self.assertEqual(summary["tv_freeze_events"][0]["display_id"], 1)
        self.assertIn("Display 1", summary["tv_freeze_events"][0]["description"])

    def test_surface_metrics_lock_and_detect_frame_advance(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Display1 VideoSurface",
            "16666666\n1 100000000 1\n1 133333333 1\n1 166666666 1",
            "16666666\n1 100000000 1\n1 133333333 1\n1 200000000 1",
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        first = monitor.collect_tv_frame_metrics(1)
        second = monitor.collect_tv_frame_metrics(1)

        self.assertEqual(first["surface_name"], "Display1 VideoSurface")
        self.assertTrue(first["frame_advanced"])
        self.assertTrue(second["frame_advanced"])
        self.assertEqual(adb._run_command.call_count, 3)
        self.assertGreater(second["max_frame_gap_ms"], 30)

    def test_old_frame_gap_is_not_repeated(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        output = (
            "16666666\n"
            "1 100000000 1\n"
            "1 500000000 1\n"
            "1 533333333 1\n"
        )

        first = monitor._parse_surface_latency(output, since_timestamp_ns=0)
        second = monitor._parse_surface_latency(
            output,
            since_timestamp_ns=500000000,
        )

        self.assertEqual(first["max_frame_gap_ms"], 0.0)
        self.assertLess(second["max_frame_gap_ms"], 40)

    def test_regular_fps_probe_does_not_move_watcher_cursor(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Display1 VideoSurface",
            "16666666\n1 100000000 1\n1 133333333 1",
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        monitor.collect_tv_frame_metrics(1, track_progress=False)

        self.assertEqual(monitor._last_tv_surface_frame_timestamp_ns, 0)

    def test_tv_stall_event_is_in_summary(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.history.append({"pss_mb": 0, "cpu_percent": 0})
        monitor.report_event(
            "TV_STALL",
            {"type": "TV_STALL", "duration_ms": 1200, "display_id": 1},
        )

        summary = monitor.get_summary()

        self.assertEqual(summary["tv_stall_count"], 1)
        self.assertEqual(summary["tv_stall_events"][0]["duration_ms"], 1200)

    def test_summary_decode_drop_uses_expected_stream_fps(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.history.extend([
            {
                "pss_mb": 10,
                "cpu_percent": 1,
                "mpp_work_count_delta_time_sec": 10,
                "mpp_work_count_delta": 270,
                "expected_stream_fps": 30,
                "video_fps": 27,
            },
            {
                "pss_mb": 10,
                "cpu_percent": 1,
                "mpp_work_count_delta_time_sec": 10,
                "mpp_work_count_delta": 300,
                "expected_stream_fps": 30,
                "video_fps": 30,
            },
        ])

        summary = monitor.get_summary()

        self.assertEqual(summary["decode_expected_frames_estimate"], 600)
        self.assertEqual(summary["decode_actual_frames_estimate"], 570)
        self.assertEqual(summary["decode_drop_estimate_total"], 30)
        self.assertEqual(summary["decode_drop_ratio"], 0.05)

    def test_thermal_throttling_is_reported_as_root_cause(self):
        analyzer = RootCauseAnalyzer(package_name="com.test.player:media")
        analyzer.record_baseline({
            "cpu_percent": 8,
            "system_cpu_percent": 25,
            "pss_mb": 100,
            "video_fps": 30,
            "top_consumers": "com.noisy.worker(3%)",
        })

        cause = analyzer.record_stutter_event({
            "timestamp": "2026-06-10 12:00:00",
            "cpu_percent": 15,
            "player_cpu_percent": 15,
            "system_cpu_percent": 92,
            "pss_mb": 110,
            "video_fps": 12,
            "expected_stream_fps": 30,
            "decode_fps_estimate": 12,
            "decode_slowdown_detected": True,
            "max_temperature_c": 84,
            "min_cpu_frequency_ratio": 0.42,
            "thermal_throttling": True,
        }, "com.noisy.worker(55%)")

        self.assertEqual(cause["root_cause_type"], "THERMAL_THROTTLING")
        self.assertEqual(cause["suspect_process"], "CPU Thermal Governor")

    def test_process_proliferation_is_reported_under_high_system_cpu(self):
        analyzer = RootCauseAnalyzer(package_name="com.test.player:media")
        analyzer.record_baseline({
            "cpu_percent": 5,
            "system_cpu_percent": 35,
            "pss_mb": 100,
            "video_fps": 30,
            "top_consumers": "",
        })

        cause = analyzer.record_stutter_event({
            "timestamp": "2026-06-10 19:00:00",
            "cpu_percent": 5,
            "system_cpu_percent": 90,
            "pss_mb": 100,
            "video_fps": 29,
        }, "/system/bin/init.thunder.sh x30(2.1%)")

        self.assertEqual(cause["root_cause_type"], "CPU_CONTENTION")
        self.assertEqual(
            cause["suspect_process"],
            "/system/bin/init.thunder.sh x30",
        )
        self.assertEqual(cause["evidence"]["instance_count"], 30)
        self.assertTrue(cause["evidence"]["resource_only"])
        self.assertLessEqual(cause["confidence"], 85.0)

        summary = analyzer.get_summary()
        process_risk = summary["process_risk_summary"][0]
        self.assertEqual(
            process_risk["process"],
            "/system/bin/init.thunder.sh x30",
        )
        self.assertEqual(process_risk["max_instance_count"], 30)
        self.assertEqual(process_risk["event_count"], 1)
        self.assertEqual(process_risk["peak_cpu_percent"], 2.1)

    def test_monitor_screencap_process_is_filtered_from_top_output(self):
        adb = object.__new__(AdbManager)
        adb._run_command = MagicMock(return_value=(
            "PID USER PR NI VIRT RES SHR S[%CPU] %MEM TIME+ ARGS\n"
            "101 root 20 0 0 0 0 R 70.0 0.1 0:01 "
            "screencap -d 1 -p /data/local/tmp/screen_temp_1.png\n"
            "102 root 20 0 0 0 0 R 8.0 0.1 0:01 "
            "/system/bin/init.thunder.sh\n"
        ))

        result = adb.get_top_heavy_processes()

        self.assertNotIn("screencap", result)
        self.assertIn("/system/bin/init.thunder.sh", result)

    def test_repeated_screen_anomalies_have_capped_deduction(self):
        evaluator = PlayStateEvaluator()
        evaluator.screen_anomaly_count = 31

        result = evaluator.evaluate_global_score({
            "tv_surface_locked": True,
        })

        self.assertEqual(result["score"], 60)
        self.assertIn("同类封顶40", result["deductions"][0])

    def test_cumulative_log_count_does_not_repeat_av_sync_candidate(self):
        analyzer = RootCauseAnalyzer(package_name="com.test.player:media")

        cause = analyzer.record_stutter_event({
            "timestamp": "2026-06-10 19:00:00",
            "cpu_percent": 5,
            "system_cpu_percent": 88,
            "pss_mb": 100,
            "video_fps": 29,
            "log_stutter_count": 12,
            "log_stutter_delta": 0,
        }, "")

        self.assertEqual(cause["root_cause_type"], "UNKNOWN")

    def test_new_log_hit_is_only_an_auxiliary_signal(self):
        analyzer = RootCauseAnalyzer(package_name="com.test.player:media")

        cause = analyzer.record_stutter_event({
            "timestamp": "2026-06-10 19:00:00",
            "cpu_percent": 5,
            "system_cpu_percent": 40,
            "pss_mb": 100,
            "video_fps": 29,
            "log_stutter_count": 12,
            "log_stutter_delta": 1,
        }, "")

        self.assertEqual(cause["root_cause_type"], "AV_SYNC_ISSUE")
        self.assertEqual(cause["confidence"], 40.0)
        self.assertTrue(cause["evidence"]["signal_only"])

    def test_high_system_cpu_without_tv_surface_blocks_release(self):
        evaluator = PlayStateEvaluator()

        result = evaluator.evaluate_global_score({
            "avg_system_cpu_percent": 87.53,
            "max_system_cpu_percent": 98.96,
            "tv_surface_locked": False,
            "root_cause_analysis": {
                "identified_causes": 12,
                "most_confident_cause": {
                    "root_cause_type": "AV_SYNC_ISSUE",
                    "confidence": 40,
                    "evidence": {"signal_only": True},
                },
            },
        })

        self.assertEqual(result["score"], 75)
        self.assertFalse(result["ready_to_release"])
        self.assertEqual(result["assessment"], "inconclusive")
        self.assertEqual(len(result["release_blockers"]), 2)

    def test_tv_perceptual_score_ignores_touchscreen_ui_jank(self):
        runner = object.__new__(TestRunner)

        result = runner._calculate_perceptual_stutter_score({
            "ui_jank_percent": 100.0,
            "log_stutter_count": 0,
        })

        self.assertEqual(result["score"], 0)
        self.assertEqual(result["level"], "流畅")
        self.assertEqual(result["details"], [])


if __name__ == "__main__":
    unittest.main()
