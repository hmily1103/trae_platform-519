import re
import time
from typing import Dict, Any, Optional
from .base_collector import BaseCollector

class CpuCollector(BaseCollector):
    """
    CPU 使用率采集器
    采集整机 CPU 和指定应用的 CPU 使用率
    """
    
    def __init__(self, adb_controller: Any, package_name: str):
        super().__init__(adb_controller)
        self.package_name = package_name
        self._last_cpu_data = None
        self._last_cpu_time = 0
        self._cache_ttl = 2.0

    def collect(self) -> Dict[str, Any]:
        current_time = time.time()
        if (self._last_cpu_data is not None and 
            current_time - self._last_cpu_time < self._cache_ttl):
            return self._last_cpu_data

        cpu_data = {
            "cpu_app": 0.0,
            "cpu_sys": 0.0,
            "cpu_user": 0.0,
            "cpu_total": 0.0
        }

        try:
            # 优先尝试 dumpsys cpuinfo，因为它更稳定且格式统一
            # 虽然是平均值，但在监控场景下足够反映趋势
            output = self._run_adb_command(["shell", "dumpsys", "cpuinfo"])
            if output and "Load" in output:
                self._parse_dumpsys_cpuinfo(output, cpu_data)
                
                # 如果 dumpsys 获取成功，直接返回
                if cpu_data["cpu_total"] > 0 or cpu_data["cpu_app"] > 0:
                     self._last_cpu_data = cpu_data
                     self._last_cpu_time = current_time
                     return cpu_data

            # 如果 dumpsys 失败或没数据，尝试 top -n 1
            output = self._run_adb_command(["shell", "top", "-n", "1", "-b"])
            self._parse_top_output(output, cpu_data)

        except Exception as e:
            pass

        self._last_cpu_data = cpu_data
        self._last_cpu_time = current_time
        return cpu_data

    def _parse_dumpsys_cpuinfo(self, output: str, cpu_data: Dict[str, float]):
        """解析 dumpsys cpuinfo 输出"""
        try:
            lines = output.splitlines()
            for line in lines:
                line = line.strip()
                # 解析总 CPU
                # 18% TOTAL: 12% user + 5.3% kernel + 0.2% iowait + 0.3% irq + 0% softirq
                if "TOTAL" in line and "%" in line:
                    parts = line.split()
                    for part in parts:
                        if "TOTAL" in part:
                            # 通常格式为 "18% TOTAL:" 或 "TOTAL: 18%"
                            # 这里处理 "18%" 在前的情况
                            idx = parts.index(part)
                            if idx > 0 and "%" in parts[idx-1]:
                                cpu_data["cpu_total"] = float(parts[idx-1].replace('%', ''))
                            break
                
                # 解析应用 CPU
                # 0.1% 10580/com.thunder.ktv: 0.1% user + 0% kernel / faults: 13 minor
                if self.package_name in line:
                    parts = line.split()
                    #通常第一个带 % 的就是总占比
                    for part in parts:
                         if "%" in part:
                             val = float(part.replace('%', ''))
                             # 简单过滤异常值
                             if val <= 1000: # 允许超过100% (多核)
                                 cpu_data["cpu_app"] = val
                                 break
        except:
            pass

    def _parse_top_output(self, output: str, cpu_data: Dict[str, float]):
        """解析 top 输出"""
        lines = output.splitlines()
        headers = []
        cpu_idx = -1
        
        for line in lines:
            line = line.strip()
            # 查找 Header
            if "PID" in line and "CPU" in line:
                headers = line.split()
                # 确定 %CPU 列索引
                for i, h in enumerate(headers):
                    if "CPU" in h:
                        cpu_idx = i
                        break
                continue
            
            # 查找应用行
            if self.package_name in line and cpu_idx != -1:
                parts = line.split()
                if len(parts) > cpu_idx:
                    try:
                        cpu_val_str = parts[cpu_idx].replace('%', '')
                        cpu_data["cpu_app"] = float(cpu_val_str)
                        return # 找到即返回
                    except:
                        pass
        
        # 如果没找到 Header，尝试盲猜 (兼容旧版 Android top)
        # 旧版: PID PR CPU% S  #THR     VSS     RSS PCY UID      Name
        if cpu_idx == -1:
            for line in lines:
                if self.package_name in line:
                    parts = line.split()
                    # 寻找可能是 CPU 的值
                    # 通常是第 3 列 (index 2) 或倒数几列
                    for part in parts:
                        if part.replace('.', '', 1).isdigit():
                            val = float(part)
                            # 排除 PID
                            if val > 10000: continue 
                            # 假设找到的第一个浮点数是 CPU
                            # 这很不准确，但在没有 Header 的情况下是权宜之计
                            # 更好的方法是使用 shell 脚本过滤
                            # cpu_data["cpu_app"] = val
                            pass

    def _run_adb_command(self, cmd_list, timeout=3):
        """适配器方法 (复制自 FpsCollector)"""
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

    def reset(self):
        self._last_cpu_data = None
