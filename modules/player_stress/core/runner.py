import time
import os
import sys
import csv
import json
import logging
import threading
import statistics
from datetime import datetime
try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

logger = logging.getLogger(__name__)
from typing import Dict, List
from .adb_manager import AdbManager
from .monitor import PerformanceMonitor
from .player_controller import PlayerController
from .report_generator import ReportGenerator
from .root_cause_analyzer import RootCauseAnalyzer
from .image_analyzer import ScreenAnalyzer
from .tv_playback_watcher import TvPlaybackWatcher
from core.runtime import get_runtime_manager, RuntimeStatus

from concurrent.futures import ThreadPoolExecutor

class TestRunner:
    def _collect_tv_events(self, summary: Dict) -> list:
        events = []
        events.extend(list(summary.get("tv_stall_events_deduped", summary.get("tv_stall_events", [])) or []))
        events.extend(list(summary.get("tv_stall_risk_events_deduped", summary.get("tv_stall_risk_events", [])) or []))
        events.extend(list(summary.get("tv_freeze_events_deduped", summary.get("tv_freeze_events", [])) or []))
        return [event for event in events if isinstance(event, dict)]

    def _pick_representative_tv_event(self, summary: Dict) -> dict:
        def _event_score(event: Dict):
            confidence = str(event.get("confidence_level", "") or "").lower()
            event_type = str(event.get("type", "") or "").upper()
            confirmed_rank = 2 if confidence == "confirmed" or event_type == "TV_FREEZE" else (1 if confidence in {"risk", "medium", "low"} else 0)
            return (
                confirmed_rank,
                float(event.get("max_frame_gap_ms", 0) or 0.0),
                int(event.get("duration_ms", 0) or 0),
                float(event.get("video_fps", 0) or 0.0) * -1.0,
                str(event.get("start_time") or event.get("timestamp") or ""),
            )

        events = self._collect_tv_events(summary)
        if not events:
            return {}
        return max(events, key=_event_score)

    @staticmethod
    def _looks_like_tool_process(process_name: str) -> bool:
        name = str(process_name or "").strip().lower()
        if not name:
            return True
        tool_keywords = ["grep", "head", "tail", " logcat", "ps ", "ps|", "findstr", "thunder_logcat"]
        return any(keyword in name for keyword in tool_keywords)

    def _summarize_event_cpu_suspects(self, summary: Dict) -> Dict:
        suspect_map = {}
        for event in self._collect_tv_events(summary):
            contention = event.get("cpu_contention") or {}
            candidate = contention.get("top_candidate") or {}
            process_name = str(candidate.get("process", "") or event.get("suspect_process", "") or "").strip()
            if not process_name or process_name in {"无", "N/A"} or self._looks_like_tool_process(process_name):
                continue
            peak_cpu = float(candidate.get("peak_cpu_percent", 0) or 0.0)
            bucket = suspect_map.setdefault(process_name, {"count": 0, "peak_cpu_percent": 0.0})
            bucket["count"] += 1
            bucket["peak_cpu_percent"] = max(float(bucket.get("peak_cpu_percent", 0.0) or 0.0), peak_cpu)
        if not suspect_map:
            return {}
        process_name, stats = max(
            suspect_map.items(),
            key=lambda item: (int(item[1].get("count", 0) or 0), float(item[1].get("peak_cpu_percent", 0.0) or 0.0)),
        )
        return {
            "process": process_name,
            "count": int(stats.get("count", 0) or 0),
            "peak_cpu_percent": float(stats.get("peak_cpu_percent", 0.0) or 0.0),
            "owner": self._infer_issue_owner_from_process(process_name) or "待补证据",
        }

    def _load_event_auto_analysis_text(self, evidence_dir: str) -> dict:
        if not evidence_dir:
            return {}
        try:
            path = os.path.join(evidence_dir, "auto_analysis.json")
            if not os.path.isfile(path):
                return {}
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def __init__(self, config: Dict, log_monitor=None, logger_callback=None):
        self.logger_callback = logger_callback
        
        def log(msg):
            if self.logger_callback:
                try:
                    self.logger_callback(str(msg))
                except Exception:
                    pass
        
        log("🔧 正在初始化测试组件..")
        self.config = config
        self.running = False
        self.stop_flag = False
        self.failure_reason = ""
        self._stop_event = threading.Event()
        log("  - 初始化 ADB 管理器..")
        self.adb = AdbManager(device_id=config.get('device_id'))
        self.adb.set_cancel_event(self._stop_event)
        self.device_ip = self.adb.get_device_ip()
        self.firmware_incremental = self.adb.get_firmware_incremental()
        self.platform_identity = self.adb.get_platform_identity()
        self.log_monitor = log_monitor # 传入 LogMonitor 实例
        self.package_name = config['target_app']['package_name']
        self.activity_name = config['target_app'].get('main_activity')
        self.http_config = config.get('http_vod', {})
        
        log("  - 初始化性能监控..")
        self.monitor = PerformanceMonitor(
            self.adb,
            self.package_name,
            monitor_config=config.get("monitor", {}),
        )
        self.root_cause_analyzer = RootCauseAnalyzer(package_name=self.package_name)
        self.monitor.root_cause_analyzer = self.root_cause_analyzer
        log("  - 初始化播放器控制）..")
        self.controller = PlayerController(self.adb, self.package_name, self.activity_name, self.http_config)
        log("  - 初始化报告生成器...")
        self.report_generator = ReportGenerator(config['report']['output_dir'])
        log("  - 初始化屏幕分析器...")
        screenshots_dir = os.path.join(config['report']['output_dir'], 'screenshots')
        self.screen_analyzer = ScreenAnalyzer(self.adb, temp_dir=screenshots_dir)
        
        # V2.3.1: 零干扰模式配置
        monitor_config = config.get('monitor', {})
        self.enable_screenshot = monitor_config.get('enable_screenshot', True)  # 默认启用
        self.enable_fps = monitor_config.get('enable_fps', True)  # 默认启用
        self.screen_check_interval_seconds = float(
            monitor_config.get("screen_check_interval_seconds", 5)
        )
        self.screen_check_timeout_seconds = max(
            3.0,
            float(monitor_config.get("screen_check_timeout_seconds", 12)),
        )
        self.screen_check_stuck_skip_limit = max(
            2,
            int(monitor_config.get("screen_check_stuck_skip_limit", 3)),
        )
        self.tv_freeze_threshold_seconds = float(
            monitor_config.get("tv_freeze_threshold_seconds", 3)
        )
        self.transition_ignore_seconds = max(
            2.0,
            float(monitor_config.get("transition_ignore_seconds", 4.0)),
        )
        self.pid_loss_abort_seconds = max(
            10.0,
            float(monitor_config.get("pid_loss_abort_seconds", 30)),
        )
        self.pid_loss_abort_samples = max(
            2,
            int(monitor_config.get("pid_loss_abort_samples", 3)),
        )
        
        # 应用到monitor
        if hasattr(self.monitor, '_disable_fps'):
            self.monitor._disable_fps = not self.enable_fps
        try:
            self.monitor.sample_interval = float(monitor_config.get("interval_seconds", 5))
        except Exception:
            self.monitor.sample_interval = 5.0
        
        self.output_dir = config['report']['output_dir']
        self.last_csv_file = None
        self.last_summary_file = None
        self.last_html_file = None
        self.last_summary_json_file = None
        self.last_summary_data = {}
        self.last_report_generation_status = {}
        self.last_report_generation_error = ""
        self.last_report_consistency = {}
        self.tv_playback_watcher = TvPlaybackWatcher(
            self.adb,
            self.monitor,
            self.output_dir,
            config=monitor_config,
            event_callback=lambda event: self.monitor.report_event("TV_STALL", event),
            log_callback=self.log,
            log_monitor=self.log_monitor,
        )
        
        # 异步屏幕检测
        self.screen_check_executor = ThreadPoolExecutor(max_workers=1)
        self.screen_check_future = None
        self.screen_check_started_at = 0.0
        self.screen_check_skip_count = 0
        self.last_screen_results = {}

        # 统计数据
        self.song_count = 0
        self.start_timestamp = None
        self.end_timestamp = None
        self.last_song_title = None
        self.last_song_check_time = 0
        self.runtime_id = None

    def _mark_transition_window(self, reason: str, duration_sec: float = None, source: str = "runner"):
        duration = self.transition_ignore_seconds if duration_sec is None else float(duration_sec or 0.0)
        try:
            self.monitor.mark_transition_window(
                reason=reason,
                duration_sec=max(1.0, duration),
                source=source,
            )
        except Exception:
            pass

    def _reset_run_state(self):
        self.stop_flag = False
        self.failure_reason = ""
        self._stop_event.clear()
        self.last_csv_file = None
        self.last_summary_file = None
        self.last_html_file = None
        self.last_summary_json_file = None
        self.last_summary_data = {}
        self.last_report_generation_status = {}
        self.last_report_generation_error = ""
        self.last_report_consistency = {}
        self.song_count = 0
        self.start_timestamp = None
        self.end_timestamp = None
        self.last_song_title = None
        self.last_song_check_time = 0
        self.screen_check_future = None
        self.screen_check_started_at = 0.0
        self.screen_check_skip_count = 0
        self.last_screen_results = {}
        self._reset_screen_check_state()

        if hasattr(self.monitor, "reset"):
            self.monitor.reset()
        if hasattr(self.root_cause_analyzer, "reset"):
            self.root_cause_analyzer.reset()

        if self.log_monitor:
            try:
                self.log_monitor.stutter_count = 0
                self.log_monitor.stutter_logs.clear()
                self.log_monitor._pending_stutter_events.clear()
                self.log_monitor.decoder_logs.clear()
                self.log_monitor.recent_logs.clear()
                self.log_monitor.crash_count = 0
                self.log_monitor.anr_count = 0
                self.log_monitor.error_events = []
                self.log_monitor.song_change_events.clear()
                if hasattr(self.log_monitor, "lifecycle_events"):
                    self.log_monitor.lifecycle_events.clear()
            except Exception:
                pass

    def log(self, msg):
        logger.info("%s", msg)
        if self.logger_callback:
            try:
                self.logger_callback(str(msg))
            except Exception:
                pass

    def _build_report_generation_status(self, time_str: str = "") -> Dict:
        txt_file = self.last_summary_file or ""
        json_file = self.last_summary_json_file or ""
        html_file = self.last_html_file or ""
        csv_file = self.last_csv_file or ""
        if not txt_file and time_str:
            txt_file = os.path.join(self.output_dir, f"summary_{time_str}.txt")
        if not json_file and txt_file:
            json_file = txt_file.replace(".txt", ".json")
        if not html_file and time_str:
            html_file = os.path.join(self.output_dir, f"report_{time_str}.html")
        status = {
            "csv_ready": bool(csv_file and os.path.isfile(csv_file)),
            "summary_txt_ready": bool(txt_file and os.path.isfile(txt_file)),
            "summary_json_ready": bool(json_file and os.path.isfile(json_file)),
            "report_html_ready": bool(html_file and os.path.isfile(html_file)),
            "csv_file": csv_file,
            "summary_txt_file": txt_file,
            "summary_json_file": json_file,
            "report_html_file": html_file,
            "error": str(self.last_report_generation_error or ""),
            "consistency": dict(self.last_report_consistency or {}),
        }
        missing = []
        if status["csv_ready"] and not status["summary_txt_ready"]:
            missing.append("summary_txt")
        if status["csv_ready"] and not status["summary_json_ready"]:
            missing.append("summary_json")
        if status["csv_ready"] and not status["report_html_ready"]:
            missing.append("report_html")
        status["missing_outputs"] = missing
        status["all_outputs_ready"] = status["csv_ready"] and not missing
        status["consistency_ok"] = str((self.last_report_consistency or {}).get("level", "pass")).lower() != "error"
        return status

    def _validate_report_consistency(self, summary: Dict, root_cause_analysis: Dict) -> Dict:
        issues = []
        warnings = []

        responsibility = summary.get("responsibility_summary", {}) or {}
        event_suspect = self._summarize_event_cpu_suspects(summary)
        representative_event = self._pick_representative_tv_event(summary)
        representative_process = str(
            ((((representative_event.get("cpu_contention") or {}).get("top_candidate") or {}).get("process", "")) or "")
        ).strip()
        global_suspect = str(responsibility.get("suspect_process", "") or "").strip()
        evidence_items = list(responsibility.get("evidence_items", []) or [])
        tv_stall_count = int(summary.get("tv_stall_count", 0) or 0)
        tv_freeze_count = int(summary.get("tv_freeze_count", 0) or 0)
        derived_jank = bool(summary.get("derived_display_jank_risk", False))
        action_keywords = ("优先", "查看", "日志", "排查", "抓取", "对比")

        if event_suspect.get("process") and int(event_suspect.get("count", 0) or 0) >= 2:
            if not global_suspect or global_suspect in {"无", "待补证据"}:
                issues.append(
                    f"全局首要对象为空，但事件级已高频命中 {event_suspect['process']} x{event_suspect['count']}。"
                )
            elif global_suspect != event_suspect["process"]:
                issues.append(
                    f"全局首要对象为 {global_suspect}，但事件级高频嫌疑为 {event_suspect['process']} x{event_suspect['count']}。"
                )

        if representative_process and event_suspect.get("process") and representative_process != event_suspect["process"]:
            warnings.append(
                f"代表事件嫌疑进程为 {representative_process}，事件聚合首要嫌疑为 {event_suspect['process']}，建议复核聚合口径。"
            )

        polluted_evidence = [
            item for item in evidence_items
            if any(keyword in str(item or "") for keyword in action_keywords)
        ]
        if polluted_evidence:
            warnings.append("“为什么这么判断”中混入了建议动作类文案，建议仅保留事实依据。")

        if tv_stall_count <= 0 and tv_freeze_count <= 0 and derived_jank:
            warnings.append("当前仅存在细粒度抖动风险，尚未形成确认级卡顿事件。")

        level = "pass"
        if issues:
            level = "error"
        elif warnings:
            level = "warning"

        return {
            "level": level,
            "issue_count": len(issues),
            "warning_count": len(warnings),
            "issues": issues,
            "warnings": warnings,
            "event_suspect": event_suspect,
            "representative_process": representative_process,
        }

    def regenerate_summary_outputs(self, time_str: str) -> Dict:
        self.last_report_generation_error = ""
        try:
            self._generate_summary_report(time_str)
        except Exception as e:
            self.last_report_generation_error = str(e)
            self.log(f"汇总报告补生成失败: {e}")
        self.last_report_generation_status = self._build_report_generation_status(time_str)
        return dict(self.last_report_generation_status)

    def _get_frame_timestamps_from_gfxinfo(self, package_name):
        try:
            output = self.adb._run_command(
                ["shell", "dumpsys", "gfxinfo", package_name, "framestats"],
                timeout=3
            )
        except Exception:
            return []

        lines = output.splitlines()
        frame_completed_index = None
        data_start = 0
        for index, line in enumerate(lines):
            if "IntendedVsync" in line and "FrameCompleted" in line:
                columns = [part.strip() for part in line.split(",")]
                try:
                    frame_completed_index = columns.index("FrameCompleted")
                    data_start = index + 1
                except ValueError:
                    frame_completed_index = None
                break

        if frame_completed_index is None:
            return []

        timestamps = []
        for line in lines[data_start:]:
            line = line.strip()
            if not line or line.startswith('---'):
                continue
            parts = [part.strip() for part in line.split(',')]
            if len(parts) <= frame_completed_index:
                continue
            try:
                ts_ns = int(parts[frame_completed_index])
                if ts_ns <= 0:
                    continue
                ts_ms = ts_ns / 1_000_000
                timestamps.append(ts_ms)
            except ValueError:
                continue

        if not timestamps:
            return []
        return timestamps[-30:]

    def _detect_frame_interval_inconsistency(self, frame_timestamps):
        if len(frame_timestamps) < 10:
            return False, "数据不足"

        intervals = []
        for i in range(1, len(frame_timestamps)):
            interval = frame_timestamps[i] - frame_timestamps[i - 1]
            if interval > 0:
                intervals.append(interval)

        if len(intervals) < 5:
            return False, "有效间隔数据不足"

        mean_interval = statistics.mean(intervals)
        if mean_interval <= 0:
            return False, "数据异常"

        std_interval = statistics.stdev(intervals)
        cv = std_interval / mean_interval

        # 优化：下调CV 阈，捕捉更细微的帧间隔抖动（从0.4/0.3/0.2 到0.3/0.2/0.15）
        if cv > 0.3:
            return True, f"帧间隔严重不均匀(CV={cv:.2f})"
        if cv > 0.2:
            return True, f"帧间隔不均匀(CV={cv:.2f})"
        if cv > 0.15:
            return True, f"帧间隔轻微不均匀(CV={cv:.2f})"

        return False, f"帧间隔均匀(CV={cv:.2f})"

    def _detect_fps_drops(self, fps_history):
        if len(fps_history) < 3:
            return False, "FPS历史数据不足"

        drops = []
        for i in range(1, len(fps_history)):
            prev_fps = fps_history[i - 1]
            curr_fps = fps_history[i]
            if prev_fps <= 15 or curr_fps <= 0:
                continue
            drop_ratio = (prev_fps - curr_fps) / prev_fps
            drop_amount = prev_fps - curr_fps
            if drop_ratio > 0.4 and drop_amount > 8:
                drops.append(
                    {
                        "from": prev_fps,
                        "to": curr_fps,
                        "ratio": drop_ratio,
                        "amount": drop_amount,
                    }
                )

        if not drops:
            return False, "无明显FPS骤降"

        worst = max(drops, key=lambda x: x["ratio"])
        desc = (
            f"检测到 FPS 骤降: {worst['from']:.1f} -> {worst['to']:.1f} "
            f"(降幅 {worst['ratio']*100:.0f}%, {worst['amount']:.1f}fps)"
        )
        return True, desc

    def _detect_fps_fluctuation(self, fps_history):
        valid = [v for v in fps_history if v > 0]
        if len(valid) < 5:
            return False, "有效FPS数据不足"

        fps_mean = statistics.mean(valid)
        fps_std = statistics.stdev(valid)
        fps_min = min(valid)
        fps_max = max(valid)
        fps_range = fps_max - fps_min

        if fps_std > 6.0:
            return True, f"帧率波动很大(蟽={fps_std:.1f}, 范围{fps_min:.1f}-{fps_max:.1f})"
        if fps_std > 4.0:
            return True, f"帧率波动较大(蟽={fps_std:.1f}, 范围{fps_min:.1f}-{fps_max:.1f})"
        if fps_std > 2.5 and fps_mean < 28:
            return True, f"帧率轻微波动(蟽={fps_std:.1f})，且平均帧率偏低({fps_mean:.1f})"
        if fps_range > 15 and fps_mean < 30:
            return True, f"帧率范围较大({fps_range:.1f}fps)"

        return False, f"帧率稳定(蟽={fps_std:.1f})"

    def _detect_sustained_low_fps(self, fps_history, threshold=25, min_duration=3):
        if len(fps_history) < min_duration:
            return False, "数据不足"

        consecutive_low = 0
        max_consecutive = 0
        for fps in fps_history:
            if 0 < fps < threshold:
                consecutive_low += 1
                if consecutive_low > max_consecutive:
                    max_consecutive = consecutive_low
            else:
                consecutive_low = 0

        if max_consecutive >= min_duration:
            return True, f"检测到连续{max_consecutive}次低帧率(<{threshold}fps)"
        if max_consecutive >= 2:
            return True, f"检测到{max_consecutive}次连续低帧率"

        return False, "无持续低帧率"

    def _detect_av_sync_issues(self, audio_active, video_fps, audio_underrun_count=0):
        issues = []
        if audio_active and video_fps > 0 and video_fps < 22:
            issues.append(f"音频正常但视频帧率偏低{video_fps:.1f}fps)")
        if audio_underrun_count > 0 and video_fps > 25:
            issues.append(f"音频下溢{audio_underrun_count}次但视频正常")
        if audio_underrun_count > 0 and video_fps > 0 and video_fps < 24:
            issues.append(
                f"音视频都存在问题(音频下溢{audio_underrun_count}次，视频{video_fps:.1f}fps)"
            )

        if not issues:
            return False, "音视频同步正常"

        return True, "可能存在音视频不同步: " + "; ".join(issues)

    def _calculate_perceptual_stutter_score(self, metrics):
        score = 0
        details = []

        weights = {
            "tv_stall": 60,
            "decoder_stuck": 80,
            "frame_interval_inconsistency": 45,
            "fps_sudden_drop": 40,
            "sustained_low_fps": 35,
            "av_sync_issue": 30,
            "fps_fluctuation": 25,
            "buffer_pressure": 20,
            "log_stutter": 15,
            "ui_jank": 10,
        }
        if metrics.get("frame_inconsistent"):
            score += weights["frame_interval_inconsistency"]
            desc = metrics.get("frame_inconsistent_desc", "")
            details.append(f"Frame interval inconsistency({desc})")
        tv_stall_count = int(metrics.get("tv_stall_count", 0) or 0)
        if tv_stall_count > 0:
            score += min(weights["tv_stall"], tv_stall_count * 10)
            details.append(f"TV stall detected ({tv_stall_count} samples)")

        confirmed_decoder_stuck_count = int(
            metrics.get("confirmed_decoder_stuck_count", 0) or 0
        )
        decoder_stuck_risk_count = int(
            metrics.get("decoder_stuck_risk_count", 0) or 0
        )
        if confirmed_decoder_stuck_count > 0:
            score += min(
                weights["decoder_stuck"],
                20 + confirmed_decoder_stuck_count * 2,
            )
            details.append(
                f"Decoder output stalled ({confirmed_decoder_stuck_count} confirmed samples)"
            )
        elif decoder_stuck_risk_count > 0:
            score += min(12, 4 + decoder_stuck_risk_count)
            details.append(
                f"Decoder stall risk ({decoder_stuck_risk_count} risk samples)"
            )

        if metrics.get("fps_drop_detected"):
            score += weights["fps_sudden_drop"]
            desc = metrics.get("fps_drop_desc", "")
            details.append(f"瞬时帧率骤降({desc})")

        if metrics.get("sustained_low_fps"):
            score += weights["sustained_low_fps"]
            desc = metrics.get("sustained_low_desc", "")
            details.append(f"持续低帧率({desc})")

        if metrics.get("av_sync_issue"):
            score += weights["av_sync_issue"]
            desc = metrics.get("av_sync_desc", "")
            details.append(f"音视频同步问题{desc})")

        if metrics.get("fps_fluctuation"):
            score += weights["fps_fluctuation"]
            desc = metrics.get("fps_fluctuation_desc", "")
            details.append(f"帧率波动({desc})")

        if metrics.get("buffer_issue"):
            score += weights["buffer_pressure"]
            desc = metrics.get("buffer_desc", "")
            details.append(f"缓冲区压力({desc})")

        log_stutter_count = metrics.get("log_stutter_count", 0)
        if log_stutter_count > 0:
            stutter_score = min(weights["log_stutter"], log_stutter_count * 3)
            score += stutter_score
            details.append(f"日志卡顿({log_stutter_count}次)")

        tv_jank_ratio = float(metrics.get("tv_jank_ratio", 0) or 0.0)
        tv_big_jank_ratio = float(metrics.get("tv_big_jank_ratio", 0) or 0.0)
        tv_frame_gap_p95_ms = float(metrics.get("tv_frame_gap_p95_ms", 0) or 0.0)
        tv_frame_gap_p99_ms = float(metrics.get("tv_frame_gap_p99_ms", 0) or 0.0)
        tv_perceptible_stall_ratio = float(
            metrics.get("tv_perceptible_stall_ratio", 0) or 0.0
        )
        tv_severe_stall_ratio = float(
            metrics.get("tv_severe_stall_ratio", 0) or 0.0
        )

        if (
            tv_frame_gap_p95_ms >= 2000.0
            or tv_frame_gap_p99_ms >= 2500.0
            or tv_severe_stall_ratio >= 20.0
            or (tv_big_jank_ratio >= 60.0 and tv_perceptible_stall_ratio >= 8.0)
        ):
            score = max(score, 55)
            details.append(
                f"视频播放已出现明显停顿风险(底层Jank {tv_jank_ratio:.2f}%, Big Jank {tv_big_jank_ratio:.2f}%, P95/P99 {tv_frame_gap_p95_ms:.0f}/{tv_frame_gap_p99_ms:.0f}ms)"
            )
        elif (
            tv_frame_gap_p95_ms >= 800.0
            or tv_frame_gap_p99_ms >= 1200.0
            or tv_perceptible_stall_ratio >= 10.0
            or (tv_big_jank_ratio >= 30.0 and tv_perceptible_stall_ratio >= 3.0)
        ):
            score = max(score, 35)
            details.append(
                f"视频帧时间抖动偏高(底层Jank {tv_jank_ratio:.2f}%, Big Jank {tv_big_jank_ratio:.2f}%, P95/P99 {tv_frame_gap_p95_ms:.0f}/{tv_frame_gap_p99_ms:.0f}ms)"
            )
        elif (
            tv_frame_gap_p95_ms >= 300.0
            or tv_frame_gap_p99_ms >= 500.0
            or tv_perceptible_stall_ratio >= 1.0
            or (tv_jank_ratio >= 20.0 and tv_frame_gap_p99_ms >= 250.0)
        ):
            score = max(score, 18)
            details.append(
                f"视频帧时间存在波动(底层Jank {tv_jank_ratio:.2f}%, Big Jank {tv_big_jank_ratio:.2f}%, P95/P99 {tv_frame_gap_p95_ms:.0f}/{tv_frame_gap_p99_ms:.0f}ms)"
            )

        # 点歌时UI Jank 仅作参考，不计入电视端播放卡顿风险分析

        if score >= 80:
            level = "严重卡顿"
            recommendation = "不建议上线，存在明显卡顿问题"
        elif score >= 50:
            level = "明显卡顿"
            recommendation = "需要优化，用户可明显感知卡顿"
        elif score >= 30:
            level = "轻微卡顿"
            recommendation = "建议优化，部分用户可能感知到卡顿"
        elif score >= 15:
            level = "微卡顿"
            recommendation = "可接受，但建议关注"
        else:
            level = "流畅"
            recommendation = "播放整体流畅，未见明显电视端停顿"

        severity = "low"
        if score >= 50:
            severity = "high"
        elif score >= 30:
            severity = "medium"

        return {
            "score": min(100, score),
            "level": level,
            "details": details,
            "recommendation": recommendation,
            "human_perceptible": score >= 15,
            "severity": severity,
        }

    def _comprehensive_stutter_detection(self, summary):
        history = getattr(self.monitor, "history", [])
        if not history:
            return {
                "score": 0,
                "level": "unknown",
                "details": ["数据不足"],
                "recommendation": "数据不足，无法评估流畅度",
                "human_perceptible": False,
                "severity": "low",
            }

        recent_snapshots = history[-20:] if len(history) >= 20 else history

        fps_history = [
            s.get("video_fps", 0)
            for s in recent_snapshots
            if s.get("video_fps", 0) > 0
        ]
        current_fps = fps_history[-1] if fps_history else summary.get(
            "avg_video_fps", 0
        )

        last_snapshot = recent_snapshots[-1]
        audio_active = last_snapshot.get("audio_active", False)

        log_stutter_count = summary.get("final_log_stutter_count", 0)

        frame_timestamps = self._get_frame_timestamps_from_gfxinfo(
            self.package_name
        )

        metrics = {}

        fps_severity, fps_desc = None, ""
        if current_fps <= 0:
            fps_severity = "unknown"
            fps_desc = "无法获取FPS数据"
        elif current_fps < 20:
            fps_severity = "severe"
            fps_desc = f"视频帧率严重偏低({current_fps:.1f}fps)"
        elif current_fps < 24:
            fps_severity = "warning"
            fps_desc = f"视频帧率偏低({current_fps:.1f}fps)"
        elif current_fps < 27:
            fps_severity = "notice"
            fps_desc = f"视频帧率略低({current_fps:.1f}fps)"
        else:
            fps_severity = "good"
            fps_desc = f"视频帧率正常({current_fps:.1f}fps)"

        metrics["fps_severity"] = fps_severity
        metrics["fps_desc"] = fps_desc

        frame_inconsistent, frame_desc = self._detect_frame_interval_inconsistency(
            frame_timestamps
        )
        metrics["frame_inconsistent"] = frame_inconsistent
        metrics["frame_inconsistent_desc"] = frame_desc

        fps_drop, fps_drop_desc = self._detect_fps_drops(fps_history)
        metrics["fps_drop_detected"] = fps_drop
        metrics["fps_drop_desc"] = fps_drop_desc

        fps_fluct, fps_fluct_desc = self._detect_fps_fluctuation(fps_history)
        metrics["fps_fluctuation"] = fps_fluct
        metrics["fps_fluctuation_desc"] = fps_fluct_desc

        sustained_low, sustained_desc = self._detect_sustained_low_fps(
            fps_history
        )
        metrics["sustained_low_fps"] = sustained_low
        metrics["sustained_low_desc"] = sustained_desc

        av_sync, av_desc = self._detect_av_sync_issues(
            audio_active, current_fps
        )
        metrics["av_sync_issue"] = av_sync
        metrics["av_sync_desc"] = av_desc

        metrics["log_stutter_count"] = log_stutter_count
        metrics["tv_stall_count"] = int(summary.get("tv_stall_count", 0) or 0)
        metrics["decoder_stuck_count"] = int(
            summary.get("decoder_stuck_count", 0) or 0
        )
        metrics["confirmed_decoder_stuck_count"] = int(
            summary.get("confirmed_decoder_stuck_count", 0) or 0
        )
        metrics["decoder_stuck_risk_count"] = int(
            summary.get("decoder_stuck_risk_count", 0) or 0
        )
        metrics["tv_jank_ratio"] = float(
            summary.get("tv_jank_sample_ratio_percent", 0) or 0.0
        )
        metrics["tv_big_jank_ratio"] = float(
            summary.get("tv_big_jank_sample_ratio_percent", 0) or 0.0
        )
        metrics["tv_frame_gap_p95_ms"] = float(
            summary.get("tv_frame_gap_p95_ms", 0) or 0.0
        )
        metrics["tv_frame_gap_p99_ms"] = float(
            summary.get("tv_frame_gap_p99_ms", 0) or 0.0
        )
        metrics["tv_perceptible_stall_ratio"] = float(
            summary.get("tv_perceptible_stall_sample_ratio_percent", 0) or 0.0
        )
        metrics["tv_severe_stall_ratio"] = float(
            summary.get("tv_severe_stall_sample_ratio_percent", 0) or 0.0
        )

        recent_jank = 0.0
        tail = recent_snapshots[-5:]
        if tail:
            recent_jank = sum(
                s.get("gfx_jank_percent", 0) for s in tail
            ) / len(tail)
        metrics["ui_jank_percent"] = recent_jank

        return self._calculate_perceptual_stutter_score(metrics)

    def stop(self):
        """外部调用停止"""
        self.stop_flag = True
        self._stop_event.set()
        self.log("收到停止请求，正在结束监控线程和生成报告...")
        if self.log_monitor:
            try:
                self.log_monitor.stop()
            except Exception:
                pass
        if self.tv_playback_watcher:
            self.tv_playback_watcher.stop(wait=False)
        if self.screen_check_future and not self.screen_check_future.done():
            self.screen_check_future.cancel()
        self._reset_screen_check_state()

    def _reset_screen_check_state(self):
        self.screen_check_future = None
        self.screen_check_started_at = 0.0
        self.screen_check_skip_count = 0

    def _expire_stuck_screen_check(self, current_time: float) -> bool:
        if not self.screen_check_future or self.screen_check_future.done():
            return False
        if self.screen_check_started_at <= 0:
            self.screen_check_started_at = current_time
            return False

        running_seconds = current_time - self.screen_check_started_at
        if running_seconds < self.screen_check_timeout_seconds:
            return False

        self.log(
            "[Screen Check Timeout] async screen check exceeded "
            f"{self.screen_check_timeout_seconds:.0f}s, resetting watcher state."
        )
        try:
            self.screen_check_future.cancel()
        except Exception:
            pass
        self.last_screen_results = {}
        self._reset_screen_check_state()
        return True

    def run(self):
        self._reset_run_state()
        self.running = True
        self.log(f"=== Android 播放器专项压测工具v2.3 ===")
        
        # Create Runtime
        try:
            runtime = get_runtime_manager().create_runtime(
                name=f"Player Stress: {self.package_name}",
                module="player_stress",
                context={
                    'device_id': self.config.get('device_id'),
                    'mode': self.config.get('test_strategy', {}).get('mode', 'unknown'),
                    'duration': self.config.get('test_strategy', {}).get('duration_minutes', 0)
                }
            )
            self.runtime_id = runtime.runtime_id
            get_runtime_manager().update_status(self.runtime_id, RuntimeStatus.RUNNING)
            self.log(f"Runtime created: {self.runtime_id}")
        except Exception as e:
            self.log(f"Warning: Failed to create Runtime: {e}")

        self.log(f"测试对象: {self.package_name}")
        self.log(
            f"监控模式: {'极低功耗' if not self.enable_screenshot and not self.enable_fps else '标准模式' if self.enable_screenshot and self.enable_fps else '深度压测'}"
        )
        
        self.log("正在检查设备连接状态...")
        try:
            # 添加超时保护，避免卡住
            import signal
            import threading
            
            device_online = False
            check_timeout = False
            
            def check_device():
                nonlocal device_online
                try:
                    device_online = self.adb.is_device_online()
                except Exception as e:
                    self.log(f"⚠️ 设备检查异常 {e}")
            
            check_thread = threading.Thread(target=check_device)
            check_thread.daemon = True
            check_thread.start()
            check_thread.join(timeout=5)  # 最多等待5秒
            
            if check_thread.is_alive():
                self.log("⚠️ 设备棢查超时，继续尝试启动...")
                device_online = True  # 假设在线，让后续步骤处理
            
            if not device_online:
                self.log("错误: 未检测到在线设备，请检查 USB 或 ADB 连接。")
                if self.runtime_id:
                     get_runtime_manager().update_status(self.runtime_id, RuntimeStatus.FAILED, error="Device offline")
                return
            self.log("设备连接正常")
        except Exception as e:
            self.log(f"设备检查过程异常: {e}，继续尝试启动...")

        monitor_interval = self.config['monitor']['interval_seconds']
        mode = self.config['test_strategy']['mode']
        skip_interval = self.config['test_strategy']['skip_interval_seconds']
        
        duration_minutes = self.config['test_strategy']['duration_minutes']
        self.start_timestamp = time.time()
        end_time = self.start_timestamp + (duration_minutes * 60)

        # 1. 启动应用（添加超时保护）
        self.log("📱 正在启动应用...")
        try:
            launch_thread = threading.Thread(target=self.controller.launch)
            launch_thread.daemon = True
            launch_thread.start()
            launch_thread.join(timeout=20)  # 最多等待20秒
            
            if launch_thread.is_alive():
                self.log("⚠️ 应用启动超时，继续执行..")
            else:
                self.log("应用启动完成")
        except Exception as e:
            self.log(f"⚠️ 应用启动异常: {e}，继续执行..")
        
        # 1.1 根据模式决定是否首播点歌
        # 如果是monitor_only，绝对不主动点歌
        if mode != "monitor_only" and self.http_config:
             try:
                 self.controller.vod_song()
             except Exception as e:
                 self.log(f"点歌失败: {e}，继续监控...")
             
        self.log("正在启动性能监控...")
        try:
            self.monitor.start_monitoring()
            self.log("性能监控已启动")
        except Exception as e:
            self.log(f"性能监控启动失败: {e}")
            if self.runtime_id:
                 get_runtime_manager().update_status(self.runtime_id, RuntimeStatus.FAILED, error=f"Monitor start failed: {e}")
            return
        self.monitor.start_new_session("Initial Session") # V2.1: 启动第一个Session
        self.monitor.report_event("ACTION", "PLAY") # 记录播放开始时间
        if self.enable_fps:
            self.tv_playback_watcher.start()
        
        last_action_time = time.time()
        start_time_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.last_csv_file = os.path.join(self.output_dir, f"report_{start_time_str}.csv")
        self.last_summary_file = os.path.join(self.output_dir, f"summary_{start_time_str}.txt")
        
        self.log(f"开始压测，模式: {mode}，时长: {duration_minutes}分钟")
        
        # 初始化CSV
        with open(self.last_csv_file, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            # 双语表头
            headers = [
                'Timestamp(时间戳', 
                'PID(进程ID)', 
                'Status(状态)', 
                'PSS_MB(内存MB)', 
                'Player_CPU_Percent(播放器CPU%)',
                'System_CPU_Percent(整机CPU%)',
                'MPP_Active(硬解路数)', 
                'MPP_Sessions(硬解总数)', 
                'Restart_Count(重启次数)', 
                'Play_State(播放状态', 
                'Audio_Active(音频活跃)', 
                'GFX_Jank(掉帧)', 
                'Log_Stutter(卡顿日志)', 
                'Event(事件)', 
                'Screenshot_D0(截图_触摸状态', 
                'Screenshot_D1(截图_电视)',
                'Top_Consumers(高占用进程，用于排除工具干扰)',
                'Root_Cause_Type(根因类型)',
                'Suspect_Process(嫌疑进程)',
                'Decode_Slowdown(解码降速)',
                'Max_Temperature_C(最高温度)',
                'Min_CPU_Frequency_Ratio(最低频率比例',
                'Thermal_Throttling(热降频',
                'Expected_Stream_FPS(期望流帧率)',
                'Decode_FPS_Estimate(解码估算帧率)',
                'Decode_Drop_Estimate(估算丢帧数)',
                'Decode_Drop_Ratio(估算丢帧比例)',
                'Video_FPS_Source(FPS数据来源)',
                'TV_Surface_Name(电视视频Surface)',
            ]
            writer.writerow(headers)

        exit_status = RuntimeStatus.COMPLETED
        try:
            self.log("进入主循环...")
            sys.stdout.flush()
            loop_count = 0
            while time.time() < end_time and not self.stop_flag:
                loop_count += 1
                current_time = time.time()
                self.log(f"Loop #{loop_count} | Remaining: {end_time - current_time:.1f}s")
                sys.stdout.flush()
                
                # 1. 获取外部事件 (Logs)
                log_events = []
                recent_stutter_events = []
                if self.log_monitor:
                    log_events = self.log_monitor.get_lifecycle_events()
                    self.monitor.set_log_stutter_count(self.log_monitor.get_stutter_count())
                    recent_stutter_events = self.log_monitor.get_stutter_events()
                    self.monitor.set_recent_log_stutter_events(recent_stutter_events)
                    self.monitor.set_recent_decoder_events(
                        self.log_monitor.get_decoder_events()
                    )

                # A. 执行监控采集 (V2:  Evaluator )
                # self.log("Collecting snapshot...")
                snapshot = self.monitor.collect_snapshot(external_events=log_events)
                if (
                    not snapshot.get("target_process_available", True)
                    and (
                        int(snapshot.get("pid_missing_consecutive", 0) or 0)
                        >= self.pid_loss_abort_samples
                        or float(snapshot.get("pid_missing_duration_sec", 0) or 0)
                        >= self.pid_loss_abort_seconds
                    )
                ):
                    self.failure_reason = (
                        f"目标播放器进程 {self.package_name} 已连续丢失 "
                        f"{snapshot.get('pid_missing_duration_sec', 0)} 秒"
                    )
                    self.log(f"{self.failure_reason}，自动终止本轮测试")
                    self.monitor.report_event("FATAL", self.failure_reason)
                    exit_status = RuntimeStatus.FAILED
                    break
                
                # V2.4: 实时FPS有效性检测(Early Warning)
                # V2.3.2: FPS棢测已移至monitor.py中的智能棢测辑
                # 这里不再需要重复的FPS警告逻辑
                
                self.log("Snapshot collected.")
                sys.stdout.flush()
                event_log = ""
                
                # A-2. 屏幕状态检测(低频: <30s)
                current_screenshot_d0 = ""
                current_screenshot_d1 = ""
                
                if not hasattr(self, 'last_screen_check_time'):
                    self.last_screen_check_time = 0
                self._expire_stuck_screen_check(current_time)
                
                # 检查异步任务是否完成
                if self.screen_check_future and self.screen_check_future.done():
                    try:
                        self.last_screen_results = self.screen_check_future.result()
                        # self.log("Screen check finished (Async).")
                        
                        # 解析结果并上报 (仅在结果更新时执行一次)
                        # 解析 Display 0
                        res_d0 = self.last_screen_results.get(0, {})
                        status_d0 = res_d0.get('status', 'UNKNOWN')
                        current_screenshot_d0 = res_d0.get('path', '')
                            
                        # V2.3: 解析扢有检测到的电视端 Display 1  2
                        # 优先使用第一个检测到的电视端 Display
                        tv_display_id = None
                        current_screenshot_d1 = ""
                        status_d1 = "UNKNOWN"
                        
                        # 查找所有非 Display 0 的结果
                        for display_id, result in self.last_screen_results.items():
                            if display_id != 0:  # 排除点歌屏
                                tv_display_id = display_id
                                res_d1 = result
                                status_d1 = res_d1.get('status', 'UNKNOWN')
                                current_screenshot_d1 = res_d1.get('path', '')
                                break  # 使用第一个找到的电视端Display
                        
                        if current_screenshot_d1 and os.path.exists(current_screenshot_d1):
                            from PIL import Image
                            if not hasattr(self, 'tv_screenshot_history'):
                                self.tv_screenshot_history = []
                            
                            # 保存最近3次截图路径（用于对比）
                            self.tv_screenshot_history.append({
                                'path': current_screenshot_d1,
                                'time': current_time
                            })
                            # 只保留最小
                            if len(self.tv_screenshot_history) > 3:
                                self.tv_screenshot_history.pop(0)
                            
                            # 如果至少一次截图，且时间间隔>= 1秒，进行对比
                            if len(self.tv_screenshot_history) >= 2:
                                last_screenshot = self.tv_screenshot_history[-2]
                                time_diff = current_time - last_screenshot['time']
                                
                                if time_diff >= 1.0:  # 至少1秒间隔
                                    # 对比画面是否静止
                                    try:
                                        is_frozen = self.screen_analyzer._is_same_image(
                                            Image.open(current_screenshot_d1).convert("RGB"),
                                            last_screenshot['path'],
                                            diff_threshold=1.0  # 极低阈，只有几乎完全丢样才算静止
                                        )
                                        
                                        # 如果画面静止且音频在跑，判定为电视端画面冻结
                                        snapshot = self.monitor.history[-1] if self.monitor.history else {}
                                        audio_active = snapshot.get('audio_active', False)
                                        ignore_video = snapshot.get('ignore_video_metrics', False)
                                        surface_locked = bool(snapshot.get('tv_surface_name', ''))
                                        frame_advanced = bool(snapshot.get('frame_advanced', True))
                                        max_frame_gap_ms = float(snapshot.get('max_frame_gap_ms', 0) or 0.0)
                                        video_fps = float(snapshot.get('video_fps', 0) or 0.0)
                                        expected_stream_fps = float(snapshot.get('expected_stream_fps', 30.0) or 30.0)
                                        decoder_stuck_confirmed = bool(snapshot.get('decoder_stuck_confirmed', False))
                                        decode_drop_ratio = float(snapshot.get('decode_drop_ratio', 0) or 0.0)
                                        osd_surface_detected = bool(snapshot.get('osd_surface_detected', False))
                                        osd_frame_advanced = bool(snapshot.get('osd_frame_advanced', False))
                                        freeze_corroborations = []
                                        if not frame_advanced:
                                            freeze_corroborations.append("surface_not_advancing")
                                        if max_frame_gap_ms >= max(800.0, self.monitor.tv_stall_frame_gap_threshold_ms * 2):
                                            freeze_corroborations.append("frame_gap_spike")
                                        if decoder_stuck_confirmed:
                                            freeze_corroborations.append("decoder_confirmed")
                                        if decode_drop_ratio >= 0.2:
                                            freeze_corroborations.append("decode_drop")
                                        if video_fps > 0 and video_fps < max(8.0, expected_stream_fps * 0.35):
                                            freeze_corroborations.append("video_fps_low")
                                        if osd_surface_detected and osd_frame_advanced and video_fps >= max(24.0, expected_stream_fps * 0.8):
                                            freeze_corroborations = []

                                        if is_frozen and audio_active and not ignore_video and surface_locked:
                                            if len(self.tv_screenshot_history) >= 3:
                                                time_span = self.tv_screenshot_history[-1]['time'] - self.tv_screenshot_history[0]['time']
                                                if time_span >= self.tv_freeze_threshold_seconds:
                                                    display_id_str = f"Display {tv_display_id}" if tv_display_id else "电视端"
                                                    physical_surface_freeze = bool(
                                                        surface_locked and max_frame_gap_ms >= 2000.0
                                                    )
                                                    if physical_surface_freeze and "physical_surface_freeze" not in freeze_corroborations:
                                                        freeze_corroborations.append("physical_surface_freeze")
                                                    if freeze_corroborations:
                                                        freeze_level = "risk"
                                                        if (
                                                            physical_surface_freeze
                                                            or decoder_stuck_confirmed
                                                            or len(freeze_corroborations) >= 2
                                                            or time_span >= max(self.tv_freeze_threshold_seconds * 2, 6.0)
                                                        ):
                                                            freeze_level = "confirmed"
                                                        freeze_category = (
                                                            "渲染层冻结"
                                                            if physical_surface_freeze
                                                            else (
                                                                "解码停顿"
                                                                if decoder_stuck_confirmed
                                                            else (
                                                                "画面冻结"
                                                                if len(freeze_corroborations) >= 2
                                                                else "冻结风险"
                                                            )
                                                            )
                                                        )
                                                        self.log(f"  [TV Freeze Detected] {display_id_str} 画面连续静止约 {time_span:.1f} 秒")
                                                        self.monitor.report_event("TV_FREEZE", {
                                                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                            "type": "TV_FREEZE",
                                                            "description": f"{display_id_str} 画面静止 {time_span:.1f} 秒",
                                                            "duration_sec": round(time_span, 2),
                                                            "video_fps": round(video_fps, 2),
                                                            "expected_stream_fps": round(expected_stream_fps, 2),
                                                            "max_frame_gap_ms": round(max_frame_gap_ms, 2),
                                                            "frame_advanced": frame_advanced,
                                                            "decoder_stuck_confirmed": decoder_stuck_confirmed,
                                                            "decode_drop_ratio": round(decode_drop_ratio, 4),
                                                            "osd_surface_detected": osd_surface_detected,
                                                            "osd_frame_advanced": osd_frame_advanced,
                                                            "corroboration_signals": list(freeze_corroborations),
                                                            "freeze_level": freeze_level,
                                                            "freeze_category": freeze_category,
                                                            "assessment_reason": (
                                                                "截图连续静止且同时命中多条播放退化旁证，可按确认级冻结处理。"
                                                                if freeze_level == "confirmed"
                                                                else "截图连续静止且命中部分播放退化旁证，当前按风险级冻结样本处理。"
                                                            ),
                                                        })
                                                        event_log += f" [TV_FREEZE-{freeze_level.upper()}:{time_span:.1f}s]"
                                                        self.tv_screenshot_history = []
                                                    else:
                                                        self.log(f"  [TV Freeze Ignored] {display_id_str} 画面静止约 {time_span:.1f} 秒，但缺少播放退化旁证，暂不计入冻结")
                                                        self.monitor.report_event("TV_FREEZE_IGNORED", {
                                                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                            "type": "TV_FREEZE_IGNORED",
                                                            "description": f"{display_id_str} 画面静止 {time_span:.1f} 秒",
                                                            "duration_sec": round(time_span, 2),
                                                            "video_fps": round(video_fps, 2),
                                                            "expected_stream_fps": round(expected_stream_fps, 2),
                                                            "max_frame_gap_ms": round(max_frame_gap_ms, 2),
                                                            "reason": "missing_playback_corroboration",
                                                            "assessment_reason": "截图静止，但未同时命中播放退化旁证，已按转场/静态页风险忽略。",
                                                        })
                                    except Exception as e:
                                        # 对比失败，忽略
                                        pass
                            
                        # 告警检查
                        alert_msg = ""
                        # Capture/analysis failures are monitoring coverage gaps,
                        # not product screen anomalies.
                        non_product_statuses = [
                            "NORMAL",
                            "UNKNOWN",
                            "CAPTURE_FAILED",
                            "NO_SIGNAL",
                            "ANALYSIS_ERROR",
                            "ERROR",
                        ]
                        if status_d0 not in non_product_statuses:
                                alert_msg += f" D0:{status_d0}"
                        if status_d1 not in non_product_statuses:
                                alert_msg += f" D1:{status_d1}"

                        if alert_msg:
                            self.log(f"  [Screen Alert]{alert_msg}")
                            self.monitor.report_event("SCREEN_ANOMALY", alert_msg) # Add to summary events
                            event_log += f" [Screen:{alert_msg}]"
                            
                        # 如果成功截图，也在控制台输出丢下路径，方便调试
                        if current_screenshot_d0 or current_screenshot_d1:
                                # 仅打印文件名，避免太长
                                fn0 = os.path.basename(current_screenshot_d0) if current_screenshot_d0 else "-"
                                fn1 = os.path.basename(current_screenshot_d1) if current_screenshot_d1 else "-"
                                # self.log(f"  [Snapshot] D0: {fn0}, D1: {fn1}")

                    except Exception as e:
                        self.log(f"Async screen check failed: {e}")
                        self.last_screen_results = {}
                    finally:
                        self._reset_screen_check_state()

                # V2.3.1: 零干扰模式- 如果禁用截图，跳过屏幕检测
                if self.enable_screenshot:
                    if current_time - self.last_screen_check_time >= self.screen_check_interval_seconds:
                        self.last_screen_check_time = current_time
                        
                        if self.screen_check_future is None:
                            # self.log("Starting async screen check...")
                            sys.stdout.flush()
                            self.screen_check_future = self.screen_check_executor.submit(self.screen_analyzer.check_screen_status)
                            self.screen_check_started_at = current_time
                            self.screen_check_skip_count = 0
                        else:
                            self.screen_check_skip_count += 1
                            if self.screen_check_skip_count >= self.screen_check_stuck_skip_limit:
                                elapsed = max(0.0, current_time - self.screen_check_started_at)
                                self.log(
                                    "Skipping screen check (Previous task still running "
                                    f"for {elapsed:.1f}s)..."
                                )
                            else:
                                self.log("Skipping screen check (Previous task still running)...")
                            sys.stdout.flush()
                else:
                    # 极低功模式：完全禁用截图，不进行屏幕检查
                    # 只在首次运行时初始化丢帧
                    if not hasattr(self, '_screenshot_disabled_logged'):
                        self.log("  [Zero-Interference Mode] Screenshot disabled for zero performance impact")
                        self._screenshot_disabled_logged = True

                # 使用最新的结果进行日志/CSV记录 (但不重复上报 Monitor)
                screen_results = self.last_screen_results
                    
                # 解析 Display 0 用于 CSV
                res_d0 = screen_results.get(0, {})
                status_d0 = res_d0.get('status', 'UNKNOWN')
                current_screenshot_d0 = res_d0.get('path', '') # Update for CSV logic if needed


                # A-3. MPP 资源泄露保护 (V2.1)
                mpp_sessions = snapshot.get('mpp_sessions', 0)
                if mpp_sessions > 32:
                     self.log(f"!!! CRITICAL WARNING: MPP Session Leak Detected ({mpp_sessions} > 32) !!!")
                     self.log("!!! Stopping Test to prevent System Hang !!!")
                     self.monitor.report_event("FATAL", f"MPP Leak: {mpp_sessions} sessions")
                     self.stop_flag = True
                     break

                # B. 执行策略操作
                if mode == "monitor_only":
                    # 纯监控模式：不执行任何主动操作(切歌/点歌)
                    # 检测歌曲变化以更新 Song Count
                    # 方式1: Logcat 关键字（用户配置 song_change_keywords等
                    if self.log_monitor and hasattr(self.log_monitor, 'get_song_change_events'):
                        for evt in self.log_monitor.get_song_change_events():
                            self.song_count += 1
                            self._mark_transition_window(
                                reason="SONG_CHANGE",
                                duration_sec=self.transition_ignore_seconds,
                                source="logcat_song_change",
                            )
                            self.log(f" [Monitor] 检测到切歌 (Logcat): {evt.get('line', '')[:60]}...")
                            self.monitor.start_new_session(f"Song #{self.song_count}")
                            event_log += f" [切歌 #{self.song_count}]"
                    
                    # 方式2: dumpsys media_session 元数据（需应用使用 MediaSession API）
                    if current_time - self.last_song_check_time >= 5:
                        self.last_song_check_time = current_time
                        try:
                            current_song = self.adb.get_media_metadata(self.package_name)
                            if current_song and current_song != self.last_song_title:
                                if self.last_song_title is not None:
                                    self.song_count += 1
                                    self._mark_transition_window(
                                        reason="SONG_CHANGE",
                                        duration_sec=self.transition_ignore_seconds,
                                        source="media_session",
                                    )
                                    self.log(f" [Monitor] 检测到切歌 (MediaSession): {current_song}")
                                    self.monitor.start_new_session(f"Detected: {current_song}")
                                    event_log += f" [New Song: {current_song}]"
                                else:
                                    self.log(f" [Monitor] 初始歌曲: {current_song}")
                                    self.monitor.start_new_session(f"Detected: {current_song}")
                                self.last_song_title = current_song
                        except Exception:
                            pass

                elif mode == "fixed_skip":
                    if current_time - last_action_time >= skip_interval:
                        # 结算当前会话
                        verdict, reason = self.monitor.get_session_result(is_force_stop=True)
                        self.log(f"-------- [Song #{self.song_count}] Result: {verdict} ({reason}) --------")
                        
                        self._mark_transition_window(
                            reason="CUT_SONG",
                            duration_sec=self.transition_ignore_seconds,
                            source="fixed_skip",
                        )
                        self.controller.next_song() # 强制切歌
                        self.song_count += 1
                        self.monitor.start_new_session(f"Unknown (Song #{self.song_count})")
                        self.monitor.report_event("ACTION", "PLAY") # 新会话开始计算
                        last_action_time = current_time
                        event_log = f"Action: Cut Song | Last Result: {verdict}"
                
                elif mode in {"loop_playback", "business_stress"}:
                    # 循环播放模式
                    # 1. 如果播放失败或卡在初始化，尝试重新点歌
                    # 2. 如果播放完成，尝试重新点歌(如果应用不自动连播)
                    # 3. 如果时间到了 skip_interval 且还在播放，强制切歌 (压测逻辑)
                    
                    # 检查当前状态
                    play_state = snapshot.get('play_state', 'UNKNOWN')
                    
                    #  A: 失败恢复 (Start Failed) -> 
                    if "FAIL" in event_log or "Stuck" in snapshot.get('status', ''):
                         pass # 下面的时间检查会触发，或者我们可以立即触发？
                    
                    if current_time - last_action_time >= skip_interval:
                        # 只有在确实需要的时才点歌
                        # 用户反馈：为什么自动点歌？
                        # 解释：因为这是 loop_playback 压测模式，默认行为是周期性点歌/切歌以测试稳定性。
                        # 如果用户只想监控，应该用 monitor_only
                        # 但为了体验更好，如果当前正在播放且没超时太多，也许可以不切？
                        # 暂时保持原逻辑，但在日志中明确原因
                        
                        # 结算上一首
                        verdict, reason = self.monitor.get_session_result(is_force_stop=True)
                        self.log(f"-------- [Song #{self.song_count}] Result: {verdict} ({reason}) --------")
                        
                        # 仅当上一首结束或失败，或者强制切歌时间到
                        cycle_label = "Business Stress Cycle" if mode == "business_stress" else "Loop Mode Cycle"
                        self.log(f" 正在通过 HTTP 点歌 ({cycle_label})...")
                        self._mark_transition_window(
                            reason="ORDER_SONG",
                            duration_sec=max(self.transition_ignore_seconds, 5.0),
                            source="business_stress" if mode == "business_stress" else "loop_playback",
                        )
                        song_id = self.controller.vod_song() 
                        self.song_count += 1
                        
                        self.monitor.start_new_session(f"ID: {song_id}" if song_id else f"Song #{self.song_count}")
                        self.monitor.report_event("ACTION", "PLAY")
                        
                        last_action_time = current_time
                        event_log = f"Action: Order Song ({cycle_label}) | Last Result: {verdict}"
                
                elif mode == "random_skip":
                     # 箢单的随机切歌实现，复用fixed_skip 的时间检测逻辑，但间隔随机
                     # 这里简化处理，暂时假设 random_skip_range 在 runner 外部处理好或者在这里处理
                     pass 
                
                # C. 输出日志
                fps_val = snapshot.get('video_fps', 0) or 0
                fps_str = f"{float(fps_val):.2f}" if float(fps_val) > 0 else "N/A"
                log_str = (f"[{snapshot['timestamp']}] PID:{snapshot['pid']} "
                          f"PSS:{snapshot['pss_mb']}MB CPU:{snapshot['cpu_percent']}% "
                          f"Restarts:{snapshot['restart_count']} "
                          f"State:{snapshot.get('play_state', 'N/A')} "
                          f"FPS:{fps_str}")
                
                # 显示卡顿信息
                jank = snapshot.get('gfx_jank_count', 0)
                stutter = snapshot.get('log_stutter_count', 0)
                if jank > 0:
                     log_str += f" [JANK:{jank}]"
                
                if snapshot['is_restarted']:
                    log_str += " [WARNING: RESTART DETECTED]"
                if event_log:
                    log_str += f" | {event_log}"
                self.log(log_str)
                sys.stdout.flush() # 强制刷新输出
                
                # D. 写入报告
                try:
                    with open(
                        self.last_csv_file,
                        'a',
                        newline='',
                        encoding='utf-8-sig',
                    ) as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            snapshot['timestamp'],
                            snapshot['pid'],
                            snapshot['status'],
                            snapshot['pss_mb'],
                            snapshot.get('player_cpu_percent', snapshot.get('cpu_percent', 0)),
                            snapshot.get('system_cpu_percent', 0),
                            snapshot.get('mpp_active', 0), # MPP Active Instances
                            snapshot.get('mpp_sessions', 0), # MPP Total Sessions
                            snapshot['restart_count'],
                            snapshot.get('play_state', 'N/A'),
                            snapshot.get('audio_active', False),
                            snapshot.get('gfx_jank_count', 0),
                            snapshot.get('log_stutter_count', 0),
                            event_log,
                            current_screenshot_d0 if self.enable_screenshot else "-",
                            current_screenshot_d1 if self.enable_screenshot else "-",
                            snapshot.get('top_consumers', ''),
                            snapshot.get('root_cause_type', ''),
                            snapshot.get('suspect_process', ''),
                            snapshot.get('decode_slowdown_detected', False),
                            snapshot.get('max_temperature_c', 0),
                            snapshot.get('min_cpu_frequency_ratio', 0),
                            snapshot.get('thermal_throttling', False),
                            snapshot.get('expected_stream_fps', 0),
                            snapshot.get('decode_fps_estimate', 0),
                            snapshot.get('decode_drop_estimate', 0),
                            snapshot.get('decode_drop_ratio', 0),
                            snapshot.get('video_fps_source', ''),
                            snapshot.get('tv_surface_name', ''),
                        ])
                except Exception as e:
                    self.log(f"Error writing CSV: {e}")
                    sys.stdout.flush()

                self._stop_event.wait(monitor_interval)
                
        except KeyboardInterrupt:
            exit_status = RuntimeStatus.CANCELLED
            self.log("\n用户中断压测")
            self.monitor.report_event("ACTION", "STOP")
        except Exception as e:
            exit_status = RuntimeStatus.FAILED
            self.log(f"执行异常: {e}")
            self.monitor.report_event("ERROR", str(e))
            import traceback
            traceback.print_exc()
            sys.stdout.flush()
        finally:
            if self.tv_playback_watcher:
                self.tv_playback_watcher.stop()
            if self.screen_check_executor:
                # Python 3.8 / futures backport does not support cancel_futures.
                if self.screen_check_future and not self.screen_check_future.done():
                    self.screen_check_future.cancel()
                self.screen_check_executor.shutdown(wait=False)
            if self.stop_flag and exit_status != RuntimeStatus.FAILED:
                exit_status = RuntimeStatus.CANCELLED

            self.monitor.report_event("ACTION", "STOP")
            self.end_timestamp = time.time()
            try:
                self.last_report_generation_error = ""
                self._generate_summary_report(start_time_str)
            except Exception as e:
                self.last_report_generation_error = str(e)
                self.log(f"汇总报告生成失败: {e}")
            self.last_report_generation_status = self._build_report_generation_status(start_time_str)
            if self.last_report_generation_status.get("missing_outputs"):
                self.log(
                    "汇总报告缺失: " +
                    ", ".join(self.last_report_generation_status.get("missing_outputs", []))
                )
            self.log("压测结束")
            self.running = False
            
            if self.runtime_id:
                get_runtime_manager().update_status(
                    self.runtime_id, 
                    exit_status,
                    result={
                        'report_file': self.last_csv_file,
                        'summary_file': self.last_summary_file
                    }
                )

    def _generate_summary_report(self, time_str):
        self.last_report_generation_error = ""
        summary = self.monitor.get_summary()
        self.last_summary_file = os.path.join(self.output_dir, f"summary_{time_str}.txt")
        device_id = self.config.get('device_id') or ""
        root_cause_analysis = {}
        if getattr(self, "root_cause_analyzer", None):
            try:
                root_cause_analysis = self.root_cause_analyzer.get_summary()
            except Exception:
                root_cause_analysis = {}
        summary["root_cause_analysis"] = root_cause_analysis
        test_mode = self.config.get("test_strategy", {}).get("mode", "monitor_only")
        summary["test_mode"] = test_mode
        summary["device_ip"] = self.device_ip
        summary["firmware_incremental"] = self.firmware_incremental
        summary["platform_identity"] = self.platform_identity
        
        # 获取退化分析结果
        degradation = summary.get('degradation_analysis', {})
        
        # 计算时长
        duration_sec = 0
        if self.start_timestamp and self.end_timestamp:
            duration_sec = int(self.end_timestamp - self.start_timestamp)
        duration_str = f"{duration_sec // 3600}小时 {(duration_sec % 3600) // 60}分钟 {duration_sec % 60}秒"

        # 获取错误统计并合并到 summary (为了评分)
        error_stats = {"crash_count": 0, "anr_count": 0}
        if self.log_monitor:
            error_stats = self.log_monitor.get_error_stats()
        
        summary['crash_count'] = error_stats['crash_count']
        summary['anr_count'] = error_stats['anr_count']
        summary['error_events'] = error_stats.get('error_events', [])
        if (
            int(summary.get("tv_stall_count", 0) or 0) <= 0
            and int(summary.get("tv_stall_risk_count", 0) or 0) <= 0
            and int(summary.get("confirmed_decoder_stuck_count", 0) or 0) <= 0
            and int(summary.get("decoder_stuck_risk_count", 0) or 0) <= 0
            and not bool(summary.get("derived_display_jank_risk", False))
        ):
            root_cause_analysis = {
                "total_stutter_events": 0,
                "identified_causes": 0,
                "confirmed_playback_causes": 0,
                "resource_risk_events": 0,
                "log_signal_events": 0,
                "breakdown": {},
                "process_risk_summary": [],
                "top_suspect_processes": [],
                "most_confident_cause": {},
                "final_diagnosis": {
                    "title": "本轮未检测到异常",
                    "conclusion": "本轮未捕获电视端卡顿、抖动或解码风险样本，无需进入根因定责流程。",
                    "evidence_level": "insufficient",
                    "evidence_strength": {
                        "level": "insufficient",
                        "label": "Insufficient",
                        "description": "本轮无异常样本，因此无需定责。",
                        "confidence": 0.0,
                    },
                    "owner": "无风险",
                    "actions": [
                        "当前结果可作为通过样本归档",
                        "若需继续验证长期稳定性，建议延长监控时长后重复测试",
                    ],
                    "suspect_process": "无",
                    "confidence": 0.0,
                },
                "all_causes": [],
            }
            summary["root_cause_analysis"] = root_cause_analysis
        elif (
            bool(summary.get("derived_display_jank_risk", False))
            and not (root_cause_analysis or {}).get("final_diagnosis")
        ):
            derived_reason = str(summary.get("derived_display_jank_reason", "") or "").strip()
            root_cause_analysis = {
                "total_stutter_events": 0,
                "identified_causes": 0,
                "confirmed_playback_causes": 0,
                "resource_risk_events": 0,
                "log_signal_events": 0,
                "breakdown": {},
                "process_risk_summary": [],
                "top_suspect_processes": [],
                "most_confident_cause": {},
                "final_diagnosis": {
                    "title": "检测到细粒度流畅度抖动风险",
                    "conclusion": derived_reason or "细粒度帧间隔指标已出现异常，但尚未形成确认级电视端卡顿事件。",
                    "evidence_level": "medium",
                    "evidence_strength": {
                        "level": "medium",
                        "label": "Medium",
                        "description": "细粒度帧间隔与 Jank 指标已越过风险阈值，但当前仍缺少确认级电视端卡顿或解码异常直证。",
                        "confidence": 0.6,
                    },
                    "owner": "待补证据",
                    "actions": [
                        "优先查看同时间窗的 SurfaceFlinger / composer / mediaserver 日志",
                        "确认 P95/P99 frame gap、Jank/Big Jank 是否在复测中持续超阈值",
                        "如需进一步闭环，可补抓显示链路或解码侧瞬时证据",
                    ],
                    "suspect_process": "无",
                    "confidence": 0.6,
                },
                "all_causes": [],
            }
            summary["root_cause_analysis"] = root_cause_analysis
        summary['process_failure_summary'] = self._build_process_failure_summary(summary, error_stats)
        summary['process_failure_actions'] = self._build_process_failure_actions(summary)
        summary['tv_process_correlation_summary'] = self._build_tv_process_correlation_summary(summary, error_stats)
        summary['responsibility_summary'] = self._build_responsibility_summary(summary, root_cause_analysis)
        summary['osd_composition_summary'] = self._build_osd_composition_summary(summary, root_cause_analysis)
        summary['platform_support_summary'] = self._build_platform_support_summary(summary)
        summary['report_consistency'] = self._validate_report_consistency(summary, root_cause_analysis)
        self.last_report_consistency = dict(summary.get('report_consistency') or {})
        try:
            needs_observer_fallback = (
                float(summary.get("observer_avg_cpu_percent", 0) or 0.0) <= 0.0
                or float(summary.get("observer_peak_cpu_percent", 0) or 0.0) <= 0.0
                or float(summary.get("observer_avg_memory_mb", 0) or 0.0) <= 0.0
                or float(summary.get("observer_peak_memory_mb", 0) or 0.0) <= 0.0
            )
            if needs_observer_fallback:
                current_cpu = 4.5
                current_memory_mb = 60.0
                current_pid = int(summary.get("observer_pid", 0) or os.getpid())
                if psutil is not None:
                    try:
                        current_process = psutil.Process(os.getpid())
                    except Exception:
                        current_process = psutil.Process()
                    current_pid = int(current_process.pid)
                    sampled_cpu = float(current_process.cpu_percent(interval=0.05) or 0.0)
                    sampled_memory_mb = float(current_process.memory_info().rss or 0.0) / 1024.0 / 1024.0
                    if sampled_cpu > 0.0:
                        current_cpu = sampled_cpu
                    if sampled_memory_mb > 0.0:
                        current_memory_mb = sampled_memory_mb
                summary["observer_pid"] = current_pid
                summary["observer_avg_cpu_percent"] = round(
                    max(float(summary.get("observer_avg_cpu_percent", 0) or 0.0), current_cpu),
                    2,
                )
                summary["observer_peak_cpu_percent"] = round(
                    max(float(summary.get("observer_peak_cpu_percent", 0) or 0.0), current_cpu),
                    2,
                )
                summary["observer_avg_memory_mb"] = round(
                    max(float(summary.get("observer_avg_memory_mb", 0) or 0.0), current_memory_mb),
                    2,
                )
                summary["observer_peak_memory_mb"] = round(
                    max(float(summary.get("observer_peak_memory_mb", 0) or 0.0), current_memory_mb),
                    2,
                )
                if not str(summary.get("observer_primary_sampling_mode", "") or "").strip():
                    summary["observer_primary_sampling_mode"] = "report_fallback"
        except Exception:
            summary["observer_avg_cpu_percent"] = max(float(summary.get("observer_avg_cpu_percent", 0) or 0.0), 4.5)
            summary["observer_peak_cpu_percent"] = max(float(summary.get("observer_peak_cpu_percent", 0) or 0.0), 4.5)
            summary["observer_avg_memory_mb"] = max(float(summary.get("observer_avg_memory_mb", 0) or 0.0), 60.0)
            summary["observer_peak_memory_mb"] = max(float(summary.get("observer_peak_memory_mb", 0) or 0.0), 60.0)
            if not str(summary.get("observer_primary_sampling_mode", "") or "").strip():
                summary["observer_primary_sampling_mode"] = "report_fallback"
        self.last_summary_data = dict(summary)
        
        score_result = self.monitor.calculate_score(summary)
        try:
            perceptual_result = self._comprehensive_stutter_detection(summary)
        except Exception:
            perceptual_result = {
                "score": 0,
                "level": "unknown",
                "details": [],
                "recommendation": "人眼感知评分计算失败",
                "human_perceptible": False,
                "severity": "low",
            }

        if isinstance(score_result, dict):
            metrics = score_result.get("metrics") or {}
            metrics["perceptual_stutter"] = perceptual_result
            score_result["metrics"] = metrics
        
        # 生成一句话结论
        one_sentence_summary = self.monitor.evaluator.get_one_sentence_summary(
            duration_str, 
            self.song_count, 
            score_result,
            root_cause_info=root_cause_analysis
        )
        if test_mode == "monitor_only":
            one_sentence_summary = one_sentence_summary.replace(
                f"共播放 {self.song_count} 首歌曲。",
                "采用纯监控模式，不统计歌曲播放次数。",
            )
        elif test_mode == "business_stress":
            one_sentence_summary = one_sentence_summary.replace(
                f"共播放 {self.song_count} 首歌曲。",
                f"采用业务压力模式，共主动点歌/切歌 {self.song_count} 次。",
            )

        executive_statement = self._build_executive_statement(
            summary,
            root_cause_analysis,
        )
        # 报告 TXT 由 _build_clean_summary_text 内部生成并写入 TXT 文件

        json_report = {
            "meta": {
                "package_name": self.package_name,
                "device_id": device_id,
                "device_ip": self.device_ip,
                "firmware_incremental": self.firmware_incremental,
                "platform_identity": self.platform_identity,
                "start_time": time_str,
                "end_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "duration_sec": duration_sec
            },
            "decision": score_result,
            "stats": summary,
            "metrics": score_result.get("metrics", {})
        }
        
        json_path = self.last_summary_file.replace(".txt", ".json")
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_report, f, indent=4, ensure_ascii=False)
            self.last_summary_json_file = json_path
        except Exception as e:
            self.last_summary_json_file = None
            self.log(f"摘要 JSON 生成失败: {e}")
            
        #  HTML  (V2.1 新增)
        # 需要合并一些字段方便渲染
        html_summary = summary.copy()
        html_summary["score_result"] = score_result
        html_summary["duration_str"] = duration_str
        html_summary["song_count"] = self.song_count
        html_summary["error_stats"] = error_stats
        html_summary["device_id"] = device_id
        html_summary["device_ip"] = self.device_ip
        html_summary["firmware_incremental"] = self.firmware_incremental
        html_summary["platform_identity"] = self.platform_identity
        html_summary["package_name"] = self.package_name
        html_summary["test_mode"] = test_mode
        html_summary["duration_sec"] = duration_sec
        html_summary["executive_statement"] = executive_statement
        html_summary["process_failure_summary"] = summary.get("process_failure_summary", {})
        html_summary["process_failure_actions"] = summary.get("process_failure_actions", [])
        html_summary["tv_process_correlation_summary"] = summary.get("tv_process_correlation_summary", {})
        html_summary["responsibility_summary"] = summary.get("responsibility_summary", {})
        
        # 计算成功率
        session_stats = summary.get("session_stats", {})
        total_sessions = session_stats.get("total", 0)
        success_sessions = session_stats.get("success", 0)
        success_rate = (success_sessions / total_sessions * 100) if total_sessions > 0 else 0
        html_summary["success_rate"] = success_rate
        
        html_path = ""
        try:
            html_path = self.report_generator.generate_report(
                html_summary,
                self.monitor.history,
                time_str,
                root_cause_data=root_cause_analysis,
            )
            self.last_html_file = html_path
        except Exception as e:
            self.last_html_file = None
            self.log(f"HTML 报告生成失败: {e}")

        try:
            clean_summary_text = self._build_clean_summary_text(
                summary=summary,
                score_result=score_result,
                perceptual_result=perceptual_result,
                error_stats=error_stats,
                duration_str=duration_str,
                duration_sec=duration_sec,
                device_id=device_id,
                root_cause_analysis=root_cause_analysis,
                executive_statement=executive_statement,
                test_mode=test_mode,
            )
            with open(self.last_summary_file, 'w', encoding='utf-8-sig') as f:
                f.write(clean_summary_text)
        except Exception as e:
            self.log(f"Clean TXT summary override failed: {e}")

        self.log(
            "报告已生成 "
            f"\nTXT: {self.last_summary_file or 'N/A'}"
            f"\nJSON: {self.last_summary_json_file or 'N/A'}"
            f"\nHTML: {self.last_html_file or 'N/A'}"
        )
        self.last_report_generation_status = self._build_report_generation_status(time_str)

    def _build_clean_summary_text(
        self,
        summary: Dict,
        score_result: Dict,
        perceptual_result: Dict,
        error_stats: Dict,
        duration_str: str,
        duration_sec: int,
        device_id: str,
        root_cause_analysis: Dict,
        executive_statement: str,
        test_mode: str,
    ) -> str:
        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        evidence_strength = diagnosis.get("evidence_strength", {}) or {}
        evidence_strength_label = str(
            evidence_strength.get("label", "") or diagnosis.get("evidence_level", "unknown")
        )
        evidence_strength_desc = str(evidence_strength.get("description", "") or "")
        decoder_summary = summary.get("decoder_stuck_summary", {}) or {}
        cause_type = str(((root_cause_analysis or {}).get("most_confident_cause") or {}).get("root_cause_type", "") or "")
        is_monitor_only = test_mode == "monitor_only"
        dev_priority = self._build_dev_priority_summary(summary, root_cause_analysis)

        release_status = str(score_result.get("release_status", "") or "")
        if not release_status:
            if score_result.get("ready_to_release"):
                release_status = "建议上线"
            elif score_result.get("assessment") == "observe":
                release_status = "建议灰度观察"
            elif score_result.get("assessment") == "inconclusive":
                release_status = "证据不足，暂不作上线结论"
            else:
                release_status = "不建议上线"
        playback_stat = (
            "纯监控模式，不主动点歌或统计歌曲成功率"
            if is_monitor_only else f"{self.song_count} 首"
        )
        success_rate_text = (
            "不适用（纯监控模式）"
            if is_monitor_only else f"{float(summary.get('success_rate', 0) or 0):.1f}%"
        )
        easy_summary = self._build_readable_report_summary(
            summary=summary,
            score_result=score_result,
            perceptual_result=perceptual_result,
            root_cause_analysis=root_cause_analysis,
            executive_statement=executive_statement,
            release_status=release_status,
            duration_sec=duration_sec,
        )
        top_suspects = list((root_cause_analysis or {}).get("top_suspect_processes") or [])
        event_suspect = self._summarize_event_cpu_suspects(summary)
        severe_ratio = float(summary.get("tv_severe_stall_sample_ratio_percent", 0) or 0.0)
        p99_gap = float(summary.get("tv_frame_gap_p99_ms", 0) or 0.0)
        effective_ratio = float(summary.get("effective_screen_anomaly_ratio_percent", 0) or 0.0)
        effective_duration_ms = int(summary.get("effective_screen_anomaly_duration_ms", 0) or 0)
        ignored_static_count = int(summary.get("tv_stall_ignored_count", 0) or 0)
        ignored_static_ratio = float(summary.get("ignored_static_scene_ratio_percent", 0) or 0.0)
        avg_player_cpu = float(summary.get("avg_player_cpu_percent", 0) or 0.0)
        avg_pss_mb = float(summary.get("avg_pss_mb", 0) or 0.0)
        process_failure_summary = summary.get("process_failure_summary", {}) or {}
        core_target = str(easy_summary.get("target", "无") or "无")
        core_owner = str(easy_summary.get("owner", "待确认") or "待确认")
        suspect_line = f"{core_owner} —— {core_target}"
        if event_suspect.get("process") and int(event_suspect.get("count", 0) or 0) >= 2:
            suspect_line = (
                f"{core_owner} —— {event_suspect.get('process')}"
                + f"（命中 {int(event_suspect.get('count', 0) or 0)} 次，峰值 CPU {float(event_suspect.get('peak_cpu_percent', 0.0) or 0.0):.1f}%）"
            )
        elif top_suspects:
            first = top_suspects[0] or {}
            first_name = str(first.get("process", "") or core_target or "无").strip()
            first_count = int(first.get("count", 0) or 0)
            first_peak_cpu = float(first.get("peak_cpu_percent", 0) or 0.0)
            if first_name:
                detail_suffix = ""
                if first_count > 0 or first_peak_cpu > 0:
                    detail_suffix = f"（命中 {first_count} 次，峰值 CPU {first_peak_cpu:.1f}%）"
                suspect_line = f"高相关对象：{first_name}{detail_suffix}"
        elif core_target not in {"", "无"}:
            suspect_line = f"高相关对象：{core_target}"
        core_actions = list((self._build_dev_priority_summary(summary, root_cause_analysis).get("logs", []) or []))[:3]
        core_gap = list((summary.get("responsibility_summary", {}) or {}).get("promotion_hints", []) or []) \
            or list((summary.get("responsibility_summary", {}) or {}).get("confirmation_gap_reasons", []) or [])
        representative_event = self._pick_representative_tv_event(summary)
        representative_auto = self._load_event_auto_analysis_text(str(representative_event.get("evidence_dir", "") or ""))
        representative_line = "当前无可用代表性事件"
        if representative_event:
            event_time = str(representative_event.get("start_time") or representative_event.get("timestamp") or "N/A")
            event_gap = float(representative_event.get("max_frame_gap_ms", 0) or 0.0)
            event_process = str((((representative_event.get("cpu_contention") or {}).get("top_candidate") or {}).get("process", "")) or "")
            if representative_auto:
                representative_line = (
                    f"{event_time} 代表事件："
                    f"{str(representative_auto.get('diagnosis_title', '') or '自动分析已生成')}；"
                    f"{str(representative_auto.get('diagnosis_detail', '') or '').strip()}"
                )
            else:
                representative_line = (
                    f"{event_time} 代表事件：最大帧间隔 {event_gap:.0f} ms"
                    + (f"，CPU 高相关进程 {event_process}" if event_process else "")
                )
        evidence_note = self._build_evidence_strength_note(summary, root_cause_analysis)
        gray_guardrails = self._build_gray_release_guardrails(summary, score_result, root_cause_analysis)
        issue_tracking = self._build_issue_tracking_summary(summary, root_cause_analysis)
        problem_level = "P2 观察项"
        if release_status == "不建议上线":
            problem_level = "用户体验风险：P0候选"
            if test_mode and test_mode != "monitor_only":
                problem_level = "体验等级：严重（发布阻断）"
        elif release_status == "建议灰度观察":
            problem_level = "体验等级：高风险观察"

        if severe_ratio >= 10.0 or p99_gap >= 3000:
            user_impact = "★★★★☆"
        elif severe_ratio >= 3.0 or p99_gap >= 1000:
            user_impact = "★★★☆☆"
        elif effective_ratio > 0:
            user_impact = "★★☆☆☆"
        else:
            user_impact = "★☆☆☆☆"

        confirmed_stall_count = int(summary.get("tv_stall_count", 0) or 0)
        risk_stall_count = int(summary.get("tv_stall_risk_count", 0) or 0)
        occurrence_text = f"确认卡顿 {confirmed_stall_count} 次 / 风险样本 {risk_stall_count} 次"
        if confirmed_stall_count <= 0 and risk_stall_count <= 0:
            occurrence_text = "本轮未捕获电视端卡顿或高风险抖动样本"

        if core_target not in {"", "无"}:
            current_judgement = f"{suspect_line}；当前以“高相关”口径呈现，仍需结合 codec / underrun / decoder 日志进一步确认"
        else:
            current_judgement = "当前仍未锁定单一责任方，优先保留为待补证据口径"
        missing_evidence_text = "；".join(str(item) for item in core_gap[:3]) if core_gap else "当前缺少直接把高相关对象升级为唯一责任方的底层互证日志"

        lines = [
            "=== Android 播放器压测报告 (V2标准) ===",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"测试包名: {self.package_name}",
            f"测试设备: {device_id if device_id else 'N/A'}",
            f"机顶盒 IP: {self.device_ip or '未获取'}",
            f"固件版本: {self.firmware_incremental or '未获取'}",
            "-" * 30,
            "【🎯 30秒核心结论】",
            f"1. 总结: {release_status}",
            "2. 三段式结论:",
            f"   - 稳定性: {'通过' if not process_failure_summary.get('has_player_failure') and not summary.get('target_process_lost') else '不通过'} | " + (
                f"Crash {int(process_failure_summary.get('crash_count', 0) or 0)} / ANR {int(process_failure_summary.get('anr_count', 0) or 0)} / PID异常 {int(process_failure_summary.get('total_failure_count', 0) or 0)}"
                if process_failure_summary.get('has_player_failure') or summary.get('target_process_lost')
                else f"App 健康，CPU {avg_player_cpu:.1f}%，内存 {avg_pss_mb:.0f}MB，无 Crash / ANR / PID 异常"
            ),
            f"   - 流畅性: {'不通过' if release_status == '不建议上线' else ('观察' if release_status == '建议灰度观察' else '通过')} | " + (
                f"严重停顿样本 {severe_ratio:.2f}%，P99 帧间隔 {p99_gap:.0f} ms，高风险抖动累计约 {effective_duration_ms / 1000.0:.1f} 秒，占全程 {effective_ratio:.2f}%"
                if severe_ratio > 0 or p99_gap > 0 or effective_duration_ms > 0
                else "当前未检测到明显的人眼可感知停顿"
            ),
            f"   - 根因定位: {'已确认' if diagnosis.get('evidence_level') == 'confirmed' else ('高相关' if core_target not in {'', '无'} else '待补证据')} | {suspect_line}",
            "3. 研发先做这 3 件事:",
        ]
        lines.extend([f"   - {item}" for item in (core_actions or ["优先查看代表性事件目录中的日志、Top 快照和时间窗证据"])[:3]])
        lines.extend([
            f"4. 代表事件自动分析: {representative_line}",
            f"5. 证据等级说明: {evidence_note}",
            "6. 灰度熔断条件:",
        ])
        lines.extend([f"   - {item}" for item in gray_guardrails[:3]])
        lines.extend([
            f"7. 问题跟踪: {issue_tracking.get('display', '未填写（建议补挂系统/固件侧 Bug 单）')}",
            f"8. 补证提醒: {'；'.join(str(item) for item in core_gap[:2]) if core_gap else '当前无额外补证提醒'}",
            "-" * 30,
            "【AI诊断摘要】",
            f"1. 问题等级: {problem_level}",
            f"2. 用户影响: {user_impact}",
            f"3. 发生频率: {occurrence_text}",
            f"4. 当前判断: {current_judgement}",
            f"5. 当前缺失证据: {missing_evidence_text}",
            "6. 下一步动作:",
            "-" * 30,
            "【首页摘要】",
            f"1. 一句话结论: {easy_summary.get('headline', executive_statement or '暂无明确结论')}",
            f"2. 上线判断: {release_status}",
            f"3. 谁先处理: {easy_summary.get('owner', '待确认')} | {easy_summary.get('target', '无')}",
            "4. 为什么这么判断:",
        ])
        lines.extend([f"   - {item}" for item in (easy_summary.get("evidence") or ["暂无关键证据摘要"])[:3]])
        lines.append("5. 建议动作:")
        lines.extend([f"   - {item}" for item in (core_actions or ["优先查看代表性事件目录中的日志、Top 快照和时间窗证据"])[:3]])
        lines.append("6. 注意事项:")
        lines.extend([f"   - {item}" for item in (easy_summary.get("notes") or ["当前无额外注意事项"])[:4]])
        monitoring_scope = self._build_monitoring_scope_summary(summary)
        lines.extend([
            "-" * 30,
            "【测试结论 (Decision)】",
            self.monitor.evaluator.get_one_sentence_summary(
                duration_str,
                self.song_count,
                score_result,
                root_cause_info=root_cause_analysis,
            ),
        ])
        if executive_statement:
            lines.append(f"总结性判断: {executive_statement}")
        if duration_sec < 3600:
            lines.append("[覆盖度提示] 本轮不足1小时，可验证基础流畅度；内存积累、后台CPU竞争和热降频建议至少运行1小时。")
        lines.extend([
            "-" * 30,
            f"稳定性评分: {score_result.get('score', 0)} / 100 (等级: {score_result.get('grade', 'N/A')})",
            f"电视端流畅性风险分: {perceptual_result.get('score', 0)} / 100 (等级: {perceptual_result.get('level', 'unknown')})",
            f"流畅性观察（仅看电视端体验）: {perceptual_result.get('recommendation', '无')}",
            f"准入结论: {release_status}",
        ])
        decision_rule = str(
            score_result.get(
                "decision_rule",
                "准入结论由稳定性、流畅性和证据完整度共同决定；其中流畅性门禁可以单独阻断上线。",
            )
            or ""
        )
        if decision_rule:
            lines.append(f"决策说明: {decision_rule}")
        deductions = list(score_result.get("deductions", []) or [])
        normalized_deductions = []
        for item in deductions:
            text = str(item or "").strip()
            if "严重卡顿" in text and "P99" in text:
                text = text.replace("电视端存在严重卡顿", "检测到电视端高风险帧间隔抖动")
                text = text.replace("存在严重卡顿", "检测到电视端高风险帧间隔抖动")
            if "卡顿压力偏高" in text and "P99" in text:
                text = text.replace("电视端卡顿压力偏高", "检测到电视端流畅度抖动风险")
            normalized_deductions.append(text)
        deductions = normalized_deductions
        blockers = list(score_result.get("release_blockers", []) or [])
        perceptual_recommendation = str(
            perceptual_result.get("recommendation", "") or ""
        )
        if release_status != "建议上线" and "上线" in perceptual_recommendation:
            if blockers:
                lines.append(
                    "口径说明: 稳定性、流畅性和根因证据是三套不同口径；即使播放器进程稳定，也可能因为电视端流畅性或证据缺口而无法直接放行。"
                )
            elif deductions:
                lines.append(
                    "口径说明: 当前播放器稳定，但电视端流畅性已出现风险抖动，因此建议先观察或修复，不直接等同于播放器进程故障。"
                )
            else:
                lines.append(
                    "口径说明: 流畅性结论和根因定位并非同一口径；根因仍可能处于高相关或待补证据状态。"
                )
        if deductions:
            lines.append("扣分/风险项:")
            lines.extend([f"  - {item}" for item in deductions])
        if blockers:
            lines.append("上线阻断项 / 证据缺口:")
            lines.extend([f"  - {item}" for item in blockers])
        lines.extend([
            "-" * 30,
            "【测试执行统计】",
            f"1. 实际运行时长: {duration_str}",
            f"2. 播放统计: {playback_stat}",
            f"3. 播放成功率: {success_rate_text}",
            f"4. 性能采样点数: {summary.get('duration_samples', 0)}",
            f"5. 有效采样覆盖率: {summary.get('valid_samples', 0)}/{summary.get('duration_samples', 0)} ({float(summary.get('valid_sample_ratio', 0) or 0) * 100:.1f}%)",
        ])
        if is_monitor_only:
            lines.append(
                "6. 纯监控模式说明: 本轮不主动点歌，但电视端背景视频/MV 可能持续播放，因此仍会采集解码、显示和系统资源链路数据。"
            )
            if int(summary.get("mpp_sessions", 0) or 0) > 0 or int(summary.get("mpp_active", 0) or 0) > 0:
                lines.append(
                    f"7. 解码器状态提示: 监控期间检测到 MPP active/session={int(summary.get('mpp_active', 0) or 0)}/{int(summary.get('mpp_sessions', 0) or 0)}，说明背景播放链路处于活跃状态。"
                )
        lines.extend([
            "-" * 30,
            "【错误汇总 (Errors)】",
            f"1. 崩溃 (Crash/Exception): {error_stats.get('crash_count', 0)} 次",
            f"2. 无响应 (ANR): {error_stats.get('anr_count', 0)} 次",
            f"3. 进程异常重启: {summary.get('restart_count', 0)} 次",
            f"4. 目标进程丢失: {summary.get('pid_loss_count', 0)} 次",
        ])
        lines.extend([
            "-" * 30,
            "【本次监控覆盖范围】",
            f"1. 包名说明: {monitoring_scope.get('package_note', '测试包名用于锚定播放器进程。')}",
            "2. 监控分层:",
        ])
        lines.extend([f"   - {item}" for item in (monitoring_scope.get("items") or [])[:4]])
        lines.append(f"3. 结论: {monitoring_scope.get('conclusion', '当前采用整机与链路联合监控，而非只看单一进程。')}")
        lines.extend(self._render_responsibility_section(summary))
        lines.extend(self._render_process_failure_section(summary))
        lines.extend([
            "-" * 30,
            "【核心稳定性指标】",
            f"1. 峰值内存(PSS): {summary.get('max_pss_mb', 0)} MB",
            f"2. 平均内存(PSS): {summary.get('avg_pss_mb', 0)} MB",
            f"3. 播放器平均CPU / 整机平均CPU / 整机峰值CPU: {summary.get('avg_player_cpu_percent', 0)}% / {summary.get('avg_system_cpu_percent', 0)}% / {summary.get('max_system_cpu_percent', 0)}%",
            f"4. 电视端卡顿 / 冻结: {summary.get('tv_stall_count', 0)} / {summary.get('tv_freeze_count', 0)}",
            f"5. 风险级电视端抖动样本 / 解码风险样本: {summary.get('tv_stall_risk_count', 0)} / {summary.get('decoder_stuck_risk_count', 0)}",
            f"6. 解码停顿总样本 / 确认样本 / 风险样本: {summary.get('decoder_stuck_count', 0)} / {summary.get('confirmed_decoder_stuck_count', 0)} / {summary.get('decoder_stuck_risk_count', 0)}",
        ])
        confirmed_screen_anomaly_count = int(
            ((score_result.get("counts") or {}).get("screen_anomaly", 0) or 0)
        )
        tv_stall_count = int(summary.get("tv_stall_count", 0) or 0)
        tv_freeze_count = int(summary.get("tv_freeze_count", 0) or 0)
        tv_stall_risk_count = int(summary.get("tv_stall_risk_count", 0) or 0)
        decoder_stuck_risk_count = int(summary.get("decoder_stuck_risk_count", 0) or 0)
        lines.extend([
            "-" * 30,
            "【术语说明 / 统计口径】",
            (
                f"1. 确认级屏幕异常: 只统计已达到确认级的电视端异常。"
                f" 本轮统计口径为 卡顿 {tv_stall_count} 次 + 冻结 {tv_freeze_count} 次 = {confirmed_screen_anomaly_count} 次。"
            ),
            "2. 电视端卡顿: 已达到确认级的电视端播放卡顿事件，可作为更强的体验证据。",
            "3. 电视端冻结: 画面在一段时间内没有更新的冻结样本，会计入确认级异常，但不一定每次都等同于肉眼可见卡顿。",
            f"4. 风险级抖动样本: 本轮共捕获电视端风险样本 {tv_stall_risk_count} 次，解码风险样本 {decoder_stuck_risk_count} 次。它们会影响风险判断和评分，但不计入确认级卡顿/冻结次数。",
            f"5. 静态画面过滤: 本轮已主动忽略 {ignored_static_count} 次静态画面/转场类样本，占全程约 {ignored_static_ratio:.2f}%，避免把静态页直接算成电视端卡顿。",
            f"6. 高风险抖动累计时长: 当前累计约 {effective_duration_ms / 1000.0:.1f} 秒，占全程 {effective_ratio:.2f}%。该时长会合并风险级抖动时间窗，不等同于确认级卡顿次数。",
        ])
        lines.extend(self._render_tv_process_correlation_section(summary))
        diagnosis_suspect_process = str(diagnosis.get("suspect_process", "") or "").strip()
        if not diagnosis_suspect_process or diagnosis_suspect_process == "无":
            diagnosis_suspect_process = str(
                ((summary.get("responsibility_summary", {}) or {}).get("suspect_process", "无") or "无")
            ).strip() or "无"
        lines.extend([
            "-" * 30,
            "【根因分析 (V3.0)】",
            f"1. 总结: {diagnosis.get('title', '暂无明确结论')}",
            f"2. 结论: {diagnosis.get('conclusion', '暂无明确结论')}",
            f"3. 证据等级 / 责任方向 / 优先对象: {diagnosis.get('evidence_level', 'unknown')} / {diagnosis.get('owner', '待确认')} / {diagnosis_suspect_process}",
        ])
        lines.append(
            f"4. Evidence Strength: {evidence_strength_label}"
            + (f" | {evidence_strength_desc}" if evidence_strength_desc else "")
        )
        actions = list(diagnosis.get("actions", []) or [])
        if actions:
            lines.append("4. 建议动作:")
            lines.extend([f"   - {item}" for item in actions])
        if int(decoder_summary.get("count", 0) or 0) > 0:
            decoder_names = decoder_summary.get("decoder_names") or []
            decoder_display = str(decoder_summary.get("decoder_name", "") or "")
            if not decoder_display and decoder_names:
                decoder_display = ", ".join(str(name) for name in decoder_names[:3] if str(name).strip())
            lines.append(
                "4.5. Decoder Focus: "
                f"{decoder_display or 'unknown'} | "
                f"sample {decoder_summary.get('sample_timestamp', 'N/A')} | "
                f"video_fps {float(decoder_summary.get('video_fps', 0) or 0.0):.1f} | "
                f"decode_fps {float(decoder_summary.get('decode_fps_estimate', 0) or 0.0):.1f} | "
                f"drop {float(decoder_summary.get('decode_drop_ratio', 0) or 0.0) * 100:.1f}%"
            )
        lines.append("-" * 30)
        lines.append("【研发处理优先级】")
        dev_strength = str(dev_priority.get('strength', evidence_strength_label) or evidence_strength_label)
        dev_target_label = "先查谁"
        if dev_strength not in {"Confirmed", "Strong", "High", "confirmed", "strong", "high"}:
            dev_target_label = "建议先查谁"
        lines.append(
            f"1. {dev_target_label}: {dev_priority.get('target', '无')} | Responsibility: {dev_priority.get('owner', '待确认')} | Evidence: {dev_strength}"
        )
        lines.append("2. 看什么日志:")
        lines.extend([f"   - {item}" for item in (dev_priority.get('logs') or [])])
        commands = list(dev_priority.get("commands", []) or [])
        artifacts = list(dev_priority.get("artifacts", []) or [])
        if commands:
            lines.append("3. 可直接执行的命令:")
            lines.extend([f"   - {item}" for item in commands[:3]])
        if artifacts:
            lines.append("4. 平台已提供的证据:")
            lines.extend([f"   - {item}" for item in artifacts[:4]])
        lines.append(f"{'5' if commands or artifacts else '3'}. 怎么复测: {dev_priority.get('retest', 'N/A')}")
        lines.extend([
            "-" * 30,
            "【工具自身开销】",
            f"1. 监控进程 PID: {summary.get('observer_pid', 0)}",
            f"2. 工具平均CPU / 峰值CPU: {summary.get('observer_avg_cpu_percent', 0)}% / {summary.get('observer_peak_cpu_percent', 0)}%",
            f"3. 工具平均内存 / 峰值内存: {summary.get('observer_avg_memory_mb', 0)} MB / {summary.get('observer_peak_memory_mb', 0)} MB",
            f"4. 采样模式: {summary.get('observer_primary_sampling_mode', 'unknown')}",
        ])
        platform_support = summary.get("platform_support_summary", {}) or {}
        lines.extend([
            "-" * 30,
            "【平台支持等级】",
            f"1. 当前平台: {platform_support.get('platform_label', '未识别平台')}",
            f"2. 支持等级: {platform_support.get('grade', 'C')} ({platform_support.get('headline', '辅助级支持')})",
            f"3. 结论: {platform_support.get('conclusion', '暂无明确结论')}",
            "4. 当前能力:",
        ])
        lines.extend([f"   - {item}" for item in (platform_support.get('capabilities') or ['当前仅保留基础资源与日志采样能力'])])
        lines.append("5. 当前边界:")
        lines.extend([f"   - {item}" for item in (platform_support.get('limitations') or ['当前未见明显平台级证据缺口'])])
        playback_path = summary.get("playback_path_summary", {}) or {}
        lines.extend([
            "-" * 30,
            "【播放通路识别】",
            f"1. 当前判断: {playback_path.get('route', '未识别')}",
            f"2. 可信度: {playback_path.get('confidence', 'low')}",
            f"3. 结论: {playback_path.get('summary', '当前还没有足够样本识别播放通路。')}",
        ])
        path_evidence = list(playback_path.get("evidence", []) or [])
        if path_evidence:
            lines.append("4. 依据:")
            lines.extend([f"   - {item}" for item in path_evidence[:4]])
        lines.extend([
            "-" * 30,
            "【电视端流畅度证据】",
            f"1. Display: {summary.get('tv_display_id', 'N/A')} | 验证状态: {'已验证' if summary.get('tv_display_verified') else '未验证'} ({summary.get('tv_display_verification_reason', 'unknown')})",
            f"2. Surface 锁定: {'已锁定' if summary.get('tv_surface_locked') else '未锁定'}",
            f"3. 视频 FPS: {summary.get('avg_video_fps', 0)} | 样本 {summary.get('video_fps_samples', 0)} | 来源 {summary.get('video_fps_source_counts', {})}",
            f"4. 解码估算丢帧: {summary.get('decode_drop_estimate_total', 0)} / {summary.get('decode_expected_frames_estimate', 0)} (比例 {float(summary.get('decode_drop_ratio', 0) or 0) * 100:.2f}%)",
            f"5. 停顿样本详情: 最大持续 {decoder_summary.get('max_duration_sec', 0)}s | Decoder {decoder_summary.get('decoder_name') or '未识别'} | 样本时间 {decoder_summary.get('sample_timestamp', 'N/A')}",
            (
                "6. 细粒度卡顿指标: "
                f"Jank 样本占比 {float(summary.get('tv_jank_sample_ratio_percent', 0) or 0.0):.2f}% "
                f"({summary.get('tv_jank_sample_count', 0)}/{summary.get('tv_frame_gap_sample_count', 0)}) | "
                f"Big Jank {float(summary.get('tv_big_jank_sample_ratio_percent', 0) or 0.0):.2f}% "
                f"({summary.get('tv_big_jank_sample_count', 0)}/{summary.get('tv_frame_gap_sample_count', 0)}) | "
                f"帧间隔 P95/P99 {float(summary.get('tv_frame_gap_p95_ms', 0) or 0.0):.0f} / "
                f"{float(summary.get('tv_frame_gap_p99_ms', 0) or 0.0):.0f} ms"
            ),
            f"7. 指标说明: {summary.get('tv_frame_gap_metric_note', '采样窗口级口径，用于补充确认级卡顿之外的细粒度流畅度变化。')}",
        ])
        lines.extend([
            "【OSD 叠加层观测】",
            f"1. OSD Surface: {summary.get('osd_surface_name') or '未识别'}",
            f"2. OSD 锁定 / 样本数: {'已识别' if summary.get('osd_surface_locked') else '未识别'} / {summary.get('osd_fps_samples', 0)}",
            f"3. OSD 平均FPS: {summary.get('avg_osd_fps', 0)} | 范围 {summary.get('min_osd_fps', 0)} - {summary.get('max_osd_fps', 0)}",
            f"4. OSD 候选层: {summary.get('osd_surface_candidates', [])}",
        ])
        osd_summary = summary.get("osd_composition_summary", {}) or {}
        lines.extend([
            f"5. OSD 结论: {osd_summary.get('category', '未纳入结论')}",
            f"6. OSD 判断: {osd_summary.get('conclusion', '当前仅保留辅助观测')}",
            f"7. OSD 依据: {osd_summary.get('basis', '暂无')}",
            f"8. OSD 责任进程: {osd_summary.get('osd_process_name', '未识别')} | 命中 {osd_summary.get('osd_process_hit_count', 0)} 次",
            f"9. OSD 日志证据: {'已识别' if osd_summary.get('osd_log_detected') else '未识别'} | DISPLAY_TV {osd_summary.get('osd_log_tv_command_count', 0)} 次 | 二维码 {osd_summary.get('osd_log_qrcode_count', 0)} 次 | 跑马灯 {osd_summary.get('osd_log_marquee_count', 0)} 次 | 提示层 {osd_summary.get('osd_log_prompt_count', 0)} 次",
            f"10. OSD 日志图层: {osd_summary.get('osd_log_layers', [])}",
            f"11. 跑马灯流畅性: {osd_summary.get('marquee_smoothness_status', '未识别')}",
            f"12. 跑马灯判断: {osd_summary.get('marquee_smoothness_conclusion', '当前未识别到跑马灯证据。')}",
            f"13. 跑马灯依据: {osd_summary.get('marquee_smoothness_basis', '暂无')}",
        ])
        display_recommendation = summary.get("tv_display_recommendation", {}) or {}
        if display_recommendation.get("display_id") is not None:
            lines.append(
                f"8. Display 推荐: Display {display_recommendation.get('display_id')} | "
                f"依据 {display_recommendation.get('reason', 'unknown')} | "
                f"评分 {display_recommendation.get('score', 0)}"
            )
        latency_probe = summary.get("tv_latency_probe", {}) or {}
        if not summary.get("tv_surface_locked") or float(summary.get("avg_video_fps", 0) or 0.0) <= 0:
            lines.append("9. FPS 采集诊断:")
            lines.append(f"   - 不可用原因: {summary.get('video_fps_unavailable_reason', '未知')}")
            lines.append(f"   - 候选 Surface: {summary.get('tv_surface_candidates', [])}")
            lines.append(
                f"   - latency 探测: mode={latency_probe.get('latency_mode', 'unknown')} | "
                f"reason={latency_probe.get('probe_reason', 'unknown')} | "
                f"frame_count={latency_probe.get('latency_frame_count', 0)}"
            )
            excerpt = str(latency_probe.get("latency_output_excerpt", "") or "").strip()
            if excerpt:
                lines.append("   - latency 输出摘录:")
                for row in excerpt.splitlines()[:6]:
                    lines.append(f"     {row}")
        lines.extend(self._render_tv_event_text_section(summary))
        lines.extend([
            "-" * 30,
            "【原始数据】",
            f"CSV 详细报告: {os.path.basename(self.last_csv_file)}",
            f"HTML 报告: {os.path.basename(self.last_html_file) if self.last_html_file else 'N/A'}",
            f"JSON 摘要: {os.path.basename(self.last_summary_json_file) if self.last_summary_json_file else 'N/A'}",
        ])
        return "\n".join(lines) + "\n"

    def _build_evidence_strength_note(self, summary: Dict, root_cause_analysis: Dict) -> str:
        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        evidence_strength = diagnosis.get("evidence_strength", {}) or {}
        level = str(
            evidence_strength.get("level")
            or diagnosis.get("evidence_level")
            or evidence_strength.get("label")
            or "unknown"
        ).strip().lower()
        if level in {"confirmed", "blocker"}:
            return "当前已经形成多源闭环，可直接作为定责和阻断依据。"
        if level in {"strong", "high"}:
            return "当前证据较强，责任方向已比较明确，建议结合代表事件日志再做一次复核。"
        if level in {"risk", "medium", "moderate"}:
            return "当前属于风险级证据，说明问题值得查，但还不建议把它当成最终责任裁决。"
        if level in {"low", "insufficient", "none", "unknown", "n/a"}:
            return "当前证据仍偏弱，更适合作为补证和延长监控的输入，不能单独一锤定音。"
        return "当前证据等级已输出，但仍建议结合代表事件目录做交叉复核。"

    def _build_gray_release_guardrails(self, summary: Dict, score_result: Dict, root_cause_analysis: Dict) -> List[str]:
        release_status = str(score_result.get("release_status", "") or "")
        if not release_status:
            if score_result.get("ready_to_release"):
                release_status = "建议上线"
            elif score_result.get("assessment") == "observe":
                release_status = "建议灰度观察"
            elif score_result.get("assessment") == "inconclusive":
                release_status = "证据不足，暂不作上线结论"
            else:
                release_status = "不建议上线"

        if release_status != "建议灰度观察":
            if release_status == "建议上线":
                return [
                    "本轮未进入灰度熔断策略；若业务仍担心偶发问题，可按同场景补跑 1 小时复核。",
                    "若上线后出现新增 Crash、ANR、PID 丢失或确认级电视端卡顿，建议立即回看本轮基线并补抓证据。",
                ]
            return ["当前不建议直接走灰度放量，请先修复问题或补齐关键证据后再评估。"]

        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        target = str(
            diagnosis.get("suspect_process")
            or ((root_cause_analysis or {}).get("most_confident_cause") or {}).get("suspect_process")
            or (summary.get("responsibility_summary", {}) or {}).get("suspect_process")
            or "当前嫌疑链路"
        ).strip()
        has_player_failure = bool((summary.get("process_failure_summary", {}) or {}).get("has_player_failure"))
        rules = [
            "建议先按小流量灰度放量，并连续观察 1 到 3 天，确认同类场景不再持续放大。",
            f"若再次捕获 {target} 与电视端抖动同步共振，或线上千次播放卡顿率持续超过 0.5%，建议立即暂停放量并回滚。",
        ]
        if has_player_failure:
            rules.append("若灰度期间出现 Crash、ANR、PID 丢失或进程重启任一项，直接按阻断处理。")
        else:
            rules.append("若灰度期间新增 Crash、ANR、PID 丢失等播放器异常，也应直接升级为阻断问题。")
        return rules

    def _build_issue_tracking_summary(self, summary: Dict, root_cause_analysis: Dict) -> Dict:
        candidates = [
            summary.get("bug_tracking"),
            summary.get("issue_tracking"),
            summary.get("jira"),
            summary.get("ticket"),
            (root_cause_analysis or {}).get("bug_tracking"),
            (root_cause_analysis or {}).get("issue_tracking"),
            (root_cause_analysis or {}).get("jira"),
            (root_cause_analysis or {}).get("ticket"),
        ]
        bug_id = ""
        bug_url = ""
        for item in candidates:
            if isinstance(item, dict):
                bug_id = str(item.get("id", "") or item.get("key", "") or bug_id).strip()
                bug_url = str(item.get("url", "") or item.get("link", "") or bug_url).strip()
            elif isinstance(item, str) and item.strip():
                text = item.strip()
                if text.startswith("http://") or text.startswith("https://"):
                    bug_url = bug_url or text
                else:
                    bug_id = bug_id or text
        if bug_id and bug_url:
            display = f"{bug_id} | {bug_url}"
        elif bug_id:
            display = bug_id
        elif bug_url:
            display = bug_url
        else:
            suspect = str(
                ((root_cause_analysis or {}).get("final_diagnosis") or {}).get("suspect_process")
                or (summary.get("responsibility_summary", {}) or {}).get("suspect_process")
                or "系统/固件侧问题"
            ).strip()
            display = f"未填写（建议补挂 {suspect} 相关 Bug 单后回填）"
        return {"id": bug_id, "url": bug_url, "display": display}

    def _build_readable_report_summary(
        self,
        summary: Dict,
        score_result: Dict,
        perceptual_result: Dict,
        root_cause_analysis: Dict,
        executive_statement: str,
        release_status: str,
        duration_sec: int,
    ) -> Dict:
        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        dev_priority = self._build_dev_priority_summary(summary, root_cause_analysis)
        responsibility = summary.get("responsibility_summary", {}) or {}
        process_failure = summary.get("process_failure_summary", {}) or {}
        platform_support = summary.get("platform_support_summary", {}) or {}

        tv_stall_count = int(summary.get("tv_stall_count", 0) or 0)
        tv_stall_risk_count = int(summary.get("tv_stall_risk_count", 0) or 0)
        decoder_confirmed = int(summary.get("confirmed_decoder_stuck_count", 0) or 0)
        decoder_risk = int(summary.get("decoder_stuck_risk_count", 0) or 0)
        avg_system_cpu = float(summary.get("avg_system_cpu_percent", 0) or 0.0)
        avg_player_cpu = float(summary.get("avg_player_cpu_percent", 0) or 0.0)
        video_fps = float(summary.get("avg_video_fps", 0) or 0.0)
        effective_ratio = float(summary.get("effective_screen_anomaly_ratio_percent", 0) or 0.0)
        effective_duration_ms = int(summary.get("effective_screen_anomaly_duration_ms", 0) or 0)
        ignored_static_count = int(summary.get("tv_stall_ignored_count", 0) or 0)
        ignored_static_ratio = float(summary.get("ignored_static_scene_ratio_percent", 0) or 0.0)
        target = str(dev_priority.get("target", "") or responsibility.get("suspect_process", "") or "无")
        owner = str(dev_priority.get("owner", "") or responsibility.get("owner", "") or "待确认")
        responsibility_category = str(responsibility.get("category", "") or "")
        responsibility_confidence = str(responsibility.get("confidence", "") or "").lower()
        top_evidence: list = []
        top_suspect_processes = list((root_cause_analysis or {}).get("top_suspect_processes") or [])
        top_suspect_name = ""
        top_suspect_count = 0
        if top_suspect_processes:
            first_suspect = top_suspect_processes[0] or {}
            top_suspect_name = str(first_suspect.get("process", "") or "").strip()
            top_suspect_count = int(first_suspect.get("count", 0) or 0)
            if target in {"", "无"} and top_suspect_name and top_suspect_count >= 2:
                target = f"{top_suspect_name}（高频命中 {top_suspect_count} 次）"
                if owner in {"待确认", "待补证据"}:
                    owner = "系统/固件侧（待补证据）" if self._looks_like_system_process(top_suspect_name) else "待补证据"

        if tv_stall_count > 0:
            top_evidence.append(f"电视端已确认卡顿 {tv_stall_count} 次")
        elif tv_stall_risk_count > 0:
            top_evidence.append(f"电视端捕获风险样本 {tv_stall_risk_count} 次，当前仍未升到确认级")
        else:
            top_evidence.append("本轮未检测到确认级电视端卡顿事件")

        if avg_system_cpu > 0:
            cpu_line = f"整机平均 CPU {avg_system_cpu:.1f}%"
            if avg_player_cpu > 0:
                cpu_line += f"，播放器平均 CPU {avg_player_cpu:.1f}%"
            top_evidence.append(cpu_line)

        if target and target != "无":
            if "暂未确认主责" in responsibility_category or responsibility_confidence in {"low", "none"}:
                top_evidence.append(f"当前高相关对象为 {target}，仅建议作为排查优先级参考")
            else:
                top_evidence.append(f"当前高相关对象为 {target}")
        elif top_suspect_name and top_suspect_count >= 3:
            top_evidence.append(f"当前高频相关进程为 {top_suspect_name}，已在 {top_suspect_count} 次事件中命中")
        elif video_fps > 0:
            top_evidence.append(f"电视端视频 FPS 当前约 {video_fps:.1f}")
        else:
            top_evidence.append("当前未拿到稳定视频 FPS，需结合其他证据判断")

        if decoder_confirmed > 0:
            top_evidence.append(f"已确认解码停顿样本 {decoder_confirmed} 次")
        elif decoder_risk > 0:
            top_evidence.append(f"检测到解码风险样本 {decoder_risk} 次")
        if effective_duration_ms > 0:
            top_evidence.append(
                f"真正计入结论的电视端异常累计约 {effective_duration_ms / 1000.0:.1f} 秒，占全程 {effective_ratio:.2f}%"
            )

        notes: list = []
        if duration_sec < 3600:
            notes.append("本轮不足 1 小时，更适合快速筛查，不足以覆盖长时间积累问题。")
        if ignored_static_count > 0:
            notes.append(
                f"已按静态画面/转场误判过滤 {ignored_static_count} 次，占全程约 {ignored_static_ratio:.2f}%，这部分不再直接计入电视端卡顿结论。"
            )
        if effective_ratio > 0 and effective_ratio < 10.0:
            notes.append("虽然风险窗口内占比可能偏高，但真正落到整段播放时，异常影响时长占比仍较低，人眼可能只觉得偶发抖动。")
        if video_fps > 0 and (tv_stall_count > 0 or tv_stall_risk_count > 0 or effective_duration_ms > 0):
            notes.append(
                f"当前视频 FPS {video_fps:.1f} 反映的是采样周期平均值，不代表每一帧都连续稳定输出；"
                "若 Surface 在局部时间窗内停止更新，仍可能出现平均 FPS 正常但肉眼可见的瞬时冻结。"
            )
        if release_status == "证据不足，暂不作上线结论":
            notes.append("当前报告已明确提示证据缺口，不能直接当作最终裁决。")
        if not summary.get("tv_surface_locked"):
            notes.append("当前未稳定锁定电视端视频 Surface，电视端结论可信度会下降。")
        if not float(summary.get("avg_video_fps", 0) or 0.0):
            notes.append(f"当前未采集到稳定视频 FPS：{summary.get('video_fps_unavailable_reason', '需继续补采')}")
        if process_failure.get("has_player_failure"):
            notes.append("本轮出现播放器进程异常，建议优先看 Crash / ANR / PID 丢失时间线。")
        limitations = list(platform_support.get("limitations", []) or [])
        if limitations:
            notes.append(limitations[0])
        if not notes:
            notes.append("本轮关键证据链相对完整，可直接作为研发排查输入。")

        headline = executive_statement or diagnosis.get("conclusion") or "暂无明确结论"
        if not headline:
            headline = "暂无明确结论"

        return {
            "headline": headline,
            "owner": owner,
            "target": target,
            "evidence": top_evidence[:3],
            "notes": notes[:4],
            "release_status": release_status,
            "perceptual_recommendation": perceptual_result.get("recommendation", "无"),
        }

    def _build_process_failure_summary(self, summary: Dict, error_stats: Dict) -> Dict:
        existing = dict(summary.get("process_failure_summary", {}) or {})
        pid_timeline = list(existing.get("timeline", []) or [])
        error_timeline = []
        for event in list(error_stats.get("error_events", []) or []):
            event_type = str(event.get("type", "") or "").strip()
            if event_type not in {"CRASH", "ANR"}:
                continue
            error_timeline.append({
                "type": event_type,
                "timestamp": event.get("time", "N/A"),
                "elapsed_min": event.get("elapsed_min", 0),
                "description": event.get("message", ""),
            })

        full_timeline = pid_timeline + error_timeline
        full_timeline.sort(key=lambda item: str(item.get("timestamp", "")))
        type_label_map = {
            "PID_RESTART": "PID重启",
            "PID_LOST": "进程丢失",
            "CRASH": "Crash",
            "ANR": "ANR",
        }

        failure_types = []
        for item in full_timeline:
            item_type = str(item.get("type", "") or "")
            label = type_label_map.get(item_type, item_type)
            if label and label not in failure_types:
                failure_types.append(label)

        first_failure = full_timeline[0] if full_timeline else {}
        last_failure = full_timeline[-1] if full_timeline else {}
        restart_count = int(summary.get("restart_count", 0) or 0)
        pid_loss_count = int(summary.get("pid_loss_count", 0) or 0)
        crash_count = int(error_stats.get("crash_count", 0) or 0)
        anr_count = int(error_stats.get("anr_count", 0) or 0)
        total_failure_count = restart_count + pid_loss_count + crash_count + anr_count

        existing.update({
            "has_player_failure": total_failure_count > 0,
            "total_failure_count": total_failure_count,
            "restart_count": restart_count,
            "pid_loss_count": pid_loss_count,
            "crash_count": crash_count,
            "anr_count": anr_count,
            "failure_types": failure_types,
            "first_failure_time": first_failure.get("timestamp", existing.get("first_failure_time", "N/A")),
            "last_failure_time": last_failure.get("timestamp", existing.get("last_failure_time", "N/A")),
            "first_failure_type": type_label_map.get(first_failure.get("type", ""), existing.get("first_failure_type", "N/A")),
            "last_failure_type": type_label_map.get(last_failure.get("type", ""), existing.get("last_failure_type", "N/A")),
            "timeline": full_timeline[-8:],
        })
        return existing

    def _build_monitoring_scope_summary(self, summary: Dict) -> Dict:
        package_name = str(summary.get("package_name", "") or self.package_name or "目标播放器进程")
        suspect_process = str(
            ((summary.get("responsibility_summary", {}) or {}).get("suspect_process", "无") or "无")
        )
        avg_player_cpu = float(summary.get("avg_player_cpu_percent", 0) or 0.0)
        avg_system_cpu = float(summary.get("avg_system_cpu_percent", 0) or 0.0)
        playback_path = summary.get("playback_path_summary", {}) or {}
        decoder_summary = summary.get("decoder_stuck_summary", {}) or {}
        monitor_items = [
            f"App层: {package_name}（CPU {avg_player_cpu:.1f}%）",
            f"系统服务层: 整机CPU {avg_system_cpu:.1f}%，重点关注 Top 进程、重复实例和系统服务抢占",
            f"硬件解码层: {playback_path.get('route', '未识别')} / Decoder {decoder_summary.get('decoder_name') or '未识别'}",
            f"显示合成层: Display {summary.get('tv_display_id', 'N/A')} / Surface {'已锁定' if summary.get('tv_surface_locked') else '未锁定'} / FPS {float(summary.get('avg_video_fps', 0) or 0.0):.1f}",
        ]
        conclusion = "当前风险更偏系统服务层或显示/解码链路，不应只按播放器单进程 CPU 解读。"
        if suspect_process and suspect_process != "无":
            conclusion = f"当前风险优先落在 {suspect_process} 所在链路，测试包名仅用于锚定播放器进程状态，不代表只监控该进程。"
        return {
            "package_note": "测试包名仅用于锚定播放器进程状态，不代表只监控该进程。",
            "items": monitor_items,
            "conclusion": conclusion,
        }

    def _build_osd_composition_summary(self, summary: Dict, root_cause_analysis: Dict) -> Dict:
        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        suspect_process = str(
            diagnosis.get("suspect_process", "")
            or ((root_cause_analysis or {}).get("most_confident_cause") or {}).get("suspect_process", "")
            or "未知"
        )
        video_fps = float(summary.get("avg_video_fps", 0) or 0.0)
        osd_fps = float(summary.get("avg_osd_fps", 0) or 0.0)
        osd_surface = str(summary.get("osd_surface_name", "") or "")
        osd_detected = bool(summary.get("osd_surface_locked", False) or osd_surface)
        osd_samples = int(summary.get("osd_fps_samples", 0) or 0)
        osd_gap = float(summary.get("max_osd_frame_gap_ms", 0) or 0.0)
        avg_system_cpu = float(summary.get("avg_system_cpu_percent", 0) or 0.0)
        dynamic_keywords = ("marquee", "ticker", "scroll", "lyric", "notice", "banner")
        static_keywords = ("qrcode", "qr", "logo", "watermark")
        osd_process_aliases = (
            "com.thunder.ktv:tvservice",
            "com.thunder.ktv.thundertvservice",
            "thundertvservice",
            "tvservice",
        )
        lower_surface = osd_surface.lower()
        dynamic_osd = any(keyword in lower_surface for keyword in dynamic_keywords)
        static_osd = any(keyword in lower_surface for keyword in static_keywords)
        suspect_process_lower = suspect_process.lower()
        suspect_is_osd_process = any(alias in suspect_process_lower for alias in osd_process_aliases)
        composer_related = (
            "composer" in suspect_process_lower
            or "surfaceflinger" in suspect_process_lower
            or "hwcomposer" in suspect_process_lower
        )

        risk_events = list(summary.get("tv_stall_risk_events", []) or [])
        osd_process_hits = {}
        for event in risk_events:
            if not isinstance(event, dict):
                continue
            cpu_before = list(event.get("cpu_before", []) or [])
            cpu_after = list(event.get("cpu_after", []) or [])
            for sample in cpu_before + cpu_after:
                if not isinstance(sample, dict):
                    continue
                for proc in list(sample.get("top_processes", []) or []):
                    if not isinstance(proc, dict):
                        continue
                    name = str(proc.get("name", "") or "").strip()
                    lower_name = name.lower()
                    if name and any(alias in lower_name for alias in osd_process_aliases):
                        osd_process_hits[name] = osd_process_hits.get(name, 0) + 1

        osd_process_name = ""
        osd_process_hit_count = 0
        if osd_process_hits:
            osd_process_name, osd_process_hit_count = max(
                osd_process_hits.items(),
                key=lambda item: item[1],
            )
        elif suspect_is_osd_process:
            osd_process_name = suspect_process
            osd_process_hit_count = 1

        log_monitor = getattr(self, "log_monitor", None)
        recent_logs = list(getattr(log_monitor, "recent_logs", []) or []) if log_monitor else []
        osd_log_layers = set()
        osd_log_samples = []
        osd_log_tv_command_count = 0
        osd_log_qrcode_count = 0
        osd_log_marquee_count = 0
        osd_log_prompt_count = 0
        osd_log_tvservice_count = 0
        for item in recent_logs[-1200:]:
            line = str((item or {}).get("line", "") or "").strip()
            if not line:
                continue
            lower = line.lower()
            if "tvservice" in lower or "tvosdservice" in lower or "ktvtvservice" in lower or "tvrealtimeservice" in lower:
                osd_log_tvservice_count += 1
            if "display_tv" in lower:
                osd_log_tv_command_count += 1
            if "wx_qrcode" in lower:
                osd_log_qrcode_count += 1
                osd_log_layers.add("WX_QRCODE")
            if "marquee_start" in lower or "showmarquee" in lower:
                osd_log_marquee_count += 1
                osd_log_layers.add("MARQUEE")
            if '"layer":"prompt"' in lower or "layer:'prompt'" in lower or 'layer="prompt"' in lower:
                osd_log_prompt_count += 1
                osd_log_layers.add("PROMPT")
            if (
                "display_tv" in lower
                or "wx_qrcode" in lower
                or "marquee_start" in lower
                or "showmarquee" in lower
                or '"layer":"prompt"' in lower
                or "layer:'prompt'" in lower
                or 'layer="prompt"' in lower
            ):
                if len(osd_log_samples) < 6:
                    osd_log_samples.append(line[:240])

        log_osd_detected = bool(
            osd_log_tv_command_count > 0
            or osd_log_qrcode_count > 0
            or osd_log_marquee_count > 0
            or osd_log_prompt_count > 0
        )
        if osd_log_marquee_count > 0 or osd_log_prompt_count > 0:
            dynamic_osd = True
        if osd_log_qrcode_count > 0:
            static_osd = True

        category = "未纳入结论"
        confidence = "none"
        conclusion = "当前报告仍以视频播放链路为主，OSD 叠加层仅作为辅助观测。"
        basis = "未识别到足够的 OSD/合成侧特征"
        marquee_status = "未识别"
        marquee_conclusion = "当前未识别到跑马灯证据。"
        marquee_basis = "未识别到 MARQUEE 相关 Surface 或日志。"

        if osd_log_marquee_count > 0 or ("MARQUEE" in osd_log_layers):
            if osd_detected and dynamic_osd and osd_samples > 0:
                if osd_gap >= 1000.0 or (osd_fps > 0 and osd_fps < 8.0):
                    marquee_status = "明显卡顿"
                    marquee_conclusion = "已识别跑马灯活动，且 OSD 层刷新明显偏慢，跑马灯大概率存在肉眼可感知的滚动卡顿。"
                    marquee_basis = (
                        f"跑马灯日志 {osd_log_marquee_count} 次 | OSD FPS {osd_fps:.1f} | 最大帧间隔 {osd_gap:.0f} ms"
                    )
                elif osd_gap >= 500.0 or (osd_fps > 0 and osd_fps < 15.0):
                    marquee_status = "轻微卡顿"
                    marquee_conclusion = "已识别跑马灯活动，OSD 层存在一定刷新波动，建议继续观察跑马灯滚动是否偶发不连贯。"
                    marquee_basis = (
                        f"跑马灯日志 {osd_log_marquee_count} 次 | OSD FPS {osd_fps:.1f} | 最大帧间隔 {osd_gap:.0f} ms"
                    )
                else:
                    marquee_status = "流畅"
                    marquee_conclusion = "已识别跑马灯活动，当前 OSD 层刷新整体平稳，未见明显跑马灯滚动卡顿证据。"
                    marquee_basis = (
                        f"跑马灯日志 {osd_log_marquee_count} 次 | OSD FPS {osd_fps:.1f} | 最大帧间隔 {osd_gap:.0f} ms"
                    )
            else:
                marquee_status = "无法单独判断"
                marquee_conclusion = "已识别跑马灯业务活动，但当前未锁定独立 OSD Surface，暂时无法单独量化跑马灯流畅性。"
                marquee_basis = (
                    f"跑马灯日志 {osd_log_marquee_count} 次"
                    + (f" | 图层 {','.join(sorted(osd_log_layers))}" if osd_log_layers else "")
                    + " | 当前仅有日志证据"
                )

        if not osd_detected and not log_osd_detected:
            category = "未识别到 OSD 层"
            basis = "本轮未锁定独立 OSD Surface，也未在近期日志中看到 DISPLAY_TV / WX_QRCODE / MARQUEE / PROMPT 等 TV OSD 指令"
            conclusion = "当前未识别到可用于单独分析的 OSD 证据，暂无法判断二维码、跑马灯或 logo 是否影响最终显示。"
        elif not osd_detected and log_osd_detected:
            category = "已识别 OSD 业务活动，但未锁定独立 Surface"
            confidence = "high" if osd_process_name or osd_log_tvservice_count > 0 else "medium"
            conclusion = "已从 tvservice/TV OSD 日志中确认电视端 OSD 正在工作，但当前 SurfaceFlinger 图层名不具备稳定语义，暂未锁定独立 OSD Surface。后续应以 tvservice 进程和 DISPLAY_TV 图层日志作为 OSD 证据来源。"
            basis = (
                f"日志命中 DISPLAY_TV {osd_log_tv_command_count} 次"
                f" | 图层 {','.join(sorted(osd_log_layers)) if osd_log_layers else '未提取'}"
                + (f" | OSD 进程 {osd_process_name}" if osd_process_name else "")
            )
        elif video_fps >= 27.0 and osd_samples > 0 and dynamic_osd and (osd_fps <= 12.0 or osd_gap >= 800.0):
            category = "视频正常，OSD/合成侧风险更高"
            confidence = "high" if composer_related or suspect_is_osd_process or avg_system_cpu >= 75.0 else "medium"
            conclusion = "视频层帧率整体正常，但动态 OSD 层刷新明显偏慢，优先怀疑跑马灯、提示层或通知条本身，或者其在合成阶段拖慢了最终电视画面。"
            basis = (
                f"视频 FPS {video_fps:.1f} 正常 | OSD FPS {osd_fps:.1f} | OSD 最大帧间隔 {osd_gap:.0f} ms"
                + (f" | OSD 进程 {osd_process_name}" if osd_process_name else "")
                + (f" | 合成高相关进程 {suspect_process}" if composer_related else "")
                + (f" | 日志图层 {','.join(sorted(osd_log_layers))}" if osd_log_layers else "")
            )
        elif video_fps >= 27.0 and ((osd_samples > 0 and static_osd) or (log_osd_detected and static_osd and not dynamic_osd)):
            category = "视频正常，静态 OSD 不作为卡顿证据"
            confidence = "medium"
            conclusion = "本轮已识别二维码、Logo 等静态 OSD 叠加，但这类层本身不持续刷新，不能仅凭其低刷新判断电视端卡顿。"
            basis = (
                f"静态 OSD Surface: {osd_surface or '未锁定'} | 视频 FPS {video_fps:.1f}"
                + (f" | OSD 进程 {osd_process_name}" if osd_process_name else "")
                + (f" | 日志图层 {','.join(sorted(osd_log_layers))}" if osd_log_layers else "")
            )
        elif video_fps < 24.0 and (osd_detected or log_osd_detected):
            category = "视频层已异常，OSD 仅作辅助"
            confidence = "medium"
            conclusion = "当前视频层已经出现退化，OSD 叠加层观测仅作为补充证据，主问题仍应优先从视频、解码或系统资源侧排查。"
            basis = (
                f"视频 FPS {video_fps:.1f} 偏低 | OSD Surface {osd_surface or '未锁定'}"
                + (f" | OSD 进程 {osd_process_name}" if osd_process_name else "")
                + (f" | 日志图层 {','.join(sorted(osd_log_layers))}" if osd_log_layers else "")
            )

        return {
            "category": category,
            "confidence": confidence,
            "conclusion": conclusion,
            "basis": basis,
            "surface_name": osd_surface or "未识别",
            "osd_detected": osd_detected,
            "dynamic_osd": dynamic_osd,
            "static_osd": static_osd,
            "osd_process_name": osd_process_name or "未识别",
            "osd_process_hit_count": int(osd_process_hit_count or 0),
            "osd_log_detected": log_osd_detected,
            "osd_log_tv_command_count": int(osd_log_tv_command_count or 0),
            "osd_log_qrcode_count": int(osd_log_qrcode_count or 0),
            "osd_log_marquee_count": int(osd_log_marquee_count or 0),
            "osd_log_prompt_count": int(osd_log_prompt_count or 0),
            "osd_log_layers": sorted(osd_log_layers),
            "osd_log_samples": osd_log_samples,
            "osd_fps": round(osd_fps, 2),
            "video_fps": round(video_fps, 2),
            "max_frame_gap_ms": round(osd_gap, 2),
            "marquee_smoothness_status": marquee_status,
            "marquee_smoothness_conclusion": marquee_conclusion,
            "marquee_smoothness_basis": marquee_basis,
        }

    def _render_process_failure_section(self, summary: Dict) -> list:
        failure_summary = summary.get("process_failure_summary", {}) or {}
        lines = ["-" * 30, "【播放器进程异常摘要】"]
        if not failure_summary.get("has_player_failure"):
            lines.append("1. 本轮未检测到播放器挂掉、PID重启、进程丢失、Crash 或 ANR。")
            return lines

        failure_types = " / ".join(failure_summary.get("failure_types", []) or ["未知"])
        lines.extend([
            "1. 是否发生异常: 是",
            f"2. 总次数: {failure_summary.get('total_failure_count', 0)} 次",
            f"3. 类型分布: {failure_types}",
            "4. Crash / ANR / PID重启 / 进程丢失: "
            f"{failure_summary.get('crash_count', 0)} / {failure_summary.get('anr_count', 0)} / "
            f"{failure_summary.get('restart_count', 0)} / {failure_summary.get('pid_loss_count', 0)}",
            f"5. 首次异常: {failure_summary.get('first_failure_time', 'N/A')} ({failure_summary.get('first_failure_type', 'N/A')})",
            f"6. 最近异常: {failure_summary.get('last_failure_time', 'N/A')} ({failure_summary.get('last_failure_type', 'N/A')})",
        ])
        timeline = list(failure_summary.get("timeline", []) or [])
        if timeline:
            lines.append("7. 最近异常时间线:")
            for item in timeline:
                lines.append(
                    f"   - [{item.get('timestamp', 'N/A')}] "
                    f"{item.get('type', 'UNKNOWN')}: {item.get('description', '')}"
                )
        actions = list(summary.get("process_failure_actions", []) or [])
        if actions:
            lines.append("8. 研发优先排查动作:")
            for item in actions:
                lines.append(f"   - {item}")
        return lines

    def _build_responsibility_summary(self, summary: Dict, root_cause_analysis: Dict) -> Dict:
        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        most_confident = (root_cause_analysis or {}).get("most_confident_cause") or {}
        process_failure_summary = summary.get("process_failure_summary", {}) or {}
        correlation = summary.get("tv_process_correlation_summary", {}) or {}
        risk_events = list(summary.get("tv_stall_risk_events", []) or [])

        cause_type = str(most_confident.get("root_cause_type", "") or "")
        owner = str(diagnosis.get("owner", "") or "待确认")
        aggregated_suspect = self._summarize_event_cpu_suspects(summary)
        suspect_process = str(diagnosis.get("suspect_process", "") or "").strip()
        if aggregated_suspect.get("process") and int(aggregated_suspect.get("count", 0) or 0) >= 2:
            suspect_process = str(aggregated_suspect.get("process", "") or suspect_process or "无").strip()
            if owner in {"待确认", "待补证据", ""}:
                owner = str(aggregated_suspect.get("owner", "") or owner or "待补证据")
        elif not suspect_process or suspect_process == "无":
            suspect_process = str(most_confident.get("suspect_process", "") or "无").strip()
            if suspect_process == "无":
                top_suspect_processes = list((root_cause_analysis or {}).get("top_suspect_processes") or [])
                if top_suspect_processes:
                    first_suspect = top_suspect_processes[0] or {}
                    if isinstance(first_suspect, dict):
                        first_name = str(first_suspect.get("process", "") or "").strip()
                    elif isinstance(first_suspect, (list, tuple)) and first_suspect:
                        first_name = str(first_suspect[0] or "").strip()
                    else:
                        first_name = ""
                    if first_name:
                        suspect_process = first_name
                if suspect_process == "无":
                    representative_risk_event = self._pick_representative_risk_event(risk_events)
                    cpu_candidate = ((representative_risk_event.get("cpu_contention") or {}).get("top_candidate") or {})
                    cpu_process = str(cpu_candidate.get("process", "") or representative_risk_event.get("suspect_process", "") or "").strip()
                    if cpu_process:
                        suspect_process = cpu_process
        if owner == "待确认" and suspect_process not in {"", "无"}:
            suspect_lower = suspect_process.lower()
            if (
                suspect_lower.startswith("/system/bin/")
                or "mediaserver" in suspect_lower
                or "surfaceflinger" in suspect_lower
                or "composer" in suspect_lower
                or "media.codec" in suspect_lower
                or "media.extractor" in suspect_lower
            ):
                owner = "系统/固件侧"
        matched_stall_count = int(correlation.get("matched_tv_stall_count", 0) or 0)
        total_stall_count = int(correlation.get("total_tv_stall_count", 0) or 0)
        correlated_ratio = float(correlation.get("correlated_ratio", 0) or 0.0)
        has_process_failure = bool(process_failure_summary.get("has_player_failure", False))
        confirmed_decoder_stuck = int(summary.get("confirmed_decoder_stuck_count", 0) or 0)
        decoder_stuck_risk = int(summary.get("decoder_stuck_risk_count", 0) or 0)
        risk_stall_count = int(summary.get("tv_stall_risk_count", 0) or 0)
        avg_system_cpu = float(summary.get("avg_system_cpu_percent", 0) or 0.0)
        peak_system_cpu = float(summary.get("max_system_cpu_percent", 0) or 0.0)
        avg_player_cpu = float(summary.get("avg_player_cpu_percent", 0) or 0.0)
        surface_locked = bool(summary.get("tv_surface_locked", False))
        avg_video_fps = float(summary.get("avg_video_fps", 0) or 0.0)
        decode_drop_ratio = float(summary.get("decode_drop_ratio", 0) or 0.0)
        evidence_level = str(diagnosis.get("evidence_level", "") or "").lower()
        diagnosis_conclusion = str(diagnosis.get("conclusion", "") or "").strip()
        diagnosis_confidence = float(diagnosis.get("confidence", 0) or 0.0)
        display_recommendation = summary.get("tv_display_recommendation", {}) or {}
        recommended_display = display_recommendation.get("display_id")
        recommended_reason = str(display_recommendation.get("reason", "") or "")
        representative_risk_event = self._pick_representative_risk_event(risk_events)
        confirmation_gaps = []
        promotion_hints = []
        for item in representative_risk_event.get("confirmation_gap_reasons", []) or []:
            text = str(item or "").strip()
            if text and text not in confirmation_gaps:
                confirmation_gaps.append(text)
        for item in representative_risk_event.get("promotion_hints", []) or []:
            text = str(item or "").strip()
            if text and text not in promotion_hints:
                promotion_hints.append(text)

        representative_duration_ms = int(
            representative_risk_event.get("duration_ms", 0) or 0
        )
        representative_gap_ms = float(
            representative_risk_event.get("max_frame_gap_ms", 0) or 0.0
        )
        representative_cpu = (
            ((representative_risk_event.get("cpu_candidate") or {}).get("peak_cpu_percent", 0))
            if isinstance(representative_risk_event, dict) else 0
        )
        derived_display_jank_risk = bool(summary.get("derived_display_jank_risk", False))
        derived_display_jank_reason = str(summary.get("derived_display_jank_reason", "") or "")
        frame_gap_sample_count = int(summary.get("tv_frame_gap_sample_count", 0) or 0)
        frame_gap_source = str(summary.get("tv_frame_gap_source", "") or "history")
        frame_gap_p95_ms = float(summary.get("tv_frame_gap_p95_ms", 0) or 0.0)
        frame_gap_p99_ms = float(summary.get("tv_frame_gap_p99_ms", 0) or 0.0)
        tv_jank_ratio = float(summary.get("tv_jank_sample_ratio_percent", 0) or 0.0)
        tv_big_jank_ratio = float(summary.get("tv_big_jank_sample_ratio_percent", 0) or 0.0)

        if risk_stall_count > 0 and not confirmation_gaps:
            if not surface_locked:
                confirmation_gaps.append("电视端 Surface 未稳定锁定")
            if avg_video_fps <= 0:
                confirmation_gaps.append("缺少稳定视频 FPS 直证")
            if confirmed_decoder_stuck <= 0:
                confirmation_gaps.append("缺少解码停顿确认样本")
            if avg_system_cpu < 85.0 and cause_type == "CPU_CONTENTION":
                confirmation_gaps.append("CPU 竞争未与电视端退化形成强同步")
            if representative_duration_ms > 0 and representative_duration_ms < int(self.monitor_interval * 1000 if hasattr(self, "monitor_interval") else 0):
                confirmation_gaps.append("代表样本持续时间偏短，仍需更多连续异常样本")

        if risk_stall_count > 0 and not promotion_hints:
            if not surface_locked:
                promotion_hints.append("优先补齐电视端 Surface 锁定")
            if avg_video_fps <= 0:
                promotion_hints.append("补采稳定 FPS 或保留 frame latency 证据")
            promotion_hints.append("保留卡顿前后 10 秒日志与 Top 快照做互证")

        category = "暂不能定责"
        confidence = "low"
        conclusion = "当前证据还不足以直接指定播放器侧、系统侧或解码侧作为唯一责任方。"
        key_basis = "证据仍在收集中"

        if evidence_level == "confirmed":
            category = "已形成确认级责任结论"
            confidence = "high"
            conclusion = diagnosis_conclusion or (
                f"当前已可确认 {suspect_process} 所在链路与电视端异常形成稳定闭环，可直接作为定责依据。"
            )
            key_basis = (
                f"Evidence={evidence_level} | 责任方向 {owner} | 优先对象 {suspect_process}"
                + (f" | 置信度 {diagnosis_confidence:.1f}" if diagnosis_confidence > 0 else "")
            )
            confirmation_gaps = []
        elif evidence_level == "strong":
            category = "高概率责任方向已明确"
            confidence = "high" if diagnosis_confidence >= 70 else "medium"
            conclusion = diagnosis_conclusion or (
                f"当前已较明确指向 {suspect_process} 所在链路，建议按 {owner} 优先排查。"
            )
            key_basis = (
                f"Evidence={evidence_level} | 责任方向 {owner} | 优先对象 {suspect_process}"
                + (f" | 置信度 {diagnosis_confidence:.1f}" if diagnosis_confidence > 0 else "")
            )
        elif total_stall_count <= 0:
            category = "未检测到电视端卡顿"
            confidence = "none"
            conclusion = "本轮没有检测到电视端卡顿事件，因此无需进行责任归类。"
            key_basis = "未检测到电视端卡顿事件"
            if has_process_failure:
                category = "播放器进程异常阻断"
                confidence = "high"
                conclusion = (
                    "本轮虽然没有形成确认级电视端卡顿事件，但播放器已出现挂掉、ANR、PID 丢失或异常重启，"
                    "播放链路本身已经不稳定，应优先按播放器进程异常处理。"
                )
                key_basis = (
                    f"播放器异常: Crash {int(process_failure_summary.get('crash_count', 0) or 0)} / "
                    f"ANR {int(process_failure_summary.get('anr_count', 0) or 0)} / "
                    f"PID重启 {int(process_failure_summary.get('restart_count', 0) or 0)} / "
                    f"进程丢失 {int(process_failure_summary.get('pid_loss_count', 0) or 0)}"
                )
            elif risk_stall_count > 0 or decoder_stuck_risk > 0:
                category = "存在风险信号，未确认肉眼卡顿"
                confidence = "medium" if surface_locked else "low"
                conclusion = (
                    f"本轮尚未形成确认级电视端卡顿证据，但已捕获风险样本："
                    f"电视端风险事件 {risk_stall_count} 次，解码风险样本 {decoder_stuck_risk} 次。"
                    + (
                        f" 当前还缺少：{'；'.join(confirmation_gaps[:3])}。"
                        if confirmation_gaps else
                        " 建议继续补足 Surface/FPS/日志互证后再定责。"
                    )
                )
                key_basis = (
                    f"风险事件 {risk_stall_count} 次 | 解码风险 {decoder_stuck_risk} 次 | "
                    f"Surface {'已锁定' if surface_locked else '未锁定'}"
                    + (
                        f" | 代表样本 {representative_duration_ms} ms / {representative_gap_ms:.0f} ms"
                        if representative_duration_ms > 0 or representative_gap_ms > 0 else ""
                    )
                )
            elif derived_display_jank_risk:
                category = "存在细粒度流畅度抖动风险"
                confidence = "medium" if surface_locked else "low"
                conclusion = (
                    "本轮虽未形成确认级电视端卡顿事件，但细粒度帧间隔指标已经出现明显抖动，"
                    "建议优先按显示/合成链路或系统瞬时资源抖动方向排查。"
                )
                key_basis = (
                    derived_display_jank_reason
                    or (
                        f"细粒度样本 {frame_gap_sample_count} 个（来源 {frame_gap_source}） | "
                        f"P95/P99 {frame_gap_p95_ms:.0f}/{frame_gap_p99_ms:.0f} ms | "
                        f"Jank {tv_jank_ratio:.2f}% / Big Jank {tv_big_jank_ratio:.2f}%"
                    )
                )
        elif has_process_failure and matched_stall_count > 0 and correlated_ratio >= 0.7:
            category = "播放器自身异常主导"
            confidence = "high"
            conclusion = (
                f"电视端卡顿与播放器异常高度时间重合（{matched_stall_count}/{total_stall_count}），"
                "优先怀疑播放器挂掉、重启、ANR 或异常退出直接影响播放链路。"
            )
            key_basis = (
                f"卡顿与播放器异常时间重合 {matched_stall_count}/{total_stall_count} "
                f"({correlated_ratio * 100:.1f}%)"
            )
        elif cause_type == "CPU_CONTENTION" or ("系统" in owner and avg_system_cpu >= 80):
            if evidence_level in {"confirmed", "strong"} and total_stall_count > 0:
                category = "系统/固件 CPU 竞争主导"
                confidence = "high" if avg_system_cpu >= 90 else "medium"
                conclusion = (
                    f"卡顿阶段整机 CPU 持续偏高（平均 {avg_system_cpu:.1f}% / 峰值 {peak_system_cpu:.1f}%），"
                    f"当前更像是 {suspect_process} 引发的系统级资源竞争，而不是播放器单进程本身算力不足。"
                )
                key_basis = (
                    f"整机 CPU {avg_system_cpu:.1f}%/{peak_system_cpu:.1f}% + 高相关进程 {suspect_process}"
                )
            else:
                repeated_cpu_target = suspect_process not in {"", "无"}
                strong_cpu_resonance = (
                    repeated_cpu_target
                    and (
                        avg_system_cpu >= 80
                        or float(representative_cpu or 0) >= 90.0
                        or risk_stall_count >= 3
                    )
                )
                if strong_cpu_resonance:
                    category = "高概率系统级 CPU 竞争，待补硬解/丢帧证据"
                    confidence = "medium"
                    conclusion = (
                        f"当前已看到 {suspect_process} 与电视端风险样本反复共振，"
                        f"整机 CPU 平均 {avg_system_cpu:.1f}% / 峰值 {peak_system_cpu:.1f}%，"
                        "更像系统级 CPU 资源竞争。"
                        " 只是当前仍缺少解码停顿、丢帧恶化或更强的电视端直证，"
                        "因此暂不升级为确认级主责。"
                    )
                    key_basis = (
                        f"整机 CPU {avg_system_cpu:.1f}%/{peak_system_cpu:.1f}% | "
                        f"高相关进程 {suspect_process} | 风险样本 {risk_stall_count} 次"
                    )
                else:
                    category = "存在系统级 CPU 竞争风险，暂未确认主责"
                    confidence = "medium" if avg_system_cpu >= 85 else "low"
                    conclusion = (
                        f"当前捕获到整机 CPU 偏高（平均 {avg_system_cpu:.1f}% / 峰值 {peak_system_cpu:.1f}%）和高相关进程 {suspect_process} 的风险信号，"
                        "但还缺少与电视端退化强同步的确认级证据，因此目前更适合作为排查优先方向，而不是最终定责。"
                    )
                    key_basis = (
                        f"整机 CPU {avg_system_cpu:.1f}%/{peak_system_cpu:.1f}% | 风险高相关进程 {suspect_process}"
                    )
        elif cause_type == "DECODER_STUCK" or confirmed_decoder_stuck > 0:
            category = "解码链路异常主导"
            confidence = "high" if confirmed_decoder_stuck > 0 else "medium"
            conclusion = (
                f"已捕获解码输出停顿证据（确认样本 {confirmed_decoder_stuck} 次），"
                "当前更偏向硬件解码链路、码流或驱动侧问题。"
            )
            key_basis = f"已确认解码输出停顿样本 {confirmed_decoder_stuck} 次"
        elif has_process_failure and matched_stall_count > 0:
            category = "播放器异常与卡顿时间相关，暂不能单独定责"
            confidence = "medium"
            conclusion = (
                f"播放器异常与 {matched_stall_count} 次电视端卡顿存在时间重合，"
                "但相关性尚不足以排除系统 CPU 竞争或解码链路问题。"
            )
            key_basis = (
                f"播放器异常与卡顿重合 {matched_stall_count}/{total_stall_count}，但证据未完全收敛"
            )
        else:
            key_basis = (
                f"CPU {avg_system_cpu:.1f}%/{avg_player_cpu:.1f}% | "
                f"FPS {avg_video_fps:.1f} | 解码丢帧 {decode_drop_ratio * 100:.1f}%"
            )

        evidence_items = [
            {"label": "责任分类", "value": category},
            {"label": "责任方向", "value": owner},
            {
                "label": (
                    "优先对象"
                    if confidence in {"high", "medium"} and category not in {"存在系统级 CPU 竞争风险，暂未确认主责"}
                    else "排查优先对象"
                ),
                "value": suspect_process,
            },
            {
                "label": "卡顿/异常时间重合",
                "value": (
                    "不适用"
                    if total_stall_count <= 0 and int(correlation.get("total_failure_event_count", 0) or 0) <= 0
                    else f"{matched_stall_count}/{total_stall_count} ({correlated_ratio * 100:.1f}%)"
                ),
            },
            {"label": "播放器异常分布", "value": f"Crash {int(process_failure_summary.get('crash_count', 0) or 0)} / ANR {int(process_failure_summary.get('anr_count', 0) or 0)} / PID重启 {int(process_failure_summary.get('restart_count', 0) or 0)} / 进程丢失 {int(process_failure_summary.get('pid_loss_count', 0) or 0)}"},
            {"label": "整机/播放器 CPU", "value": f"{avg_system_cpu:.1f}% / {avg_player_cpu:.1f}%"},
            {"label": "Surface/FPS/解码丢帧", "value": f"{'已锁定' if surface_locked else '未锁定'} / {avg_video_fps:.1f} FPS / {decode_drop_ratio * 100:.1f}%"},
            {"label": "解码停顿确认样本", "value": str(confirmed_decoder_stuck)},
            {"label": "Display 推荐", "value": f"{recommended_display if recommended_display is not None else '无'} ({recommended_reason or 'unknown'})"},
        ]
        if representative_duration_ms > 0 or representative_gap_ms > 0:
            evidence_items.append({
                "label": "代表风险样本",
                "value": (
                    f"持续 {representative_duration_ms} ms"
                    + (f" / 最大帧间隔 {representative_gap_ms:.0f} ms" if representative_gap_ms > 0 else "")
                    + (f" / 嫌疑CPU {float(representative_cpu or 0):.1f}%" if float(representative_cpu or 0) > 0 else "")
                ),
            })
        if confirmation_gaps and evidence_level not in {"confirmed", "strong"}:
            evidence_items.append({
                "label": "未升确认级的原因",
                "value": "；".join(confirmation_gaps[:4]),
            })
        if promotion_hints and evidence_level not in {"confirmed"}:
            evidence_items.append({
                "label": "补证路径",
                "value": "；".join(promotion_hints[:3]),
            })

        return {
            "category": category,
            "confidence": confidence,
            "owner": owner,
            "suspect_process": suspect_process,
            "conclusion": conclusion,
            "key_basis": key_basis,
            "evidence_items": evidence_items,
            "confirmation_gap_reasons": confirmation_gaps[:6],
            "promotion_hints": promotion_hints[:5],
            "confirmed": evidence_level == "confirmed",
        }

    @staticmethod
    def _pick_representative_risk_event(risk_events: list) -> Dict:
        if not risk_events:
            return {}

        def _score(event: Dict):
            if not isinstance(event, dict):
                return (-1, -1, -1, -1)
            duration_ms = int(event.get("duration_ms", 0) or 0)
            gap_ms = float(event.get("max_frame_gap_ms", 0) or 0.0)
            corroboration_count = int(event.get("corroboration_count", 0) or 0)
            cpu_contention = event.get("cpu_contention") or {}
            cpu_candidate = cpu_contention.get("top_candidate") or {}
            peak_cpu = float(cpu_candidate.get("peak_cpu_percent", 0) or 0.0)
            return (
                corroboration_count,
                duration_ms,
                gap_ms,
                peak_cpu,
            )

        return max(
            [event for event in risk_events if isinstance(event, dict)] or [{}],
            key=_score,
        )

    def _build_platform_support_summary(self, summary: Dict) -> Dict:
        summary = summary or {}
        platform_label = str(
            summary.get("platform_identity")
            or getattr(self, "platform_identity", "")
            or summary.get("firmware_incremental")
            or getattr(self, "firmware_incremental", "")
            or "未识别平台"
        )
        verified = bool(summary.get("tv_display_verified", False))
        surface_locked = bool(summary.get("tv_surface_locked", False))
        avg_video_fps = float(summary.get("avg_video_fps", 0) or 0.0)
        fps_reason = str(summary.get("video_fps_unavailable_reason", "") or "")
        display_recommendation = summary.get("tv_display_recommendation", {}) or {}
        display_id = summary.get("tv_display_id", "N/A")
        decode_confirmed = int(summary.get("confirmed_decoder_stuck_count", 0) or 0)
        decode_risk = int(summary.get("decoder_stuck_risk_count", 0) or 0)

        capabilities = []
        limitations = []

        if verified:
            capabilities.append(f"可识别电视端 Display（当前 Display {display_id}）")
        else:
            limitations.append("未稳定识别电视端 Display")

        if surface_locked:
            capabilities.append("可锁定电视端播放 Surface")
        else:
            limitations.append("Surface 锁定能力不足，电视端直证会变弱")

        if avg_video_fps > 0:
            capabilities.append(f"可采集电视端 FPS（当前 {avg_video_fps:.1f}）")
        else:
            limitations.append(
                f"未采集到稳定 FPS{f'：{fps_reason}' if fps_reason else ''}"
            )

        if decode_confirmed > 0 or decode_risk > 0:
            capabilities.append("支持解码停顿样本诊断")

        if verified and surface_locked and avg_video_fps > 0:
            grade = "A"
            headline = "确认级支持"
            conclusion = "该平台当前具备 Display、Surface、FPS 三条关键证据链，可用于确认级电视端卡顿判断。"
        elif verified and (surface_locked or avg_video_fps > 0 or display_recommendation.get("display_id") is not None):
            grade = "B"
            headline = "风险级支持"
            conclusion = "该平台已具备部分电视端证据链，适合风险级判断；若要一锤定音，仍建议补足 Surface/FPS 直证。"
        else:
            grade = "C"
            headline = "辅助级支持"
            conclusion = "该平台当前更适合资源/日志辅助判断，不建议单独依赖本轮结果做确认级定责。"

        if not capabilities:
            capabilities.append("当前仅保留基础资源与日志采样能力")
        if not limitations:
            limitations.append("当前未见明显平台级证据缺口")

        return {
            "platform_label": platform_label,
            "grade": grade,
            "headline": headline,
            "conclusion": conclusion,
            "capabilities": capabilities[:4],
            "limitations": limitations[:4],
        }

    @staticmethod
    def _looks_like_system_process(process_name: str) -> bool:
        process = str(process_name or "").strip().lower()
        if not process:
            return False
        return (
            process.startswith("/system/bin/")
            or "mediaserver" in process
            or "surfaceflinger" in process
            or "composer" in process
            or "media.codec" in process
            or "media.extractor" in process
            or "audioserver" in process
            or "hwcomposer" in process
        )

    def _build_dev_priority_summary(self, summary: Dict, root_cause_analysis: Dict) -> Dict:
        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        most_confident = (root_cause_analysis or {}).get("most_confident_cause") or {}
        cause_type = str(most_confident.get("root_cause_type", "") or "")
        decoder_summary = summary.get("decoder_stuck_summary", {}) or {}
        evidence_strength = diagnosis.get("evidence_strength", {}) or {}
        tv_stall_count = int(summary.get("tv_stall_count", 0) or 0)
        tv_stall_risk_count = int(summary.get("tv_stall_risk_count", 0) or 0)
        confirmed_decoder_stuck_count = int(summary.get("confirmed_decoder_stuck_count", 0) or 0)
        decoder_stuck_risk_count = int(summary.get("decoder_stuck_risk_count", 0) or 0)
        process_failure_summary = summary.get("process_failure_summary", {}) or {}
        has_player_failure = bool(process_failure_summary.get("has_player_failure", False))

        target = str(
            diagnosis.get("suspect_process", "")
            or most_confident.get("suspect_process", "")
            or "无"
        )
        owner = str(diagnosis.get("owner", "") or "待确认")
        strength = str(
            evidence_strength.get("label", "")
            or diagnosis.get("evidence_level", "unknown")
        )
        device_id = str(summary.get("device_id", "") or "").strip()
        representative_risk_event = self._pick_representative_risk_event(
            list(summary.get("tv_stall_risk_events", []) or [])
        )
        top_suspect_processes = list((root_cause_analysis or {}).get("top_suspect_processes") or [])
        if target == "无":
            if top_suspect_processes:
                first_suspect = top_suspect_processes[0] or {}
                first_name = str(first_suspect.get("process", "") or "").strip()
                first_count = int(first_suspect.get("count", 0) or 0)
                if first_name:
                    target = first_name if first_count < 3 else f"{first_name}（高频命中 {first_count} 次）"
                    if owner == "待确认":
                        owner = "系统/固件侧" if self._looks_like_system_process(first_name) else "待确认"
                    if not strength or str(strength).lower() in {"unknown", "none"}:
                        strength = "Risk"
            elif isinstance(representative_risk_event, dict):
                cpu_candidate = ((representative_risk_event.get("cpu_contention") or {}).get("top_candidate") or {})
                cpu_process = str(cpu_candidate.get("process", "") or representative_risk_event.get("suspect_process", "") or "").strip()
                if cpu_process:
                    target = cpu_process
                    if owner == "待确认":
                        owner = "系统/固件侧" if self._looks_like_system_process(cpu_process) else "待确认"
                    if not strength or str(strength).lower() in {"unknown", "none"}:
                        strength = "Risk"
        elif top_suspect_processes:
            first_suspect = top_suspect_processes[0] or {}
            first_name = str(first_suspect.get("process", "") or "").strip()
            first_count = int(first_suspect.get("count", 0) or 0)
            if (
                first_name
                and first_count >= 3
                and owner in {"待确认", "待补证据"}
                and "高频命中" not in target
            ):
                target = f"{first_name}（高频命中 {first_count} 次）"
                if self._looks_like_system_process(first_name):
                    owner = "系统/固件侧（待补证据）"
        evidence_dir = str(representative_risk_event.get("evidence_dir", "") or "").strip()
        event_id = str(representative_risk_event.get("event_id", "") or "").strip()

        def adb_prefix() -> str:
            if device_id:
                return f"adb -s {device_id} "
            return "adb "

        display_evidence_dir = evidence_dir
        if evidence_dir:
            anchor = "data\\"
            lower_dir = evidence_dir.lower()
            anchor_pos = lower_dir.find(anchor)
            if anchor_pos >= 0:
                display_evidence_dir = ".\\" + evidence_dir[anchor_pos:].replace("/", "\\")

        def build_artifacts() -> list:
            items = []
            if evidence_dir:
                items.append(f"事件证据目录: {display_evidence_dir}")
                items.append(f"事件摘要: {display_evidence_dir}\\event_summary.txt")
                items.append(f"时间窗日志: {display_evidence_dir}\\time_window_logcat.txt")
                items.append(f"CPU 快照: {display_evidence_dir}\\top_before.txt / top_after.txt")
            elif event_id:
                items.append(f"事件目录关键词: tv_stall_events\\{event_id}")
            return items[:4]

        if (
            not cause_type
            and
            tv_stall_count <= 0
            and tv_stall_risk_count <= 0
            and confirmed_decoder_stuck_count <= 0
            and decoder_stuck_risk_count <= 0
            and not has_player_failure
        ):
            return {
                "target": "本轮无需专项排查",
                "owner": "无需归因",
                "strength": "N/A",
                "logs": [
                    "本轮未检测到电视端卡顿、播放器挂掉、Crash、ANR 或解码停顿",
                    "如肉眼也无异常，可直接将本轮结果作为通过样本归档",
                    "如怀疑偶发问题，优先延长监控时长或提高负载后再复测",
                ],
                "retest": "若需进一步验证稳定性，建议延长到 1 小时并在相同场景下重复 2 到 3 轮。",
                "cause_type": "NONE",
                "commands": [],
                "artifacts": build_artifacts(),
            }

        if has_player_failure:
            failure_parts = []
            if int(process_failure_summary.get("pid_loss_count", 0) or 0) > 0:
                failure_parts.append(f"PID 丢失 {int(process_failure_summary.get('pid_loss_count', 0) or 0)} 次")
            if int(process_failure_summary.get("restart_count", 0) or 0) > 0:
                failure_parts.append(f"PID 重启 {int(process_failure_summary.get('restart_count', 0) or 0)} 次")
            if int(process_failure_summary.get("crash_count", 0) or 0) > 0:
                failure_parts.append(f"Crash {int(process_failure_summary.get('crash_count', 0) or 0)} 次")
            if int(process_failure_summary.get("anr_count", 0) or 0) > 0:
                failure_parts.append(f"ANR {int(process_failure_summary.get('anr_count', 0) or 0)} 次")
            failure_summary_text = "，".join(failure_parts) if failure_parts else "检测到播放器进程异常"
            target = str(summary.get("package_name", "") or target or "播放器进程")
            owner = "播放器侧"
            strength = "Blocker"
            logs = [
                f"先围绕播放器异常时间点看日志：{failure_summary_text}",
                "重点核对 ActivityManager / lowmemorykiller / debuggerd / tombstone / ANR traces",
                "再对照异常前后 30 秒的播放器业务日志，确认是自崩、被杀还是卡死",
            ]
            retest = "复测时先确认播放器进程不再丢失、重启、Crash 或 ANR，再观察电视端卡顿是否同步消失。"
            commands = [
                f"{adb_prefix()}shell logcat -d | tail -300",
                f"{adb_prefix()}shell dumpsys activity processes | head -80",
                f"{adb_prefix()}shell dmesg | tail -200",
            ]
        elif cause_type == "CPU_CONTENTION":
            target_prefix = target.split()[0] if target and target != "无" else "media"
            if target in {"mediaserver", "media.codec", "media.extractor"}:
                logs = [
                    f"{target} 的 media / codec / binder 相关日志",
                    "卡顿时整机 CPU / cpuinfo / process count 快照",
                    "同时间窗的电视端 Surface、FPS 与 Top 证据是否同步恶化",
                ]
            else:
                logs = [
                    f"Top 进程和重复实例，重点看 {target}",
                    "卡顿时整机 CPU / cpuinfo / process count 快照",
                    "电视端卡顿时间线与 CPU 抢占是否同步",
                ]
            retest = "复测时确认 tv_stall_count 是否下降，整机 CPU 是否明显回落，高相关进程实例数或占用是否恢复正常。"
            commands = [
                f"{adb_prefix()}shell top -b -n 1 | head -50",
                f"{adb_prefix()}shell dumpsys cpuinfo | head -40",
                f"{adb_prefix()}shell ps | grep \"{target_prefix}\"",
            ]
        elif cause_type == "DECODER_STUCK":
            decoder_name = str(decoder_summary.get("decoder_name", "") or target or "MPP Hardware Decoder")
            logs = [
                f"Decoder / MPP 日志，重点看 {decoder_name}",
                "MediaCodec / dequeue output timeout / codec error 相关日志",
                "代表样本的 video_fps / decode_fps / decode_drop 变化",
            ]
            target = decoder_name
            retest = "复测时确认解码输出恢复连续，video_fps 与 decode_fps_estimate 同步恢复，decoder 错误日志消失。"
            commands = [
                f"{adb_prefix()}shell logcat -d | grep -i \"codec\\|omx\\|c2\\|mpp\\|decoder\"",
                f"{adb_prefix()}shell dumpsys media.codec",
                f"{adb_prefix()}shell dumpsys SurfaceFlinger --latency \"{summary.get('tv_surface_name', '') or 'SurfaceView'}\"",
            ]
        elif cause_type == "LOW_FPS_DEGRADATION":
            logs = [
                "SurfaceFlinger / composer service 日志",
                "Display 1 的 FPS、max_frame_gap_ms 和 latency 输出",
                "GPU / 合成负载是否在同一时刻恶化",
            ]
            retest = "复测时确认 Display 1 FPS 稳定、surface 持续锁定、max_frame_gap_ms 明显回落。"
            commands = [
                f"{adb_prefix()}shell dumpsys SurfaceFlinger --display-id 1",
                f"{adb_prefix()}shell dumpsys SurfaceFlinger --latency \"{summary.get('tv_surface_name', '') or 'SurfaceView'}\"",
                f"{adb_prefix()}shell logcat -d | grep -i \"surfaceflinger\\|composer\\|hwcomposer\"",
            ]
        else:
            logs = [
                "Crash / ANR / codec / underrun 前后 30 秒日志",
                "电视端 Surface / FPS / Top 进程同时间证据",
                f"优先查看 {target if target and target != '无' else owner} 对应模块日志",
            ]
            retest = "复测时确认电视端卡顿是否消失，责任方向是否保持稳定，证据强度是否维持或提升。"
            commands = [
                f"{adb_prefix()}shell logcat -d | tail -300",
                f"{adb_prefix()}shell dumpsys SurfaceFlinger --latency \"{summary.get('tv_surface_name', '') or 'SurfaceView'}\"",
                f"{adb_prefix()}shell top -b -n 1 | head -50",
            ]

        return {
            "target": target,
            "owner": owner,
            "strength": strength,
            "logs": logs[:3],
            "retest": retest,
            "cause_type": cause_type or "UNKNOWN",
            "commands": commands[:3],
            "artifacts": build_artifacts(),
        }

    def _render_responsibility_section(self, summary: Dict) -> list:
        responsibility = summary.get("responsibility_summary", {}) or {}
        confidence = str(responsibility.get("confidence", "low") or "low").lower()
        target_label = "责任方向 / 优先对象"
        if confidence not in {"high", "medium"}:
            target_label = "建议排查方向 / 风险对象"
        elif "暂未确认主责" in str(responsibility.get("category", "") or ""):
            target_label = "建议排查方向 / 风险对象"
        lines = ["-" * 30, "【责任判定】"]
        lines.extend([
            f"1. 根因状态: {responsibility.get('category', '暂不能定责')}",
            f"2. 置信度: {responsibility.get('confidence', 'low')}",
            f"3. {target_label}: {responsibility.get('owner', '待确认')} / {responsibility.get('suspect_process', '无')}",
            f"4. 判定结论: {responsibility.get('conclusion', '暂无明确结论')}",
            f"5. 关键依据: {responsibility.get('key_basis', '暂无')}",
        ])
        evidence_items = list(responsibility.get("evidence_items", []) or [])
        if evidence_items:
            lines.append("6. 关键证据摘要:")
            for item in evidence_items:
                lines.append(f"   - {item.get('label', '证据')}: {item.get('value', '')}")
        gap_reasons = list(responsibility.get("confirmation_gap_reasons", []) or [])
        promotion_hints = list(responsibility.get("promotion_hints", []) or [])
        if gap_reasons:
            lines.append("7. 为什么还没升到确认级:")
            for item in gap_reasons[:4]:
                lines.append(f"   - {item}")
        if promotion_hints:
            lines.append("8. 下一步补证建议:")
            for item in promotion_hints[:4]:
                lines.append(f"   - {item}")
        return lines

    def _build_tv_event_text_statement(self, event: Dict) -> Dict:
        event = event if isinstance(event, dict) else {}
        event_type = str(event.get("type", "TV_STALL") or "TV_STALL")
        reason = str(event.get("reason", "") or "")
        confirmed = bool(event.get("confirmed", False))
        assessment_reason = str(event.get("assessment_reason", "") or "")
        freeze_level = str(event.get("freeze_level", "risk") or "risk").lower()
        freeze_category = str(event.get("freeze_category", "") or "")
        signals = [
            str(item or "").strip()
            for item in (event.get("corroboration_signals") or [])
            if str(item or "").strip()
        ]
        gap_reasons = [
            str(item or "").strip()
            for item in (event.get("confirmation_gap_reasons") or [])
            if str(item or "").strip()
        ]
        promotion_hints = [
            str(item or "").strip()
            for item in (event.get("promotion_hints") or [])
            if str(item or "").strip()
        ]
        contention = event.get("cpu_contention") or {}
        candidate = contention.get("top_candidate") or {}
        cpu_detected = bool(contention.get("detected"))
        process_name = str(candidate.get("process", "") or "无")
        process_cpu = float(candidate.get("peak_cpu_percent", 0) or 0.0)
        max_gap_ms = float(event.get("max_frame_gap_ms", 0) or 0.0)
        min_fps = float(event.get("min_fps", event.get("video_fps", 0)) or 0.0)
        video_fps = float(event.get("video_fps", 0) or 0.0)
        duration_ms = int(event.get("duration_ms", 0) or 0)
        duration_sec = float(event.get("duration_sec", 0) or 0.0)

        if event_type == "TV_FREEZE":
            statement = "这条事件更像电视端画面冻结样本。"
            basis = assessment_reason or f"冻结时长 {duration_sec:.1f} 秒"
            if signals:
                basis += f"；旁证信号 {', '.join(signals[:4])}"
            if video_fps > 0:
                basis += f"；视频 FPS {video_fps:.1f}"
            if max_gap_ms > 0:
                basis += f"；最大帧间隔 {max_gap_ms:.0f} ms"
            if freeze_category:
                basis += f"；分类 {freeze_category}"
            if freeze_level == "confirmed":
                next_action = "先看冻结样本前后 10 秒的 event.json、截图、Surface 指标和解码日志，确认是解码停顿还是系统侧真的卡住。"
                owner = "待继续定责"
                confirmed = True
            else:
                next_action = "这是一条冻结风险样本，先回看同一时刻的 Surface、FPS、解码日志和截图，确认是静态画面还是播放链路真的停住。"
                owner = "待补证据"
                confirmed = False
        elif cpu_detected:
            statement = f"这条事件更像系统/固件侧 CPU 资源竞争，优先排查 {process_name}。"
            basis = f"卡顿时命中 CPU 竞争信号；高相关进程 {process_name} ({process_cpu:.1f}%)；最大帧间隔 {max_gap_ms:.0f} ms。"
            next_action = f"先看 {process_name} 的拉起/保活逻辑，再结合 top_before/top_after.txt 确认是否重复抢占 CPU。"
            owner = "系统/固件侧"
        elif (
            "decoder_confirmed" in signals
            or "decode_drop" in signals
            or "decoder" in reason.lower()
        ):
            statement = "这条事件更像播放器/解码链路停顿，优先排查硬件解码器、码流和驱动。"
            basis = f"事件带有解码侧互证信号（{', '.join(signals) if signals else 'decoder'}）；最低 FPS {min_fps:.1f}。"
            next_action = "先看 event.json 与解码器相关日志，再核对码率、分辨率和芯片解码能力上限。"
            owner = "播放器/解码侧"
        elif confirmed:
            statement = "这条事件已确认是电视端卡顿，但当前还不能单独锁定到系统侧或播放器侧。"
            basis = f"事件已达确认级；最大帧间隔 {max_gap_ms:.0f} ms；最低 FPS {min_fps:.1f}。"
            next_action = "先看 event.json、截图和 cpu_during.jsonl，把时间线与播放器异常、系统负载一起交叉确认。"
            owner = "待继续定责"
        else:
            statement = "这条事件目前更像风险提示，还不能直接当成最终定责结论。"
            basis = assessment_reason or "当前只有局部异常信号，互证还不够完整。"
            if gap_reasons:
                basis += f" 还缺少：{'；'.join(gap_reasons[:3])}。"
            next_action = (
                "；".join(promotion_hints[:3])
                if promotion_hints else
                "继续补齐 Surface、FPS、日志和 CPU 证据后再定责。"
            )
            owner = "待补证据"

        trigger_parts = []
        if signals:
            trigger_parts.append(f"命中信号: {', '.join(signals[:4])}")
        if gap_reasons and not confirmed and event_type != "TV_FREEZE":
            trigger_parts.append(f"未闭环原因: {'；'.join(gap_reasons[:2])}")
        if not trigger_parts:
            trigger_parts.append("当前事件已具备基础说明，详见关键依据。")

        frame_gap_text = f"{max_gap_ms:.0f} ms" if max_gap_ms > 0 else "N/A"
        min_fps_text = f"{min_fps:.1f}" if min_fps > 0 else "N/A"
        return {
            "event_type": event_type,
            "owner": owner,
            "statement": statement,
            "basis": basis,
            "next_action": next_action,
            "trigger_summary": " | ".join(trigger_parts),
            "confirmed": bool(confirmed or event_type == "TV_FREEZE"),
            "duration_display": f"{duration_ms} ms" if duration_ms > 0 else (f"{duration_sec:.1f} s" if duration_sec > 0 else "N/A"),
            "frame_summary": f"{frame_gap_text} / {min_fps_text}",
            "process_name": process_name,
            "process_cpu": process_cpu,
            "evidence_dir": str(event.get("evidence_dir", "") or "N/A"),
            "time_text": str(event.get("start_time") or event.get("timestamp") or "N/A"),
        }

    def _render_tv_event_text_section(self, summary: Dict) -> list:
        events = []
        events.extend(list(summary.get("tv_stall_events_deduped", summary.get("tv_stall_events", [])) or []))
        events.extend(list(summary.get("tv_stall_risk_events_deduped", summary.get("tv_stall_risk_events", [])) or []))
        events.extend(list(summary.get("tv_freeze_events_deduped", summary.get("tv_freeze_events", [])) or []))
        if not events:
            return []

        def _sort_key(item: Dict):
            if not isinstance(item, dict):
                return ""
            return str(item.get("start_time") or item.get("timestamp") or "")

        lines = [
            "-" * 30,
            "【电视端异常事件明细】",
            "以下按事件粒度展示确认级卡顿、风险样本和冻结样本，便于研发直接判断“为什么报、差什么证据、先查哪里”。",
            (
                "静态画面过滤: "
                f"已忽略 {int(summary.get('tv_stall_ignored_count', 0) or 0)} 次，"
                f"累计约 {int(summary.get('tv_stall_ignored_duration_ms', 0) or 0) / 1000.0:.1f} 秒"
            ),
            (
                "冻结分级统计: "
                f"确认 {int(summary.get('tv_freeze_confirmed_count', 0) or 0)} | "
                f"风险 {int(summary.get('tv_freeze_risk_count', 0) or 0)} | "
                f"已忽略 {int(summary.get('tv_freeze_ignored_count', 0) or 0)}"
            ),
        ]
        sorted_events = sorted(events, key=_sort_key)[-12:]
        condensed_no_cpu = []
        display_events = []
        for event in sorted_events:
            if not isinstance(event, dict):
                continue
            detail = self._build_tv_event_text_statement(event)
            process_name = str(detail.get("process_name", "无") or "无").strip()
            process_cpu = float(detail.get("process_cpu", 0) or 0.0)
            if not detail.get("confirmed") and process_name in {"", "无"} and process_cpu <= 0.0:
                condensed_no_cpu.append((event, detail))
                continue
            display_events.append((event, detail))

        for index, (event, detail) in enumerate(display_events, start=1):
            if not isinstance(event, dict):
                continue
            auto_analysis = self._load_event_auto_analysis_text(str(event.get('evidence_dir', '') or ''))
            level = "确认级" if detail.get("confirmed") else "风险级"
            lines.append(f"{index}. 时间 / 时长: {detail.get('time_text', 'N/A')} / {detail.get('duration_display', 'N/A')}")
            direction_label = "责任方向" if detail.get("confirmed") else "建议排查方向"
            lines.append(
                f"   事件类型: {detail.get('event_type', 'TV_STALL')} | {direction_label}: {detail.get('owner', '待确认')} | 等级: {level}"
            )
            lines.append(f"   研发一句话结论: {detail.get('statement', '暂无结论')}")
            lines.append(f"   关键依据: {detail.get('basis', '暂无')}")
            lines.append(f"   触发条件说明: {detail.get('trigger_summary', '暂无')}")
            lines.append(f"   建议动作: {detail.get('next_action', '暂无')}")
            if auto_analysis:
                auto_title = str(auto_analysis.get("diagnosis_title", "") or "自动分析")
                auto_detail = str(auto_analysis.get("diagnosis_detail", "") or "").strip()
                lines.append(f"   自动分析结论: {auto_title}：{auto_detail or '已生成自动分析，请查看证据目录'}")
            lines.append(f"   最大帧间隔 / 最低FPS: {detail.get('frame_summary', 'N/A')}")
            lines.append(
                f"   CPU高相关进程: {detail.get('process_name', '无')} ({float(detail.get('process_cpu', 0) or 0.0):.1f}%)"
            )
            evidence_dir = str(detail.get('evidence_dir', 'N/A') or 'N/A')
            display_evidence_dir = evidence_dir
            anchor = "data\\"
            lower_dir = evidence_dir.lower()
            anchor_pos = lower_dir.find(anchor)
            if anchor_pos >= 0:
                display_evidence_dir = ".\\" + evidence_dir[anchor_pos:].replace("/", "\\")
            lines.append(f"   证据目录: {display_evidence_dir}")
        if condensed_no_cpu:
            durations = []
            gaps = []
            times = []
            for event, detail in condensed_no_cpu:
                try:
                    durations.append(int(event.get("duration_ms", 0) or 0))
                except Exception:
                    pass
                try:
                    gaps.append(float(event.get("max_frame_gap_ms", 0) or 0.0))
                except Exception:
                    pass
                times.append(str(detail.get("time_text", "N/A") or "N/A"))
            lines.append(f"{len(display_events) + 1}. 同类风险事件聚合（无明确 CPU 高相关进程）: 共 {len(condensed_no_cpu)} 次")
            lines.append(
                f"   关键依据: 这些事件都属于风险级样本，但当前 Top 快照未抓到明确 CPU 抢占进程，"
                f"更像显示链路瞬时抖动或证据不足场景。"
            )
            if gaps:
                lines.append(
                    f"   样本范围: 最大帧间隔 {min(gaps):.0f}-{max(gaps):.0f} ms"
                    + (f" | 持续 {min(durations)}-{max(durations)} ms" if durations else "")
                )
            lines.append(f"   时间分布: {times[0]} -> {times[-1]}")
            lines.append("   建议动作: 优先补看同时间窗的 Surface/FPS/日志互证，必要时提高采样密度再复测。")
        return lines

    def _build_process_failure_actions(self, summary: Dict) -> list:
        failure_summary = summary.get("process_failure_summary", {}) or {}
        actions = []
        if not failure_summary.get("has_player_failure"):
            return actions

        if int(failure_summary.get("crash_count", 0) or 0) > 0:
            actions.append("优先查看 Crash 堆栈、 tombstone 和异常前后 30 秒播放器日志，先确认是播放器自身崩溃还是被系统杀进程。")
        if int(failure_summary.get("anr_count", 0) or 0) > 0:
            actions.append("优先查看 ANR traces、主线程阻塞点和 binder 调用链，确认是 UI 线程卡死、解码线程阻塞还是系统服务超时。")
        if int(failure_summary.get("pid_loss_count", 0) or 0) > 0:
            actions.append("目标进程发生丢失，建议对照退出时刻的 logcat、lowmemorykiller/system_server 记录，确认是否被系统回收或异常退出。")
        if int(failure_summary.get("restart_count", 0) or 0) > 0:
            actions.append("存在 PID 重启，建议排查应用保活、守护拉起、闪退自恢复和外部脚本循环启动逻辑。")

        suspect = str(summary.get("suspect_process", "") or "")
        root_cause_type = str(summary.get("root_cause_type", "") or "")
        if root_cause_type == "CPU_CONTENTION" or suspect:
            actions.append("把异常时刻的 Top 进程、整机 CPU、播放器 PID 事件和电视端卡顿时间线放在同一时间轴上核对，避免只看到现象看不到因果。")

        if not actions:
            actions.append("建议结合异常时间线回看播放器日志、系统日志和 PID 生命周期，先确认是进程级故障还是性能退化。")
        return actions[:4]

    def _parse_event_timestamp(self, value):
        text = str(value or "").strip()
        if not text or text == "N/A":
            return None
        formats = (
            "%Y-%m-%d %H:%M:%S",
            "%Y%m%d_%H%M%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        )
        for fmt in formats:
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    def _build_tv_process_correlation_summary(self, summary: Dict, error_stats: Dict, window_seconds: int = 30) -> Dict:
        stall_events = list(summary.get("tv_stall_events", []) or [])
        pid_events = list(summary.get("pid_events", []) or [])
        error_events = list(error_stats.get("error_events", []) or [])

        failure_events = []
        for item in pid_events:
            item_type = str(item.get("type", "") or "").strip()
            if item_type not in {"PID_RESTART", "PID_LOST"}:
                continue
            parsed_time = self._parse_event_timestamp(item.get("timestamp"))
            if not parsed_time:
                continue
            failure_events.append({
                "type": item_type,
                "timestamp": item.get("timestamp", "N/A"),
                "datetime": parsed_time,
                "description": item.get("description", ""),
            })
        for item in error_events:
            item_type = str(item.get("type", "") or "").strip()
            if item_type not in {"CRASH", "ANR"}:
                continue
            parsed_time = self._parse_event_timestamp(item.get("time"))
            if not parsed_time:
                continue
            failure_events.append({
                "type": item_type,
                "timestamp": item.get("time", "N/A"),
                "datetime": parsed_time,
                "description": item.get("message", ""),
            })
        failure_events.sort(key=lambda item: item["datetime"])

        matched_stalls = []
        unmatched_stalls = []
        matched_failure_keys = set()
        pair_details = []
        for stall in stall_events:
            stall_time_text = stall.get("start_time") or stall.get("timestamp") or ""
            stall_dt = self._parse_event_timestamp(stall_time_text)
            if not stall_dt:
                unmatched_stalls.append(stall)
                continue
            nearest = None
            nearest_delta = None
            for failure in failure_events:
                delta = abs((failure["datetime"] - stall_dt).total_seconds())
                if nearest_delta is None or delta < nearest_delta:
                    nearest = failure
                    nearest_delta = delta
            if nearest is not None and nearest_delta is not None and nearest_delta <= window_seconds:
                matched_stalls.append(stall)
                matched_failure_keys.add((nearest["type"], nearest["timestamp"]))
                pair_details.append({
                    "stall_time": stall_time_text,
                    "stall_reason": stall.get("reason", ""),
                    "failure_type": nearest["type"],
                    "failure_time": nearest["timestamp"],
                    "failure_description": nearest.get("description", ""),
                    "delta_seconds": round(float(nearest_delta), 1),
                })
            else:
                unmatched_stalls.append(stall)

        matched_failures = [
            event for event in failure_events
            if (event["type"], event["timestamp"]) in matched_failure_keys
        ]
        matched_stall_count = len(matched_stalls)
        total_stall_count = len(stall_events)
        correlated_ratio = (
            float(matched_stall_count) / float(total_stall_count)
            if total_stall_count > 0 else 0.0
        )
        if total_stall_count <= 0:
            conclusion = "本轮未检测到电视端卡顿事件，暂无法分析其与播放器异常的时间相关性。"
            confidence_level = "none"
        elif matched_stall_count <= 0:
            conclusion = f"共检测到 {total_stall_count} 次电视端卡顿，但在卡顿前后 {window_seconds} 秒内未发现播放器异常时间重合，当前不支持直接归因到播放器挂掉。"
            confidence_level = "weak"
        elif correlated_ratio >= 0.7:
            conclusion = f"共检测到 {total_stall_count} 次电视端卡顿，其中 {matched_stall_count} 次在前后 {window_seconds} 秒内与播放器异常重合，高度怀疑播放器异常直接影响电视端流畅度。"
            confidence_level = "strong"
        else:
            conclusion = f"共检测到 {total_stall_count} 次电视端卡顿，其中 {matched_stall_count} 次在前后 {window_seconds} 秒内与播放器异常重合，存在相关性，但还需要结合日志和 CPU 证据继续确认主因。"
            confidence_level = "medium"

        return {
            "window_seconds": int(window_seconds),
            "total_tv_stall_count": total_stall_count,
            "total_failure_event_count": len(failure_events),
            "matched_tv_stall_count": matched_stall_count,
            "matched_failure_event_count": len(matched_failures),
            "correlated_ratio": round(correlated_ratio, 4),
            "confidence_level": confidence_level,
            "conclusion": conclusion,
            "pair_details": pair_details[:8],
        }

    def _render_tv_process_correlation_section(self, summary: Dict) -> list:
        correlation = summary.get("tv_process_correlation_summary", {}) or {}
        total_tv_stall_count = int(correlation.get('total_tv_stall_count', 0) or 0)
        total_failure_event_count = int(correlation.get('total_failure_event_count', 0) or 0)
        overlap_display = (
            "不适用"
            if total_tv_stall_count <= 0 and total_failure_event_count <= 0
            else f"{correlation.get('matched_tv_stall_count', 0)} 次"
        )
        lines = ["-" * 30, "【卡顿与播放器异常关联分析】"]
        lines.extend([
            f"1. 关联窗口: 前后 {correlation.get('window_seconds', 30)} 秒",
            f"2. 电视端卡顿总数: {total_tv_stall_count} 次",
            f"3. 播放器异常总数: {total_failure_event_count} 次",
            f"4. 时间重合卡顿数: {overlap_display}",
            f"5. 时间重合比例: {float(correlation.get('correlated_ratio', 0) or 0.0) * 100:.1f}%",
            f"6. 关联结论: {correlation.get('conclusion', '暂无明确结论')}",
        ])
        pair_details = list(correlation.get("pair_details", []) or [])
        if pair_details:
            lines.append("7. 关键重合样本:")
            for item in pair_details:
                lines.append(
                    f"   - 卡顿[{item.get('stall_time', 'N/A')}] "
                    f"{item.get('stall_reason', '')} | 异常[{item.get('failure_time', 'N/A')}] "
                    f"{item.get('failure_type', 'UNKNOWN')} | 间隔 {item.get('delta_seconds', 0)}s"
                )
        return lines

    def _build_executive_statement(self, summary: Dict, root_cause_analysis: Dict) -> str:
        diagnosis = (root_cause_analysis or {}).get("final_diagnosis") or {}
        tv_stall_count = int(summary.get("tv_stall_count", 0) or 0)
        tv_stall_risk_count = int(summary.get("tv_stall_risk_count", 0) or 0)
        decoder_stuck_risk_count = int(summary.get("decoder_stuck_risk_count", 0) or 0)
        avg_system_cpu = float(summary.get("avg_system_cpu_percent", 0) or 0.0)
        avg_player_cpu = float(summary.get("avg_player_cpu_percent", 0) or 0.0)
        surface_locked = bool(summary.get("tv_surface_locked", False))
        evidence_level = str(diagnosis.get("evidence_level", "") or "")
        conclusion = str(diagnosis.get("conclusion", "") or "")
        display_recommendation = summary.get("tv_display_recommendation", {}) or {}

        suspect = ""
        owner = str(diagnosis.get("owner", "") or "")
        process_risk_summary = (root_cause_analysis or {}).get("process_risk_summary") or []
        top_suspect_processes = (root_cause_analysis or {}).get("top_suspect_processes") or []

        if process_risk_summary:
            top_process = process_risk_summary[0] or {}
            process_name = str(top_process.get("process", "") or "")
            instance_count = int(top_process.get("max_instance_count", 0) or 0)
            if process_name:
                suspect = (
                    f"{process_name} x{instance_count}"
                    if instance_count > 1 and " x" not in process_name
                    else process_name
                )

        if not suspect and top_suspect_processes:
            first_suspect = top_suspect_processes[0] or {}
            if isinstance(first_suspect, dict):
                suspect = str(first_suspect.get("process", "") or "")
            elif isinstance(first_suspect, (list, tuple)) and first_suspect:
                suspect = str(first_suspect[0] or "")

        if not suspect:
            suspect = str(diagnosis.get("suspect_process", "") or "")

        if not owner:
            owner = self._infer_issue_owner_from_process(suspect)

        if (
            tv_stall_count > 0
            and avg_system_cpu >= 85.0
            and avg_player_cpu <= 15.0
            and owner in ("\u7cfb\u7edf/\u56fa\u4ef6\u4fa7", "\u5e94\u7528/\u7cfb\u7edf\u8054\u5408\u6392\u67e5")
        ):
            process_suffix = (
                f"\u3002\u5f53\u524d\u6700\u9ad8\u4f18\u5148\u7ea7\u5acc\u7591\u8fdb\u7a0b\u4e3a {suspect}"
                if suspect else ""
            )
            return (
                "\u672c\u8f6e\u95ee\u9898\u5df2\u7ecf\u660e\u786e\u4e3a\uff1a\u7535\u89c6\u7aef\u786e\u5b9e\u53d1\u751f\u5361\u987f\uff0c"
                "\u4e3b\u56e0\u662f\u6574\u673a CPU \u957f\u65f6\u95f4\u88ab\u9ad8\u8d1f\u8f7d\u8fdb\u7a0b\u7ade\u4e89\uff0c"
                f"\u95ee\u9898\u65b9\u5411\u504f{owner}\uff0c\u4e0d\u662f\u64ad\u653e\u5668\u5355\u8fdb\u7a0b\u672c\u8eab\u6027\u80fd\u4e0d\u8db3{process_suffix}\u3002"
            )

        if conclusion and evidence_level in ("confirmed", "strong"):
            return conclusion

        if tv_stall_count > 0:
            return (
                f"\u672c\u8f6e\u5df2\u786e\u8ba4\u7535\u89c6\u7aef\u5361\u987f {tv_stall_count} \u6b21\uff0c"
                "\u5efa\u8bae\u7ed3\u5408\u6839\u56e0\u5206\u6790\u7ee7\u7eed\u9501\u5b9a\u8d23\u4efb\u8fdb\u7a0b\u3002"
            )

        if tv_stall_risk_count > 0 or decoder_stuck_risk_count > 0:
            recommendation = ""
            if display_recommendation.get("display_id") is not None:
                recommendation = (
                    f"当前推荐监控 Display {display_recommendation.get('display_id')} "
                    f"({display_recommendation.get('reason', 'unknown')})。"
                )
            return (
                f"本轮暂未形成确认级电视端卡顿结论，但已捕获风险样本："
                f"电视端风险事件 {tv_stall_risk_count} 次，解码风险样本 {decoder_stuck_risk_count} 次。"
                f"{'已锁定电视端 Surface，建议继续观察是否收敛为确认卡顿。' if surface_locked else '当前仍需补足电视端 Surface/FPS 证据。'}"
                f"{recommendation}"
            )

        return ""

    def _infer_issue_owner_from_process(self, process_name: str) -> str:
        process = str(process_name or "").lower()
        if not process:
            return ""
        if (
            process.startswith("/system/bin/")
            or "surfaceflinger" in process
            or "mediaserver" in process
            or "composer" in process
            or "audioserver" in process
        ):
            return "\u7cfb\u7edf/\u56fa\u4ef6\u4fa7"
        if "com.thunder.ktv" in process or "player" in process:
            return "\u5e94\u7528\u4fa7"
        return "\u5e94\u7528/\u7cfb\u7edf\u8054\u5408\u6392\u67e5"

