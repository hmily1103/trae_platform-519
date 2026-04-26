import re
import time
from typing import Dict, Any, Optional
from .base_collector import BaseCollector

class RkCollector(BaseCollector):
    """
    Rockchip MPP (Media Process Platform) 硬件状态监控
    用于获取 RK3576 等芯片的硬件编解码器运行状态
    移植自 player_stress/core/rk_monitor.py
    """
    
    MPP_STATS_PATHS = [
        "/sys/kernel/debug/mpp_service/stats",
        "/proc/mpp_service/rkvdec/task_count",  # 新增: RK3588/RK3568 等新内核路径
        "/proc/mpp_service/vdpu/task_count",    # 新增: 旧款芯片路径
        "/proc/mpp_service/sessions-summary",
        "/proc/mpp_service/all",
        "/sys/kernel/debug/rkmpp/load",
        "/proc/mpp_service/load"
    ]
    
    def __init__(self, adb_controller: Any):
        super().__init__(adb_controller)
        self.is_supported = None # Lazy check
        self.active_path = None
        # V2.3: 用于检测解码器卡死
        self.last_work_count = 0
        self.last_work_count_time = 0.0
        
        # 缓存机制，防止高频调用导致 delta 计算为 0
        self._last_stats_cache = None
        self._last_stats_time = 0.0
        self._min_update_interval = 0.5  # 最小更新间隔 0.5 秒

    def check_support(self) -> bool:
        """检查设备是否支持 MPP 监控 (需要 Root 权限读取 debugfs 或 procfs)"""
        # 如果已经检测过支持 Rockchip，直接返回 True
        if self.is_supported is True:
            return True
            
        # 如果之前检测过不支持 Rockchip，我们现在支持通用模式，所以总是返回 True
        # 但我们需要区分是 Native RK 模式还是 Generic 模式
        if self.is_supported is False:
            return True # 启用通用模式

        for path in self.MPP_STATS_PATHS:
            # 尝试读取文件是否存在且可读
            # 优先尝试 su 0 (更可靠)
            cmd = ["shell", "su", "0", "ls", path]
            output = self._run_adb_command(cmd)
            
            # 如果 su 0 失败 (例如不支持 su 0 语法)，尝试 su -c
            if "invalid" in output.lower() or "not found" in output.lower() and "no such file" not in output.lower():
                 cmd = ["shell", "su", "-c", f"'ls {path}'"]
                 output = self._run_adb_command(cmd)
            
            if "No such file" not in output and "Permission denied" not in output and "Error" not in output:
                # print(f"[RkCollector] Rockchip MPP stats detected at: {path}")
                self.is_supported = True
                self.active_path = path
                return True
        
        # print(f"[RkCollector] MPP stats not accessible. Checked: {self.MPP_STATS_PATHS}")
        # 未发现 Rockchip 节点，标记为 False，但在 get_mpp_stats 中会降级为通用模式
        self.is_supported = False
        return True # 依然返回 True 以启用采集器 (通用模式)

    def _run_adb_command(self, cmd_list):
        """适配不同类型的 ADB 控制器"""
        # 如果是 player_stress 的 AdbManager
        if hasattr(self.adb, "_run_command"):
            return self.adb._run_command(cmd_list)
        
        # 如果是 log_monitor 的 AdbController
        # 它通常使用 subprocess 直接调用，这里我们需要模拟
        # 这是一个临时适配，理想情况下 AdbController 应该提供通用接口
        import subprocess
        adb_path = getattr(self.adb, "adb_path", "adb")
        device_id = getattr(self.adb, "current_device_id", None)
        
        full_cmd = [adb_path]
        if device_id:
            full_cmd.extend(["-s", device_id])
        
        # cmd_list 通常是 ["shell", ...]
        full_cmd.extend(cmd_list)
        
        try:
            # 处理 'su -c' 中的引号问题
            # 这里简单处理，如果参数包含空格，可能需要特殊处理
            # 但 subprocess 列表形式通常能处理好
            
            # 特殊处理: 如果是 su -c 'ls path'，cmd_list 可能是 ["shell", "su", "-c", "'ls path'"]
            # subprocess 不需要外层引号
            final_cmd = []
            for arg in full_cmd:
                if arg.startswith("'") and arg.endswith("'"):
                    final_cmd.append(arg[1:-1])
                else:
                    final_cmd.append(arg)
            
            result = subprocess.run(final_cmd, capture_output=True, text=True, timeout=2, encoding='utf-8', errors='ignore')
            return result.stdout
        except Exception as e:
            return f"Error: {e}"

    def collect(self) -> Dict[str, Any]:
        """
        获取 MPP 统计信息
        返回字典: {
            "mpp_active": int,
            "mpp_sessions": int,
            "mpp_work_count": int,
            "mpp_work_count_delta": int,
            "decoder_stuck": bool,
            "tv_stutter_detected": bool
        }
        """
        return self.get_mpp_stats()

    def reset(self):
        self.last_work_count = 0
        self.last_work_count_time = 0.0

    def get_mpp_stats(self) -> Dict:
        """
        获取 MPP 统计信息 (核心逻辑)
        """
        # V2.3: 缓存检查
        current_time_check = time.time()
        if (self._last_stats_cache is not None and 
            current_time_check - self._last_stats_time < self._min_update_interval):
            return self._last_stats_cache.copy()

        if self.is_supported is None:
            self.check_support()

        # 非 Rockchip 或不支持 debugfs 的场景，走通用 MediaCodec 采集
        if not self.is_supported:
            stats = self._get_generic_mediacodec_stats()
            current_time = time.time()
            self._last_stats_cache = stats
            self._last_stats_time = current_time
            return stats

        # 尝试读取 Rockchip 专用节点
        cmd = ["shell", "su", "0", "cat", self.active_path]
        raw_text = self._run_adb_command(cmd)
        
        if "invalid" in raw_text.lower():
            cmd = ["shell", "su", "-c", f"'cat {self.active_path}'"]
            raw_text = self._run_adb_command(cmd)

        if "No such file" in raw_text or "Permission denied" in raw_text or "Error" in raw_text:
            stats = {}
        else:
            stats = self._parse_mpp_stats(raw_text)
        
        # V2.3: 检测解码器是否卡死
        current_time = time.time()
        current_work_count = stats.get("total_work_count", 0)
        
        if self.last_work_count_time > 0:
            # 计算增量
            work_count_delta = current_work_count - self.last_work_count
            time_delta = current_time - self.last_work_count_time
            
            is_task_count_mode = "rkvdec/task_count" in str(self.active_path) or "vdpu/task_count" in str(self.active_path)
            
            # 优化：阈值从 2s 降至 1s，捕捉肉眼可见的短时卡顿
            if (not is_task_count_mode and 
                time_delta >= 1.0 and 
                work_count_delta == 0 and 
                stats.get("active_instances", 0) > 0):
                stats["decoder_stuck"] = True
                stats["decoder_stuck_duration_sec"] = time_delta
            else:
                stats["decoder_stuck"] = False
                stats["decoder_stuck_duration_sec"] = 0.0
            
            stats["work_count_delta"] = work_count_delta
            stats["work_count_delta_time_sec"] = time_delta
        else:
            stats["decoder_stuck"] = False
            stats["work_count_delta"] = 0
            stats["work_count_delta_time_sec"] = 0.0
        
        # 兼容性字段映射
        stats["mpp_active"] = stats.get("active_instances", 0)
        stats["mpp_sessions"] = stats.get("session_count", 0)
        stats["mpp_work_count"] = stats.get("total_work_count", 0)
        stats["mpp_work_count_delta"] = stats.get("work_count_delta", 0)
        
        # 电视端卡顿检测
        if stats.get("decoder_stuck", False):
            stats["tv_stutter_detected"] = True
        else:
            stats["tv_stutter_detected"] = False

        # 更新记录
        self.last_work_count = current_work_count
        self.last_work_count_time = current_time
        
        # 更新缓存
        self._last_stats_cache = stats
        self._last_stats_time = current_time
        
        return stats

    def _get_generic_mediacodec_stats(self) -> Dict:
        """通用 MediaCodec 采集 (Fallback)"""
        stats = {
            "mpp_active": 0,
            "mpp_sessions": 0,
            "mpp_work_count": 0,
            "mpp_work_count_delta": 0,
            "decoder_stuck": False,
            "tv_stutter_detected": False,
            "is_generic": True
        }
        
        try:
            # 方法 1: dumpsys media.codec (较新 Android)
            # 方法 2: dumpsys media.player (查看播放状态)
            # 这里使用 media.player 来判断是否有视频播放，这比 codec 更能反映用户感知的"硬解"场景
            
            output = self._run_adb_command(["shell", "dumpsys", "media.player"])
            if output:
                # 统计活跃的 Client
                # 简单判断: 如果有 "state: started" 或 "state: playing"
                if "state: started" in output.lower() or "state: playing" in output.lower():
                    stats["mpp_active"] = 1
                    stats["mpp_sessions"] = output.lower().count("state: started") + output.lower().count("state: playing")
            
            # 如果 media.player 没数据，尝试 dumpsys media.codec
            if stats["mpp_active"] == 0:
                 output_codec = self._run_adb_command(["shell", "dumpsys", "media.codec"])
                 # 这是一个非常粗略的判断，因为 codec 输出很长
                 # 查找 "num-input-buffers" 变化很难，但可以查找 "clients:" 列表
                 # 示例: "Clients:" 下面有 PID
                 pass
                 
        except Exception:
            pass
            
        return stats

    def _parse_mpp_stats(self, raw_text: str) -> Dict:
        """解析 MPP 统计信息"""
        stats = {
            "active_instances": 0,
            "session_count": 0,
            "total_work_count": 0,
            "error_found": False
        }
        
        if not raw_text or "Error" in raw_text:
            return stats
            
        # 1. 尝试解析 debugfs 格式
        active_match = re.search(r'active_instance\s*[:=]\s*(\d+)', raw_text)
        if active_match:
            stats["active_instances"] = int(active_match.group(1))
            
        session_match = re.search(r'session_count\s*[:=]\s*(\d+)', raw_text)
        if session_match:
            stats["session_count"] = int(session_match.group(1))
            
        work_match = re.search(r'(total_usage|work_count)\s*[:=]\s*(\d+)', raw_text)
        if work_match:
            stats["total_work_count"] = int(work_match.group(2))

        # 2. 尝试解析 procfs sessions-summary
        if "/proc/" in str(self.active_path):
            if stats["session_count"] == 0:
                session_lines = [line for line in raw_text.splitlines() if line.strip().startswith("session") or "id:" in line]
                count = len(session_lines)
                if count > 0:
                    stats["session_count"] = count
                    stats["active_instances"] = count

        # 3. 尝试解析纯数字 (task_count)
        if raw_text.strip().isdigit():
            stats["total_work_count"] = int(raw_text.strip())
            stats["active_instances"] = 1

        if "error" in raw_text.lower() or "timeout" in raw_text.lower():
            stats["error_found"] = True
            
        return stats
