from datetime import datetime
from typing import Dict, List
import time
import logging
import statistics
from .adb_manager import AdbManager

logger = logging.getLogger(__name__)
from .evaluator import PlayStateEvaluator
from .rk_monitor import RkMonitor

class PerformanceMonitor:
    def __init__(self, adb: AdbManager, package_name: str, monitor_config: Dict = None):
        self.adb = adb
        self.package_name = package_name
        monitor_config = monitor_config or {}
        self.history: List[Dict] = []
        self.initial_pid = None
        self.restart_count = 0
        self.last_pid = None
        
        # V2 Evaluator
        self.evaluator = PlayStateEvaluator()
        
        # Rockchip Hardware Monitor
        self.rk_monitor = RkMonitor(adb)
        self.mpp_supported = False # Will be checked on first run
        
        # 自动识别歌曲信息控制
        self.last_metadata_check_time = 0
        
        # 卡顿/丢帧统计 (V2.1)
        self.last_gfx_info = {"total_frames": 0, "janky_frames": 0}
        self.log_stutter_count = 0 # 来自 logcat
        self.prev_log_stutter_total = 0 # 上一次快照时的 log_stutter 总数
        
        # PID 生命周期事件 (V2.1)
        self.pid_events = []
        self.monitoring_start_time = 0

        # 视频FPS缓存（避免频繁调用）
        self._video_fps_cache = None
        self._video_fps_cache_time = 0
        self._video_fps_cache_ttl = 2.0  # 缓存2秒
        
        # 电视端优先使用显式配置，并在运行时验证。默认不允许回退到点歌屏。
        self._configured_tv_display_id = int(monitor_config.get("tv_display_id", 1))
        self._auto_detect_tv_display = bool(monitor_config.get("auto_detect_tv_display", True))
        self._allow_display0_fallback = bool(monitor_config.get("allow_display0_fallback", False))
        self._tv_display_id = None  # 缓存的电视端 Display ID
        self._tv_display_id_cache_time = 0
        self._tv_display_id_cache_ttl = 300.0  # 缓存5分钟（Display ID 不会频繁变化）
        self._tv_display_verified = False
        self._tv_display_verification_reason = "not_checked"
        self._last_video_fps_source = "none"
        self._last_tv_surface_name = ""
        self._last_tv_surface_frame_timestamp_ns = 0
        self._tv_surface_failure_count = 0
        self.tv_freeze_events: List[Dict] = []
        self.tv_stall_events: List[Dict] = []
        
        # V2.3.1: 零干扰模式配置
        self._disable_fps = False  # 是否禁用FPS采集（极低功耗模式）
        self._disable_screenshot = False  # 是否禁用截图（由runner控制）

        self._ignore_video_metrics_until_ts = 0.0
        self._ignore_video_metrics_since_ts = 0.0
        self._ignore_video_metrics_reason = ""
        self._ignore_video_metrics_total_sec = 0.0
        self._ignore_video_metrics_event_count = 0
        self._ignore_buffering_event_count = 0
        self._ignore_seek_event_count = 0

        self._expected_stream_fps = 30.0
        self._expected_stream_fps_ready = False
        self._decode_slowdown_ratio = max(
            0.3,
            min(0.95, float(monitor_config.get("decode_slowdown_ratio", 0.75))),
        )
        self._thermal_check_interval_seconds = max(
            5.0,
            float(monitor_config.get("thermal_check_interval_seconds", 10.0)),
        )
        self._thermal_cache_time = 0.0
        self._thermal_cache = {
            "available": False,
            "max_temperature_c": 0.0,
            "min_frequency_ratio": 0.0,
            "thermal_throttling": False,
            "temperatures": [],
            "cpu_frequencies": [],
        }
        self.root_cause_analyzer = None

    def _apply_ignore_video_window(self, events: List[Dict]):
        if not events:
            return

        now = time.time()
        for evt in events:
            t = str(evt.get("type", "") or "").upper()
            if t in ("BUFFERING_START", "SEEK_START"):
                self._ignore_video_metrics_event_count += 1
                if t == "BUFFERING_START":
                    self._ignore_buffering_event_count += 1
                    reason = "BUFFERING"
                    duration = 2.0
                else:
                    self._ignore_seek_event_count += 1
                    reason = "SEEK"
                    duration = 2.0

                if now >= self._ignore_video_metrics_until_ts:
                    self._ignore_video_metrics_since_ts = now
                self._ignore_video_metrics_until_ts = max(self._ignore_video_metrics_until_ts, now + float(duration))
                self._ignore_video_metrics_reason = reason

            elif t in ("BUFFERING_END", "SEEK_END"):
                self._ignore_video_metrics_event_count += 1
                if now >= self._ignore_video_metrics_until_ts:
                    continue
                self._ignore_video_metrics_until_ts = max(self._ignore_video_metrics_until_ts, now + 1.0)

    def _is_ignoring_video_metrics(self) -> bool:
        now = time.time()
        if now < self._ignore_video_metrics_until_ts:
            return True
        if self._ignore_video_metrics_since_ts > 0:
            self._ignore_video_metrics_total_sec += max(0.0, now - self._ignore_video_metrics_since_ts)
            self._ignore_video_metrics_since_ts = 0.0
            self._ignore_video_metrics_reason = ""
        return False

    def is_ignoring_video_metrics(self) -> bool:
        return time.time() < self._ignore_video_metrics_until_ts

    def set_log_stutter_count(self, count):
        self.log_stutter_count = count

    def _update_expected_stream_fps(self, candidate_fps: float):
        try:
            v = float(candidate_fps or 0.0)
        except (TypeError, ValueError):
            return

        if v <= 0 or v > 120:
            return

        if not self._expected_stream_fps_ready:
            self._expected_stream_fps = v
            self._expected_stream_fps_ready = True
            return

        self._expected_stream_fps = (self._expected_stream_fps * 0.8) + (v * 0.2)

    def _get_cached_thermal_status(self) -> Dict:
        now = time.time()
        if (
            self._thermal_cache_time > 0
            and now - self._thermal_cache_time < self._thermal_check_interval_seconds
        ):
            return dict(self._thermal_cache)
        try:
            status = self.adb.get_thermal_status()
            if not isinstance(status, dict):
                status = {}
        except Exception as e:
            logger.debug("热状态采集失败: %s", e)
            status = {}
        self._thermal_cache = {
            "available": bool(status.get("available", False)),
            "max_temperature_c": float(status.get("max_temperature_c", 0) or 0.0),
            "min_frequency_ratio": float(status.get("min_frequency_ratio", 0) or 0.0),
            "thermal_throttling": bool(status.get("thermal_throttling", False)),
            "temperatures": list(status.get("temperatures") or []),
            "cpu_frequencies": list(status.get("cpu_frequencies") or []),
        }
        self._thermal_cache_time = now
        return dict(self._thermal_cache)

    def start_monitoring(self):
        """开始监控，记录初始PID"""
        self.monitoring_start_time = time.time()
        pid = self.adb.get_pid(self.package_name)
        if pid:
            self.initial_pid = pid
            self.last_pid = pid
            try:
                self.adb.reset_gfx_info(self.package_name)
                self.last_gfx_info = self.adb.get_gfx_info(self.package_name)
            except Exception as e:
                logger.debug("reset_gfx_info 失败: %s", e)
            logger.info("监控开始: %s, PID: %s", self.package_name, pid)
        else:
            logger.warning("未找到进程: %s", self.package_name)

    def collect_snapshot(self, external_events: List[Dict] = None) -> Dict:
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # V2.1: 如果有外部 Log 事件，优先单独汇报给 Evaluator
            if external_events:
                self._apply_ignore_video_window(external_events)
                for evt in external_events:
                    # 简单映射 Log 事件到 Evaluator 回调
                    # 假设 evt 包含 type 和 description
                    e_type = evt.get("type", "UNKNOWN")
                    msg = evt.get("line", "") or evt.get("description", "")
                    if "CRASH" in e_type or "FATAL" in e_type:
                        self.evaluator.on_fatal_error("CRASH", msg)
                    elif "ANR" in e_type:
                        self.evaluator.on_fatal_error("ANR", msg)
                    elif e_type == "FIRST_FRAME":
                        self.evaluator.on_first_frame(source="Logcat")
            
            # 1. PID & 重启检测
            if not self.adb.is_device_online():
                logger.warning("设备已离线，跳过本次监控采集")
                return {}

            current_pid = self.adb.get_pid(self.package_name)
            is_restarted = False
            
            # 计算相对时间 (分钟)
            elapsed_min = 0
            if self.monitoring_start_time > 0:
                elapsed_min = int((time.time() - self.monitoring_start_time) / 60)
            
            if current_pid is None:
                # 进程消失
                status = "DEAD"
                if self.last_pid is not None:
                    # 记录 PID 丢失事件
                    evt = {
                        "timestamp": timestamp,
                        "type": "PID_LOST",
                        "old_pid": self.last_pid,
                        "elapsed_min": elapsed_min,
                        "description": f"Process died (PID: {self.last_pid})"
                    }
                    self.pid_events.append(evt)
                    # V2.1: Report to Evaluator (Bypass)
                    self.evaluator.on_pid_event("LOST")
                    
            elif self.last_pid is not None and current_pid != self.last_pid:
                # PID 变化，判定为重启
                self.restart_count += 1
                is_restarted = True
                status = "RESTARTED"
                
                # 记录 PID 重启事件
                evt = {
                    "timestamp": timestamp,
                    "type": "PID_RESTART",
                    "old_pid": self.last_pid,
                    "new_pid": current_pid,
                    "elapsed_min": elapsed_min,
                    "description": f"Process restarted (Old: {self.last_pid} -> New: {current_pid})"
                }
                self.pid_events.append(evt)
                # V2.1: Report to Evaluator (Bypass)
                self.evaluator.on_pid_event("RESTART")
                
                self.last_pid = current_pid
                try:
                    self.adb.reset_gfx_info(self.package_name)
                    self.last_gfx_info = self.adb.get_gfx_info(self.package_name)
                except Exception as e:
                    logger.debug("PID 重启后 reset_gfx_info 失败: %s", e)
            else:
                status = "RUNNING"
                if self.last_pid is None: # 之前没找到，现在找到了
                    self.last_pid = current_pid
                    # 首次发现 PID，不算重启，算初始化
                    if not self.pid_events:
                        self.pid_events.append({
                            "timestamp": timestamp,
                            "type": "PID_FOUND",
                            "new_pid": current_pid,
                            "elapsed_min": elapsed_min,
                            "description": f"Process found (PID: {current_pid})"
                        })
                        # Evaluator 不需要 FOUND 事件，它只关心 RESTART/LOST


            # 2. 性能数据
            mem_info = self.adb.get_memory_info(self.package_name)
            cpu_usage = self.adb.get_cpu_usage(self.package_name)
            try:
                system_cpu_usage = float(self.adb.get_system_cpu_usage() or 0.0)
            except (TypeError, ValueError):
                system_cpu_usage = 0.0
            gpu_usage = self._measure_gpu_usage()
            thermal_status = self._get_cached_thermal_status()

            # 3. 播放状态判定 (V2 新增 - Evaluator bypass)
            is_audio_active = self.adb.is_audio_active()
            
            # Audio active 也可以视为 First Frame 的一种证据
            if is_audio_active:
                self.evaluator.on_first_frame("Audio")
            
            # 移除 Monitor 主动调用 process_event("POLLING")
            # 移除自动识别歌曲逻辑 (Evaluator 不再维护 song_info)
            
            play_state = "RUNNING" # 默认为运行中，Monitor 不再推断状态

            # 4. 卡顿检测 (V2.1 新增)
            # A. GFX Info
            gfx_info = self.adb.get_gfx_info(self.package_name)
            
            # 计算增量
            delta_total = max(0, gfx_info['total_frames'] - self.last_gfx_info['total_frames'])
            delta_jank = max(0, gfx_info['janky_frames'] - self.last_gfx_info['janky_frames'])
            
            jank_percent = 0.0
            if delta_total > 0:
                jank_percent = (delta_jank / delta_total) * 100.0
                
            self.last_gfx_info = gfx_info
            
            # B. Logcat Stutter (由外部 TestRunner/GUI 更新 log_stutter_count)
            delta_log_stutter = max(0, self.log_stutter_count - self.prev_log_stutter_total)
            self.prev_log_stutter_total = self.log_stutter_count
            
            # C. 资源占用分析 (当发生卡顿/Jank 时)
            top_consumers = ""
            system_cpu_pressure = bool(
                is_audio_active and system_cpu_usage >= 85.0
            )
            # Pure CPU contention may not emit player logs or UI jank.
            if delta_jank >= 5 or delta_log_stutter > 0 or system_cpu_pressure:
                top_consumers = self.adb.get_top_heavy_processes()

            # 2.1 MPP 硬件解码状态 (RK3576)
            # print("Debug: Checking MPP...")
            mpp_stats = {}
            # 首次运行检查支持
            if self.rk_monitor.is_supported is None:
                self.rk_monitor.check_support()
            
            if self.rk_monitor.is_supported:
                mpp_stats = self.rk_monitor.get_mpp_stats()
                # 自动判定逻辑: 假播放检测 (False Playback)
                # 如果音频活跃且 MPP 实例为 0 -> 可能软解
                if is_audio_active and mpp_stats.get('active_instances', 0) == 0:
                    mpp_stats['warning'] = "Potential Soft Decoding"
                
                # V2.3: 检测解码器卡死（电视端视频卡顿的关键指标）
                if mpp_stats.get('decoder_stuck', False):
                    # 解码器卡死：硬件解码器在运行但没有输出新帧
                    # 这是电视端视频卡顿的最可靠指标
                    mpp_stats['tv_stutter_detected'] = True
                    mpp_stats['tv_stutter_reason'] = f"Decoder stuck (work_count unchanged for {mpp_stats.get('decoder_stuck_duration_sec', 0):.1f}s)"
                else:
                    mpp_stats['tv_stutter_detected'] = False
            
            # print("Debug: MPP Checked. Snapshot done.")
            # 5. V2.2 Video FPS Check（使用try-except避免阻塞整个快照收集）
            # V2.3.1: 支持零干扰模式（可通过配置禁用FPS采集）
            video_fps = 0.0
            video_fps_source = "none"
            tv_display_id = self._detect_tv_display_id()
            if not hasattr(self, '_disable_fps') or not self._disable_fps:
                self._detect_video_layer()
                try:
                    if tv_display_id is not None:
                        video_fps = self._measure_video_fps(display_id=tv_display_id, mpp_stats=mpp_stats)
                    else:
                        video_fps = 0.0

                    if video_fps == 0.0 and self._allow_display0_fallback:
                        video_fps = self._measure_video_fps(display_id=0, mpp_stats=mpp_stats)

                    if video_fps and video_fps > 0:
                        video_fps_source = self._last_video_fps_source
                except Exception as e:
                    # V2.3.2: 更友好的错误提示
                    if not hasattr(self, '_fps_error_count'):
                        self._fps_error_count = 0
                    
                    self._fps_error_count += 1
                    
                    # 每5次失败提示一次，避免日志刷屏
                    if self._fps_error_count % 5 == 1:
                        logger.warning(
                            "连续多次未能采集到有效视频FPS数据。可能原因: 1)视频未开始播放 2)应用未在前台 "
                            "3)机型不支持 gfxinfo。包名: %s", self.package_name
                        )
                    
                    video_fps = 0.0

            mpp_dt = float(mpp_stats.get("work_count_delta_time_sec", 0) or 0.0)
            mpp_delta = int(mpp_stats.get("work_count_delta", 0) or 0)
            decode_fps_estimate = 0.0
            decode_drop_estimate = 0
            decode_drop_ratio = 0.0
            decode_slowdown_detected = False
            if mpp_dt > 0 and mpp_delta >= 0:
                decode_fps_estimate = mpp_delta / mpp_dt
                expected_before_sample = (
                    float(self._expected_stream_fps)
                    if self._expected_stream_fps_ready
                    else 0.0
                )
                decode_slowdown_detected = bool(
                    expected_before_sample > 0
                    and decode_fps_estimate > 0
                    and decode_fps_estimate
                    < expected_before_sample * self._decode_slowdown_ratio
                )

                if (video_fps is None or float(video_fps) <= 0) and decode_fps_estimate > 0:
                    video_fps = round(float(decode_fps_estimate), 2)
                    video_fps_source = "mpp"

                if (
                    video_fps
                    and float(video_fps) > 0
                    and not decode_slowdown_detected
                ):
                    self._update_expected_stream_fps(float(video_fps))
                elif decode_fps_estimate > 0 and not decode_slowdown_detected:
                    self._update_expected_stream_fps(float(decode_fps_estimate))

                expected_fps = (
                    expected_before_sample
                    if expected_before_sample > 0
                    else (
                        float(video_fps)
                        if video_fps and float(video_fps) > 0
                        else float(self._expected_stream_fps or 30.0)
                    )
                )
                expected = int(round(expected_fps * mpp_dt)) if expected_fps > 0 else 0
                if expected > 0:
                    drop = expected - mpp_delta
                    if drop > 0:
                        decode_drop_estimate = int(drop)
                        decode_drop_ratio = float(drop) / float(expected)

            ignore_video = self._is_ignoring_video_metrics()
            if ignore_video:
                decode_drop_estimate = 0
                decode_drop_ratio = 0.0
                decode_slowdown_detected = False
                if mpp_stats:
                    mpp_stats["decoder_stuck"] = False
                    mpp_stats["decoder_stuck_duration_sec"] = 0.0
                    mpp_stats["tv_stutter_detected"] = False

            if decode_slowdown_detected and not top_consumers:
                top_consumers = self.adb.get_top_heavy_processes()
            
            snapshot = {
                "timestamp": timestamp,
                "pid": current_pid if current_pid else -1,
                "status": status,
                "pss_mb": mem_info.get("pss_mb", 0),
                "cpu_percent": cpu_usage,
                "player_cpu_percent": cpu_usage,
                "system_cpu_percent": round(system_cpu_usage, 2),
                "system_cpu_pressure": system_cpu_pressure,
                "gpu_percent": gpu_usage,
                "mpp_active": mpp_stats.get("active_instances", 0),
                "mpp_sessions": mpp_stats.get("session_count", 0),
                "mpp_work_count": mpp_stats.get("total_work_count", 0),  # V2.3: 解码器工作计数
                "mpp_work_count_delta": mpp_stats.get("work_count_delta", 0),  # V2.3: 增量
                "mpp_work_count_delta_time_sec": mpp_stats.get("work_count_delta_time_sec", 0.0),
                "decoder_stuck": mpp_stats.get("decoder_stuck", False),  # V2.3: 解码器卡死
                "decoder_stuck_duration_sec": mpp_stats.get("decoder_stuck_duration_sec", 0.0),
                "tv_stutter_detected": mpp_stats.get("tv_stutter_detected", False),  # V2.3: 电视端卡顿
                "decode_fps_estimate": round(decode_fps_estimate, 2) if decode_fps_estimate > 0 else 0.0,
                "decode_slowdown_detected": bool(decode_slowdown_detected),
                "decode_slowdown_ratio": round(self._decode_slowdown_ratio, 2),
                "decode_drop_estimate": decode_drop_estimate,
                "decode_drop_ratio": round(decode_drop_ratio, 4) if decode_drop_ratio > 0 else 0.0,
                "video_fps_source": video_fps_source,
                "tv_display_id": tv_display_id,
                "tv_display_verified": bool(self._tv_display_verified),
                "tv_display_verification_reason": self._tv_display_verification_reason,
                "tv_surface_name": self._last_tv_surface_name,
                "expected_stream_fps": round(float(self._expected_stream_fps), 2) if float(self._expected_stream_fps) > 0 else 0.0,
                "thermal_available": bool(thermal_status.get("available", False)),
                "max_temperature_c": float(thermal_status.get("max_temperature_c", 0) or 0.0),
                "min_cpu_frequency_ratio": float(thermal_status.get("min_frequency_ratio", 0) or 0.0),
                "thermal_throttling": bool(thermal_status.get("thermal_throttling", False)),
                "ignore_video_metrics": bool(ignore_video),
                "ignore_video_reason": self._ignore_video_metrics_reason,
                "restart_count": self.restart_count,
                "is_restarted": is_restarted,
                "audio_active": is_audio_active, # V2
                "play_state": play_state,        # V2
                "video_fps": video_fps,          # V2.2 Real Video FPS
                "gfx_jank_count": delta_jank,    # V2.1 (System Jank)
                "gfx_jank_percent": round(jank_percent, 2), # V2.1
                
                # V2.2: Perceptual Jank (人眼感知)
                # 简单模型: 单次采样如果丢帧率 > 15% 且 total_delta > 10，则记为一次 "感知卡顿"
                # 更精细可以统计连续高 Jank 的次数
                "is_perceptual_jank": (jank_percent > 15.0 and delta_total > 10),
                
                "gfx_total_delta": delta_total,
                "log_stutter_count": self.log_stutter_count, # V2.1 (Total)
                "log_stutter_delta": delta_log_stutter,
                "top_consumers": top_consumers, # V2.1 (Heavy Process)
                "root_cause_type": "",
                "suspect_process": "",
                "root_cause_confidence": 0.0,
            }

            if self.root_cause_analyzer:
                try:
                    root_cause_triggered = bool(
                        delta_jank >= 5
                        or delta_log_stutter > 0
                        or snapshot.get("is_perceptual_jank", False)
                        or snapshot.get("decoder_stuck", False)
                        or snapshot.get("decode_slowdown_detected", False)
                        or snapshot.get("system_cpu_pressure", False)
                    )
                    if root_cause_triggered:
                        cause = self.root_cause_analyzer.record_stutter_event(snapshot, top_consumers)
                        if cause:
                            snapshot["root_cause_type"] = str(cause.get("root_cause_type", "") or "")
                            snapshot["suspect_process"] = str(cause.get("suspect_process", "") or "")
                            snapshot["root_cause_confidence"] = float(cause.get("confidence", 0) or 0.0)
                            snapshot["root_cause_evidence"] = dict(
                                cause.get("evidence", {}) or {}
                            )
                    else:
                        self.root_cause_analyzer.record_baseline(snapshot)
                except Exception:
                    pass

            self.history.append(snapshot)
            return snapshot
            
        except Exception as e:
            logger.exception("collect_snapshot 严重错误: %s", e)
            # Return a dummy snapshot to prevent crash
            return {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "pid": -1,
                "status": "ERROR",
                "pss_mb": 0,
                "cpu_percent": 0.0,
                "gpu_percent": 0.0,
                "mpp_active": 0,
                "mpp_sessions": 0,
                "restart_count": self.restart_count,
                "is_restarted": False,
                "audio_active": False,
                "play_state": "ERROR",
                "gfx_jank_count": 0,
                "gfx_jank_percent": 0.0,
                "is_perceptual_jank": False, # V2.2
                "mpp_work_count_delta": 0,
                "mpp_work_count_delta_time_sec": 0.0,
                "decoder_stuck": False,
                "decoder_stuck_duration_sec": 0.0,
                "decode_fps_estimate": 0.0,
                "decode_drop_estimate": 0,
                "decode_drop_ratio": 0.0,
                "log_stutter_count": 0,
                "top_consumers": ""
            }

    def analyze_degradation(self) -> Dict:
        """
        V2 长时间运行退化分析
        返回: 内存增长率, CPU前后变化
        """
        if len(self.history) < 10:
            return {"status": "insufficient_data"}
            
        # 1. 内存增长斜率 (简单的首尾对比，或者线性回归)
        # 简单算法：(Avg_Last_10% - Avg_First_10%) / Total_Time_Hours
        
        n = len(self.history)
        sample_count = max(1, int(n * 0.1)) # 取 10% 样本
        
        first_samples = self.history[:sample_count]
        last_samples = self.history[-sample_count:]
        
        # PSS Analysis
        first_avg_pss = sum(h['pss_mb'] for h in first_samples) / len(first_samples)
        last_avg_pss = sum(h['pss_mb'] for h in last_samples) / len(last_samples)
        
        # Calculate time diff in hours
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            t_start = datetime.strptime(first_samples[0]['timestamp'], fmt)
            t_end = datetime.strptime(last_samples[-1]['timestamp'], fmt)
            hours = (t_end - t_start).total_seconds() / 3600.0
        except (KeyError, ValueError) as e:
            logger.debug("analyze_degradation 时间解析失败: %s", e)
            hours = 0.1
            
        if hours < 0.01: hours = 0.01
            
        mem_growth_rate = (last_avg_pss - first_avg_pss) / hours
        
        # CPU Analysis
        first_avg_cpu = sum(h['cpu_percent'] for h in first_samples) / len(first_samples)
        last_avg_cpu = sum(h['cpu_percent'] for h in last_samples) / len(last_samples)
        
        return {
            "mem_growth_rate_mb_per_hour": round(mem_growth_rate, 2),
            "first_avg_pss": round(first_avg_pss, 2),
            "last_avg_pss": round(last_avg_pss, 2),
            "first_avg_cpu": round(first_avg_cpu, 2),
            "last_avg_cpu": round(last_avg_cpu, 2),
            "cpu_change_percent": round((last_avg_cpu - first_avg_cpu), 2),
            "duration_hours": round(hours, 2)
        }

    def get_summary(self) -> Dict:
        """获取统计摘要 (V2)"""
        if not self.history:
            return {}
            
        pss_values = [h["pss_mb"] for h in self.history if h["pss_mb"] > 0]
        avg_pss = sum(pss_values) / len(pss_values) if pss_values else 0
        max_pss = max(pss_values) if pss_values else 0
        
        # 卡顿统计
        total_gfx_jank = sum(h.get("gfx_jank_count", 0) for h in self.history)
        total_frames_delta = sum(h.get("gfx_total_delta", 0) for h in self.history)
        if total_frames_delta > 0:
            avg_jank_percent = (total_gfx_jank / total_frames_delta) * 100.0
        else:
            avg_jank_percent = sum(h.get("gfx_jank_percent", 0) for h in self.history) / len(self.history) if self.history else 0
        
        # 视频FPS统计（V2.2 - 最重要的指标）
        video_fps_values = [h.get("video_fps", 0) for h in self.history if h.get("video_fps", 0) > 0]
        video_fps_source_counts = {}
        for item in self.history:
            source = str(item.get("video_fps_source", "") or "none")
            video_fps_source_counts[source] = (
                video_fps_source_counts.get(source, 0) + 1
            )
        if video_fps_values:
            avg_video_fps = sum(video_fps_values) / len(video_fps_values)
            max_video_fps = max(video_fps_values)
            min_video_fps = min(video_fps_values)
        else:
            avg_video_fps = 0.0
            max_video_fps = 0.0
            min_video_fps = 0.0
        
        # GPU 使用率统计（V2.4 - 新增）
        gpu_values = [h.get("gpu_percent", 0) for h in self.history if h.get("gpu_percent", 0) > 0]
        if gpu_values:
            avg_gpu = sum(gpu_values) / len(gpu_values)
            max_gpu = max(gpu_values)
            min_gpu = min(gpu_values)
        else:
            avg_gpu = 0.0
            max_gpu = 0.0
            min_gpu = 0.0

        player_cpu_values = [
            float(h.get("player_cpu_percent", h.get("cpu_percent", 0)) or 0.0)
            for h in self.history
        ]
        system_cpu_values = [
            float(h.get("system_cpu_percent", 0) or 0.0)
            for h in self.history
            if float(h.get("system_cpu_percent", 0) or 0.0) > 0
        ]
        temperature_values = [
            float(h.get("max_temperature_c", 0) or 0.0)
            for h in self.history
            if float(h.get("max_temperature_c", 0) or 0.0) > 0
        ]
        frequency_ratios = [
            float(h.get("min_cpu_frequency_ratio", 0) or 0.0)
            for h in self.history
            if float(h.get("min_cpu_frequency_ratio", 0) or 0.0) > 0
        ]
        thermal_throttling_count = sum(
            1 for h in self.history if h.get("thermal_throttling", False)
        )
        thermal_available_count = sum(
            1 for h in self.history if h.get("thermal_available", False)
        )
        decode_slowdown_count = sum(
            1
            for h in self.history
            if (
                not h.get("ignore_video_metrics", False)
                and h.get("decode_slowdown_detected", False)
            )
        )
        
        degradation = self.analyze_degradation()
        
        # 失败会话过滤
        failed_sessions = [
            {"time": time.strftime("%H:%M:%S", time.localtime(s.get("events", [{}])[0].get("time", 0))), 
             "song": s["session_id"], 
             "reason": s["fail_reason"]}
            for s in self.evaluator.session_results 
            if s["final_state"] in [self.evaluator.STATE_FAILED, self.evaluator.STATE_INTERRUPTED]
        ]

        # Perceptual Jank Stats (V2.2)
        perceptual_events = sum(1 for h in self.history if h.get("is_perceptual_jank", False))
        perceptual_jank_percent = (perceptual_events / len(self.history) * 100.0) if self.history else 0.0
        
        # V2.3: 电视端卡顿统计（最重要）
        decoder_stuck_count = sum(1 for h in self.history if (not h.get("ignore_video_metrics", False)) and h.get("decoder_stuck", False))
        tv_stutter_count = sum(1 for h in self.history if (not h.get("ignore_video_metrics", False)) and h.get("tv_stutter_detected", False))
        tv_freeze_count = len(self.tv_freeze_events)

        # 基础汇总
        decode_expected = 0.0
        decode_actual = 0
        for h in self.history:
            if h.get("ignore_video_metrics", False):
                continue
            dt = float(h.get("mpp_work_count_delta_time_sec", 0) or 0.0)
            if dt <= 0:
                continue
            expected_fps = float(h.get("expected_stream_fps", 0) or 0.0)
            if expected_fps > 0:
                decode_expected += expected_fps * dt
            decode_actual += int(h.get("mpp_work_count_delta", 0) or 0)

        decode_expected_int = int(round(decode_expected)) if decode_expected > 0 else 0
        decode_drop_total = max(0, decode_expected_int - decode_actual) if decode_expected_int > 0 else 0
        decode_drop_ratio = (float(decode_drop_total) / float(decode_expected_int)) if decode_expected_int > 0 else 0.0

        base_summary = {
            "restart_count": self.restart_count,
            "avg_pss_mb": round(avg_pss, 2),
            "max_pss_mb": round(max_pss, 2),
            "duration_samples": len(self.history),
            "degradation_analysis": degradation,
            "total_gfx_jank": total_gfx_jank,
            "total_frames_delta": total_frames_delta,     # V2.2 Explicit total frames
            "avg_jank_percent": round(avg_jank_percent, 2),
            "perceptual_jank_percent": round(perceptual_jank_percent, 2), # V2.2
            "perceptual_jank_events": perceptual_events,                  # V2.2
            "final_log_stutter_count": self.log_stutter_count,
            # V2.3: 电视端卡顿统计（最重要）
            "decoder_stuck_count": decoder_stuck_count,
            "tv_stutter_count": tv_stutter_count,
            "tv_freeze_count": tv_freeze_count,
            "tv_freeze_events": list(self.tv_freeze_events),
            "tv_stall_count": len(self.tv_stall_events),
            "tv_stall_events": list(self.tv_stall_events),
            "tv_display_id": self._tv_display_id,
            "tv_display_verified": bool(self._tv_display_verified),
            "tv_display_verification_reason": self._tv_display_verification_reason,
            "tv_surface_locked": bool(self._last_tv_surface_name),
            "tv_surface_name": self._last_tv_surface_name,
            "video_fps_source_counts": video_fps_source_counts,
            "ignore_video_metrics_total_sec": round(float(self._ignore_video_metrics_total_sec), 2),
            "ignore_video_metrics_event_count": int(self._ignore_video_metrics_event_count),
            "ignore_buffering_event_count": int(self._ignore_buffering_event_count),
            "ignore_seek_event_count": int(self._ignore_seek_event_count),
            "decode_expected_frames_estimate": decode_expected_int,
            "decode_actual_frames_estimate": int(decode_actual),
            "decode_drop_estimate_total": int(decode_drop_total),
            "decode_drop_ratio": round(decode_drop_ratio, 4),
            "decode_slowdown_count": int(decode_slowdown_count),
            "avg_player_cpu_percent": round(
                sum(player_cpu_values) / len(player_cpu_values),
                2,
            ) if player_cpu_values else 0.0,
            "avg_system_cpu_percent": round(
                sum(system_cpu_values) / len(system_cpu_values),
                2,
            ) if system_cpu_values else 0.0,
            "max_system_cpu_percent": round(
                max(system_cpu_values),
                2,
            ) if system_cpu_values else 0.0,
            "max_temperature_c": round(
                max(temperature_values),
                1,
            ) if temperature_values else 0.0,
            "min_cpu_frequency_ratio": round(
                min(frequency_ratios),
                3,
            ) if frequency_ratios else 0.0,
            "thermal_throttling_count": int(thermal_throttling_count),
            "thermal_throttling_detected": bool(thermal_throttling_count),
            "thermal_available_count": int(thermal_available_count),
            # V2.2 视频FPS统计（最重要）
            "avg_video_fps": round(avg_video_fps, 2),
            "max_video_fps": round(max_video_fps, 2),
            "min_video_fps": round(min_video_fps, 2),
            "video_fps_samples": len(video_fps_values),  # 有效FPS样本数
            # V2.4: GPU 使用率统计（新增）
            "avg_gpu_percent": round(avg_gpu, 2),
            "max_gpu_percent": round(max_gpu, 2),
            "min_gpu_percent": round(min_gpu, 2),
            "gpu_samples": len(gpu_values),  # 有效GPU样本数
            "session_stats": {
                "total": len(self.evaluator.session_results),
                "success": sum(1 for s in self.evaluator.session_results if s["final_state"] == self.evaluator.STATE_FINISHED)
            },
            "failed_sessions": failed_sessions, # V2.1 Failed Details
            "pid_events": self.pid_events, # V2.1 PID Events
            # 需要外部填入: crash_count, anr_count
            "crash_count": 0, 
            "anr_count": 0
        }
        
        return base_summary
        
    def calculate_score(self, base_summary: Dict) -> Dict:
        """调用 evaluator 计算最终得分 (V2.1)"""
        return self.evaluator.evaluate_global_score(base_summary)

    def start_new_session(self, song_info: str = None):
        """开始新的播放会话"""
        self.evaluator.start_new_session(song_info or "Unknown")

    def report_event(self, event_type: str, payload: any):
        """
        向 Evaluator 汇报外部事件 (V2.1 Unified Entry)
        """
        if event_type == "FATAL" or event_type == "ERROR":
            self.evaluator.on_fatal_error("CRASH", str(payload))
        elif event_type == "ACTION" and payload == "PLAY":
            self.evaluator.on_play_command_issued()
        elif event_type == "SCREEN_ANOMALY":
            self.evaluator.on_screen_anomaly(str(payload))
        elif event_type == "TV_FREEZE":
            event = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "TV_FREEZE",
                "description": str(payload),
                "display_id": self._tv_display_id,
            }
            self.tv_freeze_events.append(event)
            self.evaluator.on_screen_anomaly(f"TV_FREEZE: {payload}")
        elif event_type == "TV_STALL":
            event = dict(payload) if isinstance(payload, dict) else {
                "type": "TV_STALL",
                "description": str(payload),
            }
            self.tv_stall_events.append(event)
            self.evaluator.on_screen_anomaly(
                f"TV_STALL: {event.get('duration_ms', 0)}ms"
            )
        elif event_type == "ACTION" and payload == "STOP":
            self.evaluator.on_interrupt("Manual Stop")

    def get_session_result(self, is_force_stop: bool = True):
        """获取当前会话判定"""
        # 如果是强制停止，意味着 Runner 正在切歌，此时需要 finalize
        if is_force_stop:
             # 如果之前没结束，现在手动结束
             if not self.evaluator.is_finished:
                 # 假设这里 Runner 切歌算是 "INTERRUPTED" 还是 "FINISHED"?
                 # 如果是 loop_playback，时间到了强制切歌，通常算 Pass 除非有错误。
                 # 但是 evaluator.finalize() 会根据 internal state 判断。
                 # 如果没有 first_frame -> Fail
                 # 如果有 first_frame -> Interrupted (because is_finished is False)
                 pass
             
             res = self.evaluator.finalize()
             return res["final_state"], res["fail_reason"]
        
        return "UNKNOWN", "Running"

    def _detect_video_layer(self):
        """
        Detect if a video layer is active (SurfaceView/TextureView with 'Video' or similar)
        This is a placeholder for V2.2
        """
        pass

    def _detect_tv_display_id(self):
        current_time = time.time()
        cache_ttl = self._tv_display_id_cache_ttl if self._tv_display_verified else 10.0
        if (self._tv_display_id is not None and
            current_time - self._tv_display_id_cache_time < cache_ttl):
            return self._tv_display_id

        display_id = None
        ids = set()
        try:
            output = self.adb._run_command(["shell", "dumpsys", "display"], timeout=2)
            if output and "Error:" not in output:
                for line in output.splitlines():
                    line_l = line.lower()
                    import re
                    matches = re.findall(
                        r'(?:displayid|mdisplayid)\s*=\s*(\d+)|display\s+(?:id\s*=\s*)?(\d+)',
                        line_l,
                    )
                    for match in matches:
                        try:
                            ids.add(int(match[0] or match[1]))
                        except (TypeError, ValueError):
                            continue

                if self._configured_tv_display_id in ids:
                    display_id = self._configured_tv_display_id
                    self._tv_display_verified = True
                    self._tv_display_verification_reason = "configured_display_found"
                elif self._auto_detect_tv_display:
                    candidates = sorted([i for i in ids if i > 0])
                    if candidates:
                        display_id = candidates[0]
                        self._tv_display_verified = True
                        self._tv_display_verification_reason = "auto_detected_non_primary_display"
        except Exception:
            display_id = None

        if display_id is None:
            display_id = self._configured_tv_display_id
            self._tv_display_verified = False
            if ids:
                self._tv_display_verification_reason = "configured_display_not_found"
            else:
                self._tv_display_verification_reason = "display_list_unavailable"

        self._tv_display_id = display_id
        self._tv_display_id_cache_time = current_time
        return display_id

    def _measure_video_fps(self, display_id: int = 0, mpp_stats: Dict = None) -> float:
        """
        Measure real video FPS - 最优方案
        采用多策略组合：优先gfxinfo（最通用），备用SurfaceFlinger（更精确），最后尝试MPP硬件计数（最底层）
        已优化：所有操作都有超时和异常处理，避免阻塞
        
        Args:
            display_id: Display ID (0=主屏/点歌屏, 1=电视屏, 默认0)
            mpp_stats: 可选的MPP统计信息，用于Tier 3回退策略
        """
        try:
            # 使用缓存避免频繁调用（2秒内复用结果）
            # 注意：如果有 mpp_stats，我们可能希望更新它，但缓存优先
            current_time = time.time()
            if (self._video_fps_cache is not None and self._video_fps_cache > 0 and
                current_time - self._video_fps_cache_time < self._video_fps_cache_ttl):
                return self._video_fps_cache
            
            fps = 0.0
            
            # 电视端必须优先使用对应 Display 的 SurfaceFlinger 数据。
            try:
                surface_fps = self._get_fps_from_surfaceflinger(display_id=display_id)
                if surface_fps > 0 and surface_fps < 120:
                    self._video_fps_cache = surface_fps
                    self._video_fps_cache_time = current_time
                    self._last_video_fps_source = f"surfaceflinger_display_{display_id}"
                    return surface_fps
            except Exception:
                pass

            # gfxinfo 是应用/UI 级指标，只允许用于主屏诊断，不能冒充电视端 FPS。
            if display_id == 0:
                try:
                    fps = self._get_fps_from_gfxinfo()
                    if fps > 0 and fps < 120:
                        self._video_fps_cache = fps
                        self._video_fps_cache_time = current_time
                        self._last_video_fps_source = "gfxinfo_display_0"
                        return fps
                except Exception:
                    pass
            
            # 策略3: 如果前两者都失败，且有MPP统计数据，尝试使用硬件解码计数（最底层）
            # 这是针对RK平台的兜底策略，物理上统计解码帧数
            if mpp_stats and mpp_stats.get("work_count_delta", 0) > 0:
                delta = mpp_stats.get("work_count_delta", 0)
                time_sec = mpp_stats.get("work_count_delta_time_sec", 0)
                if time_sec > 0:
                    fps_estimate = delta / time_sec
                    # 只有在合理范围内才采信 (1fps - 120fps)
                    if 0 < fps_estimate < 120:
                        self._video_fps_cache = fps_estimate
                        self._video_fps_cache_time = current_time
                        self._last_video_fps_source = "mpp_estimate"
                        return round(fps_estimate, 2)

            # 如果都失败，返回0（表示无法获取）
            self._video_fps_cache = None
            self._video_fps_cache_time = 0
            self._last_video_fps_source = "none"
            return 0.0
        except Exception:
            # 最外层异常捕获，确保不会崩溃
            return 0.0

    def _measure_gpu_usage(self) -> float:
        """
        通过 dumpsys gpu 命令采集 GPU 使用率
        支持多种 Android 设备的 GPU 命令格式（Qualcomm Adreno, Mali, 等）
        
        Returns:
            GPU 使用率百分比 (0.0 - 100.0)，如果无法获取返回 0.0
        """
        try:
            # 执行 dumpsys gpu 命令
            gpu_output = self.adb._run_command(
                ["shell", "dumpsys", "gpu"],
                timeout=3
            )
            
            if not gpu_output or "Error:" in gpu_output:
                return 0.0
            
            # 策略1: 查找 "GPU memory:" 和 "GPU usage:" 或类似字段
            # 不同厂商格式不同，尝试多种匹配模式
            
            # 模式1: Qualcomm Adreno GPU (常见于小米、OPPO、vivo等)
            # 格式示例:
            #   GPU memory: Total=123MB, Used=45MB
            #   GPU usage: 45%
            if "GPU usage:" in gpu_output:
                import re
                match = re.search(r'GPU usage:\s*(\d+(?:\.\d+)?)\s*%', gpu_output, re.IGNORECASE)
                if match:
                    usage = float(match.group(1))
                    if 0.0 <= usage <= 100.0:
                        return round(usage, 2)
            
            # 模式2: Mali GPU (ARM 芯片，如海思、瑞芯微等)
            # 格式示例:
            #   Mali-G52: 35% utilization
            #   GPU utilization: 40.5%
            for pattern in [
                r'(?:GPU|Mali)[^:]*:\s*(\d+(?:\.\d+)?)\s*%?\s*(?:utilization|usage)',
                r'(?:utilization|usage):\s*(\d+(?:\.\d+)?)\s*%',
                r'GPU\s*\(\s*(\d+(?:\.\d+)?)\s*%\)'
            ]:
                import re
                match = re.search(pattern, gpu_output, re.IGNORECASE)
                if match:
                    usage = float(match.group(1))
                    if 0.0 <= usage <= 100.0:
                        return round(usage, 2)
            
            # 模式3: 尝试从 GPU 时钟频率推算
            # 格式示例:
            #   GPU clock: 450MHz (max: 600MHz)
            #   Current: 300/600 MHz
            import re
            clock_match = re.search(r'GPU\s*clock:?\s*(\d+)\s*(?:MHz|kHz)\s*(?:/\s*(\d+))?', gpu_output, re.IGNORECASE)
            if clock_match:
                current_clock = int(clock_match.group(1))
                max_clock = int(clock_match.group(2)) if clock_match.group(2) else None
                if max_clock and max_clock > 0:
                    usage_percent = (current_clock / max_clock) * 100.0
                    if 0.0 <= usage_percent <= 100.0:
                        return round(usage_percent, 2)
            
            # 模式4: 尝试查找任意百分比数字（作为最后的兜底）
            # 注意：这可能不够精确，所以仅在其他方法都失败时使用
            import re
            percent_matches = re.findall(r'(\d+(?:\.\d+)?)\s*%', gpu_output)
            if percent_matches:
                # 取第一个合理的值
                for p_str in percent_matches:
                    try:
                        p = float(p_str)
                        if 0.0 <= p <= 100.0 and p > 1.0:  # 排除 0-1% 的微小噪音
                            logger.debug("[GPU] 使用兜底方法，从输出中提取: %.1f%%", p)
                            return round(p, 2)
                    except (ValueError, TypeError):
                        continue
            
            logger.debug("[GPU] 未能从 dumpsys gpu 输出中解析出使用率")
            return 0.0
            
        except Exception as e:
            logger.debug("[GPU] _measure_gpu_usage 异常: %s", e)
            return 0.0
    
    def _get_fps_from_surfaceflinger(self, display_id: int = 0) -> float:
        metrics = self.collect_tv_frame_metrics(display_id, track_progress=False)
        return float(metrics.get("fps", 0) or 0.0)

    def collect_tv_frame_metrics(
        self,
        display_id: int = 1,
        track_progress: bool = True,
    ) -> Dict:
        result = {
            "timestamp": time.time(),
            "display_id": display_id,
            "fps": 0.0,
            "max_frame_gap_ms": 0.0,
            "p95_frame_gap_ms": 0.0,
            "frame_advanced": False,
            "surface_name": self._last_tv_surface_name,
        }
        try:
            candidates = []
            if self._last_tv_surface_name and self._tv_surface_failure_count < 3:
                candidates.append(self._last_tv_surface_name)
            else:
                candidates = self._find_tv_surface_candidates(display_id)

            for surface in candidates[:8]:
                safe_surface = str(surface).replace("'", "'\\''")
                output = self.adb._run_command(
                    ["shell", "sh", "-c", f"dumpsys SurfaceFlinger --latency '{safe_surface}'"],
                    timeout=2,
                )
                previous_timestamp = (
                    self._last_tv_surface_frame_timestamp_ns
                    if track_progress else 0
                )
                metrics = self._parse_surface_latency(
                    output,
                    since_timestamp_ns=previous_timestamp,
                )
                if metrics["frame_count"] < 2:
                    continue

                latest = metrics["latest_frame_timestamp_ns"]
                metrics["frame_advanced"] = bool(
                    latest > 0
                    and (
                        not track_progress
                        or latest != self._last_tv_surface_frame_timestamp_ns
                    )
                )
                metrics["surface_name"] = surface
                metrics["display_id"] = display_id
                if track_progress:
                    self._last_tv_surface_frame_timestamp_ns = latest
                self._last_tv_surface_name = surface
                self._tv_surface_failure_count = 0
                return metrics

            self._tv_surface_failure_count += 1
            if self._tv_surface_failure_count >= 3:
                self._last_tv_surface_name = ""
                self._last_tv_surface_frame_timestamp_ns = 0
            return result
        except Exception:
            self._tv_surface_failure_count += 1
            return result

    def _find_tv_surface_candidates(self, display_id: int) -> List[str]:
        pkg_key = self.package_name.split(':')[0]
        output = self.adb._run_command(
            ["shell", "dumpsys", "SurfaceFlinger", "--display-id", str(display_id), "--list"],
            timeout=2,
        )
        display_scoped = bool(
            output
            and "Error:" not in output
            and "Unknown" not in output
            and any(
                marker in output.lower()
                for marker in (
                    "surfaceview",
                    "textureview",
                    "video",
                    "media",
                    pkg_key.lower(),
                )
            )
        )
        if not display_scoped:
            output = self.adb._run_command(
                ["shell", "dumpsys", "SurfaceFlinger", "--list"],
                timeout=2,
            )
        if not output or "Error:" in output:
            return []

        candidates = []
        fallback_candidates = []
        seen = set()

        def add(surface: str):
            value = (surface or "").strip()
            if value and value not in seen:
                seen.add(value)
                candidates.append(value)

        def add_fallback(surface: str):
            value = (surface or "").strip()
            if value and value not in seen:
                seen.add(value)
                fallback_candidates.append(value)

        surface_lines = output.splitlines()
        if not display_scoped:
            target_root = f"root#{display_id}"
            scoped_lines = []
            in_target_root = False
            root_found = False
            for line in surface_lines:
                stripped = line.strip()
                lower_line = stripped.lower()
                if lower_line.startswith("root#"):
                    in_target_root = lower_line.startswith(target_root)
                    root_found = root_found or in_target_root
                    continue
                if in_target_root:
                    scoped_lines.append(line)
            if not root_found:
                return []
            surface_lines = scoped_lines

        for surface in surface_lines:
            lower = surface.lower()
            if "background for" in lower or "bounds for" in lower:
                continue
            display_hint = any(
                keyword in lower
                for keyword in ("secondary", "external", "hdmi", "display1", "tv")
            )
            video_hint = any(
                keyword in lower
                for keyword in ("video", "media", "surfaceview", "textureview")
            )
            package_hint = self.package_name in surface or pkg_key in surface
            if display_hint and (video_hint or package_hint):
                add(surface)
            elif video_hint or package_hint:
                add(surface)
            elif not display_scoped and surface.strip().startswith("#"):
                # Some Rockchip builds expose latency only on an anonymous
                # producer layer such as "#2", not on its SurfaceView wrapper.
                add_fallback(surface)
        return candidates + fallback_candidates

    def _parse_surface_latency(
        self,
        output: str,
        since_timestamp_ns: int = 0,
    ) -> Dict:
        frame_times = []
        for line in (output or "").strip().splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            try:
                timestamp = int(parts[1])
            except (TypeError, ValueError):
                continue
            if timestamp > 0:
                frame_times.append(timestamp)

        frame_times = frame_times[-120:]
        all_intervals_ms = []
        for index in range(1, len(frame_times)):
            delta = frame_times[index] - frame_times[index - 1]
            if delta > 0:
                all_intervals_ms.append(delta / 1_000_000.0)

        fps_intervals = [value for value in all_intervals_ms if 5.0 <= value <= 1000.0]
        fps = 0.0
        if fps_intervals:
            median_ms = statistics.median(fps_intervals)
            if median_ms > 0:
                fps = min(120.0, 1000.0 / median_ms)

        new_frame_times = [
            timestamp for timestamp in frame_times
            if timestamp > since_timestamp_ns
        ]
        event_intervals_ms = []
        if since_timestamp_ns > 0 and new_frame_times:
            sequence = [since_timestamp_ns] + new_frame_times
            for index in range(1, len(sequence)):
                delta = sequence[index] - sequence[index - 1]
                if delta > 0:
                    event_intervals_ms.append(delta / 1_000_000.0)

        p95 = 0.0
        if event_intervals_ms:
            ordered = sorted(event_intervals_ms)
            index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95)))
            p95 = ordered[index]

        return {
            "fps": round(fps, 2) if fps > 0 else 0.0,
            "max_frame_gap_ms": (
                round(max(event_intervals_ms), 2)
                if event_intervals_ms else 0.0
            ),
            "p95_frame_gap_ms": round(p95, 2),
            "frame_count": len(frame_times),
            "latest_frame_timestamp_ns": frame_times[-1] if frame_times else 0,
        }
    
    def _get_fps_from_gfxinfo(self) -> float:
        """
        通过 gfxinfo 获取FPS（主要方法，通用性好）
        V2.3.2: 智能处理KTV双进程架构
        - 优先尝试主进程（负责UI渲染和视频显示）
        - 如果主进程数据不足，尝试媒体进程
        - 自动选择帧数据最丰富的进程
        """
        try:
            # V2.3.2: 智能进程选择策略
            candidates = []
            
            # 候选1: 主进程（去掉:后缀）
            main_pkg = self.package_name.split(':')[0]
            candidates.append(("main", main_pkg))
            
            # 候选2: 如果配置的是媒体进程，也尝试原始配置
            if ':' in self.package_name:
                candidates.append(("media", self.package_name))
            
            best_fps = 0.0
            best_source = "none"
            
            for source_type, pkg_name in candidates:
                try:
                    framestats_fps = self._get_fps_from_framestats(pkg_name)
                    if framestats_fps > 0 and framestats_fps < 120:
                        logger.debug("[FPS] %s process (%s): framestats %.1ffps", source_type, pkg_name, framestats_fps)
                        if framestats_fps > best_fps:
                            best_fps = framestats_fps
                            best_source = f"{source_type}({pkg_name})/framestats"
                        continue

                    # 获取该进程的gfxinfo数据
                    gfx_output = self.adb._run_command(
                        ["shell", "dumpsys", "gfxinfo", pkg_name], 
                        timeout=3
                    )
                    if not gfx_output or "Error:" in gfx_output:
                        continue
                    
                    # 检查数据质量（总帧数）
                    total_frames = 0
                    for line in gfx_output.splitlines():
                        if "Total frames rendered:" in line:
                            parts = line.split(":")
                            if len(parts) >= 2:
                                try:
                                    total_frames = int(parts[1].strip().split()[0])
                                    break
                                except (ValueError, IndexError):
                                    pass
                    
                    # 如果帧数太少（<10），跳过这个进程
                    if total_frames < 10:
                        logger.debug("[FPS] %s process (%s): 帧数不足(%s), 跳过", source_type, pkg_name, total_frames)
                        continue
                    
                    # 尝试解析FPS
                    fps = self._parse_gfxinfo_fps(gfx_output)
                    if fps > 0 and fps < 120:
                        logger.debug("[FPS] %s process (%s): %.1ffps (帧数:%s)", source_type, pkg_name, fps, total_frames)
                        if fps > best_fps:
                            best_fps = fps
                            best_source = f"{source_type}({pkg_name})"
                    
                except Exception as e:
                    logger.debug("[FPS] %s process (%s) 获取失败: %s", source_type, pkg_name, e)
                    continue
            
            if best_fps > 0:
                logger.debug("[FPS] 最佳数据源: %s, FPS: %.1f", best_source, best_fps)
                return best_fps
            
            # 如果所有进程都失败，尝试传统方法（向后兼容）
            real_pkg = self.package_name.split(':')[0]
            gfx_info = self.adb.get_gfx_info(real_pkg)
            gfx_output = self.adb._run_command(
                ["shell", "dumpsys", "gfxinfo", real_pkg], 
                timeout=3
            )
            if not gfx_output or "Error:" in gfx_output:
                return 0.0
            
            # 传统方法的后续处理
            fps = self._parse_gfxinfo_fps(gfx_output)
            if fps > 0 and fps < 120:
                return fps
            
            # 方法2: 如果详细解析失败，尝试从统计信息估算（传统方法）
            stats_time_ms = 0
            total_frames = 0
            
            for line in gfx_output.splitlines():
                if "Stats since:" in line or "since" in line.lower():
                    import re
                    match = re.search(r'(\d+)\s*ms', line)
                    if match:
                        stats_time_ms = int(match.group(1))
                        break
                if "Total frames rendered:" in line and total_frames == 0:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        try:
                            total_frames = int(parts[1].strip().split()[0])
                        except (ValueError, IndexError):
                            pass
            
            # 如果找到了时间窗口和总帧数，计算FPS
            if stats_time_ms > 0 and total_frames > 0:
                if stats_time_ms >= 1000:  # 至少1秒的数据
                    fps_estimate = (total_frames * 1000.0) / stats_time_ms
                    if 0 < fps_estimate < 120:
                        logger.debug("[FPS] 传统方法估算: %.1ffps (帧数:%s, 时间:%sms)", fps_estimate, total_frames, stats_time_ms)
                        return round(fps_estimate, 2)
            
            logger.debug("[FPS] 所有方法都失败，无法获取FPS数据")
            return 0.0
        except Exception as e:
            logger.debug("[FPS] _get_fps_from_gfxinfo 异常: %s", e)
            return 0.0

    def _get_fps_from_framestats(self, pkg_name: str) -> float:
        try:
            output = self.adb._run_command(
                ["shell", "dumpsys", "gfxinfo", pkg_name, "framestats"],
                timeout=3
            )
            if not output or "Error:" in output:
                return 0.0

            lines = output.splitlines()
            header_idx = -1
            header_cols = []
            for i, line in enumerate(lines):
                if "IntendedVsync" in line and "FrameCompleted" in line and "," in line:
                    header_idx = i
                    header_cols = [c.strip() for c in line.strip().split(",")]
                    break

            if header_idx < 0 or not header_cols:
                return 0.0

            try:
                frame_completed_idx = header_cols.index("FrameCompleted")
            except ValueError:
                return 0.0

            timestamps_ns: List[int] = []
            for line in lines[header_idx + 1:]:
                s = line.strip()
                if not s or s.startswith("---") or "Flags" in s:
                    continue
                parts = [p.strip() for p in s.split(",")]
                if len(parts) <= frame_completed_idx:
                    continue
                try:
                    v = int(parts[frame_completed_idx])
                except (ValueError, TypeError):
                    continue
                if v <= 0:
                    continue
                timestamps_ns.append(v)

            if len(timestamps_ns) < 8:
                return 0.0

            timestamps_ns = timestamps_ns[-120:]
            intervals_ms = []
            for i in range(1, len(timestamps_ns)):
                dt_ns = timestamps_ns[i] - timestamps_ns[i - 1]
                if dt_ns <= 0:
                    continue
                dt_ms = dt_ns / 1_000_000.0
                if 5.0 <= dt_ms <= 200.0:
                    intervals_ms.append(dt_ms)

            if len(intervals_ms) < 6:
                return 0.0

            base_ms = statistics.median(intervals_ms)
            if base_ms <= 0:
                return 0.0
            fps = 1000.0 / base_ms
            if 0 < fps < 120:
                return round(fps, 2)
            return 0.0
        except Exception:
            return 0.0
    
    def _parse_gfxinfo_fps(self, gfxinfo_output: str) -> float:
        """
        从 gfxinfo 输出中解析FPS（增强版）
        支持多种gfxinfo格式，提高解析成功率
        """
        try:
            lines = gfxinfo_output.splitlines()
            frame_times = []  # 存储每帧的时间
            start_parsing = False
            
            # 方法1: 解析 "Profile data in ms" 格式（最详细的帧时间数据）
            for i, line in enumerate(lines):
                if "Profile data in ms" in line or "PROFILE" in line.upper():
                    start_parsing = True
                    continue
                if "View hierarchy:" in line or "Janky frames:" in line or "---PROFILE---" in line:
                    if start_parsing:
                        break  # 数据结束
                    continue
                if start_parsing and line.strip():
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            # 解析帧时间（Draw, Prepare, Process, Execute）
                            vals = []
                            for part in parts:
                                # 移除可能的非数字字符，尝试转换
                                cleaned = part.strip().replace(',', '')
                                try:
                                    val = float(cleaned)
                                    # 合理的帧时间范围（0.1-200ms，超过200ms明显异常）
                                    if 0.1 <= val <= 200:
                                        vals.append(val)
                                except ValueError:
                                    continue
                            
                            if len(vals) >= 2:  # 至少需要2个有效值
                                # 计算总帧时间（前4个值：Draw, Prepare, Process, Execute）
                                # 有些格式只有3个值，有些有4个
                                frame_time = sum(vals[:min(4, len(vals))])
                                if 2.0 <= frame_time <= 200.0:  # 合理范围：5fps-500fps
                                    frame_times.append(frame_time)
                        except (ValueError, IndexError, TypeError):
                            continue
            
            # 方法2: 如果方法1没有数据，尝试查找统计信息中的FPS
            if not frame_times:
                for line in lines:
                    # 查找 "60.XX fps" 或 "FPS: XX" 等格式
                    import re
                    # 匹配 "XX fps" 或 "XX FPS"
                    fps_match = re.search(r'(\d+\.?\d*)\s*fps', line, re.IGNORECASE)
                    if fps_match:
                        try:
                            fps_val = float(fps_match.group(1))
                            if 0 < fps_val < 120:
                                return round(fps_val, 2)
                        except (ValueError, IndexError):
                            pass
            
            # 方法3: 如果还没有数据，尝试通过总帧数和时间窗口计算
            if not frame_times:
                total_frames = 0
                stats_duration_ms = 0
                
                for line in lines:
                    # 查找总帧数
                    if "Total frames rendered:" in line:
                        parts = line.split(":")
                        if len(parts) >= 2:
                            try:
                                total_frames = int(parts[1].strip().split()[0])
                            except (ValueError, IndexError):
                                pass
                    
                    # 查找统计时间窗口
                    if "Stats since:" in line or "since" in line.lower():
                        import re
                        # V2.3.2: 支持纳秒格式解析
                        # 提取纳秒数 (如: 109685320297ns)
                        ns_match = re.search(r'(\d+)\s*ns', line)
                        if ns_match:
                            stats_duration_ns = int(ns_match.group(1))
                            stats_duration_ms = stats_duration_ns // 1_000_000  # 转换为毫秒
                        else:
                            # 提取毫秒数
                            ms_match = re.search(r'(\d+)\s*ms', line)
                            if ms_match:
                                stats_duration_ms = int(ms_match.group(1))
                            else:
                                # 也尝试秒数
                                sec_match = re.search(r'(\d+\.?\d*)\s*s(ec)?', line, re.IGNORECASE)
                                if sec_match and stats_duration_ms == 0:
                                    stats_duration_ms = int(float(sec_match.group(1)) * 1000)
                
                # 如果找到总帧数和时间窗口，计算FPS
                if total_frames > 0 and stats_duration_ms >= 1000:  # 至少1秒的数据
                    fps_estimate = (total_frames * 1000.0) / stats_duration_ms
                    if 0 < fps_estimate < 120:
                        return round(fps_estimate, 2)
            
            # 计算FPS（从帧时间数据）
            if len(frame_times) > 0:
                # 过滤明显异常的值
                valid_times = [t for t in frame_times if 5.0 <= t <= 200.0]  # 5ms-200ms，对应5fps-200fps
                
                if len(valid_times) >= 5:  # 至少需要5帧数据才可靠
                    # 使用中位数（更抗异常值）
                    sorted_times = sorted(valid_times)
                    mid = len(sorted_times) // 2
                    median_time = sorted_times[mid] if len(sorted_times) % 2 == 1 else \
                                  (sorted_times[mid-1] + sorted_times[mid]) / 2
                    
                    if median_time > 0:
                        fps = 1000.0 / median_time
                        if 0 < fps < 120:
                            return round(fps, 2)
                
                # 如果中位数方法失败或数据不足，使用平均值
                if len(valid_times) >= 3:
                    avg_frame_time = sum(valid_times) / len(valid_times)
                    if avg_frame_time > 0:
                        fps = 1000.0 / avg_frame_time
                        if 0 < fps < 120:
                            return round(fps, 2)
            
            return 0.0
        except Exception as e:
            # 添加调试信息
            import sys
            if hasattr(sys, '_getframe'):
                logger.debug("_parse_gfxinfo_fps 失败: %s", e)
        return 0.0
