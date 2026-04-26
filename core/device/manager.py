"""
平台级设备管理器 (Unified Device Manager)
- 统一 ADB 连接、命令执行、重试与错误处理
- 设备资源锁：避免多任务同时占用同一设备
- Mock 设备支持：离线验证
"""
import threading
import time
import logging
from typing import List, Optional, Tuple, Dict
from utils.adb_helper import AdbHelper

logger = logging.getLogger(__name__)

class DeviceManager:
    """统一设备管理：ADB 操作 + 资源锁"""

    _instance = None
    _lock = threading.Lock()
    _device_locks: dict = {}  # device_id -> (runtime_id, acquired_at)
    _locks_lock = threading.RLock()
    _mock_enabled = False # 默认关闭mock，可动态开启

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        
    def enable_mock(self, enabled: bool = True):
        self._mock_enabled = enabled
        logger.info(f"Mock 模式已{'开启' if enabled else '关闭'}")

    def get_devices(self) -> List[str]:
        """获取已连接设备列表"""
        devices = []
        try:
            # 真实设备
            if AdbHelper:
                devices.extend(AdbHelper.get_devices())
        except Exception as e:
            logger.error(f"获取真实设备列表失败: {e}")

        # Mock 设备
        if self._mock_enabled:
             devices.append("mock_device:5555")
        
        return list(set(devices)) # 去重

    def connect(self, ip: str, port: int = 8787) -> bool:
        """连接设备"""
        device_id = f"{ip}:{port}"
        
        if self._mock_enabled and (ip.startswith("mock_") or ip == "mock_device"):
            return True

        if AdbHelper:
            return AdbHelper.connect_device(ip, port)
        return False

    def disconnect(self, ip: str, port: int = 8787) -> bool:
        """断开设备连接"""
        if self._mock_enabled and (ip.startswith("mock_") or ip == "mock_device"):
            return True

        if AdbHelper:
            return AdbHelper.disconnect_device(ip, port)
        return False

    def run_adb_command(self, device_id: str, cmd_args: List[str], timeout: int = 10) -> Tuple[int, str, str]:
        """执行任意 ADB 命令 (自动添加 -s device_id)"""
        # Mock 拦截
        if self._mock_enabled and (device_id.startswith("mock_") or device_id == "mock_device"):
             return self._handle_mock_command(" ".join(cmd_args))

        if AdbHelper:
            full_cmd = ['adb']
            if device_id:
                full_cmd.extend(['-s', device_id])
            full_cmd.extend(cmd_args)
            return AdbHelper.run_command(full_cmd, timeout=timeout)
        return -1, "", "AdbHelper not available"

    def shell(self, device_id: str, command: str, timeout: int = 10) -> Tuple[int, str, str]:
        """在设备上执行 shell 命令"""
        # Mock 拦截
        if self._mock_enabled and (device_id.startswith("mock_") or device_id == "mock_device"):
            return self._handle_mock_command(command)

        if AdbHelper:
            # AdbHelper.shell_command 并不存在于原 utils/adb_helper.py 中，需要补充或直接调用 run_command
            # 这里直接调用 run_command 封装
            cmd = ['adb', '-s', device_id, 'shell'] + command.split()
            return AdbHelper.run_command(cmd, timeout=timeout)
        return -1, "", "AdbHelper not available"

    def _handle_mock_command(self, command: str) -> Tuple[int, str, str]:
        """处理 Mock 命令"""
        cmd = command.strip()
        if "dumpsys meminfo" in cmd:
            return 0, "TOTAL PSS: 102400", ""
        elif "dumpsys cpuinfo" in cmd:
            return 0, "10% com.example.test", ""
        elif "dumpsys gfxinfo" in cmd:
            return 0, "Janky frames: 0 (0.00%)\nTotal frames rendered: 100", ""
        elif "pm list packages" in cmd:
            return 0, "package:com.thunder.ktv", ""
        return 0, "mock output", ""

    def is_connected(self, device_id: str) -> bool:
        """检查设备是否已连接"""
        if device_id in self.get_devices():
            return True
        return False

    def acquire_device(self, device_id: str, runtime_id: str) -> bool:
        """
        尝试占用设备（资源锁）
        :return: True 占用成功，False 已被其他任务占用
        """
        with self._locks_lock:
            existing = self._device_locks.get(device_id)
            if existing:
                # 如果是同一个 runtime 再次申请，视为成功（重入）
                if existing[0] == runtime_id:
                    return True
                return False
            self._device_locks[device_id] = (runtime_id, time.time())
            return True

    def release_device(self, device_id: str, runtime_id: Optional[str] = None) -> bool:
        """
        释放设备占用
        :param runtime_id: 若指定，仅当占用者为该 runtime 时释放
        """
        with self._locks_lock:
            existing = self._device_locks.get(device_id)
            if not existing:
                return True
            if runtime_id and existing[0] != runtime_id:
                logger.warning(f"尝试释放非己方锁: {runtime_id} vs {existing[0]}")
                return False
            del self._device_locks[device_id]
            return True

    def get_device_owner(self, device_id: str) -> Optional[str]:
        """获取当前占用设备的 runtime_id"""
        with self._locks_lock:
            existing = self._device_locks.get(device_id)
            return existing[0] if existing else None

    def is_device_locked(self, device_id: str) -> bool:
        """检查设备是否被占用"""
        with self._locks_lock:
            return device_id in self._device_locks

    def get_device_usage(self) -> List[Dict]:
        """返回当前被占用的设备列表，用于前端展示「谁在占用」"""
        with self._locks_lock:
            return [
                {"device_id": dev_id, "runtime_id": info[0], "acquired_at": info[1]}
                for dev_id, info in self._device_locks.items()
            ]

# 全局单例
_device_manager = DeviceManager()

def get_device_manager() -> DeviceManager:
    return _device_manager
