import time
import statistics
import re
from typing import Dict, Any, Optional, List, Tuple
from .base_collector import BaseCollector

class FpsCollector(BaseCollector):
    """
    高级 FPS 采集器
    整合了 GfxInfo, Framestats, SurfaceFlinger 多种采集策略
    移植自 player_stress/core/monitor.py
    """
    
    def __init__(self, adb_controller: Any, package_name: str):
        super().__init__(adb_controller)
        self.package_name = package_name
        
        # 缓存
        self._video_fps_cache = None
        self._video_fps_cache_time = 0
        self._video_fps_cache_ttl = 2.0
        
        self._tv_display_id = None
        self._tv_display_id_cache_time = 0
        self._tv_display_id_cache_ttl = 300.0
        
        self._fps_error_count = 0
        # 用户优先指定的 Display ID（例如 1 表示 TV 端）
        self.preferred_display_id: Optional[int] = None

        # State for delta calculation (GfxInfo fallback)
        self._last_total_frames = 0
        self._last_frame_timestamp = 0

        self._mpp_supported: Optional[bool] = None
        self._mpp_active_path: Optional[str] = None
        self._mpp_last_work_count: int = 0
        self._mpp_last_work_time: float = 0.0


    def collect(self) -> Dict[str, Any]:
        """
        采集 FPS 数据
        """
        # 自动检测电视端 Display ID
        tv_display_id = self._detect_tv_display_id()
        
        # 测量视频 FPS
        # 注意：这里需要 MPP 统计信息来做 fallback，但为了解耦，
        # 我们暂时不传入 mpp_stats，或者如果在 CollectorManager 中可以协调
        # 暂时只使用 GfxInfo 和 SurfaceFlinger
        video_fps = self.measure_video_fps(
            display_id=tv_display_id if tv_display_id is not None else self.preferred_display_id,
            mpp_stats=None
        )
        
        return {
            "video_fps": video_fps,
            "tv_display_id": tv_display_id
        }

    def reset(self):
        self._video_fps_cache = None
        self._video_fps_cache_time = 0

    def _run_adb_command(self, cmd_list, timeout=3):
        """适配器方法"""
        if hasattr(self.adb, "_run_command"):
            return self.adb._run_command(cmd_list, timeout=timeout)
        
        import subprocess
        adb_path = getattr(self.adb, "adb_path", "adb")
        device_id = getattr(self.adb, "current_device_id", None)
        
        full_cmd = [adb_path]
        if device_id:
            full_cmd.extend(["-s", device_id])
        full_cmd.extend(cmd_list)
        
        try:
            result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='ignore')
            return result.stdout
        except Exception:
            return ""

    def measure_video_fps(self, display_id: Optional[int] = None, mpp_stats: Dict = None) -> float:
        """
        测量视频 FPS (核心入口)
        """
        # 统一决定本次使用的 display_id
        if display_id is None:
            display_id = self.preferred_display_id if self.preferred_display_id is not None else 0

        current_time = time.time()
        if (self._video_fps_cache is not None and self._video_fps_cache > 0 and
            current_time - self._video_fps_cache_time < self._video_fps_cache_ttl):
            return self._video_fps_cache
        
        fps = 0.0
        
        # 策略1: GfxInfo
        try:
            fps = self._get_fps_from_gfxinfo()
            if fps > 0 and fps < 120:
                self._video_fps_cache = fps
                self._video_fps_cache_time = current_time
                return fps
        except Exception:
            pass
        
        # 策略2: SurfaceFlinger
        try:
            surface_fps = self._get_fps_from_surfaceflinger(display_id=display_id)
            if surface_fps > 0 and surface_fps < 120:
                self._video_fps_cache = surface_fps
                self._video_fps_cache_time = current_time
                return surface_fps
        except Exception:
            pass
        
        # 策略3: MPP (如果有)
        if mpp_stats and mpp_stats.get("work_count_delta", 0) > 0:
            delta = mpp_stats.get("work_count_delta", 0)
            time_sec = mpp_stats.get("work_count_delta_time_sec", 0)
            if time_sec > 0:
                fps_estimate = delta / time_sec
                if 0 < fps_estimate < 120:
                    self._video_fps_cache = fps_estimate
                    self._video_fps_cache_time = current_time
                    return round(fps_estimate, 2)

        self._video_fps_cache = None
        self._video_fps_cache_time = 0
        return 0.0

    def measure_video_frame_metrics(self, display_id: Optional[int] = None, mpp_stats: Dict = None) -> Dict[str, Any]:
        if display_id is None:
            display_id = self.preferred_display_id if self.preferred_display_id is not None else 0

        metrics: Dict[str, Any] = {
            "fps": 0.0,
            "jank_count": 0,
            "dropped_frames_estimate": 0,
            "frame_interval_median_ms": 0.0,
            "frame_times_ms": [],
            "mpp_active_instances": 0,
            "mpp_total_work_count": 0,
            "mpp_work_count_delta": 0,
            "mpp_work_count_delta_time_sec": 0.0,
            "decoder_stuck": False,
            "decoder_stuck_duration_sec": 0.0,
            "decode_fps_estimate": 0.0,
            "decode_drop_estimate": 0,
            "decode_drop_ratio": 0.0,
            "source": ""
        }

        main_pkg = self.package_name.split(':')[0]
        candidates: List[str] = []
        if main_pkg:
            candidates.append(main_pkg)
        if ':' in self.package_name:
            candidates.append(self.package_name)
        else:
            candidates.append(f"{main_pkg}:media")
            candidates.append(f"{main_pkg}:player")

        seen = set()
        unique_candidates: List[str] = []
        for pkg in candidates:
            if pkg and pkg not in seen:
                unique_candidates.append(pkg)
                seen.add(pkg)

        best = None
        for pkg in unique_candidates:
            cand = self._get_frame_metrics_from_framestats(pkg)
            if not cand:
                continue
            if best is None or cand.get("frame_interval_median_ms", 0) < best.get("frame_interval_median_ms", 999999):
                best = cand
                best["source"] = f"framestats:{pkg}"

        if best:
            metrics.update(best)
        else:
            sf = self._get_frame_metrics_from_surfaceflinger(display_id=display_id)
            if sf:
                sf["source"] = f"surfaceflinger:{display_id}"
                metrics.update(sf)
            else:
                fps_only = self.measure_video_fps(display_id=display_id, mpp_stats=mpp_stats)
                metrics["fps"] = fps_only
                metrics["source"] = "fps_only"

        mpp = self._get_rk_mpp_stats()
        if mpp:
            metrics["mpp_active_instances"] = int(mpp.get("active_instances") or 0)
            metrics["mpp_total_work_count"] = int(mpp.get("total_work_count") or 0)
            metrics["mpp_work_count_delta"] = int(mpp.get("work_count_delta") or 0)
            metrics["mpp_work_count_delta_time_sec"] = float(mpp.get("work_count_delta_time_sec") or 0.0)
            metrics["decoder_stuck"] = bool(mpp.get("decoder_stuck") or False)
            metrics["decoder_stuck_duration_sec"] = float(mpp.get("decoder_stuck_duration_sec") or 0.0)
            dt = metrics["mpp_work_count_delta_time_sec"]
            if dt > 0 and metrics["mpp_work_count_delta"] >= 0:
                metrics["decode_fps_estimate"] = round(metrics["mpp_work_count_delta"] / dt, 2)
                expected = int(round(float(metrics.get("fps") or 0.0) * dt)) if float(metrics.get("fps") or 0.0) > 0 else 0
                if expected > 0:
                    drop = expected - int(metrics["mpp_work_count_delta"])
                    if drop > 0:
                        metrics["decode_drop_estimate"] = int(drop)
                        metrics["decode_drop_ratio"] = round(float(drop) / float(expected), 4)

        return metrics

    def _get_rk_mpp_stats(self) -> Dict[str, Any]:
        if self._mpp_supported is False:
            return {}

        paths = [
            "/sys/kernel/debug/mpp_service/stats",
            "/proc/mpp_service/rkvdec/task_count",
            "/proc/mpp_service/vdpu/task_count",
            "/proc/mpp_service/sessions-summary",
            "/proc/mpp_service/all",
            "/sys/kernel/debug/rkmpp/load",
            "/proc/mpp_service/load"
        ]

        if self._mpp_active_path:
            paths = [self._mpp_active_path] + [p for p in paths if p != self._mpp_active_path]

        raw_text = ""
        active_path = None
        for path in paths:
            out = self._run_adb_command(["shell", "su", "0", "cat", path], timeout=2)
            low = (out or "").lower()
            if "invalid" in low or "not found" in low or "permission denied" in low:
                out = self._run_adb_command(["shell", "su", "-c", f"cat {path}"], timeout=2)
                low = (out or "").lower()

            if not out:
                continue
            if "no such file" in low or "permission denied" in low or "not found" in low:
                continue

            raw_text = out
            active_path = path
            break

        if not raw_text:
            self._mpp_supported = False
            return {}

        self._mpp_supported = True
        self._mpp_active_path = active_path

        stats = self._parse_mpp_stats(raw_text, active_path or "")
        current_time = time.time()
        current_work_count = int(stats.get("total_work_count") or 0)

        if self._mpp_last_work_time > 0:
            work_count_delta = current_work_count - int(self._mpp_last_work_count or 0)
            time_delta = current_time - float(self._mpp_last_work_time or 0.0)
            stats["work_count_delta"] = work_count_delta
            stats["work_count_delta_time_sec"] = time_delta

            is_task_count_mode = str(active_path or "").endswith("/task_count")
            if (not is_task_count_mode and time_delta >= 1.0 and work_count_delta == 0 and int(stats.get("active_instances") or 0) > 0):
                stats["decoder_stuck"] = True
                stats["decoder_stuck_duration_sec"] = time_delta
            else:
                stats["decoder_stuck"] = False
                stats["decoder_stuck_duration_sec"] = 0.0
        else:
            stats["work_count_delta"] = 0
            stats["work_count_delta_time_sec"] = 0.0
            stats["decoder_stuck"] = False
            stats["decoder_stuck_duration_sec"] = 0.0

        self._mpp_last_work_count = current_work_count
        self._mpp_last_work_time = current_time
        return stats

    @staticmethod
    def _parse_mpp_stats(raw_text: str, active_path: str) -> Dict[str, Any]:
        stats = {
            "active_instances": 0,
            "session_count": 0,
            "total_work_count": 0
        }

        text = str(raw_text or "").strip()
        if not text:
            return stats

        if text.isdigit():
            stats["total_work_count"] = int(text)
            stats["active_instances"] = 1
            return stats

        active_match = re.search(r'active_instance\s*[:=]\s*(\d+)', text)
        if active_match:
            stats["active_instances"] = int(active_match.group(1))

        session_match = re.search(r'session_count\s*[:=]\s*(\d+)', text)
        if session_match:
            stats["session_count"] = int(session_match.group(1))

        work_match = re.search(r'(total_usage|work_count)\s*[:=]\s*(\d+)', text)
        if work_match:
            stats["total_work_count"] = int(work_match.group(2))

        if "/proc/" in str(active_path):
            if stats["session_count"] == 0:
                session_lines = [line for line in text.splitlines() if line.strip().startswith("session") or "id:" in line]
                count = len(session_lines)
                if count > 0:
                    stats["session_count"] = count
                    if stats["active_instances"] == 0:
                        stats["active_instances"] = count

        return stats

    def _get_fps_from_gfxinfo(self) -> float:
        """通过 gfxinfo 获取 FPS"""
        candidates = []
        # 主进程
        main_pkg = self.package_name.split(':')[0]
        candidates.append(("main", main_pkg))
        # 媒体进程（很多播放器在 :media 子进程渲染）
        if ':' in self.package_name:
            candidates.append(("sub", self.package_name))
        else:
            candidates.append(("media", f"{main_pkg}:media"))
            candidates.append(("player", f"{main_pkg}:player"))

        # 去重
        unique_candidates = []
        seen = set()
        for source_type, pkg_name in candidates:
            if pkg_name and pkg_name not in seen:
                unique_candidates.append((source_type, pkg_name))
                seen.add(pkg_name)
        
        best_fps = 0.0
        
        for source_type, pkg_name in unique_candidates:
            try:
                gfx_output = self._run_adb_command(["shell", "dumpsys", "gfxinfo", pkg_name], timeout=3)
                if not gfx_output or "Error:" in gfx_output:
                    continue
                
                # 检查帧数
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
                
                if total_frames < 10:
                    continue
                
                fps = self._parse_gfxinfo_fps(gfx_output)
                
                # Framestats fallback
                if fps == 0:
                    fps = self._get_fps_from_framestats(pkg_name)
                
                if fps > 0 and fps < 120:
                    if fps > best_fps:
                        best_fps = fps
            except Exception:
                continue
                
        if best_fps > 0:
            return best_fps
            
        # 传统方法 fallback
        # ... (简化处理，上面的逻辑已经覆盖了大部分情况)
        return 0.0

    def _get_fps_from_framestats(self, pkg_name: str) -> float:
        """通过 framestats 获取 FPS"""
        try:
            framestats_out = self._run_adb_command(
                ["shell", "dumpsys", "gfxinfo", pkg_name, "framestats"], 
                timeout=3
            )
            if not framestats_out:
                return 0.0

            lines = framestats_out.splitlines()
            valid_frames = []
            
            for line in lines:
                parts = line.split(',')
                if len(parts) >= 13:
                    try:
                        vsync = int(parts[1])
                        if vsync > 0:
                            valid_frames.append(vsync)
                    except (ValueError, IndexError):
                        pass
            
            if len(valid_frames) < 10:
                return 0.0
                
            intervals = []
            valid_frames.sort()
            recent_frames = valid_frames[-120:] if len(valid_frames) > 120 else valid_frames
            
            for i in range(1, len(recent_frames)):
                diff = recent_frames[i] - recent_frames[i-1]
                if 8_000_000 <= diff <= 200_000_000: 
                    intervals.append(diff)
            
            if not intervals:
                return 0.0
                
            avg_ns = statistics.mean(intervals)
            if avg_ns > 0:
                fps = 1_000_000_000.0 / avg_ns
                return round(fps, 2)
                
            return 0.0
        except Exception:
            return 0.0

    def _parse_gfxinfo_fps(self, output: str) -> float:
        """
        从 gfxinfo 解析 FPS
        策略：
        1. 尝试解析 'Total frames rendered' 并计算基于时间的增量 FPS
        2. 如果有 'Profile data'，尝试计算瞬时 FPS (不太准确，仅作参考)
        """
        try:
            current_time = time.time()
            total_frames = 0
            
            # 1. 解析 Total frames rendered
            for line in output.splitlines():
                if "Total frames rendered:" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        try:
                            total_frames = int(parts[1].strip().split()[0])
                            break
                        except (ValueError, IndexError):
                            pass
            
            fps = 0.0
            
            # 如果解析到了总帧数，尝试计算增量 FPS
            if total_frames > 0:
                if self._last_total_frames > 0 and self._last_frame_timestamp > 0:
                    delta_frames = total_frames - self._last_total_frames
                    delta_time = current_time - self._last_frame_timestamp
                    
                    # 只有当时间间隔合理（例如 > 0.5秒）且有帧数变化时才计算
                    if delta_time > 0.5 and delta_frames >= 0:
                        fps = delta_frames / delta_time
                        
                        # 过滤异常值
                        if fps > 120:
                            fps = 0.0
                
                # 更新状态
                self._last_total_frames = total_frames
                self._last_frame_timestamp = current_time
                
                if fps > 0:
                    return round(fps, 2)

            # 2. 如果增量计算失败（例如第一次运行，或 reset 之后），
            # 尝试解析 Profile data 中的最近帧来估算（作为 fallback）
            # 注意：这通常不准确，因为不知道帧间隔
            # 这里我们仅作为最后的手段
            
            return 0.0
            
        except Exception:
            return 0.0

    def _get_fps_from_surfaceflinger(self, display_id: int = 0) -> float:
        """通过 SurfaceFlinger 获取 FPS"""
        try:
            # 1. 获取列表
            cmd = ["shell", "dumpsys", "SurfaceFlinger", "--list"]
            if display_id == 1:
                cmd_display = ["shell", "dumpsys", "SurfaceFlinger", "--display-id", "1", "--list"]
                out = self._run_adb_command(cmd_display, timeout=2)
                if out and "Error" not in out:
                    # 如果支持 display-id 参数
                    pass
                else:
                    out = self._run_adb_command(cmd, timeout=2)
            else:
                out = self._run_adb_command(cmd, timeout=2)
            
            if not out: return 0.0
            
            # 2. 查找 Surface
            surfaces = out.splitlines()
            video_surface = None
            
            # 简化逻辑：查找包含包名或 Video 的 Surface
            for s in surfaces:
                if self.package_name in s or "SurfaceView" in s or "Video" in s:
                    # 简单过滤
                    video_surface = s.strip()
                    if self.package_name in s: # 优先匹配包名
                        break
            
            if not video_surface: return 0.0
            
            # 3. Latency
            latency_out = self._run_adb_command(["shell", "dumpsys", "SurfaceFlinger", "--latency", video_surface], timeout=2)
            if not latency_out: return 0.0
            
            lines = latency_out.strip().splitlines()
            if len(lines) < 3: return 0.0
            
            # 4. 计算
            frame_times = []
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 1:
                    try:
                        t = int(parts[0])
                        if t > 0: frame_times.append(t)
                    except: pass
            
            if len(frame_times) < 2: return 0.0
            
            recent = frame_times[-30:]
            intervals = []
            for i in range(1, len(recent)):
                diff = recent[i] - recent[i-1]
                if 8_000_000 <= diff <= 100_000_000:
                    intervals.append(diff)
            
            if not intervals: return 0.0
            
            avg_ns = sum(intervals) / len(intervals)
            if avg_ns > 0:
                return round(1_000_000_000.0 / avg_ns, 2)
                
            return 0.0
        except Exception:
            return 0.0

    def _get_frame_metrics_from_framestats(self, pkg_name: str) -> Optional[Dict[str, Any]]:
        framestats_out = self._run_adb_command(
            ["shell", "dumpsys", "gfxinfo", pkg_name, "framestats"],
            timeout=3
        )
        if not framestats_out or "Error:" in framestats_out:
            return None

        timestamps: List[int] = []
        for line in framestats_out.splitlines():
            parts = line.split(',')
            if len(parts) < 13:
                continue
            try:
                vsync = int(parts[1])
            except Exception:
                continue
            if vsync > 0:
                timestamps.append(vsync)

        if len(timestamps) < 15:
            return None

        timestamps.sort()
        intervals_ms = self._timestamps_to_intervals_ms(timestamps, max_intervals=180, max_gap_ms=400.0)
        if len(intervals_ms) < 10:
            return None

        return self._compute_frame_metrics(intervals_ms)

    def _get_frame_metrics_from_surfaceflinger(self, display_id: int = 0) -> Optional[Dict[str, Any]]:
        try:
            cmd = ["shell", "dumpsys", "SurfaceFlinger", "--list"]
            if display_id == 1:
                cmd_display = ["shell", "dumpsys", "SurfaceFlinger", "--display-id", "1", "--list"]
                out = self._run_adb_command(cmd_display, timeout=2)
                if not out or "Error" in out:
                    out = self._run_adb_command(cmd, timeout=2)
            else:
                out = self._run_adb_command(cmd, timeout=2)

            if not out:
                return None

            surfaces = out.splitlines()
            video_surface = None

            for s in surfaces:
                if self.package_name in s or "SurfaceView" in s or "Video" in s:
                    video_surface = s.strip()
                    if self.package_name in s:
                        break

            if not video_surface:
                return None

            latency_out = self._run_adb_command(
                ["shell", "dumpsys", "SurfaceFlinger", "--latency", video_surface],
                timeout=2
            )
            if not latency_out:
                return None

            lines = latency_out.strip().splitlines()
            if len(lines) < 4:
                return None

            present_ts: List[int] = []
            for line in lines[1:]:
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    desired = int(parts[0])
                    actual = int(parts[1])
                except Exception:
                    continue
                ts = actual if actual > 0 else desired
                if ts > 0:
                    present_ts.append(ts)

            if len(present_ts) < 10:
                return None

            present_ts.sort()
            intervals_ms = self._timestamps_to_intervals_ms(present_ts, max_intervals=120, max_gap_ms=250.0)
            if len(intervals_ms) < 10:
                return None

            return self._compute_frame_metrics(intervals_ms)
        except Exception:
            return None

    @staticmethod
    def _timestamps_to_intervals_ms(timestamps_ns: List[int], max_intervals: int, max_gap_ms: float) -> List[float]:
        if not timestamps_ns or len(timestamps_ns) < 2:
            return []

        recent = timestamps_ns[-(max_intervals + 1):]
        intervals_ms: List[float] = []
        for i in range(1, len(recent)):
            diff_ns = recent[i] - recent[i - 1]
            if diff_ns <= 0:
                continue
            diff_ms = diff_ns / 1_000_000.0
            if diff_ms < 5.0 or diff_ms > max_gap_ms:
                continue
            intervals_ms.append(diff_ms)
        return intervals_ms

    @staticmethod
    def _compute_frame_metrics(intervals_ms: List[float]) -> Dict[str, Any]:
        if not intervals_ms:
            return {
                "fps": 0.0,
                "jank_count": 0,
                "dropped_frames_estimate": 0,
                "frame_interval_median_ms": 0.0,
                "frame_times_ms": []
            }

        median_ms = statistics.median(intervals_ms)
        if median_ms <= 0:
            median_ms = 16.67

        base_ms = median_ms
        if base_ms < 8.0 or base_ms > 50.0:
            base_ms = 16.67

        jank_threshold_ms = base_ms * 1.5

        jank_count = 0
        dropped_est = 0
        clipped: List[float] = []

        for ms in intervals_ms[-120:]:
            clipped.append(ms)
            if ms > jank_threshold_ms:
                jank_count += 1
                ratio = ms / base_ms if base_ms > 0 else 0.0
                missed = int(ratio - 0.5)
                if missed > 0:
                    dropped_est += missed

        fps = 1000.0 / base_ms if base_ms > 0 else 0.0
        if fps > 120:
            fps = 0.0

        return {
            "fps": round(fps, 2) if fps > 0 else 0.0,
            "jank_count": int(jank_count),
            "dropped_frames_estimate": int(dropped_est),
            "frame_interval_median_ms": round(float(base_ms), 2),
            "frame_times_ms": clipped
        }

    def _detect_tv_display_id(self) -> Optional[int]:
        """检测电视端 Display ID"""
        current_time = time.time()
        if (self._tv_display_id is not None and 
            current_time - self._tv_display_id_cache_time < self._tv_display_id_cache_ttl):
            return self._tv_display_id
            
        try:
            output = self._run_adb_command(
                ["shell", "dumpsys", "SurfaceFlinger", "--display-id", "1", "--list"],
                timeout=2
            )
            if output and "Error:" not in output and "not found" not in output.lower():
                self._tv_display_id = 1
                self._tv_display_id_cache_time = current_time
                return 1
        except:
            pass
            
        self._tv_display_id = None
        return None
