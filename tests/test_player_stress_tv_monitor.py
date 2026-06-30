import unittest
import threading
import tempfile
import time
import os
from unittest.mock import MagicMock

from modules.player_stress.core.adb_manager import AdbManager
from modules.player_stress.core.evaluator import PlayStateEvaluator
from modules.player_stress.core.log_monitor import LogMonitor
from modules.player_stress.core.monitor import PerformanceMonitor
from modules.player_stress.core.report_generator import ReportGenerator
from modules.player_stress.core.rk_monitor import RkMonitor
from modules.player_stress.core.root_cause_analyzer import RootCauseAnalyzer
from modules.player_stress.core.runner import TestRunner
from modules.player_stress.core.tv_playback_watcher import TvPlaybackWatcher
from modules.player_stress import views as player_stress_views


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

    def test_platform_identity_prefers_soc_and_platform_props(self):
        adb = object.__new__(AdbManager)
        adb._run_command = MagicMock(side_effect=[
            "rk3576",
            "rk3576",
            "rk35xx",
            "",
            "",
        ])

        self.assertEqual(
            adb.get_platform_identity(),
            "rk3576 (rk35xx)",
        )

    def test_event_evidence_manifest_includes_preview_and_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_dir = os.path.join(temp_dir, "20260624_120000_000001")
            os.makedirs(event_dir, exist_ok=True)
            with open(os.path.join(event_dir, "event.json"), "w", encoding="utf-8") as f:
                f.write('{"reason":"surface_not_advancing"}')
            with open(os.path.join(event_dir, "top_after.txt"), "w", encoding="utf-8") as f:
                f.write("mediaserver 98%")

            manifest = player_stress_views._build_tv_event_evidence_manifest(
                event_dir,
                "20260624_120000_000001",
            )

            self.assertEqual(manifest["event_token"], "20260624_120000_000001")
            self.assertTrue(manifest["event_json_url"].endswith("/event.json"))
            self.assertTrue(manifest["top_after_url"].endswith("/top_after.txt"))
            self.assertEqual(len(manifest["files"]), 2)
            self.assertIn("surface_not_advancing", manifest["files"][0]["preview_text"] + manifest["files"][1]["preview_text"])

    def test_resolve_tv_event_evidence_dir_prefers_existing_directory(self):
        original_instance = player_stress_views.TEST_INSTANCE
        with tempfile.TemporaryDirectory() as temp_dir:
            evidence_root = os.path.join(temp_dir, "tv_stall_events")
            event_dir = os.path.join(evidence_root, "20260624_120000_000001")
            os.makedirs(event_dir, exist_ok=True)

            watcher = type("Watcher", (), {"evidence_root": evidence_root})()
            player_stress_views.TEST_INSTANCE = type("Runner", (), {"tv_playback_watcher": watcher})()

            resolved = player_stress_views._resolve_tv_event_evidence_dir(
                "20260624_120000_000001"
            )

            self.assertEqual(resolved, event_dir)
        player_stress_views.TEST_INSTANCE = original_instance

    def test_event_evidence_diagnosis_prefers_cpu_contention_statement(self):
        diagnosis = player_stress_views._build_tv_event_evidence_diagnosis({
            "confirmed": True,
            "confidence_level": "confirmed",
            "max_frame_gap_ms": 860,
            "cpu_contention": {
                "detected": True,
                "top_candidate": {
                    "process": "/system/bin/init.thunder.sh",
                },
            },
        })

        self.assertIn("CPU 资源竞争", diagnosis["statement"])
        self.assertIn("/system/bin/init.thunder.sh", diagnosis["basis"])
        self.assertEqual(diagnosis["owner"], "系统/固件侧")

    def test_event_evidence_diagnosis_prefers_decoder_statement(self):
        diagnosis = player_stress_views._build_tv_event_evidence_diagnosis({
            "confirmed": True,
            "confidence_level": "confirmed",
            "reason": "decoder_stalled",
            "min_fps": 0.0,
            "corroboration_signals": ["decoder_confirmed", "decode_drop"],
        })

        self.assertIn("解码链路", diagnosis["statement"])
        self.assertIn("decoder_confirmed", diagnosis["basis"])
        self.assertEqual(diagnosis["owner"], "播放器/解码侧")

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

    def test_display_scoped_surface_list_keeps_anonymous_and_generic_fallbacks(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Error: unknown option --display-id",
            (
                "Root#0\n"
                "StatusBar\n"
                "Root#1\n"
                "BufferQueueLayer\n"
                "#12\n"
                "Background for SurfaceView\n"
            ),
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        candidates = monitor._find_tv_surface_candidates(1)

        self.assertEqual(
            candidates,
            ["BufferQueueLayer", "#12"],
        )

    def test_unscoped_surface_list_can_parse_display_section(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Error: unknown option --display-id",
            (
                "Display 0 name=\"内置屏幕\"\n"
                "StatusBar\n"
                "Display 1 name=\"HDMI 屏幕\"\n"
                "5412b30 com.thunder.ktv#350\n"
                "SurfaceView[]#353\n"
                "SurfaceView[](BLAST)#354\n"
                "Display 2 name=\"HDMI 屏幕\"\n"
                "OtherLayer\n"
            ),
        ]
        monitor = PerformanceMonitor(adb, "com.thunder.ktv:media")

        candidates = monitor._find_tv_surface_candidates(1)

        self.assertIn("SurfaceView[]#353", candidates)
        self.assertIn("SurfaceView[](BLAST)#354", candidates)

    def test_tv_freeze_event_is_counted(self):
        adb = MagicMock()
        monitor = PerformanceMonitor(adb, "com.test.player:media")
        monitor._tv_display_id = 1
        monitor.history.append({"pss_mb": 0, "cpu_percent": 0})

        monitor.report_event("TV_FREEZE", "Display 1 画面静止 5 秒")
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
        latency_call = adb._run_command.call_args_list[1]
        self.assertEqual(
            latency_call.args[0],
            [
                "shell",
                "dumpsys",
                "SurfaceFlinger",
                "--latency",
                "'Display1 VideoSurface'",
            ],
        )
        self.assertEqual(latency_call.kwargs["retry"], 0)

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

    def test_huge_cursor_jump_is_treated_as_stale_history(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        output = (
            "16666666\n"
            "1 20100000000 1\n"
            "1 20133333333 1\n"
            "1 20166666666 1\n"
        )

        metrics = monitor._parse_surface_latency(
            output,
            since_timestamp_ns=100_000_000,
        )

        self.assertEqual(metrics["max_frame_gap_ms"], 33.33)
        self.assertLess(metrics["p95_frame_gap_ms"], 40)

    def test_regular_fps_probe_does_not_move_watcher_cursor(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Display1 VideoSurface",
            "16666666\n1 100000000 1\n1 133333333 1",
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        monitor.collect_tv_frame_metrics(1, track_progress=False)

        self.assertEqual(monitor._last_tv_surface_frame_timestamp_ns, 0)

    def test_display_latency_fallback_can_provide_fps(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Display1 VideoSurface",
            "16666666\n0 0 0",
            "16666666\n0 100000000 133333333\n0 133333333 166666666\n0 166666666 200000000",
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        metrics = monitor.collect_tv_frame_metrics(1)

        self.assertGreater(metrics["fps"], 0.0)
        self.assertEqual(metrics["latency_mode"], "display_id")
        self.assertEqual(metrics["probe_reason"], "display_fallback")

    def test_surface_latency_parser_accepts_alternate_timestamp_column(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        output = (
            "16666666\n"
            "0 100000000 133333333\n"
            "0 133333333 166666666\n"
            "0 166666666 200000000\n"
        )

        metrics = monitor._parse_surface_latency(output, since_timestamp_ns=0)

        self.assertGreater(metrics["fps"], 25.0)
        self.assertEqual(metrics["latency_frame_count"], 3)

    def test_display_latency_all_zero_rows_is_marked_invalid(self):
        adb = MagicMock()
        adb._run_command.side_effect = [
            "Display1 VideoSurface",
            "16666666\n0 0 0\n0 0 0\n",
            "16666666\n0 0 0\n0 0 0\n",
        ]
        monitor = PerformanceMonitor(adb, "com.test.player:media")

        metrics = monitor.collect_tv_frame_metrics(1)

        self.assertEqual(metrics["fps"], 0.0)
        self.assertEqual(metrics["probe_reason"], "latency_zero_frames")

    def test_task_count_source_does_not_mark_decoder_stuck(self):
        adb = MagicMock()
        rk = RkMonitor(adb)
        rk.is_supported = True
        rk.active_path = "/proc/mpp_service/rkvdec/task_count"
        rk.last_work_count = 1
        rk.last_work_count_time = time.time() - 2
        adb._run_command.return_value = "1"

        stats = rk.get_mpp_stats()

        self.assertFalse(stats["work_count_reliable"])
        self.assertFalse(stats["decoder_stuck"])

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

    def test_tv_stall_risk_event_does_not_count_as_confirmed_stall(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.history.append({"pss_mb": 0, "cpu_percent": 0})
        monitor.report_event(
            "TV_STALL",
            {
                "type": "TV_STALL_RISK",
                "confirmed": False,
                "duration_ms": 3200,
                "display_id": 1,
                "assessment_reason": "frame gap only",
            },
        )

        summary = monitor.get_summary()

        self.assertEqual(summary["tv_stall_count"], 0)
        self.assertEqual(summary["tv_stall_risk_count"], 1)
        self.assertEqual(summary["tv_stall_risk_events"][0]["type"], "TV_STALL_RISK")

    def test_tv_playback_watcher_marks_gap_only_event_as_risk(self):
        adb = MagicMock()
        adb.is_audio_active.return_value = True
        adb.take_screenshot.return_value = None
        adb.get_cpu_evidence.return_value = {"top_processes": [], "process_count": 100}
        monitor = MagicMock()
        monitor.package_name = "com.test.player:media"
        monitor.history = [{
            "video_fps": 30.0,
            "player_cpu_percent": 8.0,
            "system_cpu_percent": 28.0,
            "system_cpu_pressure": False,
            "decode_drop_ratio": 1.0,
            "mpp_work_count_reliable": False,
            "decoder_stuck_confirmed": False,
            "log_stutter_delta": 0,
            "expected_stream_fps": 30.0,
        }]
        events = []
        watcher = TvPlaybackWatcher(
            adb,
            monitor,
            output_dir=tempfile.mkdtemp(),
            config={"tv_stall_start_confirmations": 2},
            event_callback=events.append,
        )

        sample = {
            "audio_active": True,
            "ignore_video_metrics": False,
            "surface_name": "SurfaceView#1",
            "fps": 30.0,
            "max_frame_gap_ms": 2333.3,
            "frame_advanced": True,
        }
        watcher.process_sample(sample, now=100.0)
        watcher.process_sample(sample, now=100.5)
        watcher.process_sample({
            "audio_active": True,
            "ignore_video_metrics": False,
            "surface_name": "SurfaceView#1",
            "fps": 30.0,
            "max_frame_gap_ms": 0.0,
            "frame_advanced": True,
        }, now=101.0)
        watcher.process_sample({
            "audio_active": True,
            "ignore_video_metrics": False,
            "surface_name": "SurfaceView#1",
            "fps": 30.0,
            "max_frame_gap_ms": 0.0,
            "frame_advanced": True,
        }, now=101.5)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "TV_STALL_RISK")
        self.assertFalse(events[0]["confirmed"])

    def test_tv_playback_watcher_marks_decoder_backed_event_as_confirmed(self):
        adb = MagicMock()
        adb.is_audio_active.return_value = True
        adb.take_screenshot.return_value = None
        adb.get_cpu_evidence.return_value = {"top_processes": [], "process_count": 100}
        monitor = MagicMock()
        monitor.package_name = "com.test.player:media"
        monitor.history = [{
            "video_fps": 18.0,
            "player_cpu_percent": 8.0,
            "system_cpu_percent": 42.0,
            "system_cpu_pressure": False,
            "decode_drop_ratio": 0.0,
            "mpp_work_count_reliable": True,
            "decoder_stuck_confirmed": True,
            "log_stutter_delta": 0,
            "expected_stream_fps": 30.0,
        }]
        events = []
        watcher = TvPlaybackWatcher(
            adb,
            monitor,
            output_dir=tempfile.mkdtemp(),
            config={"tv_stall_start_confirmations": 2},
            event_callback=events.append,
        )

        sample = {
            "audio_active": True,
            "ignore_video_metrics": False,
            "surface_name": "SurfaceView#1",
            "fps": 18.0,
            "max_frame_gap_ms": 1800.0,
            "frame_advanced": False,
        }
        watcher.process_sample(sample, now=100.0)
        watcher.process_sample(sample, now=100.5)
        watcher.process_sample({
            "audio_active": True,
            "ignore_video_metrics": False,
            "surface_name": "SurfaceView#1",
            "fps": 30.0,
            "max_frame_gap_ms": 0.0,
            "frame_advanced": True,
        }, now=101.0)
        watcher.process_sample({
            "audio_active": True,
            "ignore_video_metrics": False,
            "surface_name": "SurfaceView#1",
            "fps": 30.0,
            "max_frame_gap_ms": 0.0,
            "frame_advanced": True,
        }, now=101.5)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "TV_STALL")
        self.assertTrue(events[0]["confirmed"])

    def test_tv_playback_watcher_auto_finishes_long_running_risk_event(self):
        adb = MagicMock()
        adb.is_audio_active.return_value = True
        adb.take_screenshot.return_value = None
        adb.get_cpu_evidence.return_value = {"top_processes": [], "process_count": 100}
        monitor = MagicMock()
        monitor.package_name = "com.test.player:media"
        monitor.history = [{
            "video_fps": 30.0,
            "player_cpu_percent": 8.0,
            "system_cpu_percent": 28.0,
            "system_cpu_pressure": False,
            "decode_drop_ratio": 0.0,
            "mpp_work_count_reliable": False,
            "decoder_stuck_confirmed": False,
            "log_stutter_delta": 0,
            "expected_stream_fps": 30.0,
        }]
        events = []
        watcher = TvPlaybackWatcher(
            adb,
            monitor,
            output_dir=tempfile.mkdtemp(),
            config={
                "tv_stall_start_confirmations": 2,
                "tv_stall_risk_auto_finish_ms": 1200,
            },
            event_callback=events.append,
        )

        sample = {
            "audio_active": True,
            "ignore_video_metrics": False,
            "surface_name": "SurfaceView#1",
            "fps": 30.0,
            "max_frame_gap_ms": 2333.3,
            "frame_advanced": True,
        }
        watcher.process_sample(sample, now=100.0)
        watcher.process_sample(sample, now=100.5)
        watcher.process_sample(sample, now=102.1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "TV_STALL_RISK")
        self.assertEqual(events[0]["recovery_reason"], "risk_timeout_no_corroboration")
        self.assertIsNone(watcher._active_event)

    def test_log_monitor_filters_time_window_logs(self):
        monitor = LogMonitor(MagicMock(), "device-1")
        monitor.recent_logs.extend([
            {"time": 100.0, "wall_time": "2026-06-25 10:00:00", "line": "before"},
            {"time": 105.0, "wall_time": "2026-06-25 10:00:05", "line": "inside-1"},
            {"time": 107.0, "wall_time": "2026-06-25 10:00:07", "line": "inside-2"},
            {"time": 120.0, "wall_time": "2026-06-25 10:00:20", "line": "after"},
        ])
        monitor.decoder_logs.extend([
            {"time": 104.0, "pattern": "MediaCodec", "line": "codec timeout"},
            {"time": 121.0, "pattern": "MediaCodec", "line": "late event"},
        ])

        log_rows = monitor.get_time_window_logs(103.0, 110.0)
        decoder_rows = monitor.get_time_window_decoder_events(103.0, 110.0)

        self.assertEqual([item["line"] for item in log_rows], ["inside-1", "inside-2"])
        self.assertEqual(len(decoder_rows), 1)
        self.assertEqual(decoder_rows[0]["line"], "codec timeout")

    def test_tv_playback_watcher_writes_time_window_log_evidence(self):
        adb = MagicMock()
        adb.is_audio_active.return_value = True
        adb.take_screenshot.return_value = None
        adb.get_cpu_evidence.return_value = {"top_processes": [], "process_count": 100, "raw_top": "top output"}
        monitor = MagicMock()
        monitor.package_name = "com.test.player:media"
        monitor.history = [{
            "video_fps": 18.0,
            "player_cpu_percent": 8.0,
            "system_cpu_percent": 42.0,
            "system_cpu_pressure": False,
            "decode_drop_ratio": 0.0,
            "mpp_work_count_reliable": True,
            "decoder_stuck_confirmed": True,
            "log_stutter_delta": 0,
            "expected_stream_fps": 30.0,
        }]
        fake_log_monitor = MagicMock()
        fake_log_monitor.get_time_window_logs.return_value = [
            {"time": 100.1, "wall_time": "2026-06-25 10:00:00", "line": "I Media: something happened"}
        ]
        fake_log_monitor.get_time_window_decoder_events.return_value = [
            {"time": 100.2, "pattern": "MediaCodec", "line": "E MediaCodec: codec timeout"}
        ]
        events = []
        with tempfile.TemporaryDirectory() as temp_dir:
            watcher = TvPlaybackWatcher(
                adb,
                monitor,
                output_dir=temp_dir,
                config={"tv_stall_start_confirmations": 2},
                event_callback=events.append,
                log_monitor=fake_log_monitor,
            )

            sample = {
                "audio_active": True,
                "ignore_video_metrics": False,
                "surface_name": "SurfaceView#1",
                "fps": 18.0,
                "max_frame_gap_ms": 1800.0,
                "frame_advanced": False,
            }
            watcher.process_sample(sample, now=100.0)
            watcher.process_sample(sample, now=100.5)
            watcher.process_sample({
                "audio_active": True,
                "ignore_video_metrics": False,
                "surface_name": "SurfaceView#1",
                "fps": 30.0,
                "max_frame_gap_ms": 0.0,
                "frame_advanced": True,
            }, now=101.0)
            watcher.process_sample({
                "audio_active": True,
                "ignore_video_metrics": False,
                "surface_name": "SurfaceView#1",
                "fps": 30.0,
                "max_frame_gap_ms": 0.0,
                "frame_advanced": True,
            }, now=101.5)

            self.assertEqual(len(events), 1)
            event_dir = events[0]["evidence_dir"]
            with open(os.path.join(event_dir, "time_window_logcat.txt"), "r", encoding="utf-8") as f:
                logcat_text = f.read()
            with open(os.path.join(event_dir, "decoder_window.txt"), "r", encoding="utf-8") as f:
                decoder_text = f.read()
            with open(os.path.join(event_dir, "event_summary.txt"), "r", encoding="utf-8") as f:
                summary_text = f.read()

            self.assertIn("something happened", logcat_text)
            self.assertIn("codec timeout", decoder_text)
            self.assertIn("建议查看", summary_text)

    def test_runner_expires_stuck_screen_check(self):
        runner = object.__new__(TestRunner)
        future = MagicMock()
        future.done.return_value = False
        runner.screen_check_future = future
        runner.screen_check_started_at = 10.0
        runner.screen_check_timeout_seconds = 12.0
        runner.screen_check_skip_count = 4
        runner.last_screen_results = {1: {"status": "NORMAL"}}
        runner.log = MagicMock()

        expired = runner._expire_stuck_screen_check(23.5)

        self.assertTrue(expired)
        future.cancel.assert_called_once()
        self.assertIsNone(runner.screen_check_future)
        self.assertEqual(runner.screen_check_started_at, 0.0)
        self.assertEqual(runner.screen_check_skip_count, 0)
        self.assertEqual(runner.last_screen_results, {})

    def test_summary_decode_drop_uses_expected_stream_fps(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.history.extend([
            {
                "pss_mb": 10,
                "cpu_percent": 1,
                "mpp_work_count_reliable": True,
                "mpp_work_count_delta_time_sec": 10,
                "mpp_work_count_delta": 270,
                "expected_stream_fps": 30,
                "video_fps": 27,
            },
            {
                "pss_mb": 10,
                "cpu_percent": 1,
                "mpp_work_count_reliable": True,
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

    def test_summary_decode_drop_ignores_unreliable_mpp_samples(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.history.extend([
            {
                "pss_mb": 10,
                "cpu_percent": 1,
                "mpp_work_count_reliable": False,
                "mpp_work_count_delta_time_sec": 10,
                "mpp_work_count_delta": 0,
                "expected_stream_fps": 30,
                "video_fps": 30,
                "decode_drop_ratio": 1.0,
            },
            {
                "pss_mb": 10,
                "cpu_percent": 1,
                "mpp_work_count_reliable": False,
                "mpp_work_count_delta_time_sec": 8,
                "mpp_work_count_delta": 0,
                "expected_stream_fps": 30,
                "video_fps": 30,
                "decode_drop_ratio": 1.0,
            },
        ])

        summary = monitor.get_summary()

        self.assertEqual(summary["decode_expected_frames_estimate"], 0)
        self.assertEqual(summary["decode_actual_frames_estimate"], 0)
        self.assertEqual(summary["decode_drop_estimate_total"], 0)
        self.assertEqual(summary["decode_drop_ratio"], 0.0)

    def test_single_snapshot_decode_drop_stays_zero_when_mpp_unreliable(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor._expected_stream_fps = 30.0
        monitor._expected_stream_fps_ready = True
        monitor._tv_display_verified = True
        monitor._last_tv_surface_name = "SurfaceView#1"
        monitor._last_tv_latency_probe = {"probe_reason": "ok"}
        monitor.adb.get_current_pid.return_value = 123
        monitor.adb.get_memory_info.return_value = {"pss_mb": 50}
        monitor.adb.get_cpu_usage.return_value = 5.0
        monitor.adb.get_system_cpu_usage.return_value = 20.0
        monitor.adb.get_gfx_info.return_value = {"total_frames": 100, "janky_frames": 5}
        monitor.adb.get_gpu_usage.return_value = 0.0
        monitor.adb.is_audio_active.return_value = True
        monitor.adb.get_top_heavy_processes.return_value = ""
        monitor.adb.get_thermal_status.return_value = {
            "available": False,
            "max_temperature_c": 0.0,
            "min_frequency_ratio": 0.0,
            "thermal_throttling": False,
        }
        monitor.rk_monitor.get_mpp_stats = MagicMock(return_value={
            "active_instances": 1,
            "session_count": 1,
            "total_work_count": 0,
            "work_count_delta": 0,
            "work_count_delta_time_sec": 10.0,
            "work_count_reliable": False,
            "decoder_stuck": False,
            "decoder_stuck_duration_sec": 0.0,
        })
        monitor._measure_video_fps = MagicMock(return_value=30.0)
        monitor._detect_tv_display_id = MagicMock(return_value=1)
        monitor._update_gfx_jank_metrics = MagicMock(return_value=(0, 0, 0.0))
        monitor._get_play_state = MagicMock(return_value="RUNNING")

        snapshot = monitor.collect_snapshot()

        self.assertEqual(snapshot["video_fps"], 30.0)
        self.assertFalse(snapshot["mpp_work_count_reliable"])
        self.assertEqual(snapshot["decode_drop_estimate"], 0)
        self.assertEqual(snapshot["decode_drop_ratio"], 0.0)

    def test_dead_samples_are_excluded_from_summary_metrics(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.history.extend([
            {
                "timestamp": "2026-06-14 12:00:00",
                "status": "RUNNING",
                "sample_valid": True,
                "pss_mb": 60,
                "cpu_percent": 5,
                "system_cpu_percent": 90,
                "video_fps": 30,
            },
            {
                "timestamp": "2026-06-14 12:00:05",
                "status": "DEAD",
                "sample_valid": False,
                "pss_mb": 0,
                "cpu_percent": 0,
                "system_cpu_percent": 10,
                "video_fps": 0,
            },
        ])
        monitor.pid_events.append({"type": "PID_LOST"})

        summary = monitor.get_summary()

        self.assertEqual(summary["valid_samples"], 1)
        self.assertEqual(summary["invalid_samples"], 1)
        self.assertEqual(summary["valid_sample_ratio"], 0.5)
        self.assertEqual(summary["avg_pss_mb"], 60)
        self.assertEqual(summary["avg_system_cpu_percent"], 90)
        self.assertEqual(summary["pid_loss_count"], 1)

    def test_pid_loss_is_a_hard_release_failure(self):
        evaluator = PlayStateEvaluator()

        result = evaluator.evaluate_global_score({
            "pid_loss_count": 1,
            "target_process_lost": True,
            "valid_sample_ratio": 0.3,
            "tv_surface_locked": False,
        })

        self.assertEqual(result["assessment"], "fail")
        self.assertFalse(result["ready_to_release"])
        self.assertLessEqual(result["score"], 59)
        self.assertTrue(any("目标播放器进程丢失" in item for item in result["release_blockers"]))

    def test_process_failure_summary_includes_pid_timeline(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.history.append({
            "timestamp": "2026-06-14 12:00:00",
            "status": "RUNNING",
            "sample_valid": True,
            "pss_mb": 60,
            "cpu_percent": 5,
            "system_cpu_percent": 40,
            "video_fps": 30,
        })
        monitor.pid_events.extend([
            {
                "timestamp": "2026-06-14 12:03:00",
                "type": "PID_RESTART",
                "elapsed_min": 3,
                "description": "Process restarted (Old: 100 -> New: 101)",
            },
            {
                "timestamp": "2026-06-14 12:05:00",
                "type": "PID_LOST",
                "elapsed_min": 5,
                "description": "Process died (PID: 101)",
            },
        ])

        summary = monitor.get_summary()
        failure_summary = summary["process_failure_summary"]

        self.assertTrue(failure_summary["has_player_failure"])
        self.assertEqual(failure_summary["total_failure_count"], 2)
        self.assertEqual(failure_summary["restart_count"], 1)
        self.assertEqual(failure_summary["pid_loss_count"], 1)
        self.assertEqual(failure_summary["first_failure_type"], "PID重启")
        self.assertEqual(failure_summary["last_failure_type"], "进程丢失")
        self.assertEqual(len(failure_summary["timeline"]), 2)

    def test_tv_stall_and_process_failure_correlation_is_reported(self):
        runner = object.__new__(TestRunner)
        summary = {
            "tv_stall_events": [
                {
                    "start_time": "2026-06-14T12:00:10.000",
                    "reason": "frame_gap_800.0ms",
                },
                {
                    "start_time": "2026-06-14T12:12:00.000",
                    "reason": "surface_not_advancing",
                },
            ],
            "pid_events": [
                {
                    "timestamp": "2026-06-14 12:00:25",
                    "type": "PID_LOST",
                    "description": "Process died (PID: 101)",
                }
            ],
        }
        error_stats = {
            "crash_count": 1,
            "anr_count": 0,
            "error_events": [
                {
                    "time": "20260614_121005",
                    "type": "CRASH",
                    "message": "fatal crash happened",
                }
            ],
        }

        correlation = runner._build_tv_process_correlation_summary(summary, error_stats, window_seconds=30)

        self.assertEqual(correlation["total_tv_stall_count"], 2)
        self.assertEqual(correlation["matched_tv_stall_count"], 1)
        self.assertEqual(correlation["matched_failure_event_count"], 1)
        self.assertEqual(len(correlation["pair_details"]), 1)
        self.assertIn("电视端卡顿", correlation["conclusion"])

    def test_responsibility_summary_prefers_player_failure_when_strongly_correlated(self):
        runner = object.__new__(TestRunner)
        summary = {
            "process_failure_summary": {
                "has_player_failure": True,
                "crash_count": 1,
                "anr_count": 0,
                "restart_count": 0,
                "pid_loss_count": 1,
            },
            "tv_process_correlation_summary": {
                "matched_tv_stall_count": 7,
                "total_tv_stall_count": 8,
                "correlated_ratio": 0.875,
            },
            "confirmed_decoder_stuck_count": 0,
            "avg_system_cpu_percent": 42.0,
            "max_system_cpu_percent": 60.0,
            "avg_player_cpu_percent": 8.0,
            "tv_surface_locked": True,
            "avg_video_fps": 28.5,
            "decode_drop_ratio": 0.01,
        }
        root_cause_analysis = {
            "final_diagnosis": {
                "owner": "播放器侧",
                "suspect_process": "com.test.player",
            },
            "most_confident_cause": {
                "root_cause_type": "AV_SYNC_ISSUE",
                "suspect_process": "com.test.player",
            },
        }

        result = runner._build_responsibility_summary(summary, root_cause_analysis)

        self.assertEqual(result["category"], "播放器自身异常主导")
        self.assertEqual(result["owner"], "播放器侧")

    def test_responsibility_summary_prefers_cpu_contention_when_system_cpu_is_high(self):
        runner = object.__new__(TestRunner)
        summary = {
            "process_failure_summary": {
                "has_player_failure": False,
                "crash_count": 0,
                "anr_count": 0,
                "restart_count": 0,
                "pid_loss_count": 0,
            },
            "tv_process_correlation_summary": {
                "matched_tv_stall_count": 0,
                "total_tv_stall_count": 6,
                "correlated_ratio": 0.0,
            },
            "confirmed_decoder_stuck_count": 0,
            "avg_system_cpu_percent": 95.0,
            "max_system_cpu_percent": 100.0,
            "avg_player_cpu_percent": 5.0,
            "tv_surface_locked": True,
            "avg_video_fps": 26.0,
            "decode_drop_ratio": 0.0,
        }
        root_cause_analysis = {
            "final_diagnosis": {
                "owner": "系统/固件侧",
                "suspect_process": "/system/bin/init.thunder.sh x37",
            },
            "most_confident_cause": {
                "root_cause_type": "CPU_CONTENTION",
                "suspect_process": "/system/bin/init.thunder.sh x37",
            },
        }

        result = runner._build_responsibility_summary(summary, root_cause_analysis)

        self.assertEqual(result["category"], "系统/固件 CPU 竞争主导")
        self.assertEqual(result["owner"], "系统/固件侧")

    def test_dev_priority_summary_prefers_cpu_contention_target(self):
        runner = object.__new__(TestRunner)
        summary = {
            "decoder_stuck_summary": {},
        }
        root_cause_analysis = {
            "final_diagnosis": {
                "owner": "系统/固件侧",
                "suspect_process": "/system/bin/init.thunder.sh x37",
                "evidence_strength": {"label": "Confirmed"},
            },
            "most_confident_cause": {
                "root_cause_type": "CPU_CONTENTION",
            },
        }

        result = runner._build_dev_priority_summary(summary, root_cause_analysis)

        self.assertEqual(result["target"], "/system/bin/init.thunder.sh x37")
        self.assertEqual(result["owner"], "系统/固件侧")
        self.assertEqual(result["strength"], "Confirmed")
        self.assertEqual(result["cause_type"], "CPU_CONTENTION")
        self.assertTrue(result["logs"])

    def test_dev_priority_summary_prefers_decoder_name_when_decoder_stuck(self):
        runner = object.__new__(TestRunner)
        summary = {
            "decoder_stuck_summary": {
                "decoder_name": "OMX.rk.video.decoder.avc",
            },
        }
        root_cause_analysis = {
            "final_diagnosis": {
                "owner": "播放器/解码侧",
                "suspect_process": "MPP Hardware Decoder",
                "evidence_strength": {"label": "Strong"},
            },
            "most_confident_cause": {
                "root_cause_type": "DECODER_STUCK",
            },
        }

        result = runner._build_dev_priority_summary(summary, root_cause_analysis)

        self.assertEqual(result["target"], "OMX.rk.video.decoder.avc")
        self.assertEqual(result["strength"], "Strong")
        self.assertEqual(result["cause_type"], "DECODER_STUCK")

    def test_dev_priority_summary_returns_no_action_when_no_issue_detected(self):
        runner = object.__new__(TestRunner)
        summary = {
            "tv_stall_count": 0,
            "tv_stall_risk_count": 0,
            "confirmed_decoder_stuck_count": 0,
            "decoder_stuck_risk_count": 0,
            "process_failure_summary": {
                "has_player_failure": False,
            },
            "decoder_stuck_summary": {},
        }

        result = runner._build_dev_priority_summary(summary, {"final_diagnosis": {}, "most_confident_cause": {}})

        self.assertEqual(result["target"], "本轮无需专项排查")
        self.assertEqual(result["owner"], "无需归因")
        self.assertEqual(result["strength"], "N/A")
        self.assertEqual(result["cause_type"], "NONE")
        self.assertTrue(result["logs"])

    def test_platform_support_summary_returns_a_grade_when_surface_and_fps_are_available(self):
        runner = object.__new__(TestRunner)
        runner.platform_identity = "rk3576"
        runner.firmware_incremental = "eng.thunder.20260609.203352"

        result = runner._build_platform_support_summary({
            "tv_display_verified": True,
            "tv_display_id": 1,
            "tv_surface_locked": True,
            "avg_video_fps": 30.0,
            "decoder_stuck_risk_count": 0,
            "confirmed_decoder_stuck_count": 0,
        })

        self.assertEqual(result["grade"], "A")
        self.assertIn("确认级", result["headline"])
        self.assertIn("rk3576", result["platform_label"])

    def test_platform_support_summary_returns_c_grade_when_no_tv_evidence_available(self):
        runner = object.__new__(TestRunner)
        runner.platform_identity = ""
        runner.firmware_incremental = "eng.thunder.20260609.203352"

        result = runner._build_platform_support_summary({
            "tv_display_verified": False,
            "tv_surface_locked": False,
            "avg_video_fps": 0,
            "video_fps_unavailable_reason": "未识别到电视端视频 Surface",
        })

        self.assertEqual(result["grade"], "C")
        self.assertIn("辅助级", result["headline"])
        self.assertTrue(result["limitations"])

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
        self.assertEqual(summary["final_diagnosis"]["evidence_level"], "risk")

    def test_decoder_stuck_tracks_decoder_name_and_logs(self):
        analyzer = RootCauseAnalyzer(package_name="com.test.player:media")

        cause = analyzer.record_stutter_event({
            "timestamp": "2026-06-15 20:00:00",
            "cpu_percent": 4,
            "player_cpu_percent": 4,
            "system_cpu_percent": 50,
            "video_fps": 0,
            "mpp_active": 1,
            "mpp_work_count_delta": 0,
            "decoder_stuck": True,
            "decoder_log_events": [
                {"pattern": "MediaCodec", "line": "E MediaCodec: codec reported error OMX.rk.video.decoder.avc"}
            ],
            "decoder_diagnostics": {
                "decoder_name": "OMX.rk.video.decoder.avc",
                "codec_lines": ["codec: OMX.rk.video.decoder.avc"],
            },
        }, "")

        self.assertEqual(cause["root_cause_type"], "DECODER_STUCK")
        self.assertEqual(cause["suspect_process"], "OMX.rk.video.decoder.avc")
        self.assertEqual(
            cause["evidence"]["decoder_name"],
            "OMX.rk.video.decoder.avc",
        )
        self.assertEqual(
            cause["evidence"]["decoder_diagnostics"]["decoder_name"],
            "OMX.rk.video.decoder.avc",
        )

    def test_decoder_stuck_without_surface_is_only_resource_risk(self):
        analyzer = RootCauseAnalyzer(package_name="com.test.player:media")

        cause = analyzer.record_stutter_event({
            "timestamp": "2026-06-15 20:00:00",
            "cpu_percent": 4,
            "player_cpu_percent": 4,
            "system_cpu_percent": 50,
            "video_fps": 0,
            "mpp_active": 1,
            "mpp_work_count_delta": 0,
            "decoder_stuck": True,
            "decoder_stuck_confirmed": False,
            "tv_surface_name": "",
            "decoder_log_events": [
                {"pattern": "codec", "line": "video chain: codec(name=rk_mpp/bitrate=0k/1920x1080x@30.00)"}
            ],
            "decoder_diagnostics": {},
        }, "")

        self.assertEqual(cause["root_cause_type"], "DECODER_STUCK")
        self.assertEqual(cause["confidence"], 72.0)
        self.assertTrue(cause["evidence"]["resource_only"])
        summary = analyzer.get_summary()
        self.assertEqual(summary["final_diagnosis"]["evidence_level"], "risk")

    def test_adb_decoder_diagnostics_extracts_decoder_name(self):
        adb = object.__new__(AdbManager)
        adb._run_command = MagicMock(side_effect=[
            "codec component OMX.rk.video.decoder.hevc for com.test.player",
            "session 0 decoder=hevc",
            "work_count: 123",
        ])

        diagnostics = adb.get_decoder_diagnostics("com.test.player:media")

        self.assertEqual(
            diagnostics["decoder_name"],
            "OMX.rk.video.decoder.hevc",
        )
        self.assertTrue(diagnostics["codec_lines"])

    def test_monitor_summary_aggregates_decoder_stuck_details(self):
        adb = MagicMock()
        monitor = PerformanceMonitor(adb, "com.test.player:media")
        monitor.history = [
            {
                "timestamp": "2026-06-17 12:00:00",
                "pid": 123,
                "status": "RUNNING",
                "pss_mb": 50,
                "cpu_percent": 4.0,
                "player_cpu_percent": 4.0,
                "system_cpu_percent": 92.0,
                "sample_valid": True,
                "decoder_stuck": True,
                "decoder_stuck_duration_sec": 1.6,
                "decode_fps_estimate": 0.0,
                "expected_stream_fps": 30.0,
                "decode_drop_ratio": 1.0,
                "video_fps": 0.0,
                "decoder_diagnostics": {
                    "decoder_name": "OMX.rk.video.decoder.avc",
                    "codec_lines": ["codec: OMX.rk.video.decoder.avc", "state: executing"],
                },
                "decoder_log_events": [
                    {"pattern": "MediaCodec", "line": "E MediaCodec: codec reported error OMX.rk.video.decoder.avc"}
                ],
                "mpp_work_count_delta": 0,
                "mpp_work_count_delta_time_sec": 1.6,
                "ignore_video_metrics": False,
                "gfx_jank_percent": 0.0,
                "is_perceptual_jank": False,
                "log_stutter_count": 0,
                "log_stutter_delta": 0,
                "observer_cpu_percent": 1.2,
                "observer_memory_mb": 42.0,
                "observer_sampling_mode": "standard",
            },
            {
                "timestamp": "2026-06-17 12:00:05",
                "pid": 123,
                "status": "RUNNING",
                "pss_mb": 50,
                "cpu_percent": 5.0,
                "player_cpu_percent": 5.0,
                "system_cpu_percent": 90.0,
                "sample_valid": True,
                "decoder_stuck": True,
                "decoder_stuck_duration_sec": 2.4,
                "decode_fps_estimate": 0.0,
                "expected_stream_fps": 30.0,
                "decode_drop_ratio": 1.0,
                "video_fps": 0.0,
                "decoder_diagnostics": {
                    "decoder_name": "OMX.rk.video.decoder.avc",
                    "codec_lines": ["codec: OMX.rk.video.decoder.avc"],
                },
                "decoder_log_events": [
                    {"pattern": "MediaCodec", "line": "W MediaCodec: OMX.rk.video.decoder.avc dequeue output timeout"}
                ],
                "mpp_work_count_delta": 0,
                "mpp_work_count_delta_time_sec": 2.4,
                "ignore_video_metrics": False,
                "gfx_jank_percent": 0.0,
                "is_perceptual_jank": False,
                "log_stutter_count": 0,
                "log_stutter_delta": 0,
                "observer_cpu_percent": 1.8,
                "observer_memory_mb": 43.5,
                "observer_sampling_mode": "standard",
            },
        ]

        summary = monitor.get_summary()
        decoder_summary = summary["decoder_stuck_summary"]

        self.assertEqual(decoder_summary["count"], 2)
        self.assertEqual(decoder_summary["decoder_name"], "OMX.rk.video.decoder.avc")
        self.assertEqual(decoder_summary["max_duration_sec"], 2.4)
        self.assertEqual(decoder_summary["sample_timestamp"], "2026-06-17 12:00:05")
        self.assertIn("OMX.rk.video.decoder.avc", decoder_summary["diagnostic_lines"][0])
        self.assertTrue(decoder_summary["log_lines"])
        self.assertEqual(summary["confirmed_decoder_stuck_count"], 0)
        self.assertEqual(summary["decoder_stuck_risk_count"], 2)
        self.assertEqual(summary["observer_avg_cpu_percent"], 1.5)
        self.assertEqual(summary["observer_peak_memory_mb"], 43.5)
        self.assertEqual(summary["observer_primary_sampling_mode"], "standard")

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
        self.assertEqual(result["assessment"], "fail")
        self.assertEqual(len(result["release_blockers"]), 2)

    def test_missing_surface_without_hard_failure_is_inconclusive(self):
        evaluator = PlayStateEvaluator()

        result = evaluator.evaluate_global_score({
            "avg_system_cpu_percent": 30,
            "max_system_cpu_percent": 50,
            "tv_surface_locked": False,
        })

        self.assertEqual(result["score"], 100)
        self.assertEqual(result["assessment"], "inconclusive")

    def test_report_generator_renders_top_cpu_suspects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            generator = ReportGenerator(temp_dir)
            html = generator._render_root_cause(
                {},
                {
                    "total_stutter_events": 2,
                    "identified_causes": 2,
                    "confirmed_playback_causes": 1,
                    "resource_risk_events": 1,
                    "log_signal_events": 0,
                    "most_confident_cause": {
                        "root_cause_type": "CPU_CONTENTION",
                        "suspect_process": "/system/bin/init.thunder.sh x37",
                        "confidence": 81.0,
                        "evidence": {"resource_only": True},
                    },
                    "final_diagnosis": {
                        "title": "可判定为 CPU 资源竞争导致电视端卡顿",
                        "conclusion": "可判定本轮电视端卡顿主要由 /system/bin/init.thunder.sh x37 引发的 CPU 资源竞争导致。",
                        "evidence_level": "confirmed",
                        "owner": "系统/固件侧",
                        "actions": ["优先排查 /system/bin/init.thunder.sh x37 的启动/保活逻辑"],
                        "confidence": 81.0,
                    },
                    "top_suspect_processes": [
                        ["/system/bin/init.thunder.sh x37", 6],
                        ["mediaserver", 2],
                    ],
                    "process_risk_summary": [],
                    "all_causes": [],
                },
                "",
                "",
            )

        self.assertIn("根因嫌疑对象 Top", html)
        self.assertIn("/system/bin/init.thunder.sh x37", html)
        self.assertIn("命中 6 次根因候选", html)
        self.assertIn("定性结论", html)
        self.assertIn("系统/固件侧", html)

    def test_report_generator_renders_tv_event_statement_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            generator = ReportGenerator(temp_dir)
            html = generator._render_tv_stall_events({
                "tv_stall_events": [
                    {
                        "start_time": "2026-06-24 20:00:00",
                        "duration_ms": 1800,
                        "type": "TV_STALL",
                        "confirmed": True,
                        "max_frame_gap_ms": 860,
                        "min_fps": 12.0,
                        "evidence_dir": "D:/reports/tv_stall_events/20260624_200000",
                        "cpu_contention": {
                            "detected": True,
                            "top_candidate": {
                                "process": "/system/bin/init.thunder.sh",
                                "peak_cpu_percent": 88.0,
                            },
                        },
                    }
                ]
            })

        self.assertIn("电视端卡顿事件明细", html)
        self.assertIn("研发一句话结论", html)
        self.assertIn("CPU 资源竞争", html)
        self.assertIn("/system/bin/init.thunder.sh", html)

    def test_report_generator_renders_decoder_stuck_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            generator = ReportGenerator(temp_dir)
            html = generator._render_decoder_stuck_summary(
                {
                    "decoder_stuck_summary": {
                        "count": 123,
                        "max_duration_sec": 2.4,
                        "decoder_name": "OMX.rk.video.decoder.avc",
                        "sample_timestamp": "2026-06-17 12:00:05",
                        "video_fps": 0.0,
                        "decode_fps_estimate": 0.0,
                        "expected_stream_fps": 30.0,
                        "decode_drop_ratio": 1.0,
                        "system_cpu_percent": 92.0,
                        "player_cpu_percent": 5.0,
                        "diagnostic_lines": ["codec: OMX.rk.video.decoder.avc"],
                        "log_lines": ["E MediaCodec: codec reported error OMX.rk.video.decoder.avc"],
                    }
                },
                {
                    "evidence_level": "confirmed",
                    "evidence_strength": {
                        "label": "Confirmed",
                        "level": "confirmed",
                        "description": "multi-source corroborated",
                        "confidence": 95.0,
                    },
                    "conclusion": "decoder path stuck",
                    "owner": "player/decoder",
                    "suspect_process": "OMX.rk.video.decoder.avc",
                }
            )

        self.assertIn("解码输出停顿详解", html)
        self.assertIn("OMX.rk.video.decoder.avc", html)
        self.assertIn("123 次 / 2.40s", html)
        self.assertIn("codec reported error", html)
        self.assertIn("Evidence Strength", html)
        self.assertIn("Confirmed", html)

    def test_report_generator_renders_observer_overhead_card(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            generator = ReportGenerator(temp_dir)
            html = generator._render_observer_overhead_card({
                "observer_pid": 12345,
                "observer_avg_cpu_percent": 1.2,
                "observer_peak_cpu_percent": 2.8,
                "observer_avg_memory_mb": 45.5,
                "observer_peak_memory_mb": 48.0,
                "observer_primary_sampling_mode": "low_overhead",
            })

        self.assertIn("工具自身开销", html)
        self.assertIn("12345", html)
        self.assertIn("low_overhead", html)
        self.assertIn("CPU 开销较低", html)

    def test_root_cause_summary_builds_final_cpu_contention_diagnosis(self):
        analyzer = RootCauseAnalyzer(package_name="com.test.player:media")
        analyzer.record_baseline({
            "cpu_percent": 5,
            "system_cpu_percent": 35,
            "pss_mb": 100,
            "video_fps": 30,
            "top_consumers": "",
        })

        analyzer.record_stutter_event(
            {
                "timestamp": "2026-06-15 10:00:00",
                "cpu_percent": 4,
                "player_cpu_percent": 4,
                "system_cpu_percent": 97,
                "pss_mb": 105,
                "video_fps": 19,
                "video_fps_source": "surfaceflinger_display_1",
                "tv_stutter_detected": True,
            },
            "surfaceflinger(80.0%) | /system/bin/init.thunder.sh x37(12.0%)",
        )

        summary = analyzer.get_summary()
        diagnosis = summary["final_diagnosis"]

        self.assertEqual(diagnosis["title"], "可判定为 CPU 资源竞争导致电视端卡顿")
        self.assertEqual(diagnosis["owner"], "系统/固件侧")
        self.assertIn("/system/bin/init.thunder.sh x37", diagnosis["conclusion"])
        self.assertEqual(diagnosis["evidence_strength"]["label"], "Confirmed")
        self.assertEqual(diagnosis["evidence_strength"]["level"], "confirmed")

    def test_tv_perceptual_score_ignores_touchscreen_ui_jank(self):
        runner = object.__new__(TestRunner)

        result = runner._calculate_perceptual_stutter_score({
            "ui_jank_percent": 100.0,
            "log_stutter_count": 0,
        })

        self.assertEqual(result["score"], 0)
        self.assertEqual(result["level"], "流畅")
        self.assertEqual(result["details"], [])

    def test_display_auto_recommendation_prefers_active_video_display(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor._list_display_ids = MagicMock(return_value=[0, 1, 2])
        probe_map = {
            1: {
                "fps": 29.9,
                "frame_count": 120,
                "surface_name": "SurfaceView - #42",
                "candidates": ["SurfaceView - #42"],
                "probe_reason": "ok",
                "latency_mode": "surface_name",
                "max_frame_gap_ms": 33.3,
            },
            2: {
                "fps": 0.0,
                "frame_count": 0,
                "surface_name": "",
                "candidates": [],
                "probe_reason": "latency_zero_frames",
                "latency_mode": "display_id",
                "max_frame_gap_ms": 0.0,
            },
        }
        monitor.collect_tv_frame_metrics = MagicMock(
            side_effect=lambda display_id, track_progress=False: probe_map.get(display_id, {})
        )

        detected = monitor._detect_tv_display_id()

        self.assertEqual(detected, 1)
        self.assertTrue(monitor._tv_display_verified)
        self.assertEqual(
            monitor._tv_display_verification_reason,
            "configured_display_confirmed_by_video_probe",
        )
        self.assertEqual(monitor._tv_display_recommendation["display_id"], 1)
        self.assertEqual(len(monitor._tv_display_probe_details), 2)

    def test_measure_video_fps_marks_display_fallback_source(self):
        monitor = PerformanceMonitor(MagicMock(), "com.test.player:media")
        monitor.collect_tv_frame_metrics = MagicMock(return_value={
            "fps": 30.0,
            "surface_name": "",
            "latency_mode": "display_id",
        })

        fps = monitor._measure_video_fps(display_id=1, mpp_stats={})

        self.assertEqual(fps, 30.0)
        self.assertEqual(
            monitor._last_video_fps_source,
            "surfaceflinger_display_1_fallback",
        )

    def test_executive_statement_describes_risk_only_case(self):
        runner = object.__new__(TestRunner)

        statement = runner._build_executive_statement(
            {
                "tv_stall_count": 0,
                "tv_stall_risk_count": 3,
                "decoder_stuck_risk_count": 2,
                "avg_system_cpu_percent": 20.0,
                "avg_player_cpu_percent": 4.0,
                "tv_surface_locked": False,
                "tv_display_recommendation": {
                    "display_id": 1,
                    "reason": "surface_locked_with_active_frames",
                },
            },
            {"final_diagnosis": {}},
        )

        self.assertIn("风险样本", statement)
        self.assertIn("Display 1", statement)


if __name__ == "__main__":
    unittest.main()
