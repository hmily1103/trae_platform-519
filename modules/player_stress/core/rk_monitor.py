import re
import time
import logging
from typing import Dict, Optional
from .adb_manager import AdbManager

logger = logging.getLogger(__name__)


class RkMonitor:
    """
    Rockchip MPP (Media Process Platform) 硬件状态监控
    用于获取 RK3576 等芯片的硬件编解码器运行状态
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
    
    def __init__(self, adb: AdbManager):
        self.adb = adb
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
        if self.is_supported is not None:
            return self.is_supported
            
        for path in self.MPP_STATS_PATHS:
            # 尝试读取文件是否存在且可读
            # 优先尝试 su 0 (更可靠)
            cmd = ["shell", "su", "0", "ls", path]
            output = self.adb._run_command(cmd)
            
            # 如果 su 0 失败 (例如不支持 su 0 语法)，尝试 su -c
            if "invalid" in output.lower() or "not found" in output.lower() and "no such file" not in output.lower():
                 cmd = ["shell", "su", "-c", f"'ls {path}'"]
                 output = self.adb._run_command(cmd)
            
            if "No such file" not in output and "Permission denied" not in output and "Error" not in output:
                logger.info("[RkMonitor] Rockchip MPP stats detected at: %s", path)
                self.is_supported = True
                self.active_path = path
                return True
        
        logger.info("[RkMonitor] MPP stats not accessible. Checked: %s", self.MPP_STATS_PATHS)
        self.is_supported = False
        return False

    def get_mpp_stats(self) -> Dict:
        """
        获取 MPP 统计信息
        返回字典: {
            "active_instances": int,
            "session_count": int,
            "total_work_count": int,
            "error_found": bool,
            "raw_output": str (optional)
        }
        """
        # V2.3: 缓存检查，避免多线程/高频调用导致 delta 归零
        current_time_check = time.time()
        if (self._last_stats_cache is not None and 
            current_time_check - self._last_stats_time < self._min_update_interval):
            return self._last_stats_cache.copy()

        if not self.is_supported:
            # 尝试重新检查（可能之前服务没起或者权限问题）
            if not self.check_support():
                return {}

        # 尝试读取
        # 优先 su 0
        cmd = ["shell", "su", "0", "cat", self.active_path]
        raw_text = self.adb._run_command(cmd)
        
        # Fallback to su -c if needed (though check_support should have confirmed su usage, 
        # but here we just blindly try su 0 then su -c to be safe)
        if "invalid" in raw_text.lower():
             cmd = ["shell", "su", "-c", f"'cat {self.active_path}'"]
             raw_text = self.adb._run_command(cmd)

        if "No such file" in raw_text or "Permission denied" in raw_text or "Error" in raw_text:
            # Maybe path disappeared?
            return {}

        stats = self._parse_mpp_stats(raw_text)
        
        # V2.3: 检测解码器是否卡死（total_work_count 停止增长）
        current_time = time.time()
        current_work_count = stats.get("total_work_count", 0)
        
        if self.last_work_count_time > 0:
            # 计算增量
            work_count_delta = current_work_count - self.last_work_count
            time_delta = current_time - self.last_work_count_time
            
            # 如果时间间隔 >= 1秒，且 work_count 增量为 0，且 active_instances > 0
            # 说明解码器在运行但没有输出新帧（卡死了）
            # 优化：阈值从 2s 降至 1s，以捕捉肉眼可见的短时卡顿
            is_task_count_mode = "rkvdec/task_count" in str(self.active_path) or "vdpu/task_count" in str(self.active_path)
            
            if (not is_task_count_mode and  # task_count 模式下不报卡死，除非有更可靠的依据
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
        
        # 更新记录
        self.last_work_count = current_work_count
        self.last_work_count_time = current_time
        
        # 更新缓存
        self._last_stats_cache = stats
        self._last_stats_time = current_time
        
        return stats

    def _parse_mpp_stats(self, raw_text: str) -> Dict:
        """
        解析 MPP 统计信息
        支持 /sys/kernel/debug/mpp_service/stats 和 /proc/mpp_service/sessions-summary
        """
        stats = {
            "active_instances": 0,
            "session_count": 0,
            "total_work_count": 0,
            "error_found": False
        }
        
        if not raw_text or "Error" in raw_text:
            return stats
            
        # 1. 尝试解析 debugfs 格式 (key: value)
        # active_instance : 1
        active_match = re.search(r'active_instance\s*[:=]\s*(\d+)', raw_text)
        if active_match:
            stats["active_instances"] = int(active_match.group(1))
            
        session_match = re.search(r'session_count\s*[:=]\s*(\d+)', raw_text)
        if session_match:
            stats["session_count"] = int(session_match.group(1))
            
        work_match = re.search(r'(total_usage|work_count)\s*[:=]\s*(\d+)', raw_text)
        if work_match:
            stats["total_work_count"] = int(work_match.group(2))

        # 2. 尝试解析 procfs sessions-summary 格式
        # 通常包含 "session" 字样，每一行一个 session?
        # 或者 "total sessions: N"
        if "/proc/" in str(self.active_path):
            # 简单策略: 统计包含 "session" 的非空行数，或者 "id:" 的行数
            # 如果是 summary 格式，可能长这样:
            # session 0: ...
            # session 1: ...
            # 这里做个简单估算，如果之前没解析出 session_count
            if stats["session_count"] == 0:
                # 统计以 "session" 开头的行
                session_lines = [line for line in raw_text.splitlines() if line.strip().startswith("session") or "id:" in line]
                count = len(session_lines)
                if count > 0:
                    stats["session_count"] = count
                    stats["active_instances"] = count # 假设 summary 里列出的都是 active 的

        # 3. 尝试解析纯数字 (task_count)
        if raw_text.strip().isdigit():
            stats["total_work_count"] = int(raw_text.strip())
            # 对于 task_count 模式，无法直接获知活跃实例数
            # 但为了支持卡死检测，我们假设只要能读到计数，就有潜在的活跃实例
            # 注意：这在暂停播放时可能会误报卡死，但在压测场景下通常一直在播放
            stats["active_instances"] = 1

        # 简单判断是否有错误标志
        if "error" in raw_text.lower() or "timeout" in raw_text.lower():
            stats["error_found"] = True
            
        return stats
