"""
统一 ADB 命令执行工具
避免代码重复，统一错误处理
"""
import subprocess
import os
import logging
from typing import Tuple, Optional, List

logger = logging.getLogger(__name__)


class AdbHelper:
    """ADB 命令执行辅助类"""
    
    @staticmethod
    def run_command(cmd: List[str], timeout: int = 15, encoding: str = 'utf-8') -> Tuple[int, str, str]:
        """
        执行 ADB 命令
        
        :param cmd: 命令列表，如 ['adb', 'devices']
        :param timeout: 超时时间（秒）
        :param encoding: 编码格式
        :return: (returncode, stdout, stderr)
        """
        try:
            # Windows 特定设置：隐藏窗口
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                startupinfo=startupinfo,
                encoding=encoding,
                errors='ignore'
            )
            
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            logger.warning(f'ADB 命令超时: {" ".join(cmd)}')
            return -1, "", "Timeout"
        except Exception as e:
            logger.error(f'ADB 命令执行失败: {" ".join(cmd)}, 错误: {e}')
            return -1, "", str(e)
    
    @staticmethod
    def get_devices() -> List[str]:
        """
        获取已连接的设备列表
        
        :return: 设备ID列表，如 ['192.168.1.100:8787', 'emulator-5554']
        """
        returncode, stdout, stderr = AdbHelper.run_command(['adb', 'devices'])
        devices = []
        
        if returncode == 0 and stdout:
            lines = stdout.splitlines()[1:]  # 跳过第一行 "List of devices attached"
            for line in lines:
                if '\t' in line:
                    device_id, status = line.split('\t', 1)
                    if status.strip() == 'device':
                        devices.append(device_id.strip())
        
        return devices
    
    @staticmethod
    def connect_device(ip: str, port: int) -> bool:
        """
        连接 ADB 设备
        
        :param ip: 设备IP
        :param port: 设备端口
        :return: 是否连接成功
        """
        addr = f"{ip}:{port}"
        returncode, stdout, stderr = AdbHelper.run_command(['adb', 'connect', addr], timeout=5)
        
        if returncode == 0:
            output_lower = stdout.lower()
            return 'connected' in output_lower or 'already connected' in output_lower
        
        return False
    
    @staticmethod
    def disconnect_device(ip: str, port: int) -> bool:
        """
        断开 ADB 设备连接
        
        :param ip: 设备IP
        :param port: 设备端口
        :return: 是否断开成功
        """
        addr = f"{ip}:{port}"
        returncode, stdout, stderr = AdbHelper.run_command(['adb', 'disconnect', addr], timeout=5)
        return returncode == 0
    
    @staticmethod
    def is_device_connected(device_id: str) -> bool:
        """
        检查设备是否已连接
        
        :param device_id: 设备ID，如 '192.168.1.100:8787'
        :return: 是否已连接
        """
        devices = AdbHelper.get_devices()
        return device_id in devices
    
    @staticmethod
    def enable_adb_via_http(ip: str, port: int = 2007) -> bool:
        """
        通过 HTTP 请求启用 ADB 调试
        
        :param ip: 设备IP
        :param port: HTTP 端口，默认 2007
        :return: 是否成功
        """
        try:
            import requests
            url = f"http://{ip}:{port}/debug/adb?enable=1"
            response = requests.get(url, timeout=2)
            return response.status_code == 200
        except Exception as e:
            logger.warning(f'HTTP 启用 ADB 失败: {ip}:{port}, 错误: {e}')
            return False
    
    @staticmethod
    def get_process_pss_mb(device_id: str, package_name: str) -> int:
        """
        获取进程 PSS 内存（MB）
        
        :param device_id: 设备ID
        :param package_name: 包名
        :return: PSS 内存（MB），失败返回 0
        """
        import re
        returncode, stdout, stderr = AdbHelper.run_command(
            ['adb', '-s', device_id, 'shell', 'dumpsys', 'meminfo', package_name],
            timeout=10
        )
        
        if returncode != 0:
            return 0
        
        # 尝试匹配 TOTAL PSS
        m = re.search(r"TOTAL\s+(\d+)", stdout)
        if m:
            return int(m.group(1)) // 1024
        
        m2 = re.search(r"Total\s+PSS\s*:\s*(\d+)\s*kB", stdout, re.I)
        if m2:
            return int(m2.group(1)) // 1024
        
        return 0
    
    @staticmethod
    def shell_command(device_id: str, command: str, timeout: int = 10) -> Tuple[int, str, str]:
        """
        在设备上执行 shell 命令
        
        :param device_id: 设备ID
        :param command: shell 命令
        :param timeout: 超时时间
        :return: (returncode, stdout, stderr)
        """
        cmd = ['adb', '-s', device_id, 'shell'] + command.split()
        return AdbHelper.run_command(cmd, timeout=timeout)

