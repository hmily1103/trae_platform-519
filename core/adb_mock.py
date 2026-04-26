"""
ADB Mock 层 - 统一 Mock 逻辑
用于无设备环境下的测试/开发，支持 mock_device 前缀
"""
from typing import Optional, Tuple, List
import time


def is_mock_device(device_id: Optional[str]) -> bool:
    """判断是否为 Mock 设备"""
    return bool(device_id and str(device_id).strip().startswith("mock_device"))


def get_mock_response(device_id: Optional[str], cmd: List[str]) -> Optional[Tuple[str, int, str]]:
    """
    获取 Mock 响应
    :return: (stdout, returncode, stderr) 或 None 表示非 Mock 需真实执行
    """
    if not is_mock_device(device_id):
        return None
    cmd_str = " ".join(str(c) for c in cmd).lower()
    # 通用 Mock 响应
    if "dumpsys" in cmd_str and "meminfo" in cmd_str:
        return "TOTAL    102400", 0, ""
    if "dumpsys" in cmd_str and "cpuinfo" in cmd_str:
        return "10% com.example.test", 0, ""
    if "dumpsys" in cmd_str and "gfxinfo" in cmd_str:
        return "Janky frames: 0 (0.00%)\nTotal frames rendered: 100", 0, ""
    if "dumpsys" in cmd_str and "media_session" in cmd_str:
        return "metadata: title=MockSong", 0, ""
    if "dumpsys" in cmd_str and "audio" in cmd_str:
        return "state:started", 0, ""
    if "get-state" in cmd_str:
        return "device", 0, ""
    if "pm" in cmd_str and "list" in cmd_str and "packages" in cmd_str:
        return "package:com.example.test", 0, ""
    if "pidof" in cmd_str:
        return "12345", 0, ""
    if "echo" in cmd_str and "ok" in cmd_str:
        return "ok", 0, ""
    if "ps" in cmd_str:
        return "u0_a123 1234 567 890 12345 6789 S com.thunder.ktv", 0, ""
    if "monkey" in cmd_str:
        time.sleep(0.5)
        return ":Monkey: seed=123 count=100\n:AllowPackage: com.thunder.ktv\n// Monkey finished", 0, ""
    return "", 0, ""


def get_mock_devices_response() -> Optional[Tuple[str, int, str]]:
    """Mock devices 命令响应"""
    return "List of devices attached\nmock_device:8787\tdevice\n", 0, ""


def get_mock_connect_response(addr: str) -> Optional[Tuple[str, int, str]]:
    """Mock connect 命令响应"""
    if "mock_device" in addr:
        return "connected to " + addr, 0, ""
    return None
