import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from modules.player_stress.core.tv_playback_watcher import TvPlaybackWatcher


class TvPlaybackWatcherTests(unittest.TestCase):
    def test_stall_event_starts_and_recovers(self):
        events = []
        adb = MagicMock()
        adb.get_cpu_evidence.return_value = {}
        monitor = MagicMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            watcher = TvPlaybackWatcher(
                adb,
                monitor,
                temp_dir,
                config={
                    "tv_stall_start_confirmations": 2,
                    "tv_stall_recovery_confirmations": 2,
                },
                event_callback=events.append,
            )
            bad = {
                "audio_active": True,
                "ignore_video_metrics": False,
                "surface_name": "Display1 VideoSurface",
                "fps": 0,
                "max_frame_gap_ms": 400,
                "frame_advanced": False,
            }
            good = {
                "audio_active": True,
                "ignore_video_metrics": False,
                "surface_name": "Display1 VideoSurface",
                "fps": 30,
                "max_frame_gap_ms": 35,
                "frame_advanced": True,
            }

            watcher.process_sample(bad, now=10.0)
            watcher.process_sample(bad, now=10.5)
            watcher.process_sample(good, now=11.0)
            watcher.process_sample(good, now=11.5)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["duration_ms"], 1000)
            self.assertEqual(events[0]["display_id"], 1)
            event_json = os.path.join(events[0]["evidence_dir"], "event.json")
            self.assertTrue(os.path.exists(event_json))
            with open(event_json, encoding="utf-8") as file:
                stored = json.load(file)
            self.assertEqual(stored["recovery_reason"], "playback_recovered")

    def test_buffering_window_does_not_start_event(self):
        events = []
        with tempfile.TemporaryDirectory() as temp_dir:
            watcher = TvPlaybackWatcher(
                MagicMock(),
                MagicMock(),
                temp_dir,
                config={"tv_stall_start_confirmations": 2},
                event_callback=events.append,
            )
            sample = {
                "audio_active": True,
                "ignore_video_metrics": True,
                "surface_name": "Display1 VideoSurface",
                "fps": 0,
                "max_frame_gap_ms": 500,
                "frame_advanced": False,
            }
            watcher.process_sample(sample, now=1.0)
            watcher.process_sample(sample, now=1.5)

            self.assertEqual(events, [])
            self.assertIsNone(watcher._active_event)

    def test_evidence_contains_before_during_and_after_images(self):
        events = []
        adb = MagicMock()

        def write_screenshot(path, display_id=None):
            with open(path, "wb") as file:
                file.write(b"png")

        adb.take_screenshot.side_effect = write_screenshot
        adb.get_cpu_evidence.return_value = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            watcher = TvPlaybackWatcher(
                adb,
                MagicMock(),
                temp_dir,
                config={
                    "tv_stall_start_confirmations": 2,
                    "tv_stall_recovery_confirmations": 1,
                },
                event_callback=events.append,
            )
            watcher._capture_periodic_screenshot(2.1)
            bad = {
                "audio_active": True,
                "ignore_video_metrics": False,
                "surface_name": "Display1 VideoSurface",
                "fps": 0,
                "max_frame_gap_ms": 300,
                "frame_advanced": False,
            }
            good = dict(bad, fps=30, max_frame_gap_ms=33, frame_advanced=True)

            watcher.process_sample(bad, now=2.0)
            watcher.process_sample(bad, now=2.5)
            watcher.process_sample(good, now=3.0)

            event_dir = events[0]["evidence_dir"]
            self.assertTrue(os.path.exists(os.path.join(event_dir, "before.png")))
            self.assertTrue(os.path.exists(os.path.join(event_dir, "during.png")))
            self.assertTrue(os.path.exists(os.path.join(event_dir, "after.png")))

    def test_cpu_contention_is_correlated_with_stall(self):
        events = []
        adb = MagicMock()
        adb.get_cpu_evidence.side_effect = [
            {
                "process_count": 100,
                "top_processes": [
                    {"name": "com.noisy.worker", "cpu_percent": 5},
                ],
                "raw_top": "before",
            },
            {
                "process_count": 145,
                "top_processes": [
                    {"name": "com.noisy.worker", "cpu_percent": 45},
                ],
                "raw_top": "during",
            },
            {
                "process_count": 102,
                "top_processes": [
                    {"name": "com.noisy.worker", "cpu_percent": 6},
                ],
                "raw_top": "after",
            },
        ]
        monitor = MagicMock()
        monitor.package_name = "com.test.player:media"
        with tempfile.TemporaryDirectory() as temp_dir:
            watcher = TvPlaybackWatcher(
                adb,
                monitor,
                temp_dir,
                config={
                    "tv_stall_start_confirmations": 2,
                    "tv_stall_recovery_confirmations": 1,
                },
                event_callback=events.append,
            )
            watcher._capture_cpu_on_schedule(2.0)
            bad = {
                "audio_active": True,
                "ignore_video_metrics": False,
                "surface_name": "Display1 VideoSurface",
                "fps": 0,
                "max_frame_gap_ms": 400,
                "frame_advanced": False,
            }
            good = dict(bad, fps=30, max_frame_gap_ms=33, frame_advanced=True)

            watcher.process_sample(bad, now=3.0)
            watcher.process_sample(bad, now=3.5)
            watcher.process_sample(good, now=4.0)

            contention = events[0]["cpu_contention"]
            self.assertTrue(contention["detected"])
            self.assertEqual(
                contention["top_candidate"]["process"],
                "com.noisy.worker",
            )
            self.assertGreaterEqual(
                contention["top_candidate"]["confidence"],
                90,
            )
            event_dir = events[0]["evidence_dir"]
            self.assertTrue(os.path.exists(os.path.join(event_dir, "cpu_before.json")))
            self.assertTrue(os.path.exists(os.path.join(event_dir, "cpu_during.jsonl")))
            self.assertTrue(os.path.exists(os.path.join(event_dir, "cpu_after.json")))

    def test_player_process_is_not_external_cpu_candidate(self):
        monitor = MagicMock()
        monitor.package_name = "com.test.player:media"
        with tempfile.TemporaryDirectory() as temp_dir:
            watcher = TvPlaybackWatcher(MagicMock(), monitor, temp_dir)
            event = {
                "cpu_before": [{
                    "top_processes": [
                        {"name": "com.test.player", "cpu_percent": 5},
                    ],
                }],
                "cpu_during": [{
                    "top_processes": [
                        {"name": "com.test.player", "cpu_percent": 80},
                    ],
                }],
                "cpu_after": {
                    "top_processes": [
                        {"name": "com.test.player", "cpu_percent": 5},
                    ],
                },
            }

            result = watcher._analyze_cpu_contention(event)

            self.assertFalse(result["detected"])


if __name__ == "__main__":
    unittest.main()
