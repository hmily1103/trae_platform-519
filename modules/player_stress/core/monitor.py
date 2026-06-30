from datetime import datetime
from typing import Dict, List
import time
import logging
import statistics
import os
from .adb_manager import AdbManager
try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

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
        self._pid_missing_since = 0.0
        self._pid_missing_consecutive = 0
        self._pid_loss_reported = False
        
        # V2 Evaluator
        self.evaluator = PlayStateEvaluator()
        
        # Rockchip Hardware Monitor
        self.rk_monitor = RkMonitor(adb)
        self.mpp_supported = False # Will be checked on first run
        
        # 鑷姩璇嗗埆姝屾洸淇℃伅鎺у埗
        self.last_metadata_check_time = 0
        
        # 鍗￠】/涓㈠抚缁熻 (V2.1)
        self.last_gfx_info = {"total_frames": 0, "janky_frames": 0}
        self.log_stutter_count = 0 # 鏉ヨ嚜 logcat
        self.prev_log_stutter_total = 0 # 涓婁竴娆″揩鐓ф椂鐨?log_stutter 鎬绘暟
        self._recent_log_stutter_events: List[Dict] = []
        self._recent_decoder_events: List[Dict] = []
        
        # PID 鐢熷懡鍛ㄦ湡浜嬩欢 (V2.1)
        self.pid_events = []
        self.monitoring_start_time = 0

        # 瑙嗛FPS缂撳瓨锛堥伩鍏嶉绻佽皟鐢級
        self._video_fps_cache = None
        self._video_fps_cache_time = 0
        self._video_fps_cache_ttl = 2.0  # 缂撳瓨2绉?        
        # 电视端优先使用显式配置，并在运行时校验。
        self._configured_tv_display_id = int(monitor_config.get("tv_display_id", 1))
        self._auto_detect_tv_display = bool(monitor_config.get("auto_detect_tv_display", True))
        self._allow_display0_fallback = bool(monitor_config.get("allow_display0_fallback", False))
        self._tv_display_id = None  # 缂撳瓨鐨勭數瑙嗙 Display ID
        self._tv_display_id_cache_time = 0
        self._tv_display_id_cache_ttl = 300.0  # 缓存 5 分钟，Display ID 通常不会频繁变化
        self._tv_display_verified = False
        self._tv_display_verification_reason = "not_checked"
        self._tv_display_recommendation = {
            "display_id": None,
            "score": 0.0,
            "reason": "not_probed",
            "probe_count": 0,
            "fps": 0.0,
            "surface_name": "",
        }
        self._tv_display_probe_details: List[Dict] = []
        self._last_video_fps_source = "none"
        self._last_tv_surface_name = ""
        self._last_tv_surface_frame_timestamp_ns = 0
        self._tv_surface_failure_count = 0
        self._last_tv_surface_candidates: List[str] = []
        self._last_tv_latency_probe: Dict = {}
        self.tv_freeze_events: List[Dict] = []
        self.tv_stall_events: List[Dict] = []
        self.tv_stall_risk_events: List[Dict] = []
        self._observer_process = None
        self._observer_pid = os.getpid()
        if psutil is not None:
            try:
                self._observer_process = psutil.Process(self._observer_pid)
                self._observer_process.cpu_percent(None)
            except Exception:
                self._observer_process = None
        
        # V2.3.1: 零干扰模式配置
        self._disable_fps = False  # 是否禁用 FPS 采集（极低功耗模式）
        self._disable_screenshot = False  # 鏄惁绂佺敤鎴浘锛堢敱runner鎺у埗锛?
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

    @staticmethod
    def _decoder_log_has_hard_failure(events: List[Dict]) -> bool:
        if not isinstance(events, list):
            return False
        strong_keywords = (
            "error",
            "timeout",
            "timed out",
            "dequeue output timeout",
            "failed",
            "fatal",
            "hang",
            "stuck",
            "watchdog",
            "exception",
        )
        ignore_keywords = (
            "audio decoder eos",
            "reach audio decoder eos",
        )
        for item in events:
            if not isinstance(item, dict):
                continue
            line = str(item.get("line", "") or "").strip().lower()
            if not line:
                continue
            if any(keyword in line for keyword in ignore_keywords):
                continue
            if any(keyword in line for keyword in strong_keywords):
                return True
        return False

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

    def set_recent_log_stutter_events(self, events: List[Dict]):
        normalized = []
        for evt in events or []:
            if not isinstance(evt, dict):
                continue
            normalized.append({
                "time": evt.get("time"),
                "pattern": str(evt.get("pattern", "") or ""),
                "line": str(evt.get("line", "") or ""),
            })
        self._recent_log_stutter_events = normalized[-5:]

    def set_recent_decoder_events(self, events: List[Dict]):
        normalized = []
        for evt in events or []:
            if not isinstance(evt, dict):
                continue
            normalized.append({
                "time": evt.get("time"),
                "pattern": str(evt.get("pattern", "") or ""),
                "line": str(evt.get("line", "") or ""),
            })
        self._recent_decoder_events = normalized[-8:]

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
            logger.debug("鐑姸鎬侀噰闆嗗け璐? %s", e)
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
        """寮€濮嬬洃鎺э紝璁板綍鍒濆PID"""
        self.monitoring_start_time = time.time()
        pid = self.adb.get_pid(self.package_name)
        if pid:
            self.initial_pid = pid
            self.last_pid = pid
            try:
                self.adb.reset_gfx_info(self.package_name)
                self.last_gfx_info = self.adb.get_gfx_info(self.package_name)
            except Exception as e:
                logger.debug("reset_gfx_info 澶辫触: %s", e)
            logger.info("鐩戞帶寮€濮? %s, PID: %s", self.package_name, pid)
        else:
            logger.warning("鏈壘鍒拌繘绋? %s", self.package_name)

    def collect_snapshot(self, external_events: List[Dict] = None) -> Dict:
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # V2.1: 濡傛灉鏈夊閮?Log 浜嬩欢锛屼紭鍏堝崟鐙眹鎶ョ粰 Evaluator
            if external_events:
                self._apply_ignore_video_window(external_events)
                for evt in external_events:
                    # 绠€鍗曟槧灏?Log 浜嬩欢鍒?Evaluator 鍥炶皟
                    # 鍋囪 evt 鍖呭惈 type 鍜?description
                    e_type = evt.get("type", "UNKNOWN")
                    msg = evt.get("line", "") or evt.get("description", "")
                    if "CRASH" in e_type or "FATAL" in e_type:
                        self.evaluator.on_fatal_error("CRASH", msg)
                    elif "ANR" in e_type:
                        self.evaluator.on_fatal_error("ANR", msg)
                    elif e_type == "FIRST_FRAME":
                        self.evaluator.on_first_frame(source="Logcat")
            
            # 1. PID 与重启检测
            if not self.adb.is_device_online():
                logger.warning("璁惧宸茬绾匡紝璺宠繃鏈鐩戞帶閲囬泦")
                return {}

            current_pid = self.adb.get_pid(self.package_name)
            is_restarted = False
            
            # 璁＄畻鐩稿鏃堕棿 (鍒嗛挓)
            elapsed_min = 0
            if self.monitoring_start_time > 0:
                elapsed_min = int((time.time() - self.monitoring_start_time) / 60)
            
            if current_pid is None:
                # 杩涚▼娑堝け
                status = "DEAD"
                self._pid_missing_consecutive += 1
                if self._pid_missing_since <= 0:
                    self._pid_missing_since = time.time()
                if self.last_pid is not None and not self._pid_loss_reported:
                    # 璁板綍 PID 涓㈠け浜嬩欢
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
                    self._pid_loss_reported = True
                    
            elif self.last_pid is not None and current_pid != self.last_pid:
                # PID 鍙樺寲锛屽垽瀹氫负閲嶅惎
                self.restart_count += 1
                is_restarted = True
                status = "RESTARTED"
                
                # 璁板綍 PID 閲嶅惎浜嬩欢
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
                self._pid_missing_since = 0.0
                self._pid_missing_consecutive = 0
                self._pid_loss_reported = False
                try:
                    self.adb.reset_gfx_info(self.package_name)
                    self.last_gfx_info = self.adb.get_gfx_info(self.package_name)
                except Exception as e:
                    logger.debug("PID 閲嶅惎鍚?reset_gfx_info 澶辫触: %s", e)
            else:
                status = "RUNNING"
                if self._pid_loss_reported:
                    self.pid_events.append({
                        "timestamp": timestamp,
                        "type": "PID_RECOVERED",
                        "new_pid": current_pid,
                        "elapsed_min": elapsed_min,
                        "description": f"Process recovered (PID: {current_pid})",
                    })
                self._pid_missing_since = 0.0
                self._pid_missing_consecutive = 0
                self._pid_loss_reported = False
                if self.last_pid is None:  # 之前未找到，现在恢复
                    self.last_pid = current_pid
                    # 棣栨鍙戠幇 PID锛屼笉绠楅噸鍚紝绠楀垵濮嬪寲
                    if not self.pid_events:
                        self.pid_events.append({
                            "timestamp": timestamp,
                            "type": "PID_FOUND",
                            "new_pid": current_pid,
                            "elapsed_min": elapsed_min,
                            "description": f"Process found (PID: {current_pid})"
                        })
                        # Evaluator 涓嶉渶瑕?FOUND 浜嬩欢锛屽畠鍙叧蹇?RESTART/LOST


            # 2. 鎬ц兘鏁版嵁
            mem_info = self.adb.get_memory_info(self.package_name)
            cpu_usage = self.adb.get_cpu_usage(self.package_name)
            try:
                system_cpu_usage = float(self.adb.get_system_cpu_usage() or 0.0)
            except (TypeError, ValueError):
                system_cpu_usage = 0.0
            gpu_usage = self._measure_gpu_usage()
            thermal_status = self._get_cached_thermal_status()

            # 3. 鎾斁鐘舵€佸垽瀹?(V2 鏂板 - Evaluator bypass)
            is_audio_active = self.adb.is_audio_active()
            
            # Audio active can also be treated as a first-frame hint.
            if is_audio_active:
                self.evaluator.on_first_frame("Audio")
            
            # 绉婚櫎 Monitor 涓诲姩璋冪敤 process_event("POLLING")
            # 绉婚櫎鑷姩璇嗗埆姝屾洸閫昏緫 (Evaluator 涓嶅啀缁存姢 song_info)
            
            play_state = "RUNNING" # 榛樿涓鸿繍琛屼腑锛孧onitor 涓嶅啀鎺ㄦ柇鐘舵€?
            # 4. 鍗￠】妫€娴?(V2.1 鏂板)
            # A. GFX Info
            gfx_info = self.adb.get_gfx_info(self.package_name)
            
            # 璁＄畻澧為噺
            delta_total = max(0, gfx_info['total_frames'] - self.last_gfx_info['total_frames'])
            delta_jank = max(0, gfx_info['janky_frames'] - self.last_gfx_info['janky_frames'])
            
            jank_percent = 0.0
            if delta_total > 0:
                jank_percent = (delta_jank / delta_total) * 100.0
                
            self.last_gfx_info = gfx_info
            
            # B. Logcat Stutter (鐢卞閮?TestRunner/GUI 鏇存柊 log_stutter_count)
            delta_log_stutter = max(0, self.log_stutter_count - self.prev_log_stutter_total)
            self.prev_log_stutter_total = self.log_stutter_count
            
            # C. 璧勬簮鍗犵敤鍒嗘瀽 (褰撳彂鐢熷崱椤?Jank 鏃?
            top_consumers = ""
            system_cpu_pressure = bool(
                is_audio_active and system_cpu_usage >= 85.0
            )
            # Pure CPU contention may not emit player logs or UI jank.
            if delta_jank >= 5 or delta_log_stutter > 0 or system_cpu_pressure:
                top_consumers = self.adb.get_top_heavy_processes()

            # 2.1 MPP 纭欢瑙ｇ爜鐘舵€?(RK3576)
            # print("Debug: Checking MPP...")
            mpp_stats = {}
            # First run: probe whether RK monitor is supported.
            if self.rk_monitor.is_supported is None:
                self.rk_monitor.check_support()
            
            if self.rk_monitor.is_supported:
                mpp_stats = self.rk_monitor.get_mpp_stats()
                # 鑷姩鍒ゅ畾閫昏緫: 鍋囨挱鏀炬娴?(False Playback)
                # 濡傛灉闊抽娲昏穬涓?MPP 瀹炰緥涓?0 -> 鍙兘杞В
                if is_audio_active and mpp_stats.get('active_instances', 0) == 0:
                    mpp_stats['warning'] = "Potential Soft Decoding"
                
                mpp_stats['tv_stutter_detected'] = False
                mpp_stats['tv_stutter_reason'] = ""
            # print("Debug: MPP Checked. Snapshot done.")
            # 5. 视频 FPS 检查。异常要被吞掉，避免影响整次快照采集。
            # V2.3.1: 支持零干扰模式，可通过配置禁用 FPS 采集。
            video_fps = 0.0
            video_fps_source = "none"
            tv_display_id = self._detect_tv_display_id()
            recommendation = dict(self._tv_display_recommendation or {})
            if not hasattr(self, '_disable_fps') or not self._disable_fps:
                self._detect_video_layer()
                try:
                    if tv_display_id is not None:
                        video_fps = self._measure_video_fps(display_id=tv_display_id, mpp_stats=mpp_stats)
                    else:
                        video_fps = 0.0

                    if (
                        video_fps == 0.0
                        and self._auto_detect_tv_display
                        and recommendation.get("display_id") is not None
                        and int(recommendation.get("display_id")) != int(tv_display_id or -1)
                        and float(recommendation.get("score", 0) or 0.0) > 0.0
                    ):
                        candidate_display_id = int(recommendation.get("display_id"))
                        candidate_fps = self._measure_video_fps(
                            display_id=candidate_display_id,
                            mpp_stats=mpp_stats,
                        )
                        if candidate_fps > 0:
                            tv_display_id = candidate_display_id
                            self._tv_display_id = candidate_display_id
                            self._tv_display_verified = True
                            self._tv_display_verification_reason = "auto_switched_by_video_probe"
                            video_fps = candidate_fps

                    if video_fps == 0.0 and self._allow_display0_fallback:
                        video_fps = self._measure_video_fps(display_id=0, mpp_stats=mpp_stats)

                    if video_fps and video_fps > 0:
                        video_fps_source = self._last_video_fps_source
                except Exception as e:
                    # V2.3.2: 鏇村弸濂界殑閿欒鎻愮ず
                    if not hasattr(self, '_fps_error_count'):
                        self._fps_error_count = 0
                    
                    self._fps_error_count += 1
                    
                    # 姣?娆″け璐ユ彁绀轰竴娆★紝閬垮厤鏃ュ織鍒峰睆
                    if self._fps_error_count % 5 == 1:
                        logger.warning(
                            "杩炵画澶氭鏈兘閲囬泦鍒版湁鏁堣棰慒PS鏁版嵁銆傚彲鑳藉師鍥? 1)瑙嗛鏈紑濮嬫挱鏀?2)搴旂敤鏈湪鍓嶅彴 "
                            "3)鏈哄瀷涓嶆敮鎸?gfxinfo銆傚寘鍚? %s", self.package_name
                        )
                    
                    video_fps = 0.0

            mpp_dt = float(mpp_stats.get("work_count_delta_time_sec", 0) or 0.0)
            mpp_delta = int(mpp_stats.get("work_count_delta", 0) or 0)
            mpp_work_count_reliable = bool(
                mpp_stats.get("work_count_reliable", False)
            )
            decode_fps_estimate = 0.0
            decode_drop_estimate = 0
            decode_drop_ratio = 0.0
            decode_slowdown_detected = False
            fps_unavailable_reason = ""
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

                if mpp_work_count_reliable:
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
                fps_unavailable_reason = str(
                    self._ignore_video_metrics_reason or "当前窗口已忽略视频指标"
                )
                if mpp_stats:
                    mpp_stats["decoder_stuck"] = False
                    mpp_stats["decoder_stuck_duration_sec"] = 0.0
                    mpp_stats["tv_stutter_detected"] = False

            if (
                (video_fps is None or float(video_fps) <= 0)
                and mpp_work_count_reliable
                and mpp_dt > 0
                and mpp_delta == 0
                and int(mpp_stats.get("active_instances", 0) or 0) > 0
            ):
                video_fps = 0.0
                video_fps_source = (
                    "mpp_stalled"
                    if bool(mpp_stats.get("decoder_stuck", False))
                    else "mpp_zero"
                )

            if not fps_unavailable_reason and (video_fps is None or float(video_fps) <= 0):
                if getattr(self, "_disable_fps", False):
                    fps_unavailable_reason = "当前模式未采集 FPS"
                elif not bool(self._tv_display_verified):
                    fps_unavailable_reason = "电视端 Display 未验证"
                elif (
                    str((self._last_tv_latency_probe or {}).get("probe_reason", "") or "")
                    == "latency_zero_frames"
                ):
                    fps_unavailable_reason = "SurfaceFlinger latency 未返回有效帧时间，当前板子不支持该 FPS 通道"
                elif bool(mpp_stats.get("decoder_stuck", False)):
                    fps_unavailable_reason = "解码输出停顿，MPP work_count 未增长"
                elif (
                    mpp_work_count_reliable
                    and mpp_dt > 0
                    and mpp_delta == 0
                    and int(mpp_stats.get("active_instances", 0) or 0) > 0
                ):
                    fps_unavailable_reason = "MPP 未产出新帧，无法估算 FPS"
                elif not self._last_tv_surface_name:
                    fps_unavailable_reason = "未识别到电视端视频 Surface"
                elif str(video_fps_source or "none") == "none":
                    fps_unavailable_reason = "未获取到有效电视端帧率数据"

            if decode_slowdown_detected and not top_consumers:
                top_consumers = self.adb.get_top_heavy_processes()

            decoder_diagnostics = {}
            if mpp_stats.get("decoder_stuck", False):
                try:
                    decoder_diagnostics = self.adb.get_decoder_diagnostics(
                        self.package_name
                    )
                except Exception:
                    decoder_diagnostics = {}
            decoder_log_events = list(self._recent_decoder_events)
            surface_locked = bool(self._last_tv_surface_name)
            decoder_stuck = bool(mpp_stats.get("decoder_stuck", False))
            decoder_hard_failure = self._decoder_log_has_hard_failure(
                decoder_log_events
            )
            decoder_stuck_confirmed = bool(
                decoder_stuck and (surface_locked or decoder_hard_failure)
            )
            decoder_stuck_risk = bool(
                decoder_stuck and not decoder_stuck_confirmed
            )
            mpp_stats["tv_stutter_detected"] = bool(decoder_stuck_confirmed)
            if decoder_stuck_confirmed:
                mpp_stats["tv_stutter_reason"] = (
                    f"Decoder stuck (work_count unchanged for {mpp_stats.get('decoder_stuck_duration_sec', 0):.1f}s)"
                )
            else:
                mpp_stats["tv_stutter_reason"] = ""
            observer_metrics = self._collect_observer_metrics()
            
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
                "mpp_work_count": mpp_stats.get("total_work_count", 0),  # V2.3: 瑙ｇ爜鍣ㄥ伐浣滆鏁?                "mpp_work_count_delta": mpp_stats.get("work_count_delta", 0),  # V2.3: 澧為噺
                "mpp_work_count_delta_time_sec": mpp_stats.get("work_count_delta_time_sec", 0.0),
                "mpp_work_count_reliable": bool(mpp_work_count_reliable),
                "decoder_stuck": mpp_stats.get("decoder_stuck", False),  # V2.3: 瑙ｇ爜鍣ㄥ崱姝?
                "decoder_stuck_confirmed": bool(decoder_stuck_confirmed),
                "decoder_stuck_risk": bool(decoder_stuck_risk),
                "decoder_stuck_duration_sec": mpp_stats.get("decoder_stuck_duration_sec", 0.0),
                "tv_stutter_detected": mpp_stats.get("tv_stutter_detected", False),  # V2.3: 鐢佃绔崱椤?
                "decode_fps_estimate": round(decode_fps_estimate, 2) if decode_fps_estimate > 0 else 0.0,
                "decode_slowdown_detected": bool(decode_slowdown_detected),
                "decode_slowdown_ratio": round(self._decode_slowdown_ratio, 2),
                "decode_drop_estimate": decode_drop_estimate,
                "decode_drop_ratio": round(decode_drop_ratio, 4) if decode_drop_ratio > 0 else 0.0,
                "video_fps_source": video_fps_source,
                "fps_unavailable_reason": fps_unavailable_reason,
                "tv_display_id": tv_display_id,
                "tv_display_verified": bool(self._tv_display_verified),
                "tv_display_verification_reason": self._tv_display_verification_reason,
                "tv_display_recommendation": dict(self._tv_display_recommendation),
                "tv_display_probe_details": list(self._tv_display_probe_details),
                "tv_surface_name": self._last_tv_surface_name,
                "tv_surface_candidates": list(self._last_tv_surface_candidates),
                "tv_latency_probe": dict(self._last_tv_latency_probe),
                "expected_stream_fps": round(float(self._expected_stream_fps), 2) if float(self._expected_stream_fps) > 0 else 0.0,
                "thermal_available": bool(thermal_status.get("available", False)),
                "max_temperature_c": float(thermal_status.get("max_temperature_c", 0) or 0.0),
                "min_cpu_frequency_ratio": float(thermal_status.get("min_frequency_ratio", 0) or 0.0),
                "thermal_throttling": bool(thermal_status.get("thermal_throttling", False)),
                "ignore_video_metrics": bool(ignore_video),
                "ignore_video_reason": self._ignore_video_metrics_reason,
                "restart_count": self.restart_count,
                "is_restarted": is_restarted,
                "target_process_available": current_pid is not None,
                "sample_valid": current_pid is not None,
                "pid_missing_consecutive": self._pid_missing_consecutive,
                "pid_missing_duration_sec": round(
                    time.time() - self._pid_missing_since, 2
                ) if self._pid_missing_since > 0 else 0.0,
                "audio_active": is_audio_active, # V2
                "play_state": play_state,        # V2
                "video_fps": video_fps,          # V2.2 Real Video FPS
                "video_fps_collected": str(video_fps_source or "none") != "none",
                "observer_pid": observer_metrics.get("pid", self._observer_pid),
                "observer_cpu_percent": observer_metrics.get("cpu_percent", 0.0),
                "observer_memory_mb": observer_metrics.get("memory_mb", 0.0),
                "observer_sampling_mode": observer_metrics.get("sampling_mode", "unknown"),
                "gfx_jank_count": delta_jank,    # V2.1 (System Jank)
                "gfx_jank_percent": round(jank_percent, 2), # V2.1
                
                # V2.2: Perceptual Jank (浜虹溂鎰熺煡)
                # 绠€鍗曟ā鍨? 鍗曟閲囨牱濡傛灉涓㈠抚鐜?> 15% 涓?total_delta > 10锛屽垯璁颁负涓€娆?"鎰熺煡鍗￠】"
                # 鏇寸簿缁嗗彲浠ョ粺璁¤繛缁珮 Jank 鐨勬鏁?                "is_perceptual_jank": (jank_percent > 15.0 and delta_total > 10),
                
                "gfx_total_delta": delta_total,
                "log_stutter_count": self.log_stutter_count, # V2.1 (Total)
                "log_stutter_delta": delta_log_stutter,
                "log_stutter_events": list(self._recent_log_stutter_events),
                "decoder_log_events": decoder_log_events,
                "decoder_diagnostics": decoder_diagnostics,
                "top_consumers": top_consumers, # V2.1 (Heavy Process)
                "root_cause_type": "",
                "suspect_process": "",
                "root_cause_confidence": 0.0,
            }

            if self.root_cause_analyzer:
                try:
                    root_cause_triggered = bool(
                        current_pid is not None
                        and (
                            delta_jank >= 5
                            or delta_log_stutter > 0
                            or snapshot.get("is_perceptual_jank", False)
                            or snapshot.get("decoder_stuck", False)
                            or snapshot.get("decode_slowdown_detected", False)
                            or snapshot.get("system_cpu_pressure", False)
                        )
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
            logger.exception("collect_snapshot 涓ラ噸閿欒: %s", e)
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
                "decoder_stuck_confirmed": False,
                "decoder_stuck_risk": False,
                "decoder_stuck_duration_sec": 0.0,
                "decode_fps_estimate": 0.0,
                "decode_drop_estimate": 0,
                "decode_drop_ratio": 0.0,
                "fps_unavailable_reason": "鐩戞帶閲囨牱寮傚父",
                "tv_surface_candidates": list(self._last_tv_surface_candidates),
                "tv_latency_probe": dict(self._last_tv_latency_probe),
                "log_stutter_count": 0,
                "top_consumers": ""
            }

    def _build_decoder_stuck_summary(self, valid_history: List[Dict]) -> Dict:
        stuck_samples = [
            h for h in (valid_history or [])
            if not h.get("ignore_video_metrics", False)
            and bool(h.get("decoder_stuck", False))
        ]
        if not stuck_samples:
            return {
                "count": 0,
                "max_duration_sec": 0.0,
                "decoder_name": "",
                "decoder_names": [],
                "sample_timestamp": "",
                "video_fps": 0.0,
                "decode_fps_estimate": 0.0,
                "expected_stream_fps": 0.0,
                "decode_drop_ratio": 0.0,
                "system_cpu_percent": 0.0,
                "player_cpu_percent": 0.0,
                "log_lines": [],
                "diagnostic_lines": [],
            }

        longest = max(
            stuck_samples,
            key=lambda item: float(item.get("decoder_stuck_duration_sec", 0) or 0.0),
        )
        strongest = max(
            stuck_samples,
            key=lambda item: (
                float(item.get("decode_drop_ratio", 0) or 0.0),
                float(item.get("decoder_stuck_duration_sec", 0) or 0.0),
            ),
        )

        decoder_names: List[str] = []
        decoder_log_lines: List[str] = []
        diagnostic_lines: List[str] = []
        for sample in stuck_samples:
            diagnostics = sample.get("decoder_diagnostics") or {}
            if isinstance(diagnostics, dict):
                name = str(diagnostics.get("decoder_name", "") or "").strip()
                if name and name not in decoder_names:
                    decoder_names.append(name)
                for line in diagnostics.get("codec_lines") or []:
                    text = str(line or "").strip()
                    if text and text not in diagnostic_lines:
                        diagnostic_lines.append(text)
            for event in sample.get("decoder_log_events") or []:
                if not isinstance(event, dict):
                    continue
                text = str(event.get("line", "") or "").strip()
                if text and text not in decoder_log_lines:
                    decoder_log_lines.append(text)

        decoder_name = ""
        diagnostics = strongest.get("decoder_diagnostics") or {}
        if isinstance(diagnostics, dict):
            decoder_name = str(diagnostics.get("decoder_name", "") or "").strip()
        if not decoder_name and decoder_names:
            decoder_name = decoder_names[0]

        return {
            "count": len(stuck_samples),
            "max_duration_sec": round(
                float(longest.get("decoder_stuck_duration_sec", 0) or 0.0), 2
            ),
            "decoder_name": decoder_name,
            "decoder_names": decoder_names[:5],
            "sample_timestamp": str(strongest.get("timestamp", "") or ""),
            "video_fps": round(float(strongest.get("video_fps", 0) or 0.0), 2),
            "decode_fps_estimate": round(
                float(strongest.get("decode_fps_estimate", 0) or 0.0), 2
            ),
            "expected_stream_fps": round(
                float(strongest.get("expected_stream_fps", 0) or 0.0), 2
            ),
            "decode_drop_ratio": round(
                float(strongest.get("decode_drop_ratio", 0) or 0.0), 4
            ),
            "system_cpu_percent": round(
                float(strongest.get("system_cpu_percent", 0) or 0.0), 2
            ),
            "player_cpu_percent": round(
                float(
                    strongest.get(
                        "player_cpu_percent",
                        strongest.get("cpu_percent", 0),
                    ) or 0.0
                ),
                2,
            ),
            "log_lines": decoder_log_lines[:5],
            "diagnostic_lines": diagnostic_lines[:5],
        }

    def analyze_degradation(self, history: List[Dict] = None) -> Dict:
        """
        V2 闀挎椂闂磋繍琛岄€€鍖栧垎鏋?        杩斿洖: 鍐呭瓨澧為暱鐜? CPU鍓嶅悗鍙樺寲
        """
        samples = self.history if history is None else history
        if len(samples) < 10:
            return {"status": "insufficient_data"}
            
        # 1. 鍐呭瓨澧為暱鏂滅巼 (绠€鍗曠殑棣栧熬瀵规瘮锛屾垨鑰呯嚎鎬у洖褰?
        # 绠€鍗曠畻娉曪細(Avg_Last_10% - Avg_First_10%) / Total_Time_Hours
        
        n = len(samples)
        sample_count = max(1, int(n * 0.1)) # 鍙?10% 鏍锋湰
        
        first_samples = samples[:sample_count]
        last_samples = samples[-sample_count:]
        
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
            logger.debug("analyze_degradation 鏃堕棿瑙ｆ瀽澶辫触: %s", e)
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
        """鑾峰彇缁熻鎽樿 (V2)"""
        if not self.history:
            return {}
            
        valid_history = [
            h for h in self.history
            if h.get(
                "sample_valid",
                str(h.get("status", "RUNNING")).upper() not in {"DEAD", "ERROR"},
            )
        ]
        invalid_samples = len(self.history) - len(valid_history)
        valid_sample_ratio = (
            len(valid_history) / len(self.history)
            if self.history else 0.0
        )
        pid_loss_events = [
            event for event in self.pid_events
            if event.get("type") == "PID_LOST"
        ]
        process_failure_summary = self._build_process_failure_summary()

        pss_values = [h["pss_mb"] for h in valid_history if h["pss_mb"] > 0]
        avg_pss = sum(pss_values) / len(pss_values) if pss_values else 0
        max_pss = max(pss_values) if pss_values else 0
        
        # 鍗￠】缁熻
        total_gfx_jank = sum(h.get("gfx_jank_count", 0) for h in valid_history)
        total_frames_delta = sum(h.get("gfx_total_delta", 0) for h in valid_history)
        if total_frames_delta > 0:
            avg_jank_percent = (total_gfx_jank / total_frames_delta) * 100.0
        else:
            avg_jank_percent = sum(h.get("gfx_jank_percent", 0) for h in valid_history) / len(valid_history) if valid_history else 0
        
        # 瑙嗛FPS缁熻锛圴2.2 - 鏈€閲嶈鐨勬寚鏍囷級
        video_fps_values = [
            float(h.get("video_fps", 0) or 0.0)
            for h in valid_history
            if (
                bool(h.get("video_fps_collected", False))
                or str(h.get("video_fps_source", "") or "none") != "none"
            )
        ]
        video_fps_source_counts = {}
        fps_unavailable_reason_counts = {}
        for item in valid_history:
            source = str(item.get("video_fps_source", "") or "none")
            video_fps_source_counts[source] = (
                video_fps_source_counts.get(source, 0) + 1
            )
            reason = str(item.get("fps_unavailable_reason", "") or "").strip()
            if reason:
                fps_unavailable_reason_counts[reason] = (
                    fps_unavailable_reason_counts.get(reason, 0) + 1
                )
        if video_fps_values:
            avg_video_fps = sum(video_fps_values) / len(video_fps_values)
            max_video_fps = max(video_fps_values)
            min_video_fps = min(video_fps_values)
        else:
            avg_video_fps = 0.0
            max_video_fps = 0.0
            min_video_fps = 0.0
        
        # GPU 使用率统计（V2.4 新增）
        gpu_values = [h.get("gpu_percent", 0) for h in valid_history if h.get("gpu_percent", 0) > 0]
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
            for h in valid_history
        ]
        system_cpu_values = [
            float(h.get("system_cpu_percent", 0) or 0.0)
            for h in valid_history
            if float(h.get("system_cpu_percent", 0) or 0.0) > 0
        ]
        temperature_values = [
            float(h.get("max_temperature_c", 0) or 0.0)
            for h in valid_history
            if float(h.get("max_temperature_c", 0) or 0.0) > 0
        ]
        frequency_ratios = [
            float(h.get("min_cpu_frequency_ratio", 0) or 0.0)
            for h in valid_history
            if float(h.get("min_cpu_frequency_ratio", 0) or 0.0) > 0
        ]
        thermal_throttling_count = sum(
            1 for h in valid_history if h.get("thermal_throttling", False)
        )
        thermal_available_count = sum(
            1 for h in valid_history if h.get("thermal_available", False)
        )
        decode_slowdown_count = sum(
            1
            for h in valid_history
            if (
                not h.get("ignore_video_metrics", False)
                and h.get("decode_slowdown_detected", False)
            )
        )
        
        degradation = self.analyze_degradation(valid_history)
        
        # 澶辫触浼氳瘽杩囨护
        failed_sessions = [
            {"time": time.strftime("%H:%M:%S", time.localtime(s.get("events", [{}])[0].get("time", 0))), 
             "song": s["session_id"], 
             "reason": s["fail_reason"]}
            for s in self.evaluator.session_results 
            if s["final_state"] in [self.evaluator.STATE_FAILED, self.evaluator.STATE_INTERRUPTED]
        ]

        # Perceptual Jank Stats (V2.2)
        perceptual_events = sum(1 for h in valid_history if h.get("is_perceptual_jank", False))
        perceptual_jank_percent = (perceptual_events / len(valid_history) * 100.0) if valid_history else 0.0
        
        # V2.3: 鐢佃绔崱椤跨粺璁★紙鏈€閲嶈锛?
        decoder_stuck_count = sum(
            1
            for h in valid_history
            if (not h.get("ignore_video_metrics", False))
            and h.get("decoder_stuck", False)
        )
        confirmed_decoder_stuck_count = sum(
            1
            for h in valid_history
            if (not h.get("ignore_video_metrics", False))
            and h.get("decoder_stuck", False)
            and h.get("decoder_stuck_confirmed", False)
        )
        decoder_stuck_risk_count = max(
            0,
            int(decoder_stuck_count) - int(confirmed_decoder_stuck_count),
        )
        tv_stutter_count = sum(1 for h in valid_history if (not h.get("ignore_video_metrics", False)) and h.get("tv_stutter_detected", False))
        tv_freeze_count = len(self.tv_freeze_events)

        # 基础解码丢帧估算
        decode_expected = 0.0
        decode_actual = 0
        for h in valid_history:
            if h.get("ignore_video_metrics", False):
                continue
            if not bool(h.get("mpp_work_count_reliable", False)):
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
        decoder_stuck_summary = self._build_decoder_stuck_summary(valid_history)
        observer_cpu_values = [
            float(h.get("observer_cpu_percent", 0) or 0.0)
            for h in valid_history
            if h.get("observer_cpu_percent") is not None
        ]
        observer_memory_values = [
            float(h.get("observer_memory_mb", 0) or 0.0)
            for h in valid_history
            if h.get("observer_memory_mb") is not None
        ]
        observer_sampling_modes = {}
        for h in valid_history:
            mode = str(h.get("observer_sampling_mode", "") or "").strip()
            if mode:
                observer_sampling_modes[mode] = observer_sampling_modes.get(mode, 0) + 1

        base_summary = {
            "restart_count": self.restart_count,
            "avg_pss_mb": round(avg_pss, 2),
            "max_pss_mb": round(max_pss, 2),
            "duration_samples": len(self.history),
            "valid_samples": len(valid_history),
            "invalid_samples": invalid_samples,
            "valid_sample_ratio": round(valid_sample_ratio, 4),
            "pid_loss_count": len(pid_loss_events),
            "target_process_lost": bool(pid_loss_events),
            "degradation_analysis": degradation,
            "total_gfx_jank": total_gfx_jank,
            "total_frames_delta": total_frames_delta,     # V2.2 Explicit total frames
            "avg_jank_percent": round(avg_jank_percent, 2),
            "perceptual_jank_percent": round(perceptual_jank_percent, 2), # V2.2
            "perceptual_jank_events": perceptual_events,                  # V2.2
            "final_log_stutter_count": self.log_stutter_count,
            # V2.3: 鐢佃绔崱椤跨粺璁★紙鏈€閲嶈锛?
            "decoder_stuck_count": decoder_stuck_count,
            "confirmed_decoder_stuck_count": confirmed_decoder_stuck_count,
            "decoder_stuck_risk_count": decoder_stuck_risk_count,
            "decoder_stuck_summary": decoder_stuck_summary,
            "tv_stutter_count": tv_stutter_count,
            "tv_freeze_count": tv_freeze_count,
            "tv_freeze_events": list(self.tv_freeze_events),
            "tv_stall_count": len(self.tv_stall_events),
            "tv_stall_events": list(self.tv_stall_events),
            "tv_stall_risk_count": len(self.tv_stall_risk_events),
            "tv_stall_risk_events": list(self.tv_stall_risk_events),
            "tv_display_id": self._tv_display_id,
            "tv_display_verified": bool(self._tv_display_verified),
            "tv_display_verification_reason": self._tv_display_verification_reason,
            "tv_display_recommendation": dict(self._tv_display_recommendation),
            "tv_display_probe_details": list(self._tv_display_probe_details),
            "tv_surface_locked": bool(self._last_tv_surface_name),
            "tv_surface_name": self._last_tv_surface_name,
            "tv_surface_candidates": list(self._last_tv_surface_candidates),
            "tv_latency_probe": dict(self._last_tv_latency_probe),
            "video_fps_source_counts": video_fps_source_counts,
            "video_fps_unavailable_reason": (
                max(
                    fps_unavailable_reason_counts.items(),
                    key=lambda item: item[1],
                )[0]
                if fps_unavailable_reason_counts else ""
            ),
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
            # V2.2 视频 FPS 统计（最重要）
            "avg_video_fps": round(avg_video_fps, 2),
            "max_video_fps": round(max_video_fps, 2),
            "min_video_fps": round(min_video_fps, 2),
            "video_fps_samples": len(video_fps_values),  # 有效 FPS 样本数
            "observer_pid": self._observer_pid,
            "observer_avg_cpu_percent": round(sum(observer_cpu_values) / len(observer_cpu_values), 2) if observer_cpu_values else 0.0,
            "observer_peak_cpu_percent": round(max(observer_cpu_values), 2) if observer_cpu_values else 0.0,
            "observer_avg_memory_mb": round(sum(observer_memory_values) / len(observer_memory_values), 2) if observer_memory_values else 0.0,
            "observer_peak_memory_mb": round(max(observer_memory_values), 2) if observer_memory_values else 0.0,
            "observer_sampling_mode_counts": observer_sampling_modes,
            "observer_primary_sampling_mode": (
                max(observer_sampling_modes.items(), key=lambda item: item[1])[0]
                if observer_sampling_modes else "unknown"
            ),
            # V2.4: GPU 使用率统计（新增）
            "avg_gpu_percent": round(avg_gpu, 2),
            "max_gpu_percent": round(max_gpu, 2),
            "min_gpu_percent": round(min_gpu, 2),
            "gpu_samples": len(gpu_values),  # 有效 GPU 样本数
            "session_stats": {
                "total": len(self.evaluator.session_results),
                "success": sum(1 for s in self.evaluator.session_results if s["final_state"] == self.evaluator.STATE_FINISHED)
            },
            "failed_sessions": failed_sessions, # V2.1 Failed Details
            "pid_events": self.pid_events, # V2.1 PID Events
            "process_failure_summary": process_failure_summary,
            # 闇€瑕佸閮ㄥ～鍏? crash_count, anr_count
            "crash_count": 0, 
            "anr_count": 0
        }
        
        return base_summary

    def _build_process_failure_summary(self) -> Dict:
        event_type_labels = {
            "PID_RESTART": "PID重启",
            "PID_LOST": "进程丢失",
            "CRASH": "Crash",
            "ANR": "ANR",
        }
        failure_events = []
        for event in self.pid_events:
            event_type = str(event.get("type", "") or "").strip()
            if event_type not in {"PID_RESTART", "PID_LOST"}:
                continue
            failure_events.append({
                "type": event_type,
                "timestamp": event.get("timestamp", "N/A"),
                "elapsed_min": event.get("elapsed_min", 0),
                "description": event.get("description", ""),
            })

        failure_types = []
        for event in failure_events:
            label = event_type_labels.get(event["type"], event["type"])
            if label not in failure_types:
                failure_types.append(label)

        first_failure = failure_events[0] if failure_events else {}
        last_failure = failure_events[-1] if failure_events else {}

        return {
            "has_player_failure": bool(failure_events),
            "total_failure_count": len(failure_events),
            "restart_count": sum(1 for event in failure_events if event["type"] == "PID_RESTART"),
            "pid_loss_count": sum(1 for event in failure_events if event["type"] == "PID_LOST"),
            "crash_count": 0,
            "anr_count": 0,
            "failure_types": failure_types,
            "first_failure_time": first_failure.get("timestamp", "N/A"),
            "last_failure_time": last_failure.get("timestamp", "N/A"),
            "first_failure_type": event_type_labels.get(first_failure.get("type", ""), "N/A"),
            "last_failure_type": event_type_labels.get(last_failure.get("type", ""), "N/A"),
            "timeline": failure_events[-5:],
        }

    def _collect_observer_metrics(self) -> Dict:
        mode = "standard"
        interval = getattr(self, "sample_interval", None)
        try:
            interval_value = float(interval) if interval is not None else None
        except (TypeError, ValueError):
            interval_value = None
        if interval_value is not None:
            if interval_value <= 1.0:
                mode = "high_frequency"
            elif interval_value >= 5.0:
                mode = "low_overhead"

        if self._observer_process is None:
            return {
                "pid": self._observer_pid,
                "cpu_percent": 0.0,
                "memory_mb": 0.0,
                "sampling_mode": mode,
            }

        try:
            cpu_percent = float(self._observer_process.cpu_percent(None) or 0.0)
        except Exception:
            cpu_percent = 0.0
        try:
            memory_mb = float(self._observer_process.memory_info().rss or 0.0) / 1024.0 / 1024.0
        except Exception:
            memory_mb = 0.0
        return {
            "pid": self._observer_pid,
            "cpu_percent": round(cpu_percent, 2),
            "memory_mb": round(memory_mb, 2),
            "sampling_mode": mode,
        }
        
    def calculate_score(self, base_summary: Dict) -> Dict:
        """璋冪敤 evaluator 璁＄畻鏈€缁堝緱鍒?(V2.1)"""
        return self.evaluator.evaluate_global_score(base_summary)

    def start_new_session(self, song_info: str = None):
        """?????????"""
        self.evaluator.start_new_session(song_info or "Unknown")

    def report_event(self, event_type: str, payload: any):
        """? evaluator ???????V2.1 unified entry??"""
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
            event_type_name = str(event.get("type", "TV_STALL") or "TV_STALL")
            is_confirmed = bool(event.get("confirmed", event_type_name == "TV_STALL"))
            if event_type_name == "TV_STALL_RISK" or not is_confirmed:
                self.tv_stall_risk_events.append(event)
            else:
                self.tv_stall_events.append(event)
                self.evaluator.on_screen_anomaly(
                    f"TV_STALL: {event.get('duration_ms', 0)}ms"
                )
        elif event_type == "ACTION" and payload == "STOP":
            self.evaluator.on_interrupt("Manual Stop")

    def get_session_result(self, is_force_stop: bool = True):
        """鑾峰彇褰撳墠浼氳瘽鍒ゅ畾"""
        # 濡傛灉鏄己鍒跺仠姝紝鎰忓懗鐫€ Runner 姝ｅ湪鍒囨瓕锛屾鏃堕渶瑕?finalize
        if is_force_stop:
             # 濡傛灉涔嬪墠娌＄粨鏉燂紝鐜板湪鎵嬪姩缁撴潫
             if not self.evaluator.is_finished:
                 # 鍋囪杩欓噷 Runner 鍒囨瓕绠楁槸 "INTERRUPTED" 杩樻槸 "FINISHED"?
                 # 濡傛灉鏄?loop_playback锛屾椂闂村埌浜嗗己鍒跺垏姝岋紝閫氬父绠?Pass 闄ら潪鏈夐敊璇€?                 # 浣嗘槸 evaluator.finalize() 浼氭牴鎹?internal state 鍒ゆ柇銆?                 # 濡傛灉娌℃湁 first_frame -> Fail
                 # 濡傛灉鏈?first_frame -> Interrupted (because is_finished is False)
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

    def _list_display_ids(self) -> List[int]:
        ids = set()
        try:
            output = self.adb._run_command(["shell", "dumpsys", "display"], timeout=2)
            if output and "Error:" not in output:
                import re
                for line in output.splitlines():
                    line_l = line.lower()
                    matches = re.findall(
                        r'(?:displayid|mdisplayid)\s*=\s*(\d+)|display\s+(?:id\s*=\s*)?(\d+)',
                        line_l,
                    )
                    for match in matches:
                        try:
                            ids.add(int(match[0] or match[1]))
                        except (TypeError, ValueError):
                            continue
        except Exception:
            ids = set()

        if not ids:
            defaults = {0, self._configured_tv_display_id, 1}
            if self._allow_display0_fallback:
                defaults.add(0)
            ids = {value for value in defaults if isinstance(value, int) and value >= 0}
        return sorted(ids)

    def _score_display_probe(self, display_id: int) -> Dict:
        metrics = self.collect_tv_frame_metrics(display_id, track_progress=False) or {}
        if not isinstance(metrics, dict):
            metrics = {}
        fps = float(metrics.get("fps", 0) or 0.0)
        frame_count = int(metrics.get("frame_count", 0) or 0)
        candidates = list(metrics.get("candidates", []) or [])
        surface_name = str(metrics.get("surface_name", "") or "")
        probe_reason = str(metrics.get("probe_reason", "") or "unknown")
        latency_mode = str(metrics.get("latency_mode", "") or "unknown")
        score = 0.0

        if display_id > 0:
            score += 5.0
        if frame_count >= 2:
            score += 30.0
        if fps > 0:
            score += min(45.0, fps)
        if surface_name:
            score += 25.0
        if candidates:
            score += min(10.0, float(len(candidates)))
        if latency_mode == "surface_name":
            score += 10.0
        elif latency_mode == "display_id":
            score += 6.0
        if probe_reason == "display_fallback":
            score += 4.0
        if probe_reason == "latency_zero_frames":
            score -= 25.0
        elif probe_reason in {"no_surface_candidates", "latency_parse_failed"}:
            score -= 10.0

        reason = "no_video_signal"
        if fps > 0 and surface_name:
            reason = "surface_locked_with_active_frames"
        elif fps > 0:
            reason = f"{latency_mode}_active_frames"
        elif frame_count >= 2:
            reason = f"{latency_mode}_frame_history_only"
        elif candidates:
            reason = "surface_candidates_found_but_no_frames"

        return {
            "display_id": display_id,
            "score": round(score, 2),
            "reason": reason,
            "probe_reason": probe_reason,
            "latency_mode": latency_mode,
            "fps": round(fps, 2),
            "frame_count": frame_count,
            "surface_name": surface_name,
            "candidate_count": len(candidates),
            "max_frame_gap_ms": float(metrics.get("max_frame_gap_ms", 0) or 0.0),
        }

    def _recommend_tv_display(self, display_ids: List[int]) -> Dict:
        probes = []
        for display_id in display_ids:
            if not isinstance(display_id, int) or display_id < 0:
                continue
            if display_id == 0 and not self._allow_display0_fallback:
                continue
            probes.append(self._score_display_probe(display_id))

        self._tv_display_probe_details = list(probes)
        if not probes:
            recommendation = {
                "display_id": None,
                "score": 0.0,
                "reason": "no_probe_candidates",
                "probe_count": 0,
                "fps": 0.0,
                "surface_name": "",
            }
            self._tv_display_recommendation = recommendation
            return recommendation

        probes.sort(
            key=lambda item: (
                float(item.get("score", 0) or 0.0),
                float(item.get("fps", 0) or 0.0),
                int(item.get("frame_count", 0) or 0),
                1 if item.get("display_id") == self._configured_tv_display_id else 0,
            ),
            reverse=True,
        )
        best = probes[0]
        recommendation = {
            "display_id": best.get("display_id"),
            "score": float(best.get("score", 0) or 0.0),
            "reason": str(best.get("reason", "") or "unknown"),
            "probe_count": len(probes),
            "fps": float(best.get("fps", 0) or 0.0),
            "surface_name": str(best.get("surface_name", "") or ""),
        }
        self._tv_display_recommendation = recommendation
        return recommendation

    def _detect_tv_display_id(self):
        current_time = time.time()
        cache_ttl = self._tv_display_id_cache_ttl if self._tv_display_verified else 10.0
        if (self._tv_display_id is not None and
            current_time - self._tv_display_id_cache_time < cache_ttl):
            return self._tv_display_id

        display_id = None
        ids = self._list_display_ids()
        recommendation = self._recommend_tv_display(ids) if self._auto_detect_tv_display else {}
        recommended_id = recommendation.get("display_id")
        recommended_score = float(recommendation.get("score", 0) or 0.0)

        if self._configured_tv_display_id in ids:
            display_id = self._configured_tv_display_id
            self._tv_display_verified = True
            self._tv_display_verification_reason = "configured_display_found"
            if (
                self._auto_detect_tv_display
                and recommended_id == self._configured_tv_display_id
                and recommended_score > 0
                and (
                    float(recommendation.get("fps", 0) or 0.0) > 0.0
                    or bool(recommendation.get("surface_name"))
                )
            ):
                self._tv_display_verification_reason = "configured_display_confirmed_by_video_probe"
        elif self._auto_detect_tv_display and recommended_id is not None and recommended_score > 0:
            display_id = int(recommended_id)
            self._tv_display_verified = True
            self._tv_display_verification_reason = "auto_recommended_by_video_probe"
        elif self._auto_detect_tv_display:
            candidates = [i for i in ids if i > 0]
            if candidates:
                display_id = candidates[0]
                self._tv_display_verified = True
                self._tv_display_verification_reason = "auto_detected_non_primary_display"

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
        Measure real video FPS - 鏈€浼樻柟妗?        閲囩敤澶氱瓥鐣ョ粍鍚堬細浼樺厛gfxinfo锛堟渶閫氱敤锛夛紝澶囩敤SurfaceFlinger锛堟洿绮剧‘锛夛紝鏈€鍚庡皾璇昅PP纭欢璁℃暟锛堟渶搴曞眰锛?        宸蹭紭鍖栵細鎵€鏈夋搷浣滈兘鏈夎秴鏃跺拰寮傚父澶勭悊锛岄伩鍏嶉樆濉?        
        Args:
            display_id: Display ID (0=涓诲睆/鐐规瓕灞? 1=鐢佃灞? 榛樿0)
            mpp_stats: 鍙€夌殑MPP缁熻淇℃伅锛岀敤浜嶵ier 3鍥為€€绛栫暐
        """
        try:
            # 浣跨敤缂撳瓨閬垮厤棰戠箒璋冪敤锛?绉掑唴澶嶇敤缁撴灉锛?            # 娉ㄦ剰锛氬鏋滄湁 mpp_stats锛屾垜浠彲鑳藉笇鏈涙洿鏂板畠锛屼絾缂撳瓨浼樺厛
            current_time = time.time()
            if (self._video_fps_cache is not None and self._video_fps_cache > 0 and
                current_time - self._video_fps_cache_time < self._video_fps_cache_ttl):
                return self._video_fps_cache
            
            fps = 0.0
            
            # 电视端优先使用对应 Display 的 SurfaceFlinger 数据。
            try:
                surface_metrics = self.collect_tv_frame_metrics(
                    display_id,
                    track_progress=False,
                ) or {}
                surface_fps = float(surface_metrics.get("fps", 0) or 0.0)
                if 0 < surface_fps < 120:
                    self._video_fps_cache = surface_fps
                    self._video_fps_cache_time = current_time
                    if surface_metrics.get("surface_name"):
                        self._last_video_fps_source = (
                            f"surfaceflinger_surface_display_{display_id}"
                        )
                    elif str(surface_metrics.get("latency_mode", "") or "") == "display_id":
                        self._last_video_fps_source = (
                            f"surfaceflinger_display_{display_id}_fallback"
                        )
                    else:
                        self._last_video_fps_source = (
                            f"surfaceflinger_display_{display_id}"
                        )
                    return surface_fps
            except Exception:
                pass

            # gfxinfo 只作为主屏 UI 指标使用，不能替代电视端 FPS。
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
            
            # 策略 3：如果前两种方式都失败，则尝试使用 MPP work_count 估算解码帧率。
            if mpp_stats and mpp_stats.get("work_count_delta", 0) > 0:
                delta = mpp_stats.get("work_count_delta", 0)
                time_sec = mpp_stats.get("work_count_delta_time_sec", 0)
                if time_sec > 0:
                    fps_estimate = delta / time_sec
                    # 鍙湁鍦ㄥ悎鐞嗚寖鍥村唴鎵嶉噰淇?(1fps - 120fps)
                    if 0 < fps_estimate < 120:
                        self._video_fps_cache = fps_estimate
                        self._video_fps_cache_time = current_time
                        self._last_video_fps_source = "mpp_estimate"
                        return round(fps_estimate, 2)

            # 濡傛灉閮藉け璐ワ紝杩斿洖0锛堣〃绀烘棤娉曡幏鍙栵級
            self._video_fps_cache = None
            self._video_fps_cache_time = 0
            self._last_video_fps_source = "none"
            return 0.0
        except Exception:
            # 最外层异常兜底，确保采集失败时返回 0。
            return 0.0

    def _measure_gpu_usage(self) -> float:
        """
        閫氳繃 dumpsys gpu 鍛戒护閲囬泦 GPU 浣跨敤鐜?        鏀寔澶氱 Android 璁惧鐨?GPU 鍛戒护鏍煎紡锛圦ualcomm Adreno, Mali, 绛夛級
        
        Returns:
            GPU 浣跨敤鐜囩櫨鍒嗘瘮 (0.0 - 100.0)锛屽鏋滄棤娉曡幏鍙栬繑鍥?0.0
        """
        try:
            # 鎵ц dumpsys gpu 鍛戒护
            gpu_output = self.adb._run_command(
                ["shell", "dumpsys", "gpu"],
                timeout=3
            )
            
            if not gpu_output or "Error:" in gpu_output:
                return 0.0
            
            # 绛栫暐1: 鏌ユ壘 "GPU memory:" 鍜?"GPU usage:" 鎴栫被浼煎瓧娈?            # 涓嶅悓鍘傚晢鏍煎紡涓嶅悓锛屽皾璇曞绉嶅尮閰嶆ā寮?            
            # 妯″紡1: Qualcomm Adreno GPU (甯歌浜庡皬绫炽€丱PPO銆乿ivo绛?
            # 鏍煎紡绀轰緥:
            #   GPU memory: Total=123MB, Used=45MB
            #   GPU usage: 45%
            if "GPU usage:" in gpu_output:
                import re
                match = re.search(r'GPU usage:\s*(\d+(?:\.\d+)?)\s*%', gpu_output, re.IGNORECASE)
                if match:
                    usage = float(match.group(1))
                    if 0.0 <= usage <= 100.0:
                        return round(usage, 2)
            
            # 妯″紡2: Mali GPU (ARM 鑺墖锛屽娴锋€濄€佺憺鑺井绛?
            # 鏍煎紡绀轰緥:
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
            
            # 妯″紡3: 灏濊瘯浠?GPU 鏃堕挓棰戠巼鎺ㄧ畻
            # 鏍煎紡绀轰緥:
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
            
            # 模式 4：兜底解析任意百分比数字。
            # 注意：这里只在前面几种方式都失败时使用。
            import re
            percent_matches = re.findall(r'(\d+(?:\.\d+)?)\s*%', gpu_output)
            if percent_matches:
                # 取第一个合理的百分比值
                for p_str in percent_matches:
                    try:
                        p = float(p_str)
                        if 0.0 <= p <= 100.0 and p > 1.0:  # 排除 0-1% 的微小噪音
                            logger.debug("[GPU] 使用兜底方法，从输出中提取到 %.1f%%", p)
                            return round(p, 2)
                    except (ValueError, TypeError):
                        continue
            
            logger.debug("[GPU] 未能从 dumpsys gpu 输出中解析出使用率")
            return 0.0
            
        except Exception as e:
            logger.debug("[GPU] _measure_gpu_usage 寮傚父: %s", e)
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
            "candidates": [],
            "probe_reason": "not_started",
            "latency_mode": "surface_name",
            "latency_frame_count": 0,
            "latency_output_excerpt": "",
        }
        try:
            candidates = []
            if self._last_tv_surface_name and self._tv_surface_failure_count < 3:
                candidates.append(self._last_tv_surface_name)
            else:
                candidates = self._find_tv_surface_candidates(display_id)
            result["candidates"] = list(candidates[:8])
            self._last_tv_surface_candidates = list(candidates[:12])
            if not candidates:
                result["probe_reason"] = "no_surface_candidates"
                self._last_tv_latency_probe = dict(result)
                return result

            for surface in candidates[:8]:
                # adb shell concatenates arguments into a remote shell command.
                # Preserve spaces and "#" in Rockchip layer names; using
                # `sh -c` here makes only "dumpsys" the command string and
                # turns the remaining tokens into positional arguments.
                quoted_surface = "'" + str(surface).replace(
                    "'",
                    "'\\''",
                ) + "'"
                output = self.adb._run_command(
                    [
                        "shell",
                        "dumpsys",
                        "SurfaceFlinger",
                        "--latency",
                        quoted_surface,
                    ],
                    timeout=4,
                    retry=0,
                )
                previous_timestamp = (
                    self._last_tv_surface_frame_timestamp_ns
                    if track_progress else 0
                )
                metrics = self._parse_surface_latency(
                    output,
                    since_timestamp_ns=previous_timestamp,
                )
                metrics["latency_output_excerpt"] = "\n".join(
                    (output or "").splitlines()[:6]
                )
                metrics["latency_mode"] = "surface_name"
                if (
                    metrics.get("frame_count", 0) == 0
                    and metrics.get("zero_row_count", 0) > 0
                ):
                    result["probe_reason"] = "latency_zero_frames"
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
                metrics["candidates"] = list(candidates[:8])
                metrics["probe_reason"] = "ok"
                if track_progress:
                    self._last_tv_surface_frame_timestamp_ns = latest
                self._last_tv_surface_name = surface
                self._tv_surface_failure_count = 0
                self._last_tv_latency_probe = dict(metrics)
                return metrics

            raw_display_metrics = self._probe_display_latency(display_id, track_progress)
            if raw_display_metrics.get("frame_count", 0) >= 2:
                raw_display_metrics["display_id"] = display_id
                raw_display_metrics["candidates"] = list(candidates[:8])
                raw_display_metrics["probe_reason"] = "display_fallback"
                self._tv_surface_failure_count = 0
                self._last_tv_latency_probe = dict(raw_display_metrics)
                return raw_display_metrics
            if raw_display_metrics.get("probe_reason") == "latency_zero_frames":
                raw_display_metrics["display_id"] = display_id
                raw_display_metrics["candidates"] = list(candidates[:8])
                self._last_tv_latency_probe = dict(raw_display_metrics)
                return raw_display_metrics

            self._tv_surface_failure_count += 1
            if self._tv_surface_failure_count >= 3:
                self._last_tv_surface_name = ""
                self._last_tv_surface_frame_timestamp_ns = 0
            result["probe_reason"] = "latency_parse_failed"
            self._last_tv_latency_probe = dict(result)
            return result
        except Exception as e:
            self._tv_surface_failure_count += 1
            result["probe_reason"] = f"exception:{type(e).__name__}"
            self._last_tv_latency_probe = dict(result)
            return result

    def _probe_display_latency(
        self,
        display_id: int,
        track_progress: bool,
    ) -> Dict:
        result = {
            "timestamp": time.time(),
            "display_id": display_id,
            "fps": 0.0,
            "max_frame_gap_ms": 0.0,
            "p95_frame_gap_ms": 0.0,
            "frame_advanced": False,
            "surface_name": "",
            "candidates": [],
            "probe_reason": "display_latency_failed",
            "latency_mode": "display_id",
            "latency_frame_count": 0,
            "latency_output_excerpt": "",
        }
        try:
            output = self.adb._run_command(
                [
                    "shell",
                    "dumpsys",
                    "SurfaceFlinger",
                    "--latency",
                    str(display_id),
                ],
                timeout=4,
                retry=0,
            )
            previous_timestamp = (
                self._last_tv_surface_frame_timestamp_ns
                if track_progress else 0
            )
            metrics = self._parse_surface_latency(
                output,
                since_timestamp_ns=previous_timestamp,
            )
            metrics["surface_name"] = ""
            metrics["display_id"] = display_id
            metrics["latency_mode"] = "display_id"
            metrics["latency_output_excerpt"] = "\n".join(
                (output or "").splitlines()[:6]
            )
            metrics["latency_frame_count"] = int(metrics.get("frame_count", 0) or 0)
            if (
                metrics.get("frame_count", 0) == 0
                and metrics.get("zero_row_count", 0) > 0
            ):
                result["probe_reason"] = "latency_zero_frames"
                result["latency_output_excerpt"] = metrics["latency_output_excerpt"]
                return result
            if metrics["frame_count"] >= 2:
                latest = metrics["latest_frame_timestamp_ns"]
                metrics["frame_advanced"] = bool(
                    latest > 0
                    and (
                        not track_progress
                        or latest != self._last_tv_surface_frame_timestamp_ns
                    )
                )
                if track_progress:
                    self._last_tv_surface_frame_timestamp_ns = latest
                metrics["probe_reason"] = "ok"
                return metrics
            return result
        except Exception:
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
        generic_candidates = []
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

        def add_generic(surface: str):
            value = (surface or "").strip()
            if value and value not in seen:
                seen.add(value)
                generic_candidates.append(value)

        surface_lines = output.splitlines()
        if not display_scoped:
            target_root = f"root#{display_id}"
            scoped_lines = []
            in_target_root = False
            root_found = False
            in_target_display = False
            display_found = False
            for line in surface_lines:
                stripped = line.strip()
                lower_line = stripped.lower()
                if lower_line.startswith("display "):
                    in_target_display = lower_line.startswith(f"display {display_id} ")
                    display_found = display_found or in_target_display
                    if in_target_display:
                        scoped_lines.append(line)
                    continue
                if lower_line.startswith("root#"):
                    in_target_root = lower_line.startswith(target_root)
                    root_found = root_found or in_target_root
                    continue
                if in_target_root or in_target_display:
                    scoped_lines.append(line)
            if not root_found and not display_found:
                return []
            surface_lines = scoped_lines

        for surface in surface_lines:
            stripped_surface = surface.strip()
            lower = stripped_surface.lower()
            if "background for" in lower or "bounds for" in lower:
                continue
            if lower.startswith("display ") or lower.startswith("root#"):
                continue
            display_hint = any(
                keyword in lower
                for keyword in ("secondary", "external", "hdmi", "display1", "tv")
            )
            video_hint = any(
                keyword in lower
                for keyword in (
                    "video",
                    "media",
                    "surfaceview",
                    "textureview",
                    "videosink",
                    "sink",
                    "producer",
                    "rk_mpp",
                    "rockit",
                    "bufferqueue",
                    "blast",
                    "surface",
                    "layer",
                    "subtitle",
                )
            )
            package_hint = self.package_name in stripped_surface or pkg_key in stripped_surface
            anonymous_hint = stripped_surface.startswith("#")
            if display_hint and (video_hint or package_hint):
                add(stripped_surface)
            elif video_hint or package_hint:
                add(stripped_surface)
            elif anonymous_hint:
                # Some Rockchip builds expose latency only on an anonymous
                # producer layer such as "#2", not on its SurfaceView wrapper.
                add_fallback(stripped_surface)
            elif (
                display_id == 1
                and ("bufferqueue" in lower or "blast" in lower or "surface" in lower)
            ):
                add_fallback(stripped_surface)
            else:
                add_generic(stripped_surface)
        return candidates + fallback_candidates + generic_candidates

    def _parse_surface_latency(
        self,
        output: str,
        since_timestamp_ns: int = 0,
    ) -> Dict:
        frame_times = []
        zero_row_count = 0
        for line in (output or "").strip().splitlines()[1:]:
            parts = line.strip().split()
            if not parts:
                continue
            if all(part == "0" for part in parts):
                zero_row_count += 1
                continue
            try:
                numeric_parts = []
                for part in parts:
                    try:
                        numeric_parts.append(int(part))
                    except (TypeError, ValueError):
                        continue
                if not numeric_parts:
                    continue
                timestamp = max(value for value in numeric_parts if value > 0)
            except (TypeError, ValueError, StopIteration):
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
            max_reasonable_gap_ms = 5000.0
            max_reasonable_gap_ns = int(max_reasonable_gap_ms * 1_000_000.0)
            sequence = list(new_frame_times)
            first_delta = new_frame_times[0] - since_timestamp_ns
            if 0 < first_delta <= max_reasonable_gap_ns:
                sequence.insert(0, since_timestamp_ns)
            for index in range(1, len(sequence)):
                delta = sequence[index] - sequence[index - 1]
                if delta <= 0:
                    continue
                delta_ms = delta / 1_000_000.0
                if delta_ms <= max_reasonable_gap_ms:
                    event_intervals_ms.append(delta_ms)

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
            "zero_row_count": zero_row_count,
            "latest_frame_timestamp_ns": frame_times[-1] if frame_times else 0,
            "latency_frame_count": len(frame_times),
        }
    
    def _get_fps_from_gfxinfo(self) -> float:
        """
        閫氳繃 gfxinfo 鑾峰彇FPS锛堜富瑕佹柟娉曪紝閫氱敤鎬уソ锛?        V2.3.2: 鏅鸿兘澶勭悊KTV鍙岃繘绋嬫灦鏋?        - 浼樺厛灏濊瘯涓昏繘绋嬶紙璐熻矗UI娓叉煋鍜岃棰戞樉绀猴級
        - 濡傛灉涓昏繘绋嬫暟鎹笉瓒筹紝灏濊瘯濯掍綋杩涚▼
        - 鑷姩閫夋嫨甯ф暟鎹渶涓板瘜鐨勮繘绋?        """
        try:
            # V2.3.2: 鏅鸿兘杩涚▼閫夋嫨绛栫暐
            candidates = []
            
            # 候选 1：主进程（去掉 : 后缀）
            main_pkg = self.package_name.split(':')[0]
            candidates.append(("main", main_pkg))
            
            # 鍊欓€?: 濡傛灉閰嶇疆鐨勬槸濯掍綋杩涚▼锛屼篃灏濊瘯鍘熷閰嶇疆
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

                    # 鑾峰彇璇ヨ繘绋嬬殑gfxinfo鏁版嵁
                    gfx_output = self.adb._run_command(
                        ["shell", "dumpsys", "gfxinfo", pkg_name], 
                        timeout=3
                    )
                    if not gfx_output or "Error:" in gfx_output:
                        continue
                    
                    # 妫€鏌ユ暟鎹川閲忥紙鎬诲抚鏁帮級
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
                    
                    # 濡傛灉甯ф暟澶皯锛?10锛夛紝璺宠繃杩欎釜杩涚▼
                    if total_frames < 10:
                        logger.debug("[FPS] %s process (%s): 甯ф暟涓嶈冻(%s), 璺宠繃", source_type, pkg_name, total_frames)
                        continue
                    
                    # 灏濊瘯瑙ｆ瀽FPS
                    fps = self._parse_gfxinfo_fps(gfx_output)
                    if fps > 0 and fps < 120:
                        logger.debug("[FPS] %s process (%s): %.1ffps (甯ф暟:%s)", source_type, pkg_name, fps, total_frames)
                        if fps > best_fps:
                            best_fps = fps
                            best_source = f"{source_type}({pkg_name})"
                    
                except Exception as e:
                    logger.debug("[FPS] %s process (%s) 鑾峰彇澶辫触: %s", source_type, pkg_name, e)
                    continue
            
            if best_fps > 0:
                logger.debug("[FPS] 鏈€浣虫暟鎹簮: %s, FPS: %.1f", best_source, best_fps)
                return best_fps
            
            # 如果候选进程都失败，则退回传统方式。
            real_pkg = self.package_name.split(':')[0]
            gfx_info = self.adb.get_gfx_info(real_pkg)
            gfx_output = self.adb._run_command(
                ["shell", "dumpsys", "gfxinfo", real_pkg], 
                timeout=3
            )
            if not gfx_output or "Error:" in gfx_output:
                return 0.0
            
            # 传统方式的后续处理
            fps = self._parse_gfxinfo_fps(gfx_output)
            if fps > 0 and fps < 120:
                return fps
            
            # 鏂规硶2: 濡傛灉璇︾粏瑙ｆ瀽澶辫触锛屽皾璇曚粠缁熻淇℃伅浼扮畻锛堜紶缁熸柟娉曪級
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
            
            # 濡傛灉鎵惧埌浜嗘椂闂寸獥鍙ｅ拰鎬诲抚鏁帮紝璁＄畻FPS
            if stats_time_ms > 0 and total_frames > 0:
                if stats_time_ms >= 1000:  # 鑷冲皯1绉掔殑鏁版嵁
                    fps_estimate = (total_frames * 1000.0) / stats_time_ms
                    if 0 < fps_estimate < 120:
                        logger.debug("[FPS] 浼犵粺鏂规硶浼扮畻: %.1ffps (甯ф暟:%s, 鏃堕棿:%sms)", fps_estimate, total_frames, stats_time_ms)
                        return round(fps_estimate, 2)
            
            logger.debug("[FPS] 鎵€鏈夋柟娉曢兘澶辫触锛屾棤娉曡幏鍙朏PS鏁版嵁")
            return 0.0
        except Exception as e:
            logger.debug("[FPS] _get_fps_from_gfxinfo 寮傚父: %s", e)
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
        浠?gfxinfo 杈撳嚭涓В鏋怓PS锛堝寮虹増锛?        鏀寔澶氱gfxinfo鏍煎紡锛屾彁楂樿В鏋愭垚鍔熺巼
        """
        try:
            lines = gfxinfo_output.splitlines()
            frame_times = []  # 保存每帧耗时
            start_parsing = False
            
            # 方法 1：解析 "Profile data in ms" 格式
            for i, line in enumerate(lines):
                if "Profile data in ms" in line or "PROFILE" in line.upper():
                    start_parsing = True
                    continue
                if "View hierarchy:" in line or "Janky frames:" in line or "---PROFILE---" in line:
                    if start_parsing:
                        break  # 鏁版嵁缁撴潫
                    continue
                if start_parsing and line.strip():
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            # 瑙ｆ瀽甯ф椂闂达紙Draw, Prepare, Process, Execute锛?                            vals = []
                            for part in parts:
                                # 移除非数字字符后尝试转浮点
                                cleaned = part.strip().replace(',', '')
                                try:
                                    val = float(cleaned)
                                    # 合理帧耗时范围：0.1ms - 200ms
                                    if 0.1 <= val <= 200:
                                        vals.append(val)
                                except ValueError:
                                    continue
                            
                            if len(vals) >= 2:  # 至少需要两个有效值
                                # 计算总帧耗时：优先取前 4 项（Draw, Prepare, Process, Execute）
                                frame_time = sum(vals[:min(4, len(vals))])
                                if 2.0 <= frame_time <= 200.0:  # 鍚堢悊鑼冨洿锛?fps-500fps
                                    frame_times.append(frame_time)
                        except (ValueError, IndexError, TypeError):
                            continue
            
            # 鏂规硶2: 濡傛灉鏂规硶1娌℃湁鏁版嵁锛屽皾璇曟煡鎵剧粺璁′俊鎭腑鐨凢PS
            if not frame_times:
                for line in lines:
                    # 查找 "60.XX fps" 或 "FPS: XX" 等格式
                    import re
                    # 鍖归厤 "XX fps" 鎴?"XX FPS"
                    fps_match = re.search(r'(\d+\.?\d*)\s*fps', line, re.IGNORECASE)
                    if fps_match:
                        try:
                            fps_val = float(fps_match.group(1))
                            if 0 < fps_val < 120:
                                return round(fps_val, 2)
                        except (ValueError, IndexError):
                            pass
            
            # 鏂规硶3: 濡傛灉杩樻病鏈夋暟鎹紝灏濊瘯閫氳繃鎬诲抚鏁板拰鏃堕棿绐楀彛璁＄畻
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
                    
                    # 鏌ユ壘缁熻鏃堕棿绐楀彛
                    if "Stats since:" in line or "since" in line.lower():
                        import re
                        # V2.3.2: 鏀寔绾崇鏍煎紡瑙ｆ瀽
                        # 鎻愬彇绾崇鏁?(濡? 109685320297ns)
                        ns_match = re.search(r'(\d+)\s*ns', line)
                        if ns_match:
                            stats_duration_ns = int(ns_match.group(1))
                            stats_duration_ms = stats_duration_ns // 1_000_000  # 杞崲涓烘绉?                        else:
                            # 鎻愬彇姣鏁?                            ms_match = re.search(r'(\d+)\s*ms', line)
                            if ms_match:
                                stats_duration_ms = int(ms_match.group(1))
                            else:
                                # 涔熷皾璇曠鏁?                                sec_match = re.search(r'(\d+\.?\d*)\s*s(ec)?', line, re.IGNORECASE)
                                if sec_match and stats_duration_ms == 0:
                                    stats_duration_ms = int(float(sec_match.group(1)) * 1000)
                
                # 濡傛灉鎵惧埌鎬诲抚鏁板拰鏃堕棿绐楀彛锛岃绠桭PS
                if total_frames > 0 and stats_duration_ms >= 1000:  # 鑷冲皯1绉掔殑鏁版嵁
                    fps_estimate = (total_frames * 1000.0) / stats_duration_ms
                    if 0 < fps_estimate < 120:
                        return round(fps_estimate, 2)
            
            # 璁＄畻FPS锛堜粠甯ф椂闂存暟鎹級
            if len(frame_times) > 0:
                # 过滤明显异常值
                valid_times = [t for t in frame_times if 5.0 <= t <= 200.0]  # 5ms-200ms，大致对应 5fps-200fps
                
                if len(valid_times) >= 5:  # 鑷冲皯闇€瑕?甯ф暟鎹墠鍙潬
                    # 浣跨敤涓綅鏁帮紙鏇存姉寮傚父鍊硷級
                    sorted_times = sorted(valid_times)
                    mid = len(sorted_times) // 2
                    median_time = sorted_times[mid] if len(sorted_times) % 2 == 1 else \
                                  (sorted_times[mid-1] + sorted_times[mid]) / 2
                    
                    if median_time > 0:
                        fps = 1000.0 / median_time
                        if 0 < fps < 120:
                            return round(fps, 2)
                
                # 如果中位数方法不适用，则退回平均值
                if len(valid_times) >= 3:
                    avg_frame_time = sum(valid_times) / len(valid_times)
                    if avg_frame_time > 0:
                        fps = 1000.0 / avg_frame_time
                        if 0 < fps < 120:
                            return round(fps, 2)
            
            return 0.0
        except Exception as e:
            # 娣诲姞璋冭瘯淇℃伅
            import sys
            if hasattr(sys, '_getframe'):
                logger.debug("_parse_gfxinfo_fps 澶辫触: %s", e)
        return 0.0
