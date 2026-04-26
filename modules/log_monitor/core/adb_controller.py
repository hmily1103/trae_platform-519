"""
简化版 ADB Controller - 适配 Web 端
支持日志监控和性能监控功能
使用全局 ADB 连接池，避免多模块并发冲突
"""
import subprocess
import os
import threading
import time
import re
import statistics
from typing import Optional, Callable, List, Tuple, Dict, Any
from datetime import datetime
from utils.logger import setup_logger
from .models.analysis_models import PerformanceSnapshot, ProcessSnapshot

logger = setup_logger('log_monitor_adb')

from core.device.manager import get_device_manager

class AdbController:
    """简化的 ADB Controller，专注于日志监控"""

    def __init__(self, adb_path="adb"):
        """
        初始化控制器
        
        :param adb_path: ADB 可执行文件路径
        """
        self.adb_path = adb_path
        self.logcat_process = None
        self.monitoring_thread = None
        self.is_monitoring = False
        self.current_device_id = None
        self.log_callback: Optional[Callable] = None
        self.performance_callback: Optional[Callable] = None
        self.filter_func: Optional[Callable] = None
        self.min_log_level = "Verbose"
        self.performance_monitoring_thread = None
        self.target_package = "com.thunder.ktv"
        self.polling_interval = 3.0
        self.last_net_stats = None  # {timestamp, rx_bytes, tx_bytes}
        self._cached_process_snapshots = []
        self._cached_fps = 0
        self._cached_jank = 0
        self._cached_frame_times = []  # 帧时间序列（用于人眼感知卡顿分析）
        self._cached_rx = 0.0
        self._cached_tx = 0.0
        self._fps_collector = None
        self._fps_collector_pkg = None
        
        # 引入 Unified Device Manager
        self.device_manager = get_device_manager()

    def get_connected_devices(self):
        """获取已连接的设备列表（优先使用 Unified Device Manager）"""
        if self.device_manager:
            return self.device_manager.get_devices()
            
        try:
            result = subprocess.run(
                [self.adb_path, "devices"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                return []
            devices = []
            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == 'device':
                    devices.append(parts[0])
            return devices
        except Exception as e:
            logger.error(f"获取设备列表失败: {e}")
            return []

    def connect_device(self, ip: str, port: int = 8787) -> bool:
        """
        连接设备（优先使用 Unified Device Manager）
        :param ip: 设备IP
        :param port: ADB端口
        :return: 是否成功
        """
        if ip.startswith('mock_') or ip == 'mock_device':
            self.current_device_id = f"{ip}:{port}"
            # 如果启用了 Mock，通知 Device Manager 开启 Mock 模式
            if self.device_manager:
                self.device_manager.enable_mock(True)
            return True
            
        if self.device_manager:
            ok = self.device_manager.connect(ip, port)
            if ok:
                self.current_device_id = f"{ip}:{port}"
            return ok
            
        if _pool_connect:

            ok = _pool_connect(ip, port)
            if ok:
                self.current_device_id = f"{ip}:{port}"
            return ok
        try:
            device_id = f"{ip}:{port}"
            result = subprocess.run(
                [self.adb_path, "connect", device_id],
                capture_output=True,
                text=True,
                timeout=5
            )
            if "connected to" in (result.stdout or "").lower() or "already connected" in (result.stdout or "").lower():
                self.current_device_id = device_id
                return True
            return False
        except Exception as e:
            logger.error(f"连接设备失败: {e}")
            return False

    def disconnect_device(self, device_id: Optional[str] = None):
        """断开设备连接（使用全局连接池）"""
        target = device_id or self.current_device_id
        if not target:
            return
        if self.device_manager and ":" in target:
             parts = target.rsplit(":", 1)
             if len(parts) == 2 and parts[1].isdigit():
                self.device_manager.disconnect(parts[0], int(parts[1]))
        else:
            try:
                subprocess.run(
                    [self.adb_path, "disconnect", target],
                    capture_output=True,
                    timeout=5
                )
            except Exception:
                pass
        if target == self.current_device_id:
            self.current_device_id = None

    def start_monitoring(self, device_id: str, log_callback: Callable, 
                        filter_func: Optional[Callable] = None,
                        min_log_level: str = "Verbose",
                        performance_callback: Optional[Callable] = None,
                        target_package: str = "com.thunder.ktv"):
        """
        开始监控日志
        
        :param device_id: 设备ID
        :param log_callback: 日志回调函数 (log_line, analysis_result)
        :param filter_func: 过滤函数
        :param min_log_level: 最小日志级别
        :param performance_callback: 性能数据回调函数（已废弃，不再使用）
        :param target_package: 目标应用包名
        """
        if self.is_monitoring:
            self.stop_monitoring()
        
        self.current_device_id = device_id
        self.log_callback = log_callback
        self.filter_func = filter_func
        self.min_log_level = min_log_level
        self.target_package = target_package
        self.performance_callback = performance_callback  # 保存性能回调
        self.is_monitoring = True
        
        self.monitoring_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self.monitoring_thread.start()
        
        # 如果传入了性能回调，启动性能监控循环
        if performance_callback:
            self.performance_monitoring_thread = threading.Thread(
                target=self._performance_monitoring_loop, 
                daemon=True
            )
            self.performance_monitoring_thread.start()

    def stop_monitoring(self):
        """停止监控"""
        self.is_monitoring = False
        
        if self.logcat_process:
            try:
                self.logcat_process.terminate()
                self.logcat_process.wait(timeout=2)
            except Exception:
                try:
                    self.logcat_process.kill()
                except Exception:
                    pass
            self.logcat_process = None
        
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=2)
        
        # 性能监控线程已移除

    def _monitoring_loop(self):
        """监控循环"""
        from .log_analyzer import LogAnalyzer
        
        analyzer = LogAnalyzer()
        self.analyzer = analyzer  # 保存 analyzer 供性能监控使用
        retry_count = 0
        max_retries = 5
        
        # Mock mode handling
        if self.current_device_id and 'mock_' in self.current_device_id:
            logger.info("Starting mock monitoring loop")
            import random
            while self.is_monitoring:
                time.sleep(0.1)
                log_line = f"01-01 12:00:00.000 1000 1000 D MockDevice: Mock log message {random.randint(0, 100)}"
                
                # Apply filter
                if self.filter_func and not self.filter_func(log_line):
                    continue
                
                # Analyze
                analysis_result = None
                try:
                    analysis_result = analyzer.analyze_line(log_line)
                except Exception:
                    pass
                
                # Callback
                if self.log_callback:
                    try:
                        self.log_callback(log_line, analysis_result)
                    except Exception as e:
                        logger.error(f"Mock log callback failed: {e}")
            return

        while self.is_monitoring:
            try:
                # 清空日志缓冲区
                self._clear_logcat()
                
                # 启动 logcat 进程
                log_stream = self._start_logcat()
                if not log_stream:
                    retry_count += 1
                    if retry_count >= max_retries:
                        logger.error("启动 logcat 失败，达到最大重试次数")
                        break
                    time.sleep(2)
                    continue
                
                retry_count = 0  # 重置重试计数
                
                # 读取日志流
                while self.is_monitoring:
                    log_line = log_stream.readline()
                    if not log_line:
                        break  # 连接断开
                    
                    log_line = log_line.strip()
                    if not log_line:
                        continue
                    
                    # 应用过滤
                    if self.filter_func and not self.filter_func(log_line):
                        continue
                    
                    # 分析日志
                    analysis_result = None
                    try:
                        analysis_result = analyzer.analyze_line(log_line)
                    except Exception:
                        pass
                    
                    # 调用回调
                    if self.log_callback:
                        try:
                            self.log_callback(log_line, analysis_result)
                        except Exception as e:
                            logger.error(f"日志回调失败: {e}")
                            
            except Exception as e:
                logger.error(f"监控循环错误: {e}")
                time.sleep(2)
            finally:
                # 清理 logcat 进程
                if self.logcat_process:
                    try:
                        self.logcat_process.terminate()
                        self.logcat_process.wait(timeout=1)
                    except Exception:
                        try:
                            self.logcat_process.kill()
                        except Exception:
                            pass
                    self.logcat_process = None
                
                if self.is_monitoring:
                    # 连接断开，尝试重连
                    time.sleep(2)

    def _clear_logcat(self):
        """清空 logcat 缓冲区"""
        try:
            cmd = [self.adb_path]
            if self.current_device_id:
                cmd.extend(["-s", self.current_device_id])
            cmd.extend(["logcat", "-c"])
            subprocess.run(cmd, capture_output=True, timeout=5)
        except Exception:
            pass

    def _start_logcat(self):
        """启动 logcat 进程"""
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            command = [self.adb_path]
            if self.current_device_id:
                command.extend(["-s", self.current_device_id])
            command.extend(["logcat", "-v", "threadtime"])
            
            # 应用日志级别过滤
            if self.min_log_level:
                level_char = self.min_log_level[0].upper()
                if level_char in ['V', 'D', 'I', 'W', 'E', 'F']:
                    command.append(f"*:{level_char}")
            
            self.logcat_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=startupinfo
            )
            return self.logcat_process.stdout
        except Exception as e:
            logger.error(f"启动 logcat 失败: {e}")
            return None

    def _performance_monitoring_loop(self):
        """性能监控循环"""
        tick = 0
        while self.is_monitoring:
            try:
                if not self.target_package:
                    time.sleep(self.polling_interval)
                    continue
                
                # 分阶段收集策略（降低负载）
                # Cycle 0: CPU & Memory (Top) - 重负载
                # Cycle 1: FPS (Gfxinfo) - 重负载
                # Cycle 2: Network - 轻负载
                current_cycle = tick % 3
                
                # 1. CPU/Memory (Top) + 系统 Top10 进程
                if current_cycle == 0:
                    self._cached_process_snapshots = self._collect_performance_data(self.target_package)
                    self._cached_system_top10 = self._collect_system_top_processes(10)
                
                # 2. FPS
                if current_cycle == 1:
                    self._cached_fps, self._cached_jank, self._cached_frame_times = self._collect_fps_data(self.target_package)
                
                # 3. Network
                if current_cycle == 2:
                    self._cached_rx, self._cached_tx = self._collect_network_data()
                
                # 生成性能快照
                if not hasattr(self, '_cached_process_snapshots'):
                    self._cached_process_snapshots = []
                if not hasattr(self, '_cached_system_top10'):
                    self._cached_system_top10 = []
                if not hasattr(self, '_cached_fps'):
                    self._cached_fps, self._cached_jank = 0, 0
                if not hasattr(self, '_cached_frame_times'):
                    self._cached_frame_times = []
                if not hasattr(self, '_cached_rx'):
                    self._cached_rx, self._cached_tx = 0.0, 0.0
                
                process_snapshots = self._cached_process_snapshots
                system_top10 = getattr(self, '_cached_system_top10', []) or []
                
                if process_snapshots or self._cached_fps > 0 or self._cached_rx > 0 or system_top10:
                    total_pss = sum(p.pss_kb for p in process_snapshots) if process_snapshots else 0
                    total_cpu = sum(p.cpu_usage for p in process_snapshots) if process_snapshots else 0
                    total_gc = sum(p.gc_count for p in process_snapshots) if process_snapshots else 0
                    
                    snapshot = PerformanceSnapshot(
                        timestamp=datetime.now(),
                        processes=process_snapshots,
                        system_top_processes=system_top10,
                        device_info=self.current_device_id or "",
                        total_pss=total_pss,
                        gc_count=total_gc,
                        cpu_usage=total_cpu,
                        fps=self._cached_fps,
                        jank_count=self._cached_jank,
                        network_rx_kb=self._cached_rx,
                        network_tx_kb=self._cached_tx
                    )
                    
                    if self.performance_callback:
                        try:
                            self.performance_callback(snapshot)
                        except Exception as e:
                            logger.error(f"性能回调失败: {e}")
                
                tick += 1
                
            except Exception as e:
                logger.error(f"性能监控循环错误: {e}")
            
            # 等待下一次轮询
            step_sleep = max(1.0, self.polling_interval / 3.0)
            sleep_start = time.time()
            while time.time() - sleep_start < step_sleep:
                if not self.is_monitoring:
                    break
                time.sleep(0.1)

    def _collect_performance_data(self, package_name: str) -> List[ProcessSnapshot]:
        """收集 CPU 和内存数据"""
        # Mock Support
        if self.current_device_id and 'mock_' in self.current_device_id:
            import random
            return [ProcessSnapshot(
                pid=1234,
                process_name=package_name,
                cpu_usage=random.uniform(0, 100),
                rss_kb=random.randint(10000, 50000),
                pss_kb=random.randint(5000, 40000),
                gc_count=random.randint(0, 10)
            )]

        snapshots = []
        try:
            # 1. 使用 top 获取 PID, CPU, RSS
            cmd = [self.adb_path]
            if self.current_device_id:
                cmd.extend(["-s", self.current_device_id])
            cmd.extend(["shell", "top", "-n", "1", "-b"])
            
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=5,
                startupinfo=startupinfo
            )
            if result.returncode != 0:
                return []
            
            pid_map = {}  # PID -> {'cpu': float, 'rss': int, 'name': str}
            
            for line in result.stdout.splitlines():
                if package_name in line:
                    parts = line.split()
                    try:
                        pid = int(parts[0])
                        cmd_name = parts[-1]
                        
                        if not cmd_name.startswith(package_name):
                            continue
                        
                        cpu_val = 0.0
                        rss_val = 0
                        
                        # 查找 CPU 百分比
                        cpu_found = False
                        for part in parts:
                            if part.endswith('%') and not cpu_found:
                                try:
                                    cpu_val = float(part.rstrip('%'))
                                    cpu_found = True
                                except ValueError:
                                    pass
                        
                        # 如果没找到，尝试标准位置（第8列）
                        if not cpu_found and len(parts) > 8:
                            try:
                                cpu_val = float(parts[8].rstrip('%'))
                            except (ValueError, IndexError):
                                pass
                        
                        # 解析 RSS (RES) - 通常是第6列
                        if len(parts) > 6:
                            res_str = parts[5]
                            if 'M' in res_str:
                                rss_val = int(float(res_str.replace('M', '')) * 1024)
                            elif 'K' in res_str:
                                rss_val = int(float(res_str.replace('K', '')))
                            elif res_str.isdigit():
                                rss_val = int(res_str)
                        
                        pid_map[pid] = {'cpu': cpu_val, 'rss': rss_val, 'name': cmd_name, 'pss': 0}
                    except Exception:
                        continue
            
            if not pid_map:
                return []
            
            # 2. 使用 dumpsys meminfo 获取 PSS
            cmd_mem = [self.adb_path]
            if self.current_device_id:
                cmd_mem.extend(["-s", self.current_device_id])
            cmd_mem.extend(["shell", "dumpsys", "meminfo", package_name])
            
            mem_result = subprocess.run(
                cmd_mem, 
                capture_output=True, 
                text=True, 
                timeout=10,
                startupinfo=startupinfo
            )
            
            current_parsing_pid = None
            for line in mem_result.stdout.splitlines():
                if "** MEMINFO in pid" in line:
                    try:
                        parts = line.split()
                        pid_idx = parts.index("pid") + 1
                        current_parsing_pid = int(parts[pid_idx])
                    except (ValueError, IndexError):
                        current_parsing_pid = None
                    continue
                
                if current_parsing_pid and "TOTAL" in line:
                    try:
                        parts = line.split()
                        total_idx = parts.index("TOTAL")
                        pss_val = int(parts[total_idx + 1])
                        if current_parsing_pid in pid_map:
                            pid_map[current_parsing_pid]['pss'] = pss_val
                    except Exception:
                        pass
            
            # 3. 构建快照
            for pid, data in pid_map.items():
                gc_count = 0
                if hasattr(self, 'analyzer') and self.analyzer:
                    gc_count = self.analyzer.gc_events.get(pid, 0)
                snapshot = ProcessSnapshot(
                    pid=pid,
                    process_name=data['name'],
                    cpu_usage=data['cpu'],
                    rss_kb=data['rss'],
                    pss_kb=data['pss'],
                    gc_count=gc_count
                )
                snapshots.append(snapshot)
            
            return snapshots
            
        except Exception as e:
            logger.error(f"收集性能数据失败: {e}")
            return []

    def _collect_system_top_processes(self, limit: int = 10) -> List[ProcessSnapshot]:
        """收集系统级 Top N 进程（按 CPU），用于查看整机占用。"""
        if self.current_device_id and 'mock_' in self.current_device_id:
            import random
            return [ProcessSnapshot(
                pid=2000 + i,
                process_name=f"system_process_{i}",
                cpu_usage=random.uniform(0, 30),
                rss_kb=random.randint(5000, 80000),
                pss_kb=0,
                gc_count=0
            ) for i in range(min(limit, 10))]

        out_list = []
        try:
            cmd = [self.adb_path]
            if self.current_device_id:
                cmd.extend(["-s", self.current_device_id])
            cmd.extend(["shell", "top", "-n", "1", "-b"])
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5, startupinfo=startupinfo
            )
            if result.returncode != 0 or not result.stdout:
                return []

            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 6:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                if pid <= 0:
                    continue
                cmd_name = parts[-1] if parts else ""
                cpu_val = 0.0
                for part in parts:
                    if part.endswith('%'):
                        try:
                            cpu_val = float(part.rstrip('%'))
                            break
                        except ValueError:
                            pass
                if len(parts) > 6:
                    res_str = parts[5]
                    if 'M' in res_str:
                        rss_val = int(float(res_str.replace('M', '')) * 1024)
                    elif 'K' in res_str:
                        rss_val = int(float(res_str.replace('K', '')))
                    elif res_str.isdigit():
                        rss_val = int(res_str)
                    else:
                        rss_val = 0
                else:
                    rss_val = 0
                out_list.append(ProcessSnapshot(
                    pid=pid,
                    process_name=cmd_name,
                    cpu_usage=cpu_val,
                    rss_kb=rss_val,
                    pss_kb=0,
                    gc_count=0
                ))
            out_list.sort(key=lambda p: p.cpu_usage, reverse=True)
            return out_list[:limit]
        except Exception as e:
            logger.error(f"收集系统 Top 进程失败: {e}")
            return []

    def _collect_fps_data(self, package_name: str) -> Tuple[int, int, List[float]]:
        """
        收集 FPS 数据
        
        :return: (fps, jank_count, frame_times_list)
        """
        # Mock Support
        if self.current_device_id and 'mock_' in self.current_device_id:
            import random
            fps = random.randint(30, 60)
            return fps, 0, [1000.0/fps] * fps

        try:
            # 复用性能监控模块的 FPS 采集逻辑（支持 Display ID）
            from modules.performance_monitor.core.collectors.fps_collector import FpsCollector

            if self._fps_collector is None or self._fps_collector_pkg != package_name:
                self._fps_collector = FpsCollector(self, package_name)
                self._fps_collector_pkg = package_name

            collector = self._fps_collector
            # 使用首选 Display ID（例如 1 表示电视端）
            preferred_display_id = getattr(self, "preferred_display_id", None)
            if preferred_display_id is not None:
                collector.preferred_display_id = preferred_display_id

            fps = collector.measure_video_fps(display_id=preferred_display_id)
            fps_int = int(fps) if fps and fps > 0 else 0
            # 当前实现不从 SurfaceFlinger 提取标准 Jank 和帧时间，先返回 0 与空列表
            return fps_int, 0, []

        except Exception as e:
            logger.error(f"收集 FPS 数据失败: {e}")
            return 0, 0, []

    def _get_fps_from_framestats(self, package_name: str) -> float:
        """通过 framestats 获取 FPS (Fallback)"""
        try:
            cmd = [self.adb_path]
            if self.current_device_id:
                cmd.extend(["-s", self.current_device_id])
            cmd.extend(["shell", "dumpsys", "gfxinfo", package_name, "framestats"])
            
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=3,
                startupinfo=startupinfo
            )
            
            if not result.stdout:
                return 0.0

            lines = result.stdout.splitlines()
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
                # 过滤掉不合理的间隔 (例如 > 200ms 或 < 8ms)
                if 8_000_000 <= diff <= 200_000_000: 
                    intervals.append(diff)
            
            if not intervals:
                return 0.0
                
            avg_ns = statistics.mean(intervals)
            if avg_ns > 0:
                fps = 1_000_000_000.0 / avg_ns
                return round(fps, 2)
                
            return 0.0
        except Exception as e:
            logger.warning(f"framestats FPS 计算失败: {e}")
            return 0.0

    def _collect_network_data(self) -> Tuple[float, float]:
        """收集网络数据"""
        # Mock Support
        if self.current_device_id and 'mock_' in self.current_device_id:
            import random
            return random.uniform(0, 100), random.uniform(0, 100)

        try:
            cmd = [self.adb_path]
            if self.current_device_id:
                cmd.extend(["-s", self.current_device_id])
            cmd.extend(["shell", "cat", "/proc/net/dev"])
            
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=2,
                startupinfo=startupinfo
            )
            if result.returncode != 0:
                return 0.0, 0.0
            
            total_rx = 0
            total_tx = 0
            timestamp = time.time()
            
            for line in result.stdout.splitlines():
                clean_line = line.replace(':', ' ').strip()
                parts = clean_line.split()
                if not parts:
                    continue
                
                iface = parts[0]
                if iface.startswith('wlan') or iface.startswith('rmnet') or iface.startswith('eth'):
                    if len(parts) > 9:
                        try:
                            total_rx += int(parts[1])
                            total_tx += int(parts[9])
                        except ValueError:
                            pass
            
            rx_speed = 0.0
            tx_speed = 0.0
            
            if self.last_net_stats:
                dt = timestamp - self.last_net_stats['time']
                if dt > 0:
                    rx_diff = total_rx - self.last_net_stats['rx']
                    tx_diff = total_tx - self.last_net_stats['tx']
                    rx_speed = (rx_diff / 1024.0) / dt
                    tx_speed = (tx_diff / 1024.0) / dt
            
            self.last_net_stats = {'time': timestamp, 'rx': total_rx, 'tx': total_tx}
            return max(0.0, rx_speed), max(0.0, tx_speed)
            
        except Exception as e:
            logger.error(f"收集网络数据失败: {e}")
            return 0.0, 0.0
