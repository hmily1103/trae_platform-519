"""
统一 ADB 连接管理 - 全局设备连接池
解决各模块独立连接导致的端口冲突、资源抢占问题
"""
import subprocess
import threading
import urllib.request
import logging
import os
import platform
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# 全局连接锁：connect/disconnect 操作串行化，避免并发冲突
_connect_lock = threading.Lock()


def list_devices() -> List[str]:
    """统一获取已连接设备列表（所有模块共用）"""
    devices = []
    try:
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=creationflags,
            env={k: v for k, v in os.environ.items() if k != "ANDROID_SERIAL"},
        )
        if result.returncode != 0:
            return []
        for line in (result.stdout or "").splitlines()[1:]:
            if line.strip() and "\tdevice" in line:
                devices.append(line.split()[0])
    except Exception as e:
        logger.warning("list_devices 失败: %s", e)
    return devices


def connect(ip: str, port: int = 8787, enable_http: bool = True) -> bool:
    """
    连接设备（带锁，避免多模块并发冲突）
    :param ip: 设备 IP
    :param port: ADB 端口，默认 8787
    :param enable_http: 是否先通过 HTTP 启用 ADB
    """
    address = f"{ip}:{port}"
    with _connect_lock:
        try:
            if enable_http:
                try:
                    url = f"http://{ip}:2007/debug/adb?enable=1"
                    urllib.request.urlopen(url, timeout=2)
                except Exception:
                    pass
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if platform.system() == 'Windows' else 0
            env = os.environ.copy()
            env.pop("ANDROID_SERIAL", None)
            result = subprocess.run(
                ["adb", "connect", address],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creationflags,
                env=env,
            )
            out = (result.stdout or "").lower()
            if "connected to" in out or "already connected" in out:
                logger.info("ADB 连接成功: %s", address)
                return True
            logger.warning("ADB 连接失败: %s", result.stderr or result.stdout)
            return False
        except Exception as e:
            logger.warning("ADB 连接异常: %s", e)
            return False


def disconnect(ip: str, port: int = 8787) -> bool:
    """断开设备连接（带锁）"""
    address = f"{ip}:{port}"
    with _connect_lock:
        try:
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if platform.system() == 'Windows' else 0
            result = subprocess.run(
                ["adb", "disconnect", address],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=creationflags,
            )
            logger.info("ADB 断开: %s", address)
            return result.returncode == 0
        except Exception as e:
            logger.warning("ADB 断开异常: %s", e)
            return False


def run_command(device_id: Optional[str], cmd: List[str], timeout: int = 10) -> Tuple[int, str, str]:
    """
    执行 ADB 命令（统一入口）
    :param device_id: 设备 ID，None 时用于 devices/connect 等无设备命令
    :param cmd: 命令列表，如 ["shell", "dumpsys", "meminfo", "com.xxx"]
    :return: (returncode, stdout, stderr)
    """
    base = ["adb"]
    if device_id:
        base.extend(["-s", str(device_id).strip()])
    base.extend(cmd)
    try:
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if platform.system() == 'Windows' else 0
        env = os.environ.copy()
        env.pop("ANDROID_SERIAL", None)
        result = subprocess.run(
            base,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            errors='ignore',
            creationflags=creationflags,
            env=env,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except subprocess.TimeoutExpired:
        logger.warning("ADB 命令超时: %s", " ".join(base[:5]))
        return -1, "", "Timeout"
    except Exception as e:
        logger.warning("ADB 命令异常: %s", e)
        return -1, "", str(e)
