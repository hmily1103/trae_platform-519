from datetime import datetime
from typing import Dict, List
import time
import logging
from .adb_manager import AdbManager

logger = logging.getLogger(__name__)
from .evaluator import PlayStateEvaluator
from .rk_monitor import RkMonitor

class PerformanceMonitor:
    def __init__(self, adb: AdbManager, package_name: str):
        self.adb = adb
        self.package_name = package_name
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
        
        # V2.3: 电视端 Display ID 自动检测
        self._tv_display_id = None  # 缓存的电视端 Display ID
        self._tv_display_id_cache_time = 0
        self._tv_display_id_cache_ttl = 300.0  # 缓存5分钟（Display ID 不会频繁变化）
        
        # V2.3.1: 零干扰模式配置
        self._disable_fps = False  # 是否禁用FPS采集（极低功耗模式）
        self._disable_screenshot = False  # 是否禁用截图（由runner控制）

    def set_log_stutter_count(self, count):
        self.log_stutter_count = count

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
            gpu_usage = self._measure_gpu_usage()

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
            # 阈值：5帧 Jank 或 有新的卡顿日志
            if delta_jank >= 5 or delta_log_stutter > 0:
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
            if not hasattr(self, '_disable_fps') or not self._disable_fps:
                self._detect_video_layer()
                try:
                    # V2.3: 自动检测电视端 Display ID，优先获取电视端的 FPS
                    tv_display_id = self._detect_tv_display_id()
                    if tv_display_id is not None:
                        video_fps = self._measure_video_fps(display_id=tv_display_id, mpp_stats=mpp_stats)
                    else:
                        video_fps = 0.0
                    
                    # 如果电视端获取失败，回退到默认（Display 0）
                    if video_fps == 0.0:
                        video_fps = self._measure_video_fps(display_id=0, mpp_stats=mpp_stats)
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
            
            snapshot = {
                "timestamp": timestamp,
                "pid": current_pid if current_pid else -1,
                "status": status,
                "pss_mb": mem_info.get("pss_mb", 0),
                "cpu_percent": cpu_usage,
                "gpu_percent": gpu_usage,
                "mpp_active": mpp_stats.get("active_instances", 0),
                "mpp_sessions": mpp_stats.get("session_count", 0),
                "mpp_work_count": mpp_stats.get("total_work_count", 0),  # V2.3: 解码器工作计数
                "mpp_work_count_delta": mpp_stats.get("work_count_delta", 0),  # V2.3: 增量
                "decoder_stuck": mpp_stats.get("decoder_stuck", False),  # V2.3: 解码器卡死
                "tv_stutter_detected": mpp_stats.get("tv_stutter_detected", False),  # V2.3: 电视端卡顿
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
                "top_consumers": top_consumers # V2.1 (Heavy Process)
            }
            
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
        decoder_stuck_count = sum(1 for h in self.history if h.get("decoder_stuck", False))
        tv_stutter_count = sum(1 for h in self.history if h.get("tv_stutter_detected", False))
        # 从事件中统计 TV_FREEZE 事件（需要遍历所有会话的事件）
        tv_freeze_count = 0
        for session in self.evaluator.session_results:
            for event in session.get("events", []):
                if "TV_FREEZE" in str(event.get("type", "")) or "TV_FREEZE" in str(event.get("description", "")):
                    tv_freeze_count += 1

        # 基础汇总
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
            if (self._video_fps_cache is not None and 
                current_time - self._video_fps_cache_time < self._video_fps_cache_ttl):
                return self._video_fps_cache
            
            fps = 0.0
            
            # 策略1: 优先使用 gfxinfo（最通用、最可靠、最快）
            # gfxinfo 获取的是应用整体帧率，对于视频播放应用，通常就是视频帧率
            try:
                fps = self._get_fps_from_gfxinfo()
                # 如果gfxinfo获取成功且合理（>0且<120），直接返回
                if fps > 0 and fps < 120:
                    self._video_fps_cache = fps
                    self._video_fps_cache_time = current_time
                    return fps
            except Exception as e:
                # 静默失败，继续尝试其他方法
                pass
            
            # 策略2: 如果gfxinfo失败或异常，尝试SurfaceFlinger（更精确但可能不支持且较慢）
            # 注意：SurfaceFlinger可能较慢，所以作为备用
            # V2.3: 针对 Display 1（电视端）使用 SurfaceFlinger
            try:
                surface_fps = self._get_fps_from_surfaceflinger(display_id=display_id)
                if surface_fps > 0 and surface_fps < 120:
                    self._video_fps_cache = surface_fps
                    self._video_fps_cache_time = current_time
                    return surface_fps
            except Exception:
                # 静默失败
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
                        return round(fps_estimate, 2)

            # 如果都失败，返回0（表示无法获取）
            self._video_fps_cache = 0.0
            self._video_fps_cache_time = current_time
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
        """
        通过 SurfaceFlinger 获取硬件层视频帧率（精确但可能不支持）
        注意：此方法可能较慢，已设置超时避免阻塞
        
        Args:
            display_id: Display ID (0=主屏, 1=电视屏, 默认0)
        """
        try:
            # V2.3: 针对 Display 1（电视端）的优化查询
            if display_id == 1:
                # 尝试直接查询 Display 1 的 Surface 列表
                result_output = self.adb._run_command(
                    ["shell", "dumpsys", "SurfaceFlinger", "--display-id", "1", "--list"], 
                    timeout=2
                )
                # 如果 --display-id 不支持，回退到默认方式
                if not result_output or "Error:" in result_output or "Unknown" in result_output:
                    result_output = self.adb._run_command(["shell", "dumpsys", "SurfaceFlinger", "--list"], timeout=2)
            else:
                # Display 0 或默认方式
                result_output = self.adb._run_command(["shell", "dumpsys", "SurfaceFlinger", "--list"], timeout=2)
            if not result_output or "Error:" in result_output:
                return 0.0
            
            # 2. 查找视频相关的Surface
            surfaces = result_output.splitlines()
            video_surface = None
            
            # V2.3: 针对电视端 Display 的优化查找（自动检测的 Display ID）
            if display_id > 0:  # 电视端 Display（1 或 2）
                # 优先级1: 查找副屏/外部显示相关的Surface
                # 通常包含: SecondaryDisplay, ExternalRoot, HDMI, Display1 等关键字
                for surface in surfaces:
                    surface_lower = surface.lower()
                    if any(keyword in surface_lower for keyword in ['secondary', 'external', 'hdmi', 'display1', 'tv']):
                        # 进一步确认：包含包名或视频相关关键字
                        if self.package_name in surface or 'video' in surface_lower or 'media' in surface_lower:
                            parts = surface.split()
                            if parts:
                                video_surface = parts[0].strip()
                                break
                
                # 优先级2: 如果没找到副屏专用Surface，查找包含包名的Surface
                if not video_surface:
                    for surface in surfaces:
                        if self.package_name in surface:
                            parts = surface.split()
                            if parts:
                                video_surface = parts[0].strip()
                                break
            
            # 默认查找逻辑（Display 0 或通用）
            if not video_surface:
                # 优先级1: 包含包名的Surface
                for surface in surfaces:
                    if self.package_name in surface:
                        parts = surface.split()
                        if parts:
                            video_surface = parts[0].strip()
                            break
                
                # 优先级2: 包含Video/Media关键字的Surface
                if not video_surface:
                    for surface in surfaces:
                        surface_lower = surface.lower()
                        if 'video' in surface_lower or 'media' in surface_lower:
                            parts = surface.split()
                            if parts:
                                video_surface = parts[0].strip()
                                break
                
                # 优先级3: SurfaceView/TextureView
                if not video_surface:
                    for surface in surfaces:
                        if 'SurfaceView' in surface or 'TextureView' in surface:
                            parts = surface.split()
                            if parts:
                                video_surface = parts[0].strip()
                                break
            
            if not video_surface:
                return 0.0
            
            # 3. 获取latency数据（设置较短超时，避免阻塞）
            latency_output = self.adb._run_command(
                ["shell", "dumpsys", "SurfaceFlinger", "--latency", video_surface], 
                timeout=2
            )
            if not latency_output or "Error:" in latency_output:
                return 0.0
            
            # 4. 解析latency数据
            lines = latency_output.strip().splitlines()
            if len(lines) < 3:  # 至少需要刷新周期+2帧数据
                return 0.0
            
            try:
                # 第一行是刷新周期（纳秒）
                refresh_period_ns = int(lines[0])
                
                # 后续行是帧时间戳（纳秒），格式: frame_time_ns [其他数据...]
                frame_times = []
                for line in lines[1:]:
                    if line.strip():
                        parts = line.split()
                        if parts:
                            try:
                                frame_time_ns = int(parts[0])
                                if frame_time_ns > 0:
                                    frame_times.append(frame_time_ns)
                            except (ValueError, IndexError):
                                continue
                
                if len(frame_times) < 2:
                    return 0.0
                
                # 5. 计算FPS（使用最近的有效帧）
                recent_frames = frame_times[-min(30, len(frame_times)):]  # 取最近30帧
                if len(recent_frames) < 2:
                    return 0.0
                
                # 计算帧间隔（过滤异常值）
                intervals = []
                for i in range(1, len(recent_frames)):
                    interval_ns = recent_frames[i] - recent_frames[i-1]
                    # 过滤异常间隔（应该在合理范围内：8ms-100ms，对应12.5fps-125fps）
                    if 8_000_000 <= interval_ns <= 100_000_000:  # 8ms到100ms
                        intervals.append(interval_ns)
                
                if len(intervals) < 2:
                    return 0.0
                
                # 计算平均帧间隔
                avg_interval_ns = sum(intervals) / len(intervals)
                avg_interval_ms = avg_interval_ns / 1_000_000.0
                
                # 计算FPS
                if avg_interval_ms > 0:
                    fps = 1000.0 / avg_interval_ms
                    return round(fps, 2)
                
            except (ValueError, IndexError, ZeroDivisionError):
                return 0.0
            
            return 0.0
            
        except Exception:
            return 0.0
    
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
