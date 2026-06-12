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
        
        log("🔧 正在初始化测试组件...")
        self.config = config
        self.running = False
        self.stop_flag = False
        self._stop_event = threading.Event()
        log("  - 初始化 ADB 管理器...")
        self.adb = AdbManager(device_id=config.get('device_id'))
        self.adb.set_cancel_event(self._stop_event)
        self.device_ip = self.adb.get_device_ip()
        self.firmware_incremental = self.adb.get_firmware_incremental()
        self.log_monitor = log_monitor # 传入 LogMonitor 实例
        self.package_name = config['target_app']['package_name']
        self.activity_name = config['target_app'].get('main_activity')
        self.http_config = config.get('http_vod', {})
        
        log("  - 初始化性能监控器...")
        self.monitor = PerformanceMonitor(
            self.adb,
            self.package_name,
            monitor_config=config.get("monitor", {}),
        )
        self.root_cause_analyzer = RootCauseAnalyzer(package_name=self.package_name)
        self.monitor.root_cause_analyzer = self.root_cause_analyzer
        log("  - 初始化播放器控制器...")
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
        self.tv_freeze_threshold_seconds = float(
            monitor_config.get("tv_freeze_threshold_seconds", 3)
        )
        
        # 应用到monitor
        if hasattr(self.monitor, '_disable_fps'):
            self.monitor._disable_fps = not self.enable_fps
        
        self.output_dir = config['report']['output_dir']
        self.last_csv_file = None
        self.last_summary_file = None
        self.tv_playback_watcher = TvPlaybackWatcher(
            self.adb,
            self.monitor,
            self.output_dir,
            config=monitor_config,
            event_callback=lambda event: self.monitor.report_event("TV_STALL", event),
            log_callback=self.log,
        )
        
        # 异步屏幕检测
        self.screen_check_executor = ThreadPoolExecutor(max_workers=1)
        self.screen_check_future = None
        self.last_screen_results = {} # 缓存上一次结果

        # 统计数据
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

        # 优化：下调 CV 阈值，捕捉更细微的帧间隔抖动（原 0.4/0.3/0.2 → 0.3/0.2/0.15）
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
            f"检测到FPS骤降: {worst['from']:.1f}→{worst['to']:.1f} "
            f"(降幅{worst['ratio']*100:.0f}%, {worst['amount']:.1f}fps)"
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
            return True, f"帧率波动很大(σ={fps_std:.1f}, 范围{fps_min:.1f}-{fps_max:.1f})"
        if fps_std > 4.0:
            return True, f"帧率波动较大(σ={fps_std:.1f}, 范围{fps_min:.1f}-{fps_max:.1f})"
        if fps_std > 2.5 and fps_mean < 28:
            return True, f"帧率轻微波动(σ={fps_std:.1f})，且平均帧率偏低({fps_mean:.1f})"
        if fps_range > 15 and fps_mean < 30:
            return True, f"帧率范围较大({fps_range:.1f}fps)"

        return False, f"帧率稳定(σ={fps_std:.1f})"

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
            issues.append(f"音频正常但视频帧率偏低({video_fps:.1f}fps)")
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
            details.append(f"帧间隔不均匀({desc})")

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
            details.append(f"音视频同步问题({desc})")

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

        # 点歌屏 UI Jank 仅作参考，不计入电视端播放卡顿风险分。

        if score >= 80:
            level = "严重卡顿"
            recommendation = "❌ 不建议上线，存在明显卡顿问题"
        elif score >= 50:
            level = "明显卡顿"
            recommendation = "⚠️ 需要优化，用户可明显感知卡顿"
        elif score >= 30:
            level = "轻微卡顿"
            recommendation = "⚠️ 建议优化，部分用户可能感知到卡顿"
        elif score >= 15:
            level = "微卡顿"
            recommendation = "ℹ️ 可接受，但建议关注"
        else:
            level = "流畅"
            recommendation = "✅ 播放流畅，可以上线"

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

    def run(self):
        self.running = True
        self.log(f"=== Android 播放器专项压测工具 v2.3 ===")
        
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
        self.log(f"监控模式: {'极低功耗' if not self.enable_screenshot and not self.enable_fps else '标准模式' if self.enable_screenshot and self.enable_fps else '深度压测'}")
        
        self.log("🔍 正在检查设备连接状态...")
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
                    self.log(f"⚠️ 设备检查异常: {e}")
            
            check_thread = threading.Thread(target=check_device)
            check_thread.daemon = True
            check_thread.start()
            check_thread.join(timeout=5)  # 最多等待5秒
            
            if check_thread.is_alive():
                self.log("⚠️ 设备检查超时，继续尝试启动...")
                device_online = True  # 假设在线，让后续步骤处理
            
            if not device_online:
                self.log("❌ 错误: 未检测到在线设备，请检查 USB 连接或 ADB 连接。")
                if self.runtime_id:
                     get_runtime_manager().update_status(self.runtime_id, RuntimeStatus.FAILED, error="Device offline")
                return
            self.log("✅ 设备连接正常")
        except Exception as e:
            self.log(f"⚠️ 设备检查过程异常: {e}，继续尝试启动...")

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
                self.log("⚠️ 应用启动超时，继续执行...")
            else:
                self.log("✅ 应用启动完成")
        except Exception as e:
            self.log(f"⚠️ 应用启动异常: {e}，继续执行...")
        
        # 1.1 根据模式决定是否首播点歌
        # 如果是 monitor_only，绝对不主动点歌
        if mode != "monitor_only" and self.http_config:
             self.log("检测到 HTTP 点歌配置，尝试首播...")
             try:
                 self.controller.vod_song()
             except Exception as e:
                 self.log(f"⚠️ 点歌失败: {e}，继续监控...")
             
        self.log("📊 正在启动性能监控...")
        try:
            self.monitor.start_monitoring()
            self.log("✅ 性能监控已启动")
        except Exception as e:
            self.log(f"❌ 性能监控启动失败: {e}")
            if self.runtime_id:
                 get_runtime_manager().update_status(self.runtime_id, RuntimeStatus.FAILED, error=f"Monitor start failed: {e}")
            return
        self.monitor.start_new_session("Initial Session") # V2.1: 启动第一个 Session
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
                'Timestamp(时间戳)', 
                'PID(进程ID)', 
                'Status(状态)', 
                'PSS_MB(内存MB)', 
                'Player_CPU_Percent(播放器CPU%)',
                'System_CPU_Percent(整机CPU%)',
                'MPP_Active(硬解路数)', 
                'MPP_Sessions(硬解总数)', 
                'Restart_Count(重启次数)', 
                'Play_State(播放状态)', 
                'Audio_Active(音频活跃)', 
                'GFX_Jank(掉帧)', 
                'Log_Stutter(卡顿日志)', 
                'Event(事件)', 
                'Screenshot_D0(截图_触摸屏)', 
                'Screenshot_D1(截图_电视)',
                'Top_Consumers(高占用进程-用于排除工具干扰)',
                'Root_Cause_Type(根因类型)',
                'Suspect_Process(嫌疑进程)',
                'Decode_Slowdown(解码降速)',
                'Max_Temperature_C(最高温度)',
                'Min_CPU_Frequency_Ratio(最低频率比例)',
                'Thermal_Throttling(热降频)',
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
                if self.log_monitor:
                    log_events = self.log_monitor.get_lifecycle_events()
                    self.monitor.set_log_stutter_count(self.log_monitor.get_stutter_count())

                # A. 执行监控采集 (V2: 包含 Evaluator 更新)
                # self.log("Collecting snapshot...")
                snapshot = self.monitor.collect_snapshot(external_events=log_events)
                
                # V2.4: 实时FPS有效性检查 (Early Warning)
                # V2.3.2: FPS检测已移至monitor.py中的智能检测逻辑
                # 这里不再需要重复的FPS警告逻辑
                
                self.log("Snapshot collected.")
                sys.stdout.flush()
                event_log = ""
                
                # A-2. 屏幕状态检测 (低频: 每 30s)
                current_screenshot_d0 = ""
                current_screenshot_d1 = ""
                
                if not hasattr(self, 'last_screen_check_time'):
                    self.last_screen_check_time = 0
                
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
                            
                        # V2.3: 解析所有检测到的电视端 Display（可能是 1 或 2）
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
                                break  # 使用第一个找到的电视端 Display
                        
                        # V2.3: 电视端画面冻结检测（针对检测到的电视端 Display）
                        if current_screenshot_d1 and os.path.exists(current_screenshot_d1):
                            from PIL import Image
                            if not hasattr(self, 'tv_screenshot_history'):
                                self.tv_screenshot_history = []
                            
                            # 保存最近3次截图路径（用于对比）
                            self.tv_screenshot_history.append({
                                'path': current_screenshot_d1,
                                'time': current_time
                            })
                            # 只保留最近3次
                            if len(self.tv_screenshot_history) > 3:
                                self.tv_screenshot_history.pop(0)
                            
                            # 如果至少有2次截图，且时间间隔 >= 1秒，进行对比
                            if len(self.tv_screenshot_history) >= 2:
                                last_screenshot = self.tv_screenshot_history[-2]
                                time_diff = current_time - last_screenshot['time']
                                
                                if time_diff >= 1.0:  # 至少1秒间隔
                                    # 对比画面是否静止
                                    try:
                                        is_frozen = self.screen_analyzer._is_same_image(
                                            Image.open(current_screenshot_d1).convert("RGB"),
                                            last_screenshot['path'],
                                            diff_threshold=1.0  # 极低阈值，只有几乎完全一样才算静止
                                        )
                                        
                                        # 如果画面静止且音频在跑，判定为电视端画面冻结
                                        snapshot = self.monitor.history[-1] if self.monitor.history else {}
                                        audio_active = snapshot.get('audio_active', False)
                                        
                                        ignore_video = snapshot.get('ignore_video_metrics', False)

                                        if is_frozen and audio_active and not ignore_video:
                                            if len(self.tv_screenshot_history) >= 3:
                                                time_span = self.tv_screenshot_history[-1]['time'] - self.tv_screenshot_history[0]['time']
                                                if time_span >= self.tv_freeze_threshold_seconds:
                                                    display_id_str = f"Display {tv_display_id}" if tv_display_id else "电视端"
                                                    self.log(f"  [TV Freeze Detected] {display_id_str}画面连续静止约{time_span:.1f}秒！")
                                                    self.monitor.report_event("TV_FREEZE", f"{display_id_str}画面静止{time_span:.1f}秒")
                                                    event_log += f" [TV_FREEZE:{time_span:.1f}s]"
                                                    self.tv_screenshot_history = []
                                    except Exception as e:
                                        # 对比失败，忽略
                                        pass
                            
                        # 告警检测
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
                            
                        # 如果成功截图，也在控制台输出一下路径，方便调试
                        if current_screenshot_d0 or current_screenshot_d1:
                                # 仅打印文件名，避免太长
                                fn0 = os.path.basename(current_screenshot_d0) if current_screenshot_d0 else "-"
                                fn1 = os.path.basename(current_screenshot_d1) if current_screenshot_d1 else "-"
                                # self.log(f"  [Snapshot] D0: {fn0}, D1: {fn1}")

                    except Exception as e:
                        self.log(f"Async screen check failed: {e}")
                        self.last_screen_results = {}
                    finally:
                        self.screen_check_future = None

                # V2.3.1: 零干扰模式 - 如果禁用截图，跳过屏幕检测
                if self.enable_screenshot:
                    if current_time - self.last_screen_check_time >= self.screen_check_interval_seconds:
                        self.last_screen_check_time = current_time
                        
                        if self.screen_check_future is None:
                            # self.log("Starting async screen check...")
                            sys.stdout.flush()
                            self.screen_check_future = self.screen_check_executor.submit(self.screen_analyzer.check_screen_status)
                        else:
                            self.log("Skipping screen check (Previous task still running)...")
                            sys.stdout.flush()
                else:
                    # 极低功耗模式：完全禁用截图，不进行屏幕检测
                    # 只在首次运行时初始化一次
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
                    # 纯监控模式：不执行任何主动操作 (切歌/点歌)
                    # 检测歌曲变化以更新 Song Count
                    # 方式1: Logcat 关键字（用户配置 song_change_keywords）
                    if self.log_monitor and hasattr(self.log_monitor, 'get_song_change_events'):
                        for evt in self.log_monitor.get_song_change_events():
                            self.song_count += 1
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
                        
                        self.controller.next_song() # 强制切歌
                        self.song_count += 1
                        self.monitor.start_new_session(f"Unknown (Song #{self.song_count})")
                        self.monitor.report_event("ACTION", "PLAY") # 新会话开始计时
                        last_action_time = current_time
                        event_log = f"Action: Cut Song | Last Result: {verdict}"
                
                elif mode == "loop_playback":
                    # 循环播放模式：
                    # 1. 如果播放失败或卡在初始化，尝试重新点歌
                    # 2. 如果播放完成，尝试重新点歌 (如果应用不自动连播)
                    # 3. 如果时间到了 skip_interval 且还在播放，强制切歌 (压测逻辑)
                    
                    # 检查当前状态
                    play_state = snapshot.get('play_state', 'UNKNOWN')
                    
                    # 策略 A: 失败恢复 (Start Failed) -> 立即重试
                    if "FAIL" in event_log or "Stuck" in snapshot.get('status', ''):
                         pass # 下面的时间检查会触发，或者我们可以立即触发？
                    
                    if current_time - last_action_time >= skip_interval:
                        # 只有在确实需要的时候才点歌？
                        # 用户反馈：为什么自动点歌？
                        # 解释：因为这是 loop_playback 压测模式，默认行为是周期性点歌/切歌以测试稳定性。
                        # 如果用户只想监控，应该用 monitor_only。
                        # 但为了体验更好，如果当前正在播放且没超时太多，也许可以不切？
                        # 暂时保持原逻辑，但在日志中明确原因
                        
                        # 结算上一首
                        verdict, reason = self.monitor.get_session_result(is_force_stop=True)
                        self.log(f"-------- [Song #{self.song_count}] Result: {verdict} ({reason}) --------")
                        
                        # 仅当上一首结束或失败，或者强制切歌时间到
                        self.log(f" 正在通过 HTTP 点歌 (Loop Mode Cycle)...")
                        song_id = self.controller.vod_song() 
                        self.song_count += 1
                        
                        self.monitor.start_new_session(f"ID: {song_id}" if song_id else f"Song #{self.song_count}")
                        self.monitor.report_event("ACTION", "PLAY")
                        
                        last_action_time = current_time
                        event_log = f"Action: Order Song (Loop Cycle) | Last Result: {verdict}"
                
                elif mode == "random_skip":
                     # 简单的随机切歌实现，复用 fixed_skip 的时间检测逻辑，但间隔随机化
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
            self._generate_summary_report(start_time_str)
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

        with open(self.last_summary_file, 'w', encoding='utf-8') as f:
            f.write("=== Android 播放器压测报告 (V2标准) ===\n")
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"测试包名: {self.package_name}\n")
            f.write(f"测试设备: {device_id if device_id else 'N/A'}\n")
            f.write(f"机顶盒 IP: {self.device_ip or '未获取'}\n")
            f.write(f"固件版本: {self.firmware_incremental or '未获取'}\n")
            f.write("-" * 30 + "\n")
            
            # === 新增：评分结论 ===
            f.write("【测试结论 (Decision)】\n")
            f.write(f"{one_sentence_summary}\n")
            if duration_sec < 3600:
                f.write(
                    "[覆盖度提示] 本轮不足1小时，可验证基础流畅度；"
                    "内存积累、后台CPU竞争和热降频建议至少运行1小时。\n"
                )
            f.write("-" * 30 + "\n")
            
            f.write(f"稳定性评分: {score_result['score']} / 100 (等级: {score_result['grade']})\n")
            f.write(
                f"电视端卡顿风险分: {perceptual_result['score']} / 100 "
                f"(分数越低越流畅，等级: {perceptual_result['level']})\n"
            )
            f.write(f"感知建议: {perceptual_result['recommendation']}\n")
            
            if score_result.get("ready_to_release"):
                release_status = "✅ 建议上线"
            elif score_result.get("assessment") == "inconclusive":
                release_status = "⚠️ 证据不足，暂不作上线结论"
            else:
                release_status = "❌ 不建议上线 (存在阻断性问题)"
            f.write(f"准入结论: {release_status}\n")
            blockers = score_result.get("release_blockers", []) or []
            if blockers:
                f.write("发布阻断/覆盖缺口:\n")
                for blocker in blockers:
                    f.write(f"  - {blocker}\n")
            
            if score_result['deductions']:
                f.write("扣分项:\n")
                for ded in score_result['deductions']:
                    f.write(f"  - {ded}\n")
            f.write("-" * 30 + "\n")
            # =====================
            
            f.write("【测试执行统计】\n")
            f.write(f"1. 实际运行时长: {duration_str}\n")
            if test_mode == "monitor_only":
                f.write("2. 播放统计: 纯监控模式，不主动点歌或统计歌曲成功率\n")
            else:
                f.write(f"2. 歌曲切换/点播次数: {self.song_count} 首\n")
            
            # V2.1 Session Stats
            session_stats = summary.get("session_stats", {})
            total_sessions = session_stats.get("total", 0)
            success_sessions = session_stats.get("success", 0)
            success_rate = (success_sessions / total_sessions * 100) if total_sessions > 0 else 0
            
            if test_mode == "monitor_only":
                f.write("3. 播放成功率: 不适用（纯监控模式）\n")
            elif total_sessions > 0:
                f.write(f"3. 播放成功率: {success_rate:.1f}% ({success_sessions}/{total_sessions})\n")
            else:
                f.write("3. 播放成功率: 无有效播放会话\n")
            f.write(f"4. 性能采样点数: {summary.get('duration_samples', 0)}\n")
            
            f.write("-" * 30 + "\n")
            
            f.write("【错误汇总 (Errors)】\n")
            f.write(f"1. 崩溃 (Crash/Exception): {error_stats['crash_count']} 次\n")
            f.write(f"2. 无响应 (ANR): {error_stats['anr_count']} 次\n")
            f.write(f"3. 进程异常重启: {summary.get('restart_count', 0)} 次\n")
            
            f.write("-" * 30 + "\n")
            f.write("【详细错误记录 (Detailed Errors)】\n")
            
            # 1. 播放失败会话
            failed_sessions = summary.get('failed_sessions', [])
            if failed_sessions:
                f.write(">> 播放失败/中断记录:\n")
                for item in failed_sessions:
                    f.write(f"   - [{item['time']}] Song: {item['song']} | Reason: {item['reason']}\n")
            else:
                f.write(">> 无播放失败记录\n")
                
            # 2. 系统级错误 (Crash/ANR)
            error_events = summary.get('error_events', [])
            # 过滤掉 log_file 字段以简化显示，或者保留
            if error_events:
                f.write("\n>> 系统崩溃/ANR记录:\n")
                for evt in error_events:
                    f.write(f"   - [{evt['time']}] Type: {evt['type']} | Msg: {evt['message']}\n")
                    f.write(f"     (Log: {evt.get('log_file', '')})\n")
            else:
                f.write("\n>> 无系统级崩溃记录\n")
            
            f.write("-" * 30 + "\n")
            
            f.write("【核心稳定性指标】\n")
            f.write(f"1. 进程重启次数: {summary.get('restart_count', 0)}\n")
            
            # V2.1: 显式计算首次重启时间
            first_restart_time = "N/A"
            if summary.get('pid_events'):
                for evt in summary['pid_events']:
                    if evt['type'] in ["PID_RESTART", "PID_LOST"]:
                        first_restart_time = evt['timestamp']
                        break
            f.write(f"2. 首次重启时间: {first_restart_time}\n")

            if summary.get('pid_events'):
                f.write("   [生命周期事件详情]:\n")
                for evt in summary['pid_events']:
                    if evt['type'] in ["PID_RESTART", "PID_LOST", "PID_FOUND"]:
                        elapsed = evt.get('elapsed_min', 0)
                        f.write(f"   - [{evt['timestamp']}] (第 {elapsed} 分钟) {evt['description']}\n")
            
            f.write(f"3. 峰值内存(PSS): {summary.get('max_pss_mb', 0)} MB\n")
            f.write(f"4. 平均内存(PSS): {summary.get('avg_pss_mb', 0)} MB\n")
            f.write(
                f"5. 播放器平均CPU / 整机平均CPU / 整机峰值CPU: "
                f"{summary.get('avg_player_cpu_percent', 0)}% / "
                f"{summary.get('avg_system_cpu_percent', 0)}% / "
                f"{summary.get('max_system_cpu_percent', 0)}%\n"
            )
            if summary.get("thermal_available_count", 0) > 0:
                f.write(
                    f"6. 最高温度 / 最低CPU频率比例 / 热降频采样: "
                    f"{summary.get('max_temperature_c', 0)}°C / "
                    f"{float(summary.get('min_cpu_frequency_ratio', 0) or 0) * 100:.1f}% / "
                    f"{summary.get('thermal_throttling_count', 0)} 次\n"
                )
            else:
                f.write("6. 温度与CPU频率: 设备节点不支持或无读取权限，未采集\n")
            f.write(
                f"7. 解码吞吐明显下降: "
                f"{summary.get('decode_slowdown_count', 0)} 次\n"
            )
            f.write("-" * 30 + "\n")
            f.write("【根因分析 (V3.0)】\n")
            if root_cause_analysis:
                f.write(f"1. 异常触发快照: {root_cause_analysis.get('total_stutter_events', 0)} 次\n")
                f.write(f"2. 根因/风险候选: {root_cause_analysis.get('identified_causes', 0)} 次\n")
                f.write(
                    f"   已确认播放退化: {root_cause_analysis.get('confirmed_playback_causes', 0)} 次 | "
                    f"资源风险: {root_cause_analysis.get('resource_risk_events', 0)} 次 | "
                    f"日志信号: {root_cause_analysis.get('log_signal_events', 0)} 次\n"
                )
                top_cause = (root_cause_analysis.get('most_confident_cause') or {}) if isinstance(root_cause_analysis, dict) else {}
                if top_cause:
                    evidence = top_cause.get("evidence", {}) or {}
                    if evidence.get("signal_only"):
                        label = "首要日志信号"
                    elif evidence.get("resource_only"):
                        label = "首要资源风险候选"
                    else:
                        label = "首要风险候选"
                    f.write(f"3. {label}: {top_cause.get('root_cause_type', '')} | {top_cause.get('suspect_process', '')} (风险评分: {top_cause.get('confidence', 0)})\n")
                process_risks = root_cause_analysis.get(
                    "process_risk_summary",
                    [],
                ) or []
                if process_risks:
                    f.write("4. 重复/高负载进程聚合榜:\n")
                    for index, item in enumerate(process_risks[:8], 1):
                        f.write(
                            f"   {index}) {item.get('process', '')} | "
                            f"最多 {item.get('max_instance_count', 1)} 个实例 | "
                            f"命中 {item.get('event_count', 0)} 个采样 | "
                            f"合计CPU均值/峰值 "
                            f"{item.get('avg_cpu_percent', 0)}%/"
                            f"{item.get('peak_cpu_percent', 0)}% | "
                            f"整机峰值 {item.get('max_system_cpu_percent', 0)}%\n"
                        )
            else:
                f.write("1. 根因分析数据不足\n")
            f.write("-" * 30 + "\n")
            
            f.write("【流畅度分析 (V2.3 - 零干扰监控版)】\n")
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
                f"电视端 Display: {summary.get('tv_display_id', 'N/A')} | "
                f"验证状态: {display_state} "
                f"({summary.get('tv_display_verification_reason', 'unknown')})\n"
            )
            
            # 零干扰模式说明
            monitor_config = self.config.get('monitor', {})
            enable_screenshot = monitor_config.get('enable_screenshot', True)
            enable_fps = monitor_config.get('enable_fps', True)
            if not enable_screenshot and not enable_fps:
                f.write("\n⚠️ 本次测试使用【极低功耗模式】，仅监控RK硬件节点，物理上与视频渲染路径完全隔离。\n")
                f.write("   此模式下检测到的卡顿100%排除工具干扰，最具说服力。\n")
            
            # 1. 真实视频帧率（最重要指标）
            video_fps = summary.get('avg_video_fps', 0)
            video_fps_samples = summary.get('video_fps_samples', 0)
            max_video_fps = summary.get('max_video_fps', 0)
            min_video_fps = summary.get('min_video_fps', 0)
            
            fps_sources = summary.get("video_fps_source_counts", {}) or {}
            direct_fps = any(
                str(source).startswith("surfaceflinger")
                for source in fps_sources
            )
            fps_label = (
                "电视画面直接帧率"
                if direct_fps and surface_locked
                else "解码吞吐估算帧率"
            )
            f.write(f"1. 视频帧率 ({fps_label}):\n")
            f.write(f"   数据来源分布: {fps_sources or {'none': 0}}\n")
            if video_fps > 0:
                f.write(f"   平均帧率: {video_fps} FPS\n")
                if max_video_fps > 0 and min_video_fps > 0:
                    f.write(f"   帧率范围: {min_video_fps:.1f} - {max_video_fps:.1f} FPS\n")
                if video_fps_samples > 0:
                    f.write(f"   有效样本: {video_fps_samples} 个\n")
                
                if video_fps < 20:
                    f.write("   >> [严重警告] 视频帧率严重偏低，存在明显卡顿！\n")
                    f.write("   >> 视频播放不流畅，用户体验差。\n")
                elif video_fps < 24:
                    f.write("   >> [警告] 视频帧率偏低，存在轻微卡顿风险。\n")
                elif video_fps < 27:
                    f.write("   >> [注意] 视频帧率略低于标准（24/25/30fps），可能存在微卡顿。\n")
                else:
                    if direct_fps and surface_locked:
                        f.write("   >> [良好] 电视画面直接帧率正常。\n")
                    else:
                        f.write("   >> [参考] 解码吞吐整体正常，但不能替代电视画面直接卡顿证据。\n")
            else:
                f.write("   (未能获取到视频帧率数据)\n")
                if not enable_fps:
                     f.write("   >> [原因]: 当前处于'极低功耗/零干扰'模式，该模式已主动禁用FPS采集以确保零干扰。\n")
                     f.write("   >> [建议]: 如果您需要分析微卡顿/掉帧，请重新开始测试并选择'标准模式'。\n")
                else:
                     f.write("   >> [提示] 可能原因：\n")
                     f.write("      - 应用未启动或进程不存在\n")
                     f.write("      - gfxinfo 数据不可用（需要应用运行一段时间）\n")
                     f.write("      - 设备不支持 gfxinfo 统计\n")
                     f.write("   建议：请参考下方日志卡顿分析作为替代指标\n")

            # 2. Log Stutter
            f.write(f"2. 卡顿相关日志信号 (Log Signal - 辅助指标):\n")
            f.write(f"   累计 {summary.get('final_log_stutter_count', 0)} 次\n")
            f.write("   (检测关键字: droppedFrames, underrun, buffer starvation)\n")
            f.write("   (说明: 日志命中不等于一次肉眼可见卡顿，需结合Surface帧停滞、解码下降或CPU证据确认)\n")

            # 3. UI Jank (Reference Only - 点歌屏)
            f.write(f"3. 点歌屏界面交互流畅度 (UI Jank - 仅供参考):\n")
            total_gfx_jank = int(summary.get("total_gfx_jank", 0) or 0)
            total_frames_delta = int(summary.get("total_frames_delta", 0) or 0)
            avg_jank_percent = float(summary.get("avg_jank_percent", 0) or 0.0)
            f.write(
                f"   原始数据 {total_gfx_jank} / {total_frames_delta} 帧 "
                f"(比例: {avg_jank_percent}%)\n"
            )
            if (
                total_frames_delta > 0
                and avg_jank_percent >= 99.0
                and video_fps >= 24
                and summary.get("final_log_stutter_count", 0) == 0
            ):
                f.write(
                    "   >> [已忽略] gfxinfo疑似累计口径异常；"
                    "电视FPS正常且无卡顿日志，不作为电视端卡顿证据。\n"
                )
            else:
                f.write(
                    "   (说明: 此指标不参与电视端流畅度评分，"
                    "仅用于点歌屏交互问题排查)\n"
                )

            # 4. Decode Drop (MPP Estimate - TV)
            f.write("4. 电视端解码丢帧 (MPP估算 - 关键指标):\n")
            decode_drop_total = summary.get('decode_drop_estimate_total', 0)
            decode_expected = summary.get('decode_expected_frames_estimate', 0)
            decode_ratio = float(summary.get('decode_drop_ratio', 0) or 0.0)
            f.write(f"   估算丢帧 {decode_drop_total} / {decode_expected} (丢帧率: {(decode_ratio * 100):.2f}%)\n")
            
            # V2.3.1: 零干扰模式说明
            monitor_config = self.config.get('monitor', {})
            enable_screenshot = monitor_config.get('enable_screenshot', True)
            enable_fps = monitor_config.get('enable_fps', True)
            
            if not enable_screenshot and not enable_fps:
                f.write("\n   >> [零干扰模式]: 本次测试使用'极低功耗'模式，仅监控RK硬件节点。\n")
                f.write("      >> 此模式下检测到的卡顿100%排除工具干扰，物理上与视频渲染路径完全隔离。\n")
                f.write("      >> 工作原理: 仅读取 /sys/kernel/debug/mpp_service/stats 节点，通过 work_count 增量判断解码器状态。\n")
                f.write("      >> 验证方法: 如果在此模式下检测到 decoder_stuck，100%是播放器或解码器问题，与工具无关。\n")
            
            # V2.3: 智能分析结论（优化：区分点歌屏和电视屏）
            # 结合人眼感知评分结果进行更深入的分析
            avg_jank = summary.get('avg_jank_percent', 0)
            log_stutter = summary.get('final_log_stutter_count', 0)
            decoder_stuck_count = summary.get('decoder_stuck_count', 0)
            tv_freeze_count = summary.get('tv_freeze_count', 0)
            
            # 使用感知评分详情
            perceptual_details = perceptual_result.get('details', [])
            perceptual_score = perceptual_result.get('score', 0)
            
            fps_severe = video_fps > 0 and video_fps < 20
            
            if decoder_stuck_count > 0 or tv_freeze_count > 0:
                f.write("\n   >> [严重警告]: 检测到\"电视端视频播放卡顿\" (TV Video Stutter)。\n")
                if decoder_stuck_count > 0:
                    f.write(f"      - 解码器卡死: {decoder_stuck_count} 次（硬件解码器停止输出新帧）\n")
                    if not enable_screenshot and not enable_fps:
                        f.write("      >> [零干扰验证]: 在极低功耗模式下检测到，100%排除工具干扰！\n")
                f.write("      >> 请查看CSV报告中的 'Top_Consumers' 列，如果监控工具不在前列，说明不是工具导致的。\n")
                if tv_freeze_count > 0:
                    f.write(f"      - 画面冻结: {tv_freeze_count} 次（电视端画面静止超过3秒）\n")
                f.write("      >> 这是最严重的卡顿类型，直接影响用户体验！\n")
            
            elif perceptual_score >= 50: # 明显卡顿
                f.write(f"\n   >> [智能分析]: 检测到\"明显视频卡顿\" (Perceptual Score: {perceptual_score})。\n")
                f.write("      主要问题:\n")
                for det in perceptual_details:
                     f.write(f"      - {det}\n")
                if fps_severe:
                     f.write("      - 视频帧率严重偏低\n")
            
            elif perceptual_score >= 30: # 轻微卡顿
                f.write(f"\n   >> [智能分析]: 检测到\"轻微视频卡顿\" (Perceptual Score: {perceptual_score})。\n")
                 # 列出具体原因
                for det in perceptual_details:
                     f.write(f"      - {det}\n")

            elif perceptual_score >= 15: # 微卡顿
                 f.write(f"\n   >> [智能分析]: 检测到\"微卡顿迹象\" (Perceptual Score: {perceptual_score})。\n")
                 for det in perceptual_details:
                     f.write(f"      - {det}\n")

            elif video_fps == 0 and log_stutter == 0:
                f.write("\n   >> [智能分析]: 数据不足 (Insufficient Data)。\n")
                f.write("      >> 未获取到有效视频帧率，且日志中无卡顿记录。\n")
                f.write("      >> 无法确认是否流畅，请检查：\n")
                f.write("         1. 视频是否真正开始播放？\n")
                f.write("         2. 测试时长是否太短？(建议 > 1分钟)\n")
                f.write("         3. adb shell dumpsys gfxinfo 是否有权限/数据？\n")
            else:
                f.write("\n   >> [智能分析]: 播放流畅度整体良好。\n")

            tv_stall_events = summary.get("tv_stall_events", [])
            f.write("\n5. Display 1 高频卡顿事件:\n")
            if tv_stall_events:
                f.write(f"   共检测到 {len(tv_stall_events)} 次完整卡顿事件\n")
                for event in tv_stall_events[-10:]:
                    contention = event.get("cpu_contention") or {}
                    candidate = contention.get("top_candidate") or {}
                    cpu_detail = ""
                    if candidate:
                        cpu_detail = (
                            f" | CPU嫌疑进程 {candidate.get('process', '')} "
                            f"{candidate.get('baseline_cpu_percent', 0)}%"
                            f"→{candidate.get('peak_cpu_percent', 0)}%"
                            f"→{candidate.get('after_cpu_percent', 0)}% "
                            f"(置信度 {candidate.get('confidence', 0)}%)"
                        )
                    f.write(
                        "   - "
                        f"{event.get('start_time', '')} | "
                        f"持续 {event.get('duration_ms', 0)}ms | "
                        f"最大帧间隔 {event.get('max_frame_gap_ms', 0)}ms | "
                        f"证据目录 {event.get('evidence_dir', '')}"
                        f"{cpu_detail}\n"
                    )
            else:
                f.write("   未检测到已完成的 Display 1 卡顿事件\n")

            f.write("-" * 30 + "\n")
            f.write("【长时间运行退化分析】\n")
            if degradation.get("status") == "insufficient_data":
                f.write("数据样本不足，无法进行退化分析 (需运行更长时间)\n")
            else:
                growth_rate = degradation.get('mem_growth_rate_mb_per_hour', 0)
                f.write(f"1. 内存增长速率: {growth_rate} MB/小时\n")
                f.write(f"   (首段均值: {degradation.get('first_avg_pss', 0)} MB -> 末段均值: {degradation.get('last_avg_pss', 0)} MB)\n")
                
                cpu_change = degradation.get('cpu_change_percent', 0)
                f.write(f"2. CPU 负载变化: {cpu_change}%\n")
                f.write(f"   (首段均值: {degradation.get('first_avg_cpu', 0)}% -> 末段均值: {degradation.get('last_avg_cpu', 0)}%)\n")
                
                # 简单结论
                if growth_rate > 10:
                    f.write(">> 警告: 存在明显的内存增长趋势 (疑似泄漏)\n")
                elif growth_rate < -5:
                    f.write(">> 注意: 内存占用呈下降趋势\n")
                else:
                    f.write(">> 结论: 内存占用相对平稳\n")
            
            f.write("-" * 30 + "\n")
            
            # V2 最终判定 & 评分 (已迁移至顶部)
            f.write("\n【原始数据】\n")
            f.write(f"CSV 详细报告: {os.path.basename(self.last_csv_file)}\n")
            
        # 生成 JSON 报告 (V2 Schema)
        json_report = {
            "meta": {
                "package_name": self.package_name,
                "device_id": device_id,
                "device_ip": self.device_ip,
                "firmware_incremental": self.firmware_incremental,
                "start_time": time_str,
                "end_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "duration_sec": duration_sec
            },
            "decision": score_result,
            "stats": summary,
            "metrics": score_result.get("metrics", {})
        }
        
        json_path = self.last_summary_file.replace(".txt", ".json")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_report, f, indent=4, ensure_ascii=False)
            
        # 生成 HTML 报告 (V2.1 新增)
        # 需要合并一些字段方便渲染
        html_summary = summary.copy()
        html_summary["score_result"] = score_result
        html_summary["duration_str"] = duration_str
        html_summary["song_count"] = self.song_count
        html_summary["error_stats"] = error_stats
        html_summary["device_id"] = device_id
        html_summary["device_ip"] = self.device_ip
        html_summary["firmware_incremental"] = self.firmware_incremental
        html_summary["package_name"] = self.package_name
        html_summary["test_mode"] = test_mode
        html_summary["duration_sec"] = duration_sec
        
        # 计算成功率
        session_stats = summary.get("session_stats", {})
        total_sessions = session_stats.get("total", 0)
        success_sessions = session_stats.get("success", 0)
        success_rate = (success_sessions / total_sessions * 100) if total_sessions > 0 else 0
        html_summary["success_rate"] = success_rate
        
        html_path = self.report_generator.generate_report(html_summary, self.monitor.history, time_str, root_cause_data=root_cause_analysis)
            
        self.log(f"报告已生成: \nTXT: {self.last_summary_file}\nJSON: {json_path}\nHTML: {html_path}")
