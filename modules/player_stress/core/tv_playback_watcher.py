import json
import os
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Dict, Optional


class TvPlaybackWatcher:
    """Track Display 1 stalls independently from the main performance loop."""

    def __init__(
        self,
        adb,
        performance_monitor,
        output_dir: str,
        config: Optional[Dict] = None,
        event_callback: Optional[Callable[[Dict], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        log_monitor=None,
    ):
        config = config or {}
        self.adb = adb
        self.monitor = performance_monitor
        self.display_id = int(config.get("tv_display_id", 1))
        self.poll_interval = max(0.2, float(config.get("tv_poll_interval_seconds", 0.5)))
        self.screenshot_interval = max(
            1.0,
            float(config.get("tv_evidence_screenshot_interval_seconds", 2.0)),
        )
        self.micro_stall_threshold_ms = max(
            300.0,
            float(config.get("tv_micro_stall_frame_gap_threshold_ms", 250.0)),
        )
        self.stall_threshold_ms = max(
            self.micro_stall_threshold_ms,
            float(config.get("tv_stall_frame_gap_threshold_ms", 800.0)),
        )
        self.start_confirmations = max(
            2,
            int(config.get("tv_stall_start_confirmations", 3)),
        )
        self.recovery_confirmations = max(
            1,
            int(config.get("tv_stall_recovery_confirmations", 2)),
        )
        self.cpu_baseline_interval = max(
            1.0,
            float(config.get("tv_cpu_baseline_interval_seconds", 2.0)),
        )
        self.cpu_during_interval = max(
            0.5,
            float(config.get("tv_cpu_during_interval_seconds", 1.0)),
        )
        self.confirm_gap_ms = max(
            self.stall_threshold_ms,
            float(config.get("tv_stall_confirm_frame_gap_threshold_ms", 1000.0)),
        )
        self.confirm_duration_ms = max(
            500.0,
            float(config.get("tv_stall_confirm_duration_ms", 1500.0)),
        )
        self.max_risk_duration_ms = max(
            self.confirm_duration_ms,
            float(config.get("tv_stall_risk_auto_finish_ms", 12000.0)),
        )
        self.log_window_seconds = max(
            3.0,
            float(config.get("tv_event_log_window_seconds", 10.0)),
        )
        self.event_callback = event_callback
        self.log_callback = log_callback
        self.log_monitor = log_monitor
        self.evidence_root = os.path.join(output_dir, "tv_stall_events")
        os.makedirs(self.evidence_root, exist_ok=True)

        self._stop_event = threading.Event()
        self._thread = None
        self._bad_samples = 0
        self._healthy_samples = 0
        self._active_event = None
        self._latest_screenshot = ""
        self._last_screenshot_time = 0.0
        self._cpu_baselines = deque(maxlen=5)
        self._last_cpu_baseline_time = 0.0
        self._last_cpu_during_time = 0.0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="tv-playback-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, wait: bool = True):
        self._stop_event.set()
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(2.0, self.poll_interval * 3))
        if self._active_event:
            self._finish_event(time.time(), "watcher_stopped")

    def _run(self):
        self._log(
            f"[TV Watcher] Display {self.display_id} monitoring started "
            f"({self.poll_interval:.1f}s interval)"
        )
        while not self._stop_event.is_set():
            started = time.time()
            try:
                sample = self.monitor.collect_tv_frame_metrics(self.display_id)
                sample["audio_active"] = bool(self.adb.is_audio_active())
                sample["ignore_video_metrics"] = bool(
                    self.monitor.is_ignoring_video_metrics()
                )
                self.monitor.record_tv_frame_gap_sample(sample, now=started)
                self.process_sample(sample, now=started)
                self._capture_periodic_screenshot(started)
                self._capture_cpu_on_schedule(started)
            except Exception as exc:
                self._log(f"[TV Watcher] sample failed: {exc}")
            elapsed = time.time() - started
            self._stop_event.wait(max(0.05, self.poll_interval - elapsed))

    def process_sample(self, sample: Dict, now: Optional[float] = None):
        now = float(now if now is not None else time.time())
        audio_active = bool(sample.get("audio_active", False))
        ignored = bool(sample.get("ignore_video_metrics", False))
        surface = str(sample.get("surface_name", "") or "")
        fps = float(sample.get("fps", 0) or 0.0)
        max_gap_ms = float(sample.get("max_frame_gap_ms", 0) or 0.0)
        frame_advanced = bool(sample.get("frame_advanced", False))

        reason = ""
        if not ignored and audio_active and surface:
            if max_gap_ms >= self.micro_stall_threshold_ms:
                reason = f"frame_gap_{max_gap_ms:.1f}ms"
            elif not frame_advanced and max_gap_ms >= 300.0:
                reason = "surface_not_advancing"

        if reason:
            self._bad_samples += 1
            self._healthy_samples = 0
            if self._active_event is None and self._bad_samples >= self.start_confirmations:
                self._start_event(now, sample, reason)
            elif self._active_event:
                self._update_event(sample, reason, now)
                self._maybe_force_finish_risk_event(now, sample)
            return

        self._bad_samples = 0
        if not self._active_event:
            self._healthy_samples = 0
            return

        self._healthy_samples += 1
        if self._healthy_samples >= self.recovery_confirmations:
            recovery = "metrics_ignored" if ignored else "playback_recovered"
            self._finish_event(now, recovery)

    def _maybe_force_finish_risk_event(self, now: float, sample: Optional[Dict] = None):
        event = self._active_event
        if not event or bool(event.get("confirmed", False)):
            return

        duration_ms = int(
            max(0.0, now - float(event.get("start_timestamp", now) or now)) * 1000
        )
        if duration_ms < self.max_risk_duration_ms:
            return

        self._refresh_event_assessment(sample, now)
        if bool(event.get("confirmed", False)):
            return

        self._finish_event(now, "risk_timeout_no_corroboration")

    def _start_event(self, now: float, sample: Dict, reason: str):
        event_id = datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S_%f")
        event_dir = os.path.join(self.evidence_root, event_id)
        os.makedirs(event_dir, exist_ok=True)
        self._active_event = {
            "event_id": event_id,
            "type": "TV_STALL",
            "display_id": self.display_id,
            "start_timestamp": now,
            "start_time": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
            "end_timestamp": None,
            "end_time": None,
            "duration_ms": 0,
            "surface_name": sample.get("surface_name", ""),
            "reason": reason,
            "max_frame_gap_ms": float(sample.get("max_frame_gap_ms", 0) or 0.0),
            "min_fps": float(sample.get("fps", 0) or 0.0),
            "sample_count": 1,
            "evidence_dir": event_dir,
            "cpu_before": list(self._cpu_baselines),
            "cpu_during": [],
            "cpu_after": None,
            "cpu_contention": None,
        }
        self._copy_latest_screenshot(event_dir, "before.png")
        self._capture_screenshot(os.path.join(event_dir, "during.png"))
        self._write_cpu_before_files()
        self._capture_cpu_during(now, force=True)
        self._refresh_event_assessment(sample, now)
        self._write_event_json()
        self._write_time_window_evidence(now)
        self._log(f"[TV Stall] started: {reason}")

    def _update_event(self, sample: Dict, reason: str, now: float):
        event = self._active_event
        event["sample_count"] += 1
        event["reason"] = reason
        event["max_frame_gap_ms"] = max(
            float(event.get("max_frame_gap_ms", 0) or 0.0),
            float(sample.get("max_frame_gap_ms", 0) or 0.0),
        )
        fps = float(sample.get("fps", 0) or 0.0)
        if fps > 0:
            current_min = float(event.get("min_fps", 0) or 0.0)
            event["min_fps"] = fps if current_min <= 0 else min(current_min, fps)
        self._capture_cpu_during(now)
        self._refresh_event_assessment(sample, now)
        self._write_event_json()
        self._write_time_window_evidence(now)

    def _finish_event(self, now: float, recovery_reason: str):
        event = self._active_event
        if not event:
            return
        event["end_timestamp"] = now
        event["end_time"] = datetime.fromtimestamp(now).isoformat(timespec="milliseconds")
        event["duration_ms"] = int(max(0.0, now - event["start_timestamp"]) * 1000)
    def _finish_event(self, now: float, recovery_reason: str):
        event = self._active_event
        if not event:
            return
        event["end_timestamp"] = now
        event["end_time"] = datetime.fromtimestamp(now).isoformat(timespec="milliseconds")
        event["duration_ms"] = int(max(0.0, now - event["start_timestamp"]) * 1000)
        event["recovery_reason"] = recovery_reason
        self._capture_screenshot(os.path.join(event["evidence_dir"], "after.png"))
        event["cpu_after"] = self._collect_cpu_evidence()
        self._write_json_file("cpu_after.json", event["cpu_after"])
        self._write_text_file(
            "top_after.txt",
            (event["cpu_after"] or {}).get("raw_top", ""),
        )
        event["cpu_contention"] = self._analyze_cpu_contention(event)
        self._refresh_event_assessment(None, now)
        self._assess_visual_motion(event)
        if bool(event.get("ignored_as_static_content", False)):
            event["type"] = "TV_STALL_IGNORED"
        else:
            event["type"] = "TV_STALL" if bool(event.get("confirmed", False)) else "TV_STALL_RISK"
        self._write_time_window_evidence(now, force=True)
        self._write_event_summary()
        self._write_event_json()
        if self.event_callback:
            self.event_callback(dict(event))
        self._log(f"[TV Stall] ended after {event['duration_ms']}ms")
        self._active_event = None
        self._healthy_samples = 0
        self._healthy_samples = 0
    def _latest_monitor_snapshot(self) -> Dict:
        history = getattr(self.monitor, "history", None) or []
        if not history:
            return {}
        latest = history[-1]
        return latest if isinstance(latest, dict) else {}

    def _refresh_event_assessment(
        self,
        sample: Optional[Dict],
        now: float,
    ):
        event = self._active_event
        if not event:
            return
        latest = self._latest_monitor_snapshot()
        sample = sample or {}
        duration_ms = int(
            max(0.0, now - float(event.get("start_timestamp", now) or now)) * 1000
        )
        event["duration_ms"] = duration_ms

        max_gap_ms = float(
            sample.get("max_frame_gap_ms", event.get("max_frame_gap_ms", 0)) or 0.0
        )
        video_fps = float(sample.get("fps", latest.get("video_fps", 0)) or 0.0)
        frame_advanced = bool(sample.get("frame_advanced", True))
        expected_fps = float(
            latest.get("expected_stream_fps", sample.get("expected_stream_fps", 30.0)) or 30.0
        )
        system_cpu = float(latest.get("system_cpu_percent", 0) or 0.0)
        player_cpu = float(
            latest.get("player_cpu_percent", latest.get("cpu_percent", 0)) or 0.0
        )
        decode_drop_ratio = float(latest.get("decode_drop_ratio", 0) or 0.0)
        decode_reliable = bool(latest.get("mpp_work_count_reliable", False))
        decoder_confirmed = bool(latest.get("decoder_stuck_confirmed", False))
        log_signal = int(latest.get("log_stutter_delta", 0) or 0) > 0
        cpu_pressure = bool(latest.get("system_cpu_pressure", False)) or (
            system_cpu >= 85.0 and player_cpu <= 15.0
        )
        fps_low = bool(video_fps > 0 and video_fps < max(24.0, expected_fps * 0.85))
        micro_gap = bool(max_gap_ms >= self.micro_stall_threshold_ms)
        perceptible_gap = bool(max_gap_ms >= self.stall_threshold_ms)
        severe_gap = bool(max_gap_ms >= self.confirm_gap_ms)
        surface_not_advancing = (
            str(sample.get("reason", event.get("reason", "")) or "") == "surface_not_advancing"
            or not frame_advanced
        )
        decode_drop_confirmed = bool(decode_reliable and decode_drop_ratio >= 0.15)
        surface_locked = bool(
            latest.get("tv_surface_name", sample.get("tv_surface_name", "")) or ""
        )
        physical_surface_freeze = bool(
            surface_locked and max_gap_ms >= 2000.0 and frame_advanced
        )

        corroborations = []
        if decoder_confirmed:
            corroborations.append("decoder_confirmed")
        if cpu_pressure:
            corroborations.append("cpu_pressure")
        if log_signal:
            corroborations.append("log_signal")
        if fps_low:
            corroborations.append("fps_low")
        if decode_drop_confirmed:
            corroborations.append("decode_drop")

        missing_evidence = []
        promotion_hints = []
        if duration_ms < self.confirm_duration_ms:
            missing_evidence.append(
                f"事件持续 {duration_ms:.0f} ms，不足确认阈值 {self.confirm_duration_ms:.0f} ms"
            )
            promotion_hints.append(
                f"建议事件持续达到 {self.confirm_duration_ms:.0f} ms 以上再判定"
            )
        if not decoder_confirmed:
            missing_evidence.append("缺少解码器卡死确认证据")
            promotion_hints.append("补充 MPP / decoder 卡死确认证据")
        if not decode_drop_confirmed:
            missing_evidence.append("缺少解码丢帧证据")
            promotion_hints.append("关注 decode_drop_ratio 是否超过阈值")
        if not fps_low:
            missing_evidence.append("缺少有效电视端 FPS 证据")
            promotion_hints.append("对比 video_fps 与 expected_stream_fps 是否匹配")
        if not log_signal:
            missing_evidence.append("缺少 droppedFrames/underrun/buffer starvation 日志信号")
            promotion_hints.append("建议持续观测 10 秒以上再确认")
        if not cpu_pressure:
            missing_evidence.append("缺少整机 CPU 压力证据")
            promotion_hints.append("查看 CPU Top 进程是否存在异常占用")
        if perceptible_gap and len(corroborations) < 1:
            missing_evidence.append("感知级卡顿至少需要 1 个旁证信号")
        if severe_gap and len(corroborations) < 2:
            missing_evidence.append("严重卡顿至少需要 2 个旁证信号")
        if surface_not_advancing and len(corroborations) < 1:
            missing_evidence.append("Surface 未推进且缺少旁证信号")

        if physical_surface_freeze and "physical_surface_freeze" not in corroborations:
            corroborations.append("physical_surface_freeze")

        confirmed = bool(
            physical_surface_freeze
            or decoder_confirmed
            or (
                duration_ms >= self.confirm_duration_ms
                and (
                    (surface_not_advancing and perceptible_gap and len(corroborations) >= 1)
                    or (severe_gap and len(corroborations) >= 2)
                )
            )
        )
        if confirmed:
            confidence_level = "confirmed"
        elif perceptible_gap or surface_not_advancing:
            confidence_level = "risk"
        else:
            confidence_level = "micro"

        if confirmed:
            assessment_reason = "已形成多源证据闭环，可按确认级电视端卡顿处理"
        elif severe_gap or surface_not_advancing or perceptible_gap:
            assessment_reason = "仅检测到帧间隔/Surface 异常，缺少解码、日志或CPU共振证据，先按风险提示"
        else:
            assessment_reason = "检测到轻微播放波动，暂不建议按肉眼卡顿处理"

        event["confirmed"] = confirmed
        event["confidence_level"] = confidence_level
        event["perceptual_level"] = (
            "confirmed" if confirmed else
            "risk" if (perceptible_gap or surface_not_advancing) else
            "micro"
        )
        event["assessment_reason"] = assessment_reason
        event["corroboration_count"] = len(corroborations)
        event["corroboration_signals"] = corroborations
        event["confirmation_gap_reasons"] = missing_evidence[:6] if not confirmed else []
        event["promotion_hints"] = promotion_hints[:5] if not confirmed else []
        event["evidence_flags"] = {
            "micro_frame_gap": micro_gap,
            "perceptible_frame_gap": perceptible_gap,
            "severe_frame_gap": severe_gap,
            "surface_not_advancing": surface_not_advancing,
            "physical_surface_freeze": physical_surface_freeze,
            "decoder_confirmed": decoder_confirmed,
            "cpu_pressure": cpu_pressure,
            "fps_low": fps_low,
            "decode_drop_confirmed": decode_drop_confirmed,
            "log_signal": log_signal,
            "mpp_reliable": decode_reliable,
        }

    def _assess_visual_motion(self, event: Dict):
        if not event:
            return
        event_dir = str(event.get("evidence_dir", "") or "")
        before_path = os.path.join(event_dir, "before.png")
        during_path = os.path.join(event_dir, "during.png")
        after_path = os.path.join(event_dir, "after.png")
        before_during = self._compare_screenshot_similarity(before_path, during_path)
        during_after = self._compare_screenshot_similarity(during_path, after_path)
        event["screen_motion_assessment"] = {
            "before_during": before_during,
            "during_after": during_after,
        }
        static_pairs = 0
        for item in (before_during, during_after):
            if item.get("available") and item.get("is_static"):
                static_pairs += 1
        corroborations = list(event.get("corroboration_signals", []) or [])
        has_strong_playback_evidence = bool(
            event.get("confirmed", False)
            or "decoder_confirmed" in corroborations
            or "decode_drop" in corroborations
            or "fps_low" in corroborations
        )
        if static_pairs >= 2 and not has_strong_playback_evidence:
            event["ignored_as_static_content"] = True
            event["assessment_reason"] = (
                "前后多帧对比均为静态画面，且缺少解码/丢帧/FPS 等播放链路直证，按静态内容误判剔除"
            )
        else:
            event["ignored_as_static_content"] = False

    def _compare_screenshot_similarity(self, path_a: str, path_b: str) -> Dict:
        result = {
            "available": False,
            "avg_diff": None,
            "is_static": False,
        }
        if not path_a or not path_b or not os.path.exists(path_a) or not os.path.exists(path_b):
            return result
        try:
            from PIL import Image, ImageChops, ImageStat

            img_a = Image.open(path_a).convert("RGB")
            img_b = Image.open(path_b).convert("RGB")
            if img_a.size != img_b.size:
                return result
            diff = ImageChops.difference(img_a, img_b)
            stat = ImageStat.Stat(diff)
            avg_diff = sum(stat.mean) / max(1, len(stat.mean))
            result["available"] = True
            result["avg_diff"] = round(float(avg_diff), 4)
            result["is_static"] = avg_diff < 1.0
            return result
        except Exception:
            return result

    def _capture_cpu_on_schedule(self, now: float):
        if self._active_event:
            self._capture_cpu_during(now)
            return
        if now - self._last_cpu_baseline_time < self.cpu_baseline_interval:
            return
        evidence = self._collect_cpu_evidence()
        if evidence:
            self._cpu_baselines.append(evidence)
            self._last_cpu_baseline_time = now

    def _capture_cpu_during(self, now: float, force: bool = False):
        if not self._active_event:
            return
        if (
            not force
            and now - self._last_cpu_during_time < self.cpu_during_interval
        ):
            return
        evidence = self._collect_cpu_evidence()
        if not evidence:
            return
        self._active_event["cpu_during"].append(evidence)
        self._last_cpu_during_time = now
        self._append_json_line("cpu_during.jsonl", evidence)
        self._write_text_file(
            "top_during.txt",
            evidence.get("raw_top", ""),
            append=True,
        )

    def _collect_cpu_evidence(self) -> Dict:
        try:
            evidence = self.adb.get_cpu_evidence(limit=10)
            return evidence if isinstance(evidence, dict) else {}
        except Exception:
            return {}

    def _write_cpu_before_files(self):
        if not self._active_event:
            return
        before = list(self._active_event.get("cpu_before", []))
        self._write_json_file("cpu_before.json", before)
        raw_sections = []
        for item in before:
            if item.get("raw_top"):
                raw_sections.append(
                    f"===== {item.get('time', '')} =====\n{item['raw_top']}"
                )
        self._write_text_file("top_before.txt", "\n\n".join(raw_sections))

    def _analyze_cpu_contention(self, event: Dict) -> Dict:
        baseline = event.get("cpu_before") or []
        during = event.get("cpu_during") or []
        after = event.get("cpu_after") or {}
        baseline_cpu = self._average_process_cpu(baseline)
        during_peak = self._peak_process_cpu(during)
        after_cpu = self._snapshot_process_cpu(after)

        excluded = {
            "top",
            "system_server",
            "surfaceflinger",
            "audioserver",
        }
        candidates = []
        package_name = str(getattr(self.monitor, "package_name", "") or "")
        main_package = package_name.split(":")[0]
        for name, peak_cpu in during_peak.items():
            lower = name.lower()
            if lower in excluded or main_package in name or package_name in name:
                continue
            base_cpu = float(baseline_cpu.get(name, 0.0))
            recovered_cpu = float(after_cpu.get(name, 0.0))
            surge = peak_cpu - base_cpu
            recovery_drop = peak_cpu - recovered_cpu
            if peak_cpu < 20 or surge < 15:
                continue
            confidence = 55.0 + min(25.0, surge) + min(
                15.0,
                max(0.0, recovery_drop) * 0.5,
            )
            candidates.append({
                "process": name,
                "baseline_cpu_percent": round(base_cpu, 2),
                "peak_cpu_percent": round(peak_cpu, 2),
                "after_cpu_percent": round(recovered_cpu, 2),
                "cpu_surge_percent": round(surge, 2),
                "recovery_drop_percent": round(recovery_drop, 2),
                "confidence": round(min(95.0, confidence), 1),
            })

        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        top = candidates[0] if candidates else None
        return {
            "detected": bool(top),
            "root_cause_type": "CPU_CONTENTION" if top else "UNKNOWN",
            "top_candidate": top,
            "candidates": candidates[:5],
            "baseline_process_count": self._average_value(
                baseline,
                "process_count",
            ),
            "during_peak_process_count": max(
                [int(item.get("process_count", 0) or 0) for item in during]
                or [0]
            ),
            "after_process_count": int(after.get("process_count", 0) or 0),
        }

    def _average_process_cpu(self, snapshots) -> Dict[str, float]:
        totals = {}
        counts = {}
        for snapshot in snapshots:
            for process in snapshot.get("top_processes", []):
                name = str(process.get("name", "") or "")
                if not name:
                    continue
                totals[name] = totals.get(name, 0.0) + float(
                    process.get("cpu_percent", 0) or 0.0
                )
                counts[name] = counts.get(name, 0) + 1
        return {
            name: totals[name] / counts[name]
            for name in totals
            if counts.get(name, 0) > 0
        }

    def _peak_process_cpu(self, snapshots) -> Dict[str, float]:
        peaks = {}
        for snapshot in snapshots:
            for process in snapshot.get("top_processes", []):
                name = str(process.get("name", "") or "")
                cpu = float(process.get("cpu_percent", 0) or 0.0)
                if name:
                    peaks[name] = max(peaks.get(name, 0.0), cpu)
        return peaks

    def _snapshot_process_cpu(self, snapshot) -> Dict[str, float]:
        return {
            str(process.get("name", "") or ""): float(
                process.get("cpu_percent", 0) or 0.0
            )
            for process in (snapshot or {}).get("top_processes", [])
            if process.get("name")
        }

    def _average_value(self, snapshots, key: str) -> float:
        values = [
            float(snapshot.get(key, 0) or 0.0)
            for snapshot in snapshots
        ]
        return round(sum(values) / len(values), 2) if values else 0.0

    def _capture_screenshot(self, path: str) -> bool:
        try:
            self.adb.take_screenshot(path, display_id=self.display_id)
            return os.path.exists(path) and os.path.getsize(path) > 0
        except Exception:
            return False

    def _capture_periodic_screenshot(self, now: float):
        if now - self._last_screenshot_time < self.screenshot_interval:
            return
        path = os.path.join(self.evidence_root, "_latest_display_1.png")
        if self._capture_screenshot(path):
            self._latest_screenshot = path
            self._last_screenshot_time = now

    def _copy_latest_screenshot(self, event_dir: str, filename: str):
        if not self._latest_screenshot or not os.path.exists(self._latest_screenshot):
            return
        try:
            shutil.copy2(
                self._latest_screenshot,
                os.path.join(event_dir, filename),
            )
        except Exception:
            pass

    def _write_event_json(self):
        if not self._active_event:
            return
        path = os.path.join(self._active_event["evidence_dir"], "event.json")
        with open(path, "w", encoding="utf-8") as file:
            json.dump(self._active_event, file, ensure_ascii=False, indent=2)

    def _write_time_window_evidence(self, now: float, force: bool = False):
        if not self._active_event or not self.log_monitor:
            return
        event = self._active_event
        last_written = float(event.get("_last_log_window_write_time", 0.0) or 0.0)
        if not force and last_written > 0 and now - last_written < 1.5:
            return

        start_ts = float(event.get("start_timestamp", now) or now) - self.log_window_seconds
        end_ts = now + self.log_window_seconds
        window_logs = []
        decoder_logs = []
        try:
            if hasattr(self.log_monitor, "get_time_window_logs"):
                window_logs = list(self.log_monitor.get_time_window_logs(start_ts, end_ts))
            if hasattr(self.log_monitor, "get_time_window_decoder_events"):
                decoder_logs = list(self.log_monitor.get_time_window_decoder_events(start_ts, end_ts))
        except Exception as exc:
            self._log(f"[TV Stall] collect time-window logs failed: {exc}")
            return

        window_summary = {
            "window_start": datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
            "window_end": datetime.fromtimestamp(end_ts).isoformat(timespec="seconds"),
            "captured_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "log_line_count": len(window_logs),
            "decoder_event_count": len(decoder_logs),
        }
        self._write_json_file("time_window_summary.json", window_summary)
        self._write_json_file("decoder_window.json", decoder_logs)
        self._write_text_file(
            "time_window_logcat.txt",
            "\n".join(
                f"[{item.get('wall_time', '')}] {item.get('line', '')}".rstrip()
                for item in window_logs
            ),
        )
        self._write_text_file(
            "decoder_window.txt",
            "\n".join(
                f"[{datetime.fromtimestamp(float(item.get('time', 0) or 0.0)).strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{item.get('pattern', '')} | {item.get('line', '')}".rstrip()
                for item in decoder_logs
            ),
        )
        event["_last_log_window_write_time"] = now

    def _write_event_summary(self):
        if not self._active_event:
            return
        event = self._active_event
        cpu_candidate = ((event.get("cpu_contention") or {}).get("top_candidate") or {})
        summary_lines = [
            f"Event type: {event.get('type', 'TV_STALL')}",
            f"Start time: {event.get('start_time', '')}",
            f"End time: {event.get('end_time', '')}",
            f"Duration: {event.get('duration_ms', 0)} ms",
            f"Assessment: {'confirmed' if bool(event.get('confirmed', False)) else 'risk'}",
            f"Reason: {event.get('assessment_reason', '') or event.get('reason', '')}",
            f"Max frame gap: {float(event.get('max_frame_gap_ms', 0) or 0.0):.1f} ms",
            f"Min FPS: {float(event.get('min_fps', 0) or 0.0):.1f}",
            f"Signals: {', '.join(event.get('corroboration_signals', []) or []) or '-'}",
            f"CPU suspect: {cpu_candidate.get('process', '-')}",
            f"CPU peak: {cpu_candidate.get('peak_cpu_percent', '-')}",
        ]
        gap_reasons = list(event.get("confirmation_gap_reasons", []) or [])
        promotion_hints = list(event.get("promotion_hints", []) or [])
        if gap_reasons:
            summary_lines.append("Confirmation gaps:")
            summary_lines.extend(f"- {item}" for item in gap_reasons[:5])
        if promotion_hints:
            summary_lines.append("Promotion hints:")
            summary_lines.extend(f"- {item}" for item in promotion_hints[:4])
        summary_lines.append(
            "建议查看 / Check: time_window_logcat.txt / decoder_window.txt / top_before.txt / top_after.txt"
        )
        self._write_text_file("event_summary.txt", "\n".join(summary_lines))

    def _write_json_file(self, filename: str, data):
        if not self._active_event:
            return
        path = os.path.join(self._active_event["evidence_dir"], filename)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

    def _append_json_line(self, filename: str, data: Dict):
        if not self._active_event:
            return
        path = os.path.join(self._active_event["evidence_dir"], filename)
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False) + "\n")

    def _write_text_file(
        self,
        filename: str,
        text: str,
        append: bool = False,
    ):
        if not self._active_event:
            return
        path = os.path.join(self._active_event["evidence_dir"], filename)
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as file:
            if append and os.path.getsize(path) > 0:
                file.write("\n\n")
            file.write(text or "")

    def _log(self, message: str):
        if self.log_callback:
            try:
                self.log_callback(message)
            except Exception:
                pass
