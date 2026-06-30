import time
import os
import sys
import csv
import json
import logging
import threading
import statistics
from datetime import datetime

logger = logging.getLogger(__name__)
from typing import Dict
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
    def __init__(self, config: Dict, log_monitor=None, logger_callback=None):
        self.logger_callback = logger_callback
        
        def log(msg):
            if self.logger_callback:
                try:
                    self.logger_callback(str(msg))
                except Exception:
                    pass
        
        log("馃敡 姝ｅ湪鍒濆鍖栨祴璇曠粍浠?..")
        self.config = config
        self.running = False
        self.stop_flag = False
        self.failure_reason = ""
        self._stop_event = threading.Event()
        log("  - 鍒濆鍖?ADB 绠＄悊鍣?..")
        self.adb = AdbManager(device_id=config.get('device_id'))
        self.adb.set_cancel_event(self._stop_event)
        self.device_ip = self.adb.get_device_ip()
        self.firmware_incremental = self.adb.get_firmware_incremental()
        self.platform_identity = self.adb.get_platform_identity()
        self.log_monitor = log_monitor # 浼犲叆 LogMonitor 瀹炰緥
        self.package_name = config['target_app']['package_name']
        self.activity_name = config['target_app'].get('main_activity')
        self.http_config = config.get('http_vod', {})
        
        log("  - 鍒濆鍖栨€ц兘鐩戞帶鍣?..")
        self.monitor = PerformanceMonitor(
            self.adb,
            self.package_name,
            monitor_config=config.get("monitor", {}),
        )
        self.root_cause_analyzer = RootCauseAnalyzer(package_name=self.package_name)
        self.monitor.root_cause_analyzer = self.root_cause_analyzer
        log("  - 鍒濆鍖栨挱鏀惧櫒鎺у埗鍣?..")
        self.controller = PlayerController(self.adb, self.package_name, self.activity_name, self.http_config)
        log("  - 鍒濆鍖栨姤鍛婄敓鎴愬櫒...")
        self.report_generator = ReportGenerator(config['report']['output_dir'])
        log("  - 鍒濆鍖栧睆骞曞垎鏋愬櫒...")
        screenshots_dir = os.path.join(config['report']['output_dir'], 'screenshots')
        self.screen_analyzer = ScreenAnalyzer(self.adb, temp_dir=screenshots_dir)
        
        # V2.3.1: 闆跺共鎵版ā寮忛厤缃?
        monitor_config = config.get('monitor', {})
        self.enable_screenshot = monitor_config.get('enable_screenshot', True)  # 榛樿鍚敤
        self.enable_fps = monitor_config.get('enable_fps', True)  # 榛樿鍚敤
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
        self.pid_loss_abort_seconds = max(
            10.0,
            float(monitor_config.get("pid_loss_abort_seconds", 30)),
        )
        self.pid_loss_abort_samples = max(
            2,
            int(monitor_config.get("pid_loss_abort_samples", 3)),
        )
        
        # 搴旂敤鍒癿onitor
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
        self.tv_playback_watcher = TvPlaybackWatcher(
            self.adb,
            self.monitor,
            self.output_dir,
            config=monitor_config,
            event_callback=lambda event: self.monitor.report_event("TV_STALL", event),
            log_callback=self.log,
            log_monitor=self.log_monitor,
        )
        
        # 寮傛灞忓箷妫€娴?
        self.screen_check_executor = ThreadPoolExecutor(max_workers=1)
        self.screen_check_future = None
        self.screen_check_started_at = 0.0
        self.screen_check_skip_count = 0
        self.last_screen_results = {} # 缂撳瓨涓婁竴娆＄粨鏋?

        # 缁熻鏁版嵁
        self.song_count = 0
        self.start_timestamp = None
        self.end_timestamp = None
        self.last_song_title = None
        self.last_song_check_time = 0
        self.runtime_id = None
    
    def log(self, msg):
        logger.info("%s", msg)
        if self.logger_callback:
            try:
                self.logger_callback(str(msg))
            except Exception:
                pass

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
            return False, "鏁版嵁涓嶈冻"

        intervals = []
        for i in range(1, len(frame_timestamps)):
            interval = frame_timestamps[i] - frame_timestamps[i - 1]
            if interval > 0:
                intervals.append(interval)

        if len(intervals) < 5:
            return False, "鏈夋晥闂撮殧鏁版嵁涓嶈冻"

        mean_interval = statistics.mean(intervals)
        if mean_interval <= 0:
            return False, "鏁版嵁寮傚父"

        std_interval = statistics.stdev(intervals)
        cv = std_interval / mean_interval

        # 浼樺寲锛氫笅璋?CV 闃堝€硷紝鎹曟崏鏇寸粏寰殑甯ч棿闅旀姈鍔紙鍘?0.4/0.3/0.2 鈫?0.3/0.2/0.15锛?
        if cv > 0.3:
            return True, f"甯ч棿闅斾弗閲嶄笉鍧囧寑(CV={cv:.2f})"
        if cv > 0.2:
            return True, f"甯ч棿闅斾笉鍧囧寑(CV={cv:.2f})"
        if cv > 0.15:
            return True, f"甯ч棿闅旇交寰笉鍧囧寑(CV={cv:.2f})"

        return False, f"甯ч棿闅斿潎鍖€(CV={cv:.2f})"

    def _detect_fps_drops(self, fps_history):
        if len(fps_history) < 3:
            return False, "FPS鍘嗗彶鏁版嵁涓嶈冻"

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
            return False, "鏃犳槑鏄綟PS楠ら檷"

        worst = max(drops, key=lambda x: x["ratio"])
        desc = (
            f"检测到 FPS 骤降: {worst['from']:.1f} -> {worst['to']:.1f} "
            f"(降幅 {worst['ratio']*100:.0f}%, {worst['amount']:.1f}fps)"
        )
        return True, desc

    def _detect_fps_fluctuation(self, fps_history):
        valid = [v for v in fps_history if v > 0]
        if len(valid) < 5:
            return False, "鏈夋晥FPS鏁版嵁涓嶈冻"

        fps_mean = statistics.mean(valid)
        fps_std = statistics.stdev(valid)
        fps_min = min(valid)
        fps_max = max(valid)
        fps_range = fps_max - fps_min

        if fps_std > 6.0:
            return True, f"甯х巼娉㈠姩寰堝ぇ(蟽={fps_std:.1f}, 鑼冨洿{fps_min:.1f}-{fps_max:.1f})"
        if fps_std > 4.0:
            return True, f"甯х巼娉㈠姩杈冨ぇ(蟽={fps_std:.1f}, 鑼冨洿{fps_min:.1f}-{fps_max:.1f})"
        if fps_std > 2.5 and fps_mean < 28:
            return True, f"甯х巼杞诲井娉㈠姩(蟽={fps_std:.1f})锛屼笖骞冲潎甯х巼鍋忎綆({fps_mean:.1f})"
        if fps_range > 15 and fps_mean < 30:
            return True, f"甯х巼鑼冨洿杈冨ぇ({fps_range:.1f}fps)"

        return False, f"甯х巼绋冲畾(蟽={fps_std:.1f})"

    def _detect_sustained_low_fps(self, fps_history, threshold=25, min_duration=3):
        if len(fps_history) < min_duration:
            return False, "鏁版嵁涓嶈冻"

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
            return True, f"妫€娴嬪埌杩炵画{max_consecutive}娆′綆甯х巼(<{threshold}fps)"
        if max_consecutive >= 2:
            return True, f"妫€娴嬪埌{max_consecutive}娆¤繛缁綆甯х巼"

        return False, "鏃犳寔缁綆甯х巼"

    def _detect_av_sync_issues(self, audio_active, video_fps, audio_underrun_count=0):
        issues = []
        if audio_active and video_fps > 0 and video_fps < 22:
            issues.append(f"闊抽姝ｅ父浣嗚棰戝抚鐜囧亸浣?{video_fps:.1f}fps)")
        if audio_underrun_count > 0 and video_fps > 25:
            issues.append(f"闊抽涓嬫孩{audio_underrun_count}娆′絾瑙嗛姝ｅ父")
        if audio_underrun_count > 0 and video_fps > 0 and video_fps < 24:
            issues.append(
                f"闊宠棰戦兘瀛樺湪闂(闊抽涓嬫孩{audio_underrun_count}娆★紝瑙嗛{video_fps:.1f}fps)"
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
            details.append(f"鐬椂甯х巼楠ら檷({desc})")

        if metrics.get("sustained_low_fps"):
            score += weights["sustained_low_fps"]
            desc = metrics.get("sustained_low_desc", "")
            details.append(f"鎸佺画浣庡抚鐜?{desc})")

        if metrics.get("av_sync_issue"):
            score += weights["av_sync_issue"]
            desc = metrics.get("av_sync_desc", "")
            details.append(f"闊宠棰戝悓姝ラ棶棰?{desc})")

        if metrics.get("fps_fluctuation"):
            score += weights["fps_fluctuation"]
            desc = metrics.get("fps_fluctuation_desc", "")
            details.append(f"甯х巼娉㈠姩({desc})")

        if metrics.get("buffer_issue"):
            score += weights["buffer_pressure"]
            desc = metrics.get("buffer_desc", "")
            details.append(f"缂撳啿鍖哄帇鍔?{desc})")

        log_stutter_count = metrics.get("log_stutter_count", 0)
        if log_stutter_count > 0:
            stutter_score = min(weights["log_stutter"], log_stutter_count * 3)
            score += stutter_score
            details.append(f"日志卡顿({log_stutter_count}次)")

        # 鐐规瓕灞?UI Jank 浠呬綔鍙傝€冿紝涓嶈鍏ョ數瑙嗙鎾斁鍗￠】椋庨櫓鍒嗐€?

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
            recommendation = "播放流畅，可以上线"

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
                "details": ["鏁版嵁涓嶈冻"],
                "recommendation": "鏁版嵁涓嶈冻锛屾棤娉曡瘎浼版祦鐣呭害",
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
            fps_desc = "鏃犳硶鑾峰彇FPS鏁版嵁"
        elif current_fps < 20:
            fps_severity = "severe"
            fps_desc = f"瑙嗛甯х巼涓ラ噸鍋忎綆({current_fps:.1f}fps)"
        elif current_fps < 24:
            fps_severity = "warning"
            fps_desc = f"瑙嗛甯х巼鍋忎綆({current_fps:.1f}fps)"
        elif current_fps < 27:
            fps_severity = "notice"
            fps_desc = f"瑙嗛甯х巼鐣ヤ綆({current_fps:.1f}fps)"
        else:
            fps_severity = "good"
            fps_desc = f"瑙嗛甯х巼姝ｅ父({current_fps:.1f}fps)"

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

        recent_jank = 0.0
        tail = recent_snapshots[-5:]
        if tail:
            recent_jank = sum(
                s.get("gfx_jank_percent", 0) for s in tail
            ) / len(tail)
        metrics["ui_jank_percent"] = recent_jank

        return self._calculate_perceptual_stutter_score(metrics)

    def stop(self):
        """澶栭儴璋冪敤鍋滄"""
        self.stop_flag = True
        self._stop_event.set()
        self.log("鏀跺埌鍋滄璇锋眰锛屾鍦ㄧ粨鏉熺洃鎺х嚎绋嬪拰鐢熸垚鎶ュ憡...")
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
        self.running = True
        self.log(f"=== Android 鎾斁鍣ㄤ笓椤瑰帇娴嬪伐鍏?v2.3 ===")
        
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
            # 娣诲姞瓒呮椂淇濇姢锛岄伩鍏嶅崱浣?
            import signal
            import threading
            
            device_online = False
            check_timeout = False
            
            def check_device():
                nonlocal device_online
                try:
                    device_online = self.adb.is_device_online()
                except Exception as e:
                    self.log(f"鈿狅笍 璁惧妫€鏌ュ紓甯? {e}")
            
            check_thread = threading.Thread(target=check_device)
            check_thread.daemon = True
            check_thread.start()
            check_thread.join(timeout=5)  # 鏈€澶氱瓑寰?绉?
            
            if check_thread.is_alive():
                self.log("鈿狅笍 璁惧妫€鏌ヨ秴鏃讹紝缁х画灏濊瘯鍚姩...")
                device_online = True  # 鍋囪鍦ㄧ嚎锛岃鍚庣画姝ラ澶勭悊
            
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

        # 1. 鍚姩搴旂敤锛堟坊鍔犺秴鏃朵繚鎶わ級
        self.log("馃摫 姝ｅ湪鍚姩搴旂敤...")
        try:
            launch_thread = threading.Thread(target=self.controller.launch)
            launch_thread.daemon = True
            launch_thread.start()
            launch_thread.join(timeout=20)  # 鏈€澶氱瓑寰?0绉?
            
            if launch_thread.is_alive():
                self.log("鈿狅笍 搴旂敤鍚姩瓒呮椂锛岀户缁墽琛?..")
            else:
                self.log("鉁?搴旂敤鍚姩瀹屾垚")
        except Exception as e:
            self.log(f"鈿狅笍 搴旂敤鍚姩寮傚父: {e}锛岀户缁墽琛?..")
        
        # 1.1 鏍规嵁妯″紡鍐冲畾鏄惁棣栨挱鐐规瓕
        # 濡傛灉鏄?monitor_only锛岀粷瀵逛笉涓诲姩鐐规瓕
        if mode != "monitor_only" and self.http_config:
             self.log("妫€娴嬪埌 HTTP 鐐规瓕閰嶇疆锛屽皾璇曢鎾?..")
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
        self.monitor.start_new_session("Initial Session") # V2.1: 鍚姩绗竴涓?Session
        self.monitor.report_event("ACTION", "PLAY") # 璁板綍鎾斁寮€濮嬫椂闂?
        if self.enable_fps:
            self.tv_playback_watcher.start()
        
        last_action_time = time.time()
        start_time_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.last_csv_file = os.path.join(self.output_dir, f"report_{start_time_str}.csv")
        self.last_summary_file = os.path.join(self.output_dir, f"summary_{start_time_str}.txt")
        
        self.log(f"寮€濮嬪帇娴嬶紝妯″紡: {mode}锛屾椂闀? {duration_minutes}鍒嗛挓")
        
        # 鍒濆鍖朇SV
        with open(self.last_csv_file, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            # 鍙岃琛ㄥご
            headers = [
                'Timestamp(鏃堕棿鎴?', 
                'PID(杩涚▼ID)', 
                'Status(鐘舵€?', 
                'PSS_MB(鍐呭瓨MB)', 
                'Player_CPU_Percent(鎾斁鍣–PU%)',
                'System_CPU_Percent(鏁存満CPU%)',
                'MPP_Active(纭В璺暟)', 
                'MPP_Sessions(纭В鎬绘暟)', 
                'Restart_Count(閲嶅惎娆℃暟)', 
                'Play_State(鎾斁鐘舵€?', 
                'Audio_Active(闊抽娲昏穬)', 
                'GFX_Jank(鎺夊抚)', 
                'Log_Stutter(鍗￠】鏃ュ織)', 
                'Event(浜嬩欢)', 
                'Screenshot_D0(鎴浘_瑙︽懜灞?', 
                'Screenshot_D1(鎴浘_鐢佃)',
                'Top_Consumers(楂樺崰鐢ㄨ繘绋?鐢ㄤ簬鎺掗櫎宸ュ叿骞叉壈)',
                'Root_Cause_Type(鏍瑰洜绫诲瀷)',
                'Suspect_Process(瀚岀枒杩涚▼)',
                'Decode_Slowdown(瑙ｇ爜闄嶉€?',
                'Max_Temperature_C(鏈€楂樻俯搴?',
                'Min_CPU_Frequency_Ratio(鏈€浣庨鐜囨瘮渚?',
                'Thermal_Throttling(鐑檷棰?',
                'Expected_Stream_FPS(鏈熸湜娴佸抚鐜?',
                'Decode_FPS_Estimate(瑙ｇ爜浼扮畻甯х巼)',
                'Decode_Drop_Estimate(浼扮畻涓㈠抚鏁?',
                'Decode_Drop_Ratio(浼扮畻涓㈠抚姣斾緥)',
                'Video_FPS_Source(FPS鏁版嵁鏉ユ簮)',
                'TV_Surface_Name(鐢佃瑙嗛Surface)',
            ]
            writer.writerow(headers)

        exit_status = RuntimeStatus.COMPLETED
        try:
            self.log("杩涘叆涓诲惊鐜?..")
            sys.stdout.flush()
            loop_count = 0
            while time.time() < end_time and not self.stop_flag:
                loop_count += 1
                current_time = time.time()
                self.log(f"Loop #{loop_count} | Remaining: {end_time - current_time:.1f}s")
                sys.stdout.flush()
                
                # 1. 鑾峰彇澶栭儴浜嬩欢 (Logs)
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

                # A. 鎵ц鐩戞帶閲囬泦 (V2: 鍖呭惈 Evaluator 鏇存柊)
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
                
                # V2.4: 瀹炴椂FPS鏈夋晥鎬ф鏌?(Early Warning)
                # V2.3.2: FPS妫€娴嬪凡绉昏嚦monitor.py涓殑鏅鸿兘妫€娴嬮€昏緫
                # 杩欓噷涓嶅啀闇€瑕侀噸澶嶇殑FPS璀﹀憡閫昏緫
                
                self.log("Snapshot collected.")
                sys.stdout.flush()
                event_log = ""
                
                # A-2. 灞忓箷鐘舵€佹娴?(浣庨: 姣?30s)
                current_screenshot_d0 = ""
                current_screenshot_d1 = ""
                
                if not hasattr(self, 'last_screen_check_time'):
                    self.last_screen_check_time = 0
                self._expire_stuck_screen_check(current_time)
                
                # 妫€鏌ュ紓姝ヤ换鍔℃槸鍚﹀畬鎴?
                if self.screen_check_future and self.screen_check_future.done():
                    try:
                        self.last_screen_results = self.screen_check_future.result()
                        # self.log("Screen check finished (Async).")
                        
                        # 瑙ｆ瀽缁撴灉骞朵笂鎶?(浠呭湪缁撴灉鏇存柊鏃舵墽琛屼竴娆?
                        # 瑙ｆ瀽 Display 0
                        res_d0 = self.last_screen_results.get(0, {})
                        status_d0 = res_d0.get('status', 'UNKNOWN')
                        current_screenshot_d0 = res_d0.get('path', '')
                            
                        # V2.3: 瑙ｆ瀽鎵€鏈夋娴嬪埌鐨勭數瑙嗙 Display锛堝彲鑳芥槸 1 鎴?2锛?
                        # 浼樺厛浣跨敤绗竴涓娴嬪埌鐨勭數瑙嗙 Display
                        tv_display_id = None
                        current_screenshot_d1 = ""
                        status_d1 = "UNKNOWN"
                        
                        # 鏌ユ壘鎵€鏈夐潪 Display 0 鐨勭粨鏋?
                        for display_id, result in self.last_screen_results.items():
                            if display_id != 0:  # 鎺掗櫎鐐规瓕灞?
                                tv_display_id = display_id
                                res_d1 = result
                                status_d1 = res_d1.get('status', 'UNKNOWN')
                                current_screenshot_d1 = res_d1.get('path', '')
                                break  # 浣跨敤绗竴涓壘鍒扮殑鐢佃绔?Display
                        
                        # V2.3: 鐢佃绔敾闈㈠喕缁撴娴嬶紙閽堝妫€娴嬪埌鐨勭數瑙嗙 Display锛?
                        if current_screenshot_d1 and os.path.exists(current_screenshot_d1):
                            from PIL import Image
                            if not hasattr(self, 'tv_screenshot_history'):
                                self.tv_screenshot_history = []
                            
                            # 淇濆瓨鏈€杩?娆℃埅鍥捐矾寰勶紙鐢ㄤ簬瀵规瘮锛?
                            self.tv_screenshot_history.append({
                                'path': current_screenshot_d1,
                                'time': current_time
                            })
                            # 鍙繚鐣欐渶杩?娆?
                            if len(self.tv_screenshot_history) > 3:
                                self.tv_screenshot_history.pop(0)
                            
                            # 濡傛灉鑷冲皯鏈?娆℃埅鍥撅紝涓旀椂闂撮棿闅?>= 1绉掞紝杩涜瀵规瘮
                            if len(self.tv_screenshot_history) >= 2:
                                last_screenshot = self.tv_screenshot_history[-2]
                                time_diff = current_time - last_screenshot['time']
                                
                                if time_diff >= 1.0:  # 鑷冲皯1绉掗棿闅?
                                    # 瀵规瘮鐢婚潰鏄惁闈欐
                                    try:
                                        is_frozen = self.screen_analyzer._is_same_image(
                                            Image.open(current_screenshot_d1).convert("RGB"),
                                            last_screenshot['path'],
                                            diff_threshold=1.0  # 鏋佷綆闃堝€硷紝鍙湁鍑犱箮瀹屽叏涓€鏍锋墠绠楅潤姝?
                                        )
                                        
                                        # 濡傛灉鐢婚潰闈欐涓旈煶棰戝湪璺戯紝鍒ゅ畾涓虹數瑙嗙鐢婚潰鍐荤粨
                                        snapshot = self.monitor.history[-1] if self.monitor.history else {}
                                        audio_active = snapshot.get('audio_active', False)
                                        ignore_video = snapshot.get('ignore_video_metrics', False)
                                        surface_locked = bool(snapshot.get('tv_surface_name', ''))

                                        if is_frozen and audio_active and not ignore_video and surface_locked:
                                            if len(self.tv_screenshot_history) >= 3:
                                                time_span = self.tv_screenshot_history[-1]['time'] - self.tv_screenshot_history[0]['time']
                                                if time_span >= self.tv_freeze_threshold_seconds:
                                                    display_id_str = f"Display {tv_display_id}" if tv_display_id else "电视端"
                                                    self.log(f"  [TV Freeze Detected] {display_id_str} 画面连续静止约 {time_span:.1f} 秒")
                                                    self.monitor.report_event("TV_FREEZE", f"{display_id_str} 画面静止 {time_span:.1f} 秒")
                                                    event_log += f" [TV_FREEZE:{time_span:.1f}s]"
                                                    self.tv_screenshot_history = []
                                    except Exception as e:
                                        # 瀵规瘮澶辫触锛屽拷鐣?
                                        pass
                            
                        # 鍛婅妫€娴?
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
                            
                        # 濡傛灉鎴愬姛鎴浘锛屼篃鍦ㄦ帶鍒跺彴杈撳嚭涓€涓嬭矾寰勶紝鏂逛究璋冭瘯
                        if current_screenshot_d0 or current_screenshot_d1:
                                # 浠呮墦鍗版枃浠跺悕锛岄伩鍏嶅お闀?
                                fn0 = os.path.basename(current_screenshot_d0) if current_screenshot_d0 else "-"
                                fn1 = os.path.basename(current_screenshot_d1) if current_screenshot_d1 else "-"
                                # self.log(f"  [Snapshot] D0: {fn0}, D1: {fn1}")

                    except Exception as e:
                        self.log(f"Async screen check failed: {e}")
                        self.last_screen_results = {}
                    finally:
                        self._reset_screen_check_state()

                # V2.3.1: 闆跺共鎵版ā寮?- 濡傛灉绂佺敤鎴浘锛岃烦杩囧睆骞曟娴?
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
                    # 鏋佷綆鍔熻€楁ā寮忥細瀹屽叏绂佺敤鎴浘锛屼笉杩涜灞忓箷妫€娴?
                    # 鍙湪棣栨杩愯鏃跺垵濮嬪寲涓€娆?
                    if not hasattr(self, '_screenshot_disabled_logged'):
                        self.log("  [Zero-Interference Mode] Screenshot disabled for zero performance impact")
                        self._screenshot_disabled_logged = True

                # 浣跨敤鏈€鏂扮殑缁撴灉杩涜鏃ュ織/CSV璁板綍 (浣嗕笉閲嶅涓婃姤 Monitor)
                screen_results = self.last_screen_results
                    
                # 瑙ｆ瀽 Display 0 鐢ㄤ簬 CSV
                res_d0 = screen_results.get(0, {})
                status_d0 = res_d0.get('status', 'UNKNOWN')
                current_screenshot_d0 = res_d0.get('path', '') # Update for CSV logic if needed


                # A-3. MPP 璧勬簮娉勯湶淇濇姢 (V2.1)
                mpp_sessions = snapshot.get('mpp_sessions', 0)
                if mpp_sessions > 32:
                     self.log(f"!!! CRITICAL WARNING: MPP Session Leak Detected ({mpp_sessions} > 32) !!!")
                     self.log("!!! Stopping Test to prevent System Hang !!!")
                     self.monitor.report_event("FATAL", f"MPP Leak: {mpp_sessions} sessions")
                     self.stop_flag = True
                     break

                # B. 鎵ц绛栫暐鎿嶄綔
                if mode == "monitor_only":
                    # 绾洃鎺фā寮忥細涓嶆墽琛屼换浣曚富鍔ㄦ搷浣?(鍒囨瓕/鐐规瓕)
                    # 妫€娴嬫瓕鏇插彉鍖栦互鏇存柊 Song Count
                    # 鏂瑰紡1: Logcat 鍏抽敭瀛楋紙鐢ㄦ埛閰嶇疆 song_change_keywords锛?
                    if self.log_monitor and hasattr(self.log_monitor, 'get_song_change_events'):
                        for evt in self.log_monitor.get_song_change_events():
                            self.song_count += 1
                            self.log(f" [Monitor] 妫€娴嬪埌鍒囨瓕 (Logcat): {evt.get('line', '')[:60]}...")
                            self.monitor.start_new_session(f"Song #{self.song_count}")
                            event_log += f" [鍒囨瓕 #{self.song_count}]"
                    
                    # 鏂瑰紡2: dumpsys media_session 鍏冩暟鎹紙闇€搴旂敤浣跨敤 MediaSession API锛?
                    if current_time - self.last_song_check_time >= 5:
                        self.last_song_check_time = current_time
                        try:
                            current_song = self.adb.get_media_metadata(self.package_name)
                            if current_song and current_song != self.last_song_title:
                                if self.last_song_title is not None:
                                    self.song_count += 1
                                    self.log(f" [Monitor] 妫€娴嬪埌鍒囨瓕 (MediaSession): {current_song}")
                                    self.monitor.start_new_session(f"Detected: {current_song}")
                                    event_log += f" [New Song: {current_song}]"
                                else:
                                    self.log(f" [Monitor] 鍒濆姝屾洸: {current_song}")
                                    self.monitor.start_new_session(f"Detected: {current_song}")
                                self.last_song_title = current_song
                        except Exception:
                            pass

                elif mode == "fixed_skip":
                    if current_time - last_action_time >= skip_interval:
                        # 缁撶畻褰撳墠浼氳瘽
                        verdict, reason = self.monitor.get_session_result(is_force_stop=True)
                        self.log(f"-------- [Song #{self.song_count}] Result: {verdict} ({reason}) --------")
                        
                        self.controller.next_song() # 寮哄埗鍒囨瓕
                        self.song_count += 1
                        self.monitor.start_new_session(f"Unknown (Song #{self.song_count})")
                        self.monitor.report_event("ACTION", "PLAY") # 鏂颁細璇濆紑濮嬭鏃?
                        last_action_time = current_time
                        event_log = f"Action: Cut Song | Last Result: {verdict}"
                
                elif mode == "loop_playback":
                    # 寰幆鎾斁妯″紡锛?
                    # 1. 濡傛灉鎾斁澶辫触鎴栧崱鍦ㄥ垵濮嬪寲锛屽皾璇曢噸鏂扮偣姝?
                    # 2. 濡傛灉鎾斁瀹屾垚锛屽皾璇曢噸鏂扮偣姝?(濡傛灉搴旂敤涓嶈嚜鍔ㄨ繛鎾?
                    # 3. 濡傛灉鏃堕棿鍒颁簡 skip_interval 涓旇繕鍦ㄦ挱鏀撅紝寮哄埗鍒囨瓕 (鍘嬫祴閫昏緫)
                    
                    # 妫€鏌ュ綋鍓嶇姸鎬?
                    play_state = snapshot.get('play_state', 'UNKNOWN')
                    
                    # 绛栫暐 A: 澶辫触鎭㈠ (Start Failed) -> 绔嬪嵆閲嶈瘯
                    if "FAIL" in event_log or "Stuck" in snapshot.get('status', ''):
                         pass # 涓嬮潰鐨勬椂闂存鏌ヤ細瑙﹀彂锛屾垨鑰呮垜浠彲浠ョ珛鍗宠Е鍙戯紵
                    
                    if current_time - last_action_time >= skip_interval:
                        # 鍙湁鍦ㄧ‘瀹為渶瑕佺殑鏃跺€欐墠鐐规瓕锛?
                        # 鐢ㄦ埛鍙嶉锛氫负浠€涔堣嚜鍔ㄧ偣姝岋紵
                        # 瑙ｉ噴锛氬洜涓鸿繖鏄?loop_playback 鍘嬫祴妯″紡锛岄粯璁よ涓烘槸鍛ㄦ湡鎬х偣姝?鍒囨瓕浠ユ祴璇曠ǔ瀹氭€с€?
                        # 濡傛灉鐢ㄦ埛鍙兂鐩戞帶锛屽簲璇ョ敤 monitor_only銆?
                        # 浣嗕负浜嗕綋楠屾洿濂斤紝濡傛灉褰撳墠姝ｅ湪鎾斁涓旀病瓒呮椂澶锛屼篃璁稿彲浠ヤ笉鍒囷紵
                        # 鏆傛椂淇濇寔鍘熼€昏緫锛屼絾鍦ㄦ棩蹇椾腑鏄庣‘鍘熷洜
                        
                        # 缁撶畻涓婁竴棣?
                        verdict, reason = self.monitor.get_session_result(is_force_stop=True)
                        self.log(f"-------- [Song #{self.song_count}] Result: {verdict} ({reason}) --------")
                        
                        # 浠呭綋涓婁竴棣栫粨鏉熸垨澶辫触锛屾垨鑰呭己鍒跺垏姝屾椂闂村埌
                        self.log(f" 姝ｅ湪閫氳繃 HTTP 鐐规瓕 (Loop Mode Cycle)...")
                        song_id = self.controller.vod_song() 
                        self.song_count += 1
                        
                        self.monitor.start_new_session(f"ID: {song_id}" if song_id else f"Song #{self.song_count}")
                        self.monitor.report_event("ACTION", "PLAY")
                        
                        last_action_time = current_time
                        event_log = f"Action: Order Song (Loop Cycle) | Last Result: {verdict}"
                
                elif mode == "random_skip":
                     # 绠€鍗曠殑闅忔満鍒囨瓕瀹炵幇锛屽鐢?fixed_skip 鐨勬椂闂存娴嬮€昏緫锛屼絾闂撮殧闅忔満鍖?
                     # 杩欓噷绠€鍖栧鐞嗭紝鏆傛椂鍋囪 random_skip_range 鍦?runner 澶栭儴澶勭悊濂芥垨鑰呭湪杩欓噷澶勭悊
                     pass 
                
                # C. 杈撳嚭鏃ュ織
                fps_val = snapshot.get('video_fps', 0) or 0
                fps_str = f"{float(fps_val):.2f}" if float(fps_val) > 0 else "N/A"
                log_str = (f"[{snapshot['timestamp']}] PID:{snapshot['pid']} "
                          f"PSS:{snapshot['pss_mb']}MB CPU:{snapshot['cpu_percent']}% "
                          f"Restarts:{snapshot['restart_count']} "
                          f"State:{snapshot.get('play_state', 'N/A')} "
                          f"FPS:{fps_str}")
                
                # 鏄剧ず鍗￠】淇℃伅
                jank = snapshot.get('gfx_jank_count', 0)
                stutter = snapshot.get('log_stutter_count', 0)
                if jank > 0:
                     log_str += f" [JANK:{jank}]"
                
                if snapshot['is_restarted']:
                    log_str += " [WARNING: RESTART DETECTED]"
                if event_log:
                    log_str += f" | {event_log}"
                self.log(log_str)
                sys.stdout.flush() # 寮哄埗鍒锋柊杈撳嚭
                
                # D. 鍐欏叆鎶ュ憡
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
            self.log("\n鐢ㄦ埛涓柇鍘嬫祴")
            self.monitor.report_event("ACTION", "STOP")
        except Exception as e:
            exit_status = RuntimeStatus.FAILED
            self.log(f"鎵ц寮傚父: {e}")
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
            self._generate_summary_report(start_time_str)
            self.log("鍘嬫祴缁撴潫")
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
        
        # 鑾峰彇閫€鍖栧垎鏋愮粨鏋?
        degradation = summary.get('degradation_analysis', {})
        
        # 璁＄畻鏃堕暱
        duration_sec = 0
        if self.start_timestamp and self.end_timestamp:
            duration_sec = int(self.end_timestamp - self.start_timestamp)
        duration_str = f"{duration_sec // 3600}小时 {(duration_sec % 3600) // 60}分钟 {duration_sec % 60}秒"

        # 鑾峰彇閿欒缁熻骞跺悎骞跺埌 summary (涓轰簡璇勫垎)
        error_stats = {"crash_count": 0, "anr_count": 0}
        if self.log_monitor:
            error_stats = self.log_monitor.get_error_stats()
        
        summary['crash_count'] = error_stats['crash_count']
        summary['anr_count'] = error_stats['anr_count']
        summary['error_events'] = error_stats.get('error_events', [])
        summary['process_failure_summary'] = self._build_process_failure_summary(summary, error_stats)
        summary['process_failure_actions'] = self._build_process_failure_actions(summary)
        summary['tv_process_correlation_summary'] = self._build_tv_process_correlation_summary(summary, error_stats)
        summary['responsibility_summary'] = self._build_responsibility_summary(summary, root_cause_analysis)
        summary['platform_support_summary'] = self._build_platform_support_summary(summary)
        self.last_summary_data = dict(summary)
        
        score_result = self.monitor.calculate_score(summary)
        try:
            perceptual_result = self._comprehensive_stutter_detection(summary)
        except Exception:
            perceptual_result = {
                "score": 0,
                "level": "unknown",
                "details": [],
                "recommendation": "浜虹溂鎰熺煡璇勫垎璁＄畻澶辫触",
                "human_perceptible": False,
                "severity": "low",
            }

        if isinstance(score_result, dict):
            metrics = score_result.get("metrics") or {}
            metrics["perceptual_stutter"] = perceptual_result
            score_result["metrics"] = metrics
        
        # 鐢熸垚涓€鍙ヨ瘽缁撹
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

        executive_statement = self._build_executive_statement(
            summary,
            root_cause_analysis,
        )
        with open(self.last_summary_file, 'w', encoding='utf-8') as f:
            f.write("=== Android 播放器压测报告 (V2标准) ===\n")
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"测试包名: {self.package_name}\n")
            f.write(f"测试设备: {device_id if device_id else 'N/A'}\n")
            f.write(f"机顶盒 IP: {self.device_ip or '未获取'}\n")
            f.write(f"固件版本: {self.firmware_incremental or '未获取'}\n")
            f.write("-" * 30 + "\n")
            
            # === 鏂板锛氳瘎鍒嗙粨璁?===
            f.write("銆愭祴璇曠粨璁?(Decision)銆慭n")
            f.write(f"{one_sentence_summary}\n")
            if duration_sec < 3600:
                f.write(
                    "[瑕嗙洊搴︽彁绀篯 鏈疆涓嶈冻1灏忔椂锛屽彲楠岃瘉鍩虹娴佺晠搴︼紱"
                    "鍐呭瓨绉疮銆佸悗鍙癈PU绔炰簤鍜岀儹闄嶉寤鸿鑷冲皯杩愯1灏忔椂銆俓n"
                )
            f.write("-" * 30 + "\n")
            
            f.write(f"绋冲畾鎬ц瘎鍒? {score_result['score']} / 100 (绛夌骇: {score_result['grade']})\n")
            f.write(
                f"鐢佃绔崱椤块闄╁垎: {perceptual_result['score']} / 100 "
                f"(鍒嗘暟瓒婁綆瓒婃祦鐣咃紝绛夌骇: {perceptual_result['level']})\n"
            )
            f.write(f"鎰熺煡寤鸿: {perceptual_result['recommendation']}\n")
            
            if score_result.get("ready_to_release"):
                release_status = "鉁?寤鸿涓婄嚎"
            elif score_result.get("assessment") == "inconclusive":
                release_status = "鈿狅笍 璇佹嵁涓嶈冻锛屾殏涓嶄綔涓婄嚎缁撹"
            else:
                release_status = "鉂?涓嶅缓璁笂绾?(瀛樺湪闃绘柇鎬ч棶棰?"
            f.write(f"鍑嗗叆缁撹: {release_status}\n")
            blockers = score_result.get("release_blockers", []) or []
            if blockers:
                f.write("鍙戝竷闃绘柇/瑕嗙洊缂哄彛:\n")
                for blocker in blockers:
                    f.write(f"  - {blocker}\n")
            
            if score_result['deductions']:
                f.write("鎵ｅ垎椤?\n")
                for ded in score_result['deductions']:
                    f.write(f"  - {ded}\n")
            if executive_statement:
                f.write(f"Summary: {executive_statement}\n")
            f.write("-" * 30 + "\n")
            # =====================
            
            f.write("銆愭祴璇曟墽琛岀粺璁°€慭n")
            f.write(f"1. 瀹為檯杩愯鏃堕暱: {duration_str}\n")
            if test_mode == "monitor_only":
                f.write("2. 鎾斁缁熻: 绾洃鎺фā寮忥紝涓嶄富鍔ㄧ偣姝屾垨缁熻姝屾洸鎴愬姛鐜嘰n")
            else:
                f.write(f"2. 姝屾洸鍒囨崲/鐐规挱娆℃暟: {self.song_count} 棣朶n")
            
            # V2.1 Session Stats
            session_stats = summary.get("session_stats", {})
            total_sessions = session_stats.get("total", 0)
            success_sessions = session_stats.get("success", 0)
            success_rate = (success_sessions / total_sessions * 100) if total_sessions > 0 else 0
            
            if test_mode == "monitor_only":
                f.write("3. 鎾斁鎴愬姛鐜? 涓嶉€傜敤锛堢函鐩戞帶妯″紡锛塡n")
            elif total_sessions > 0:
                f.write(f"3. 鎾斁鎴愬姛鐜? {success_rate:.1f}% ({success_sessions}/{total_sessions})\n")
            else:
                f.write("3. 鎾斁鎴愬姛鐜? 鏃犳湁鏁堟挱鏀句細璇漒n")
            f.write(f"4. 鎬ц兘閲囨牱鐐规暟: {summary.get('duration_samples', 0)}\n")
            f.write(
                f"5. 鏈夋晥閲囨牱瑕嗙洊鐜? {summary.get('valid_samples', 0)}/"
                f"{summary.get('duration_samples', 0)} "
                f"({float(summary.get('valid_sample_ratio', 0) or 0) * 100:.1f}%)\n"
            )
            
            f.write("-" * 30 + "\n")
            
            f.write("銆愰敊璇眹鎬?(Errors)銆慭n")
            f.write(f"1. 宕╂簝 (Crash/Exception): {error_stats['crash_count']} 娆n")
            f.write(f"2. 鏃犲搷搴?(ANR): {error_stats['anr_count']} 娆n")
            f.write(f"3. 杩涚▼寮傚父閲嶅惎: {summary.get('restart_count', 0)} 娆n")
            f.write(f"4. 鐩爣杩涚▼涓㈠け: {summary.get('pid_loss_count', 0)} 娆n")
            
            f.write("-" * 30 + "\n")
            f.write("銆愯缁嗛敊璇褰?(Detailed Errors)銆慭n")
            
            # 1. 鎾斁澶辫触浼氳瘽
            failed_sessions = summary.get('failed_sessions', [])
            if failed_sessions:
                f.write(">> 鎾斁澶辫触/涓柇璁板綍:\n")
                for item in failed_sessions:
                    f.write(f"   - [{item['time']}] Song: {item['song']} | Reason: {item['reason']}\n")
            else:
                f.write(">> 鏃犳挱鏀惧け璐ヨ褰昞n")
                
            # 2. 绯荤粺绾ч敊璇?(Crash/ANR)
            error_events = summary.get('error_events', [])
            # 杩囨护鎺?log_file 瀛楁浠ョ畝鍖栨樉绀猴紝鎴栬€呬繚鐣?
            if error_events:
                f.write("\n>> 绯荤粺宕╂簝/ANR璁板綍:\n")
                for evt in error_events:
                    f.write(f"   - [{evt['time']}] Type: {evt['type']} | Msg: {evt['message']}\n")
                    f.write(f"     (Log: {evt.get('log_file', '')})\n")
            else:
                f.write("\n>> 鏃犵郴缁熺骇宕╂簝璁板綍\n")
            
            f.write("-" * 30 + "\n")
            
            f.write("銆愭牳蹇冪ǔ瀹氭€ф寚鏍囥€慭n")
            f.write(f"1. 杩涚▼閲嶅惎娆℃暟: {summary.get('restart_count', 0)}\n")
            
            # V2.1: 鏄惧紡璁＄畻棣栨閲嶅惎鏃堕棿
            first_restart_time = "N/A"
            if summary.get('pid_events'):
                for evt in summary['pid_events']:
                    if evt['type'] in ["PID_RESTART", "PID_LOST"]:
                        first_restart_time = evt['timestamp']
                        break
            f.write(f"2. 棣栨杩涚▼寮傚父鏃堕棿: {first_restart_time}\n")

            if summary.get('pid_events'):
                f.write("   [鐢熷懡鍛ㄦ湡浜嬩欢璇︽儏]:\n")
                for evt in summary['pid_events'][:10]:
                    if evt['type'] in ["PID_RESTART", "PID_LOST", "PID_FOUND"]:
                        elapsed = evt.get('elapsed_min', 0)
                        f.write(f"   - [{evt['timestamp']}] (绗?{elapsed} 鍒嗛挓) {evt['description']}\n")
            
            f.write(f"3. 宄板€煎唴瀛?PSS): {summary.get('max_pss_mb', 0)} MB\n")
            f.write(f"4. 骞冲潎鍐呭瓨(PSS): {summary.get('avg_pss_mb', 0)} MB\n")
            f.write(
                f"5. 鎾斁鍣ㄥ钩鍧嘋PU / 鏁存満骞冲潎CPU / 鏁存満宄板€糃PU: "
                f"{summary.get('avg_player_cpu_percent', 0)}% / "
                f"{summary.get('avg_system_cpu_percent', 0)}% / "
                f"{summary.get('max_system_cpu_percent', 0)}%\n"
            )
            if summary.get("thermal_available_count", 0) > 0:
                f.write(
                    f"6. 鏈€楂樻俯搴?/ 鏈€浣嶤PU棰戠巼姣斾緥 / 鐑檷棰戦噰鏍? "
                    f"{summary.get('max_temperature_c', 0)}掳C / "
                    f"{float(summary.get('min_cpu_frequency_ratio', 0) or 0) * 100:.1f}% / "
                    f"{summary.get('thermal_throttling_count', 0)} 娆n"
                )
            else:
                f.write("6. 娓╁害涓嶤PU棰戠巼: 璁惧鑺傜偣涓嶆敮鎸佹垨鏃犺鍙栨潈闄愶紝鏈噰闆哱n")
            f.write(
                f"7. 瑙ｇ爜鍚炲悙鏄庢樉涓嬮檷: "
                f"{summary.get('decode_slowdown_count', 0)} 娆n"
            )
            f.write("-" * 30 + "\n")
            f.write("銆愭牴鍥犲垎鏋?(V3.0)銆慭n")
            if root_cause_analysis:
                diagnosis = root_cause_analysis.get("final_diagnosis") or {}
                if isinstance(diagnosis, dict) and diagnosis:
                    cause_type = str(((root_cause_analysis.get("most_confident_cause") or {}).get("root_cause_type", "") or ""))
                    validation_hint = {
                        "CPU_CONTENTION": "复测时重点看 tv_stall_count 是否下降，同时确认整机 CPU 明显回落，且嫌疑进程实例数或占用恢复正常。",
                        "DECODER_STUCK": "复测时重点看解码输出是否恢复连续，video_fps 与 decode_fps_estimate 是否同步恢复，并核对具体 decoder 错误日志是否消失。",
                        "LOW_FPS_DEGRADATION": "复测时重点看 Display 1 的 max_frame_gap_ms 是否回落，电视端 FPS 是否稳定，同时排查 composer / SurfaceFlinger 相关负载。",
                        "AV_SYNC_ISSUE": "复测时重点看 droppedFrames / underrun / buffer starvation 日志是否消失，并确认电视端卡顿事件不再增长。",
                        "THERMAL_THROTTLING": "复测时重点看温度是否下降、CPU 频率是否恢复，且高温阶段不再伴随电视端卡顿或整机 CPU 异常。",
                    }.get(cause_type, "复测时请同时核对电视端卡顿事件、关键日志、整机 CPU 和嫌疑进程变化，确认问题已收敛。")
                    f.write(f"0. Summary: {diagnosis.get('title', '')}\n")
                    f.write(f"   Diagnosis: {diagnosis.get('conclusion', '')}\n")
                    f.write(
                        f"   Evidence Level: {diagnosis.get('evidence_level', 'unknown')} | "
                        f"Owner: {diagnosis.get('owner', '待确认')}\n"
                    )
                    evidence_strength = diagnosis.get("evidence_strength", {}) or {}
                    evidence_strength_label = str(
                        evidence_strength.get("label", "") or diagnosis.get("evidence_level", "unknown")
                    )
                    evidence_strength_desc = str(evidence_strength.get("description", "") or "")
                    f.write(
                        f"   Evidence Strength: {evidence_strength_label}"
                        + (f" | {evidence_strength_desc}" if evidence_strength_desc else "")
                        + "\n"
                    )
                    f.write(f"   Priority Target: {diagnosis.get('suspect_process', 'N/A')}\n")
                    actions = diagnosis.get("actions") or []
                    if actions:
                        f.write("   Dev Actions:\n")
                        for action in actions[:3]:
                            f.write(f"   - {action}\n")
                    f.write(f"   Validation: {validation_hint}\n")
                f.write(f"1. 寮傚父瑙﹀彂蹇収: {root_cause_analysis.get('total_stutter_events', 0)} 娆n")
                f.write(f"2. 鏍瑰洜/椋庨櫓鍊欓€? {root_cause_analysis.get('identified_causes', 0)} 娆n")
                f.write(
                    f"   宸茬‘璁ゆ挱鏀鹃€€鍖? {root_cause_analysis.get('confirmed_playback_causes', 0)} 娆?| "
                    f"璧勬簮椋庨櫓: {root_cause_analysis.get('resource_risk_events', 0)} 娆?| "
                    f"鏃ュ織淇″彿: {root_cause_analysis.get('log_signal_events', 0)} 娆n"
                )
                top_cause = (root_cause_analysis.get('most_confident_cause') or {}) if isinstance(root_cause_analysis, dict) else {}
                if top_cause:
                    evidence = top_cause.get("evidence", {}) or {}
                    if evidence.get("signal_only"):
                        label = "棣栬鏃ュ織淇″彿"
                    elif evidence.get("resource_only"):
                        label = "首要资源风险候选"
                    else:
                        label = "首要风险候选"
                    f.write(f"3. {label}: {top_cause.get('root_cause_type', '')} | {top_cause.get('suspect_process', '')} (椋庨櫓璇勫垎: {top_cause.get('confidence', 0)})\n")
                process_risks = root_cause_analysis.get(
                    "process_risk_summary",
                    [],
                ) or []
                top_suspects = root_cause_analysis.get(
                    "top_suspect_processes",
                    [],
                ) or []
                if top_suspects:
                    f.write("4. 鏍瑰洜瀚岀枒瀵硅薄 Top:\n")
                    for index, item in enumerate(top_suspects[:3], 1):
                        if not isinstance(item, (list, tuple)) or len(item) < 2:
                            continue
                        f.write(
                            f"   {index}) {item[0]} | 鍛戒腑 {item[1]} 娆℃牴鍥犲€欓€塡n"
                        )
                if process_risks:
                    f.write("5. 閲嶅/楂樿礋杞借繘绋嬭仛鍚堟:\n")
                    for index, item in enumerate(process_risks[:8], 1):
                        f.write(
                            f"   {index}) {item.get('process', '')} | "
                            f"鏈€澶?{item.get('max_instance_count', 1)} 涓疄渚?| "
                            f"鍛戒腑 {item.get('event_count', 0)} 涓噰鏍?| "
                            f"鍚堣CPU鍧囧€?宄板€?"
                            f"{item.get('avg_cpu_percent', 0)}%/"
                            f"{item.get('peak_cpu_percent', 0)}% | "
                            f"鏁存満宄板€?{item.get('max_system_cpu_percent', 0)}%\n"
                        )
                decoder_stuck_summary = summary.get("decoder_stuck_summary") or {}
                if int(decoder_stuck_summary.get("count", 0) or 0) > 0:
                    f.write("6. 瑙ｇ爜杈撳嚭鍋滈】璇︽儏:\n")
                    decoder_name = str(decoder_stuck_summary.get("decoder_name", "") or "")
                    decoder_names = decoder_stuck_summary.get("decoder_names") or []
                    if decoder_name:
                        f.write(f"   - 娑夊強 Decoder: {decoder_name}\n")
                    elif decoder_names:
                        f.write(f"   - 娑夊強 Decoder: {', '.join(str(name) for name in decoder_names[:3])}\n")
                    f.write(
                        f"   - 鍒ゆ柇鍚箟: 瑙ｇ爜鍣ㄤ粛鏈夋椿璺冨疄渚嬶紝浣嗚繛缁? >=1s 鏈骇鍑烘柊甯с€?`MPP work_count` 鏈闀裤€俓n"
                    )
                    f.write(
                        f"   - 鏈€闀垮崟娆″仠椤?: {float(decoder_stuck_summary.get('max_duration_sec', 0) or 0.0):.2f}s | "
                        f"浠ｈ〃鏍锋湰: {decoder_stuck_summary.get('sample_timestamp', 'N/A')}\n"
                    )
                    f.write(
                        f"   - 褰撴椂鎸囨爣: video_fps {float(decoder_stuck_summary.get('video_fps', 0) or 0.0):.1f} | "
                        f"decode_fps {float(decoder_stuck_summary.get('decode_fps_estimate', 0) or 0.0):.1f} | "
                        f"expected_fps {float(decoder_stuck_summary.get('expected_stream_fps', 0) or 0.0):.1f} | "
                        f"decode_drop {float(decoder_stuck_summary.get('decode_drop_ratio', 0) or 0.0) * 100:.1f}%\n"
                    )
                    f.write(
                        f"   - 褰撴椂 CPU: 鎾斁鍣?{float(decoder_stuck_summary.get('player_cpu_percent', 0) or 0.0):.1f}% | "
                        f"鏁存満 {float(decoder_stuck_summary.get('system_cpu_percent', 0) or 0.0):.1f}%\n"
                    )
                    diagnostic_lines = decoder_stuck_summary.get("diagnostic_lines") or []
                    if diagnostic_lines:
                        f.write("   - Decoder Diagnostics:\n")
                        for line in diagnostic_lines[:3]:
                            f.write(f"     * {line}\n")
                    log_lines = decoder_stuck_summary.get("log_lines") or []
                    if log_lines:
                        f.write("   - 鍏宠仈 Decoder 鏃ュ織:\n")
                        for line in log_lines[:3]:
                            f.write(f"     * {line}\n")
            else:
                f.write("1. 鏍瑰洜鍒嗘瀽鏁版嵁涓嶈冻\n")
            f.write("-" * 30 + "\n")
            
            f.write("銆愭祦鐣呭害鍒嗘瀽 (V2.3 - 闆跺共鎵扮洃鎺х増)銆慭n")
            surface_locked = bool(summary.get("tv_surface_locked", False))
            display_state = (
                "Surface已锁定"
                if surface_locked
                else (
                    "仅Display已识别"
                    if summary.get("tv_display_verified", False)
                    else "未验证"
                )
            )
            f.write(
                f"鐢佃绔?Display: {summary.get('tv_display_id', 'N/A')} | "
                f"楠岃瘉鐘舵€? {display_state} "
                f"({summary.get('tv_display_verification_reason', 'unknown')})\n"
            )
            
            # 闆跺共鎵版ā寮忚鏄?
            monitor_config = self.config.get('monitor', {})
            enable_screenshot = monitor_config.get('enable_screenshot', True)
            enable_fps = monitor_config.get('enable_fps', True)
            if not enable_screenshot and not enable_fps:
                f.write("\n鈿狅笍 鏈娴嬭瘯浣跨敤銆愭瀬浣庡姛鑰楁ā寮忋€戯紝浠呯洃鎺K纭欢鑺傜偣锛岀墿鐞嗕笂涓庤棰戞覆鏌撹矾寰勫畬鍏ㄩ殧绂汇€俓n")
                f.write("   姝ゆā寮忎笅妫€娴嬪埌鐨勫崱椤?00%鎺掗櫎宸ュ叿骞叉壈锛屾渶鍏疯鏈嶅姏銆俓n")
            
            # 1. 鐪熷疄瑙嗛甯х巼锛堟渶閲嶈鎸囨爣锛?
            video_fps = summary.get('avg_video_fps', 0)
            video_fps_samples = summary.get('video_fps_samples', 0)
            max_video_fps = summary.get('max_video_fps', 0)
            min_video_fps = summary.get('min_video_fps', 0)
            fps_unavailable_reason = summary.get('video_fps_unavailable_reason', '')
            
            fps_sources = summary.get("video_fps_source_counts", {}) or {}
            direct_fps = any(
                str(source).startswith("surfaceflinger")
                for source in fps_sources
            )
            fps_label = (
                "鐢佃鐢婚潰鐩存帴甯х巼"
                if direct_fps and surface_locked
                else "瑙ｇ爜鍚炲悙浼扮畻甯х巼"
            )
            f.write(f"1. 瑙嗛甯х巼 ({fps_label}):\n")
            f.write(f"   鏁版嵁鏉ユ簮鍒嗗竷: {fps_sources or {'none': 0}}\n")
            if video_fps_samples > 0:
                f.write(f"   骞冲潎甯х巼: {video_fps} FPS\n")
                if max_video_fps > 0 and min_video_fps > 0:
                    f.write(f"   甯х巼鑼冨洿: {min_video_fps:.1f} - {max_video_fps:.1f} FPS\n")
                if video_fps_samples > 0:
                    f.write(f"   鏈夋晥鏍锋湰: {video_fps_samples} 涓猏n")
                
                if video_fps < 20:
                    f.write("   >> [涓ラ噸璀﹀憡] 瑙嗛甯х巼涓ラ噸鍋忎綆锛屽瓨鍦ㄦ槑鏄惧崱椤匡紒\n")
                    f.write("   >> 瑙嗛鎾斁涓嶆祦鐣咃紝鐢ㄦ埛浣撻獙宸€俓n")
                elif video_fps < 24:
                    f.write("   >> [璀﹀憡] 瑙嗛甯х巼鍋忎綆锛屽瓨鍦ㄨ交寰崱椤块闄┿€俓n")
                elif video_fps < 27:
                    f.write("   >> [娉ㄦ剰] 瑙嗛甯х巼鐣ヤ綆浜庢爣鍑嗭紙24/25/30fps锛夛紝鍙兘瀛樺湪寰崱椤裤€俓n")
                else:
                    if direct_fps and surface_locked:
                        f.write("   >> [鑹ソ] 鐢佃鐢婚潰鐩存帴甯х巼姝ｅ父銆俓n")
                    else:
                        f.write("   >> [鍙傝€僝 瑙ｇ爜鍚炲悙鏁翠綋姝ｅ父锛屼絾涓嶈兘鏇夸唬鐢佃鐢婚潰鐩存帴鍗￠】璇佹嵁銆俓n")
            else:
                f.write("   (鏈兘鑾峰彇鍒拌棰戝抚鐜囨暟鎹?\n")
                if fps_unavailable_reason:
                     f.write(f"   >> [鍘熷洜]: {fps_unavailable_reason}\n")
                if not enable_fps:
                     f.write("   >> [鍘熷洜]: 褰撳墠澶勪簬'鏋佷綆鍔熻€?闆跺共鎵?妯″紡锛岃妯″紡宸蹭富鍔ㄧ鐢‵PS閲囬泦浠ョ‘淇濋浂骞叉壈銆俓n")
                     f.write("   >> [寤鸿]: 濡傛灉鎮ㄩ渶瑕佸垎鏋愬井鍗￠】/鎺夊抚锛岃閲嶆柊寮€濮嬫祴璇曞苟閫夋嫨'鏍囧噯妯″紡'銆俓n")
                else:
                     f.write("   >> [鎻愮ず] 鍙兘鍘熷洜锛歕n")
                     f.write("      - 搴旂敤鏈惎鍔ㄦ垨杩涚▼涓嶅瓨鍦╘n")
                     f.write("      - gfxinfo 鏁版嵁涓嶅彲鐢紙闇€瑕佸簲鐢ㄨ繍琛屼竴娈垫椂闂达級\n")
                     f.write("      - 璁惧涓嶆敮鎸?gfxinfo 缁熻\n")
                     f.write("   寤鸿锛氳鍙傝€冧笅鏂规棩蹇楀崱椤垮垎鏋愪綔涓烘浛浠ｆ寚鏍嘰n")

            # 2. Log Stutter
            f.write(f"2. 鍗￠】鐩稿叧鏃ュ織淇″彿 (Log Signal - 杈呭姪鎸囨爣):\n")
            f.write(f"   绱 {summary.get('final_log_stutter_count', 0)} 娆n")
            f.write("   (妫€娴嬪叧閿瓧: droppedFrames, underrun, buffer starvation)\n")
            f.write("   (璇存槑: 鏃ュ織鍛戒腑涓嶇瓑浜庝竴娆¤倝鐪煎彲瑙佸崱椤匡紝闇€缁撳悎Surface甯у仠婊炪€佽В鐮佷笅闄嶆垨CPU璇佹嵁纭)\n")

            # 3. UI Jank (Reference Only - 鐐规瓕灞?
            f.write(f"3. 鐐规瓕灞忕晫闈氦浜掓祦鐣呭害 (UI Jank - 浠呬緵鍙傝€?:\n")
            total_gfx_jank = int(summary.get("total_gfx_jank", 0) or 0)
            total_frames_delta = int(summary.get("total_frames_delta", 0) or 0)
            avg_jank_percent = float(summary.get("avg_jank_percent", 0) or 0.0)
            f.write(
                f"   鍘熷鏁版嵁 {total_gfx_jank} / {total_frames_delta} 甯?"
                f"(姣斾緥: {avg_jank_percent}%)\n"
            )
            if (
                total_frames_delta > 0
                and avg_jank_percent >= 99.0
                and video_fps >= 24
                and summary.get("final_log_stutter_count", 0) == 0
            ):
                f.write(
                    "   >> [已忽略] gfxinfo 疑似累计口径异常，电视端 FPS 正常且无卡顿日志，不作为电视端卡顿证据。\n"
                    "鐢佃FPS姝ｅ父涓旀棤鍗￠】鏃ュ織锛屼笉浣滀负鐢佃绔崱椤胯瘉鎹€俓n"
                )
            else:
                f.write(
                    "   (说明: 此指标不参与电视端流畅度评分，仅用于点歌屏交互问题排查)\n"
                    "浠呯敤浜庣偣姝屽睆浜や簰闂鎺掓煡)\n"
                )

            # 4. Decode Drop (MPP Estimate - TV)
            f.write("4. 鐢佃绔В鐮佷涪甯?(MPP浼扮畻 - 鍏抽敭鎸囨爣):\n")
            decode_drop_total = summary.get('decode_drop_estimate_total', 0)
            decode_expected = summary.get('decode_expected_frames_estimate', 0)
            decode_ratio = float(summary.get('decode_drop_ratio', 0) or 0.0)
            f.write(f"   浼扮畻涓㈠抚 {decode_drop_total} / {decode_expected} (涓㈠抚鐜? {(decode_ratio * 100):.2f}%)\n")
            
            # V2.3.1: 闆跺共鎵版ā寮忚鏄?
            monitor_config = self.config.get('monitor', {})
            enable_screenshot = monitor_config.get('enable_screenshot', True)
            enable_fps = monitor_config.get('enable_fps', True)
            
            if not enable_screenshot and not enable_fps:
                f.write("\n   >> [闆跺共鎵版ā寮廬: 鏈娴嬭瘯浣跨敤'鏋佷綆鍔熻€?妯″紡锛屼粎鐩戞帶RK纭欢鑺傜偣銆俓n")
                f.write("      >> 姝ゆā寮忎笅妫€娴嬪埌鐨勫崱椤?00%鎺掗櫎宸ュ叿骞叉壈锛岀墿鐞嗕笂涓庤棰戞覆鏌撹矾寰勫畬鍏ㄩ殧绂汇€俓n")
                f.write("      >> 宸ヤ綔鍘熺悊: 浠呰鍙?/sys/kernel/debug/mpp_service/stats 鑺傜偣锛岄€氳繃 work_count 澧為噺鍒ゆ柇瑙ｇ爜鍣ㄧ姸鎬併€俓n")
                f.write("      >> 楠岃瘉鏂规硶: 濡傛灉鍦ㄦ妯″紡涓嬫娴嬪埌 decoder_stuck锛?00%鏄挱鏀惧櫒鎴栬В鐮佸櫒闂锛屼笌宸ュ叿鏃犲叧銆俓n")
            
            # V2.3: 鏅鸿兘鍒嗘瀽缁撹锛堜紭鍖栵細鍖哄垎鐐规瓕灞忓拰鐢佃灞忥級
            # 缁撳悎浜虹溂鎰熺煡璇勫垎缁撴灉杩涜鏇存繁鍏ョ殑鍒嗘瀽
            avg_jank = summary.get('avg_jank_percent', 0)
            log_stutter = summary.get('final_log_stutter_count', 0)
            decoder_stuck_count = summary.get('decoder_stuck_count', 0)
            confirmed_decoder_stuck_count = summary.get('confirmed_decoder_stuck_count', 0)
            decoder_stuck_risk_count = summary.get('decoder_stuck_risk_count', 0)
            tv_freeze_count = summary.get('tv_freeze_count', 0)
            
            # 浣跨敤鎰熺煡璇勫垎璇︽儏
            perceptual_details = perceptual_result.get('details', [])
            perceptual_score = perceptual_result.get('score', 0)
            
            fps_severe = video_fps > 0 and video_fps < 20
            
            if confirmed_decoder_stuck_count > 0 or tv_freeze_count > 0:
                f.write("\n   >> [严重警告]: 检测到电视端视频播放卡顿 (TV Video Stutter)。\n")
                if confirmed_decoder_stuck_count > 0:
                    f.write(f"      - 解码输出停顿: {confirmed_decoder_stuck_count} 次（硬件解码器短时未输出新帧）\n")
                    decoder_stuck_summary = summary.get("decoder_stuck_summary") or {}
                    decoder_name = str(decoder_stuck_summary.get("decoder_name", "") or "")
                    if decoder_name:
                        f.write(f"      - 主要涉及 Decoder: {decoder_name}\n")
                    if decoder_stuck_summary.get("log_lines"):
                        f.write("      - 建议直接查看上方“解码输出停顿详情”中的 decoder 日志，定位是哪一路解码器停顿。\n")
                    if not enable_screenshot and not enable_fps:
                        f.write("      >> [零干扰验证] 在极低功耗模式下检测到此问题，可排除工具干扰。\n")
                f.write("      >> 请查看 CSV 报告中的 Top_Consumers 列，如果监控工具不在前列，说明不是工具导致的。\n")
                if tv_freeze_count > 0:
                    f.write(f"      - 画面冻结: {tv_freeze_count} 次（电视端画面静止超过阈值）\n")
                f.write("      >> 这是最严重的卡顿类型，直接影响用户体验。\n")
            
            elif decoder_stuck_risk_count > 0:
                f.write("\n   >> [风险提示]: 检测到解码输出停顿风险，但当前缺少电视端 Surface/帧时间直证。\n")
                f.write(f"      - 风险样本: {decoder_stuck_risk_count} 次（当前仅作为风险，不直接判定为电视端卡顿）\n")
                f.write("      - 建议结合 Surface 锁定结果、Display 1 帧间隔和异常时段 Top 进程再确认。\n")
             
            elif perceptual_score >= 50: # 鏄庢樉鍗￠】
                f.write(f"\n   >> [鏅鸿兘鍒嗘瀽]: 妫€娴嬪埌\"鏄庢樉瑙嗛鍗￠】\" (Perceptual Score: {perceptual_score})銆俓n")
                f.write("      涓昏闂:\n")
                for det in perceptual_details:
                     f.write(f"      - {det}\n")
                if fps_severe:
                     f.write("      - 瑙嗛甯х巼涓ラ噸鍋忎綆\n")
            
            elif perceptual_score >= 30: # 杞诲井鍗￠】
                f.write(f"\n   >> [智能分析]: 检测到轻微视频卡顿 (Perceptual Score: {perceptual_score})。\n")
                 # 鍒楀嚭鍏蜂綋鍘熷洜
                for det in perceptual_details:
                     f.write(f"      - {det}\n")

            elif perceptual_score >= 15: # 寰崱椤?
                 f.write(f"\n   >> [智能分析]: 检测到微卡顿迹象 (Perceptual Score: {perceptual_score})。\n")
                 for det in perceptual_details:
                     f.write(f"      - {det}\n")

            elif video_fps == 0 and log_stutter == 0:
                f.write("\n   >> [智能分析]: 数据不足 (Insufficient Data)。\n")
                f.write("      >> 鏈幏鍙栧埌鏈夋晥瑙嗛甯х巼锛屼笖鏃ュ織涓棤鍗￠】璁板綍銆俓n")
                f.write("      >> 鏃犳硶纭鏄惁娴佺晠锛岃妫€鏌ワ細\n")
                f.write("         1. 瑙嗛鏄惁鐪熸寮€濮嬫挱鏀撅紵\n")
                f.write("         2. 娴嬭瘯鏃堕暱鏄惁澶煭锛?寤鸿 > 1鍒嗛挓)\n")
                f.write("         3. adb shell dumpsys gfxinfo 鏄惁鏈夋潈闄?鏁版嵁锛焅n")
            else:
                f.write("\n   >> [鏅鸿兘鍒嗘瀽]: 鎾斁娴佺晠搴︽暣浣撹壇濂姐€俓n")

            tv_stall_events = summary.get("tv_stall_events", [])
            f.write("\n5. Display 1 楂橀鍗￠】浜嬩欢:\n")
            if tv_stall_events:
                f.write(f"   鍏辨娴嬪埌 {len(tv_stall_events)} 娆″畬鏁村崱椤夸簨浠禱n")
                for event in tv_stall_events[-10:]:
                    contention = event.get("cpu_contention") or {}
                    candidate = contention.get("top_candidate") or {}
                    cpu_detail = ""
                    if candidate:
                        cpu_detail = (
                            f" | CPU 嫌疑进程 {candidate.get('process', '')} "
                            f"{candidate.get('baseline_cpu_percent', 0)}% -> "
                            f"{candidate.get('peak_cpu_percent', 0)}% -> "
                            f"{candidate.get('after_cpu_percent', 0)}% "
                            f"(置信度 {candidate.get('confidence', 0)}%)"
                        )
                    f.write(
                        "   - "
                        f"{event.get('start_time', '')} | "
                        f"鎸佺画 {event.get('duration_ms', 0)}ms | "
                        f"鏈€澶у抚闂撮殧 {event.get('max_frame_gap_ms', 0)}ms | "
                        f"璇佹嵁鐩綍 {event.get('evidence_dir', '')}"
                        f"{cpu_detail}\n"
                    )
            else:
                f.write("   鏈娴嬪埌宸插畬鎴愮殑 Display 1 鍗￠】浜嬩欢\n")

            f.write("-" * 30 + "\n")
            f.write("銆愰暱鏃堕棿杩愯閫€鍖栧垎鏋愩€慭n")
            if degradation.get("status") == "insufficient_data":
                f.write("鏁版嵁鏍锋湰涓嶈冻锛屾棤娉曡繘琛岄€€鍖栧垎鏋?(闇€杩愯鏇撮暱鏃堕棿)\n")
            else:
                growth_rate = degradation.get('mem_growth_rate_mb_per_hour', 0)
                f.write(f"1. 鍐呭瓨澧為暱閫熺巼: {growth_rate} MB/灏忔椂\n")
                f.write(f"   (棣栨鍧囧€? {degradation.get('first_avg_pss', 0)} MB -> 鏈鍧囧€? {degradation.get('last_avg_pss', 0)} MB)\n")
                
                cpu_change = degradation.get('cpu_change_percent', 0)
                f.write(f"2. CPU 璐熻浇鍙樺寲: {cpu_change}%\n")
                f.write(f"   (棣栨鍧囧€? {degradation.get('first_avg_cpu', 0)}% -> 鏈鍧囧€? {degradation.get('last_avg_cpu', 0)}%)\n")
                
                # 绠€鍗曠粨璁?
                if growth_rate > 10:
                    f.write(">> 璀﹀憡: 瀛樺湪鏄庢樉鐨勫唴瀛樺闀胯秼鍔?(鐤戜技娉勬紡)\n")
                elif growth_rate < -5:
                    f.write(">> 娉ㄦ剰: 鍐呭瓨鍗犵敤鍛堜笅闄嶈秼鍔縗n")
                else:
                    f.write(">> 缁撹: 鍐呭瓨鍗犵敤鐩稿骞崇ǔ\n")
            
            f.write("-" * 30 + "\n")
            
            # V2 鏈€缁堝垽瀹?& 璇勫垎 (宸茶縼绉昏嚦椤堕儴)
            f.write("\n銆愬師濮嬫暟鎹€慭n")
            f.write(f"CSV 璇︾粏鎶ュ憡: {os.path.basename(self.last_csv_file)}\n")
            
        # 鐢熸垚 JSON 鎶ュ憡 (V2 Schema)
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
            self.log(f"鎽樿 JSON 鐢熸垚澶辫触: {e}")
            
        # 鐢熸垚 HTML 鎶ュ憡 (V2.1 鏂板)
        # 闇€瑕佸悎骞朵竴浜涘瓧娈垫柟渚挎覆鏌?
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
        html_summary["process_failure_summary"] = summary.get("process_failure_summary", {})
        html_summary["process_failure_actions"] = summary.get("process_failure_actions", [])
        html_summary["tv_process_correlation_summary"] = summary.get("tv_process_correlation_summary", {})
        html_summary["responsibility_summary"] = summary.get("responsibility_summary", {})
        
        # 璁＄畻鎴愬姛鐜?
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
            self.log(f"HTML 鎶ュ憡鐢熸垚澶辫触: {e}")

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
            "鎶ュ憡宸茬敓鎴? "
            f"\nTXT: {self.last_summary_file or 'N/A'}"
            f"\nJSON: {self.last_summary_json_file or 'N/A'}"
            f"\nHTML: {self.last_html_file or 'N/A'}"
        )

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

        if score_result.get("ready_to_release"):
            release_status = "建议上线"
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

        lines = [
            "=== Android 播放器压测报告 (V2标准) ===",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"测试包名: {self.package_name}",
            f"测试设备: {device_id if device_id else 'N/A'}",
            f"机顶盒 IP: {self.device_ip or '未获取'}",
            f"固件版本: {self.firmware_incremental or '未获取'}",
            "-" * 30,
            "【测试结论 (Decision)】",
            self.monitor.evaluator.get_one_sentence_summary(
                duration_str,
                self.song_count,
                score_result,
                root_cause_info=root_cause_analysis,
            ),
        ]
        if executive_statement:
            lines.append(f"总结性判断: {executive_statement}")
        if duration_sec < 3600:
            lines.append("[覆盖度提示] 本轮不足1小时，可验证基础流畅度；内存积累、后台CPU竞争和热降频建议至少运行1小时。")
        lines.extend([
            "-" * 30,
            f"稳定性评分: {score_result.get('score', 0)} / 100 (等级: {score_result.get('grade', 'N/A')})",
            f"电视端卡顿风险分: {perceptual_result.get('score', 0)} / 100 (等级: {perceptual_result.get('level', 'unknown')})",
            f"感知建议: {perceptual_result.get('recommendation', '无')}",
            f"准入结论: {release_status}",
        ])
        deductions = list(score_result.get("deductions", []) or [])
        if deductions:
            lines.append("扣分/风险项:")
            lines.extend([f"  - {item}" for item in deductions])
        blockers = list(score_result.get("release_blockers", []) or [])
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
            "-" * 30,
            "【错误汇总 (Errors)】",
            f"1. 崩溃 (Crash/Exception): {error_stats.get('crash_count', 0)} 次",
            f"2. 无响应 (ANR): {error_stats.get('anr_count', 0)} 次",
            f"3. 进程异常重启: {summary.get('restart_count', 0)} 次",
            f"4. 目标进程丢失: {summary.get('pid_loss_count', 0)} 次",
        ])
        lines.extend(self._render_responsibility_section(summary))
        lines.extend(self._render_process_failure_section(summary))
        lines.extend([
            "-" * 30,
            "【核心稳定性指标】",
            f"1. 峰值内存(PSS): {summary.get('max_pss_mb', 0)} MB",
            f"2. 平均内存(PSS): {summary.get('avg_pss_mb', 0)} MB",
            f"3. 播放器平均CPU / 整机平均CPU / 整机峰值CPU: {summary.get('avg_player_cpu_percent', 0)}% / {summary.get('avg_system_cpu_percent', 0)}% / {summary.get('max_system_cpu_percent', 0)}%",
            f"4. 电视端卡顿 / 冻结: {summary.get('tv_stall_count', 0)} / {summary.get('tv_freeze_count', 0)}",
            f"5. 解码停顿总样本 / 确认样本 / 风险样本: {summary.get('decoder_stuck_count', 0)} / {summary.get('confirmed_decoder_stuck_count', 0)} / {summary.get('decoder_stuck_risk_count', 0)}",
        ])
        lines.extend(self._render_tv_process_correlation_section(summary))
        lines.extend([
            "-" * 30,
            "【根因分析 (V3.0)】",
            f"1. 总结: {diagnosis.get('title', '暂无明确结论')}",
            f"2. 结论: {diagnosis.get('conclusion', '暂无明确结论')}",
            f"3. 证据等级 / 责任方向 / 优先对象: {diagnosis.get('evidence_level', 'unknown')} / {diagnosis.get('owner', '待确认')} / {diagnosis.get('suspect_process', '无')}",
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
        lines.append(
            f"1. 先查谁: {dev_priority.get('target', '无')} | Responsibility: {dev_priority.get('owner', '待确认')} | Evidence: {dev_priority.get('strength', evidence_strength_label)}"
        )
        lines.append("2. 看什么日志:")
        lines.extend([f"   - {item}" for item in (dev_priority.get('logs') or [])])
        lines.append(f"3. 怎么复测: {dev_priority.get('retest', 'N/A')}")
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
        lines.extend([
            "-" * 30,
            "【电视端流畅度证据】",
            f"1. Display: {summary.get('tv_display_id', 'N/A')} | 验证状态: {'已验证' if summary.get('tv_display_verified') else '未验证'} ({summary.get('tv_display_verification_reason', 'unknown')})",
            f"2. Surface 锁定: {'已锁定' if summary.get('tv_surface_locked') else '未锁定'}",
            f"3. 视频 FPS: {summary.get('avg_video_fps', 0)} | 样本 {summary.get('video_fps_samples', 0)} | 来源 {summary.get('video_fps_source_counts', {})}",
            f"4. 解码估算丢帧: {summary.get('decode_drop_estimate_total', 0)} / {summary.get('decode_expected_frames_estimate', 0)} (比例 {float(summary.get('decode_drop_ratio', 0) or 0) * 100:.2f}%)",
            f"5. 停顿样本详情: 最大持续 {decoder_summary.get('max_duration_sec', 0)}s | Decoder {decoder_summary.get('decoder_name') or '未识别'} | 样本时间 {decoder_summary.get('sample_timestamp', 'N/A')}",
        ])
        display_recommendation = summary.get("tv_display_recommendation", {}) or {}
        if display_recommendation.get("display_id") is not None:
            lines.append(
                f"6. Display 推荐: Display {display_recommendation.get('display_id')} | "
                f"依据 {display_recommendation.get('reason', 'unknown')} | "
                f"评分 {display_recommendation.get('score', 0)}"
            )
        latency_probe = summary.get("tv_latency_probe", {}) or {}
        if not summary.get("tv_surface_locked") or float(summary.get("avg_video_fps", 0) or 0.0) <= 0:
            lines.append("7. FPS 采集诊断:")
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
        lines.extend([
            "-" * 30,
            "【原始数据】",
            f"CSV 详细报告: {os.path.basename(self.last_csv_file)}",
            f"HTML 报告: {os.path.basename(self.last_html_file) if self.last_html_file else 'N/A'}",
            f"JSON 摘要: {os.path.basename(self.last_summary_json_file) if self.last_summary_json_file else 'N/A'}",
        ])
        return "\n".join(lines) + "\n"

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

        cause_type = str(most_confident.get("root_cause_type", "") or "")
        owner = str(diagnosis.get("owner", "") or "待确认")
        suspect_process = str(diagnosis.get("suspect_process", "") or most_confident.get("suspect_process", "") or "无")
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
        display_recommendation = summary.get("tv_display_recommendation", {}) or {}
        recommended_display = display_recommendation.get("display_id")
        recommended_reason = str(display_recommendation.get("reason", "") or "")

        category = "暂不能定责"
        confidence = "low"
        conclusion = "当前证据还不足以直接指定播放器侧、系统侧或解码侧作为唯一责任方。"
        key_basis = "证据仍在收集中"

        if total_stall_count <= 0:
            category = "未检测到电视端卡顿"
            confidence = "none"
            conclusion = "本轮没有检测到电视端卡顿事件，因此无需进行责任归类。"
            key_basis = "未检测到电视端卡顿事件"
            if risk_stall_count > 0 or decoder_stuck_risk > 0:
                category = "存在风险信号，未确认肉眼卡顿"
                confidence = "medium" if surface_locked else "low"
                conclusion = (
                    f"本轮尚未形成确认级电视端卡顿证据，但已捕获风险样本："
                    f"电视端风险事件 {risk_stall_count} 次，解码风险样本 {decoder_stuck_risk} 次。"
                    "建议继续补足 Surface/FPS/日志互证后再定责。"
                )
                key_basis = (
                    f"风险事件 {risk_stall_count} 次 | 解码风险 {decoder_stuck_risk} 次 | "
                    f"Surface {'已锁定' if surface_locked else '未锁定'}"
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
            category = "系统/固件 CPU 竞争主导"
            confidence = "high" if avg_system_cpu >= 90 else "medium"
            conclusion = (
                f"卡顿阶段整机 CPU 持续偏高（平均 {avg_system_cpu:.1f}% / 峰值 {peak_system_cpu:.1f}%），"
                f"当前更像是 {suspect_process} 引发的系统级资源竞争，而不是播放器单进程本身算力不足。"
            )
            key_basis = (
                f"整机 CPU {avg_system_cpu:.1f}%/{peak_system_cpu:.1f}% + 嫌疑进程 {suspect_process}"
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
            {"label": "优先对象", "value": suspect_process},
            {"label": "卡顿/异常时间重合", "value": f"{matched_stall_count}/{total_stall_count} ({correlated_ratio * 100:.1f}%)"},
            {"label": "播放器异常分布", "value": f"Crash {int(process_failure_summary.get('crash_count', 0) or 0)} / ANR {int(process_failure_summary.get('anr_count', 0) or 0)} / PID重启 {int(process_failure_summary.get('restart_count', 0) or 0)} / 进程丢失 {int(process_failure_summary.get('pid_loss_count', 0) or 0)}"},
            {"label": "整机/播放器 CPU", "value": f"{avg_system_cpu:.1f}% / {avg_player_cpu:.1f}%"},
            {"label": "Surface/FPS/解码丢帧", "value": f"{'已锁定' if surface_locked else '未锁定'} / {avg_video_fps:.1f} FPS / {decode_drop_ratio * 100:.1f}%"},
            {"label": "解码停顿确认样本", "value": str(confirmed_decoder_stuck)},
            {"label": "Display 推荐", "value": f"{recommended_display if recommended_display is not None else '无'} ({recommended_reason or 'unknown'})"},
        ]

        return {
            "category": category,
            "confidence": confidence,
            "owner": owner,
            "suspect_process": suspect_process,
            "conclusion": conclusion,
            "key_basis": key_basis,
            "evidence_items": evidence_items,
        }

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
            }

        if cause_type == "CPU_CONTENTION":
            logs = [
                f"Top 进程和重复实例，重点看 {target}",
                "卡顿时整机 CPU / cpuinfo / process count 快照",
                "电视端卡顿时间线与 CPU 抢占是否同步",
            ]
            retest = "复测时确认 tv_stall_count 是否下降，整机 CPU 是否明显回落，嫌疑进程实例数是否恢复正常。"
        elif cause_type == "DECODER_STUCK":
            decoder_name = str(decoder_summary.get("decoder_name", "") or target or "MPP Hardware Decoder")
            logs = [
                f"Decoder / MPP 日志，重点看 {decoder_name}",
                "MediaCodec / dequeue output timeout / codec error 相关日志",
                "代表样本的 video_fps / decode_fps / decode_drop 变化",
            ]
            target = decoder_name
            retest = "复测时确认解码输出恢复连续，video_fps 与 decode_fps_estimate 同步恢复，decoder 错误日志消失。"
        elif cause_type == "LOW_FPS_DEGRADATION":
            logs = [
                "SurfaceFlinger / composer service 日志",
                "Display 1 的 FPS、max_frame_gap_ms 和 latency 输出",
                "GPU / 合成负载是否在同一时刻恶化",
            ]
            retest = "复测时确认 Display 1 FPS 稳定、surface 持续锁定、max_frame_gap_ms 明显回落。"
        else:
            logs = [
                "Crash / ANR / codec / underrun 前后 30 秒日志",
                "电视端 Surface / FPS / Top 进程同时间证据",
                f"责任方向 {owner} 对应模块日志",
            ]
            retest = "复测时确认电视端卡顿是否消失，责任方向是否保持稳定，证据强度是否维持或提升。"

        return {
            "target": target,
            "owner": owner,
            "strength": strength,
            "logs": logs[:3],
            "retest": retest,
            "cause_type": cause_type or "UNKNOWN",
        }

    def _render_responsibility_section(self, summary: Dict) -> list:
        responsibility = summary.get("responsibility_summary", {}) or {}
        lines = ["-" * 30, "【责任判定】"]
        lines.extend([
            f"1. 责任分类: {responsibility.get('category', '暂不能定责')}",
            f"2. 置信度: {responsibility.get('confidence', 'low')}",
            f"3. 责任方向 / 优先对象: {responsibility.get('owner', '待确认')} / {responsibility.get('suspect_process', '无')}",
            f"4. 判定结论: {responsibility.get('conclusion', '暂无明确结论')}",
            f"5. 关键依据: {responsibility.get('key_basis', '暂无')}",
        ])
        evidence_items = list(responsibility.get("evidence_items", []) or [])
        if evidence_items:
            lines.append("6. 关键证据摘要:")
            for item in evidence_items:
                lines.append(f"   - {item.get('label', '证据')}: {item.get('value', '')}")
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
        lines = ["-" * 30, "【卡顿与播放器异常关联分析】"]
        lines.extend([
            f"1. 关联窗口: 前后 {correlation.get('window_seconds', 30)} 秒",
            f"2. 电视端卡顿总数: {correlation.get('total_tv_stall_count', 0)} 次",
            f"3. 播放器异常总数: {correlation.get('total_failure_event_count', 0)} 次",
            f"4. 时间重合卡顿数: {correlation.get('matched_tv_stall_count', 0)} 次",
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
