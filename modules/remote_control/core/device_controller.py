import subprocess
import time
import os
from typing import Optional, Tuple
from utils.logger import setup_logger

logger = setup_logger('remote_device_controller')

try:
    from core.adb_pool import list_devices as _pool_list_devices, connect as _pool_connect, disconnect as _pool_disconnect, run_command as _pool_run_command
except ImportError:
    _pool_list_devices = _pool_connect = _pool_disconnect = _pool_run_command = None


class RemoteDeviceController:
    """远程设备控制器 - 使用 adb_pool 统一连接管理"""
    
    def __init__(self, device_id: str, adb_path: str = "adb"):
        self.device_id = device_id
        self.adb_path = adb_path
    
    def _run_adb_command(self, command: list, timeout: int = 5) -> Tuple[bool, str]:
        """使用 adb_pool 执行，避免多模块并发冲突"""
        try:
            if _pool_run_command:
                if command[0] == "devices":
                    devs = _pool_list_devices()
                    out = "List of devices attached\n" + "\n".join(f"{d}\tdevice" for d in devs) + "\n"
                    return True, out
                if command[0] == "disconnect" and len(command) >= 2:
                    addr = command[1]
                    if ":" in addr:
                        ip, port = addr.rsplit(":", 1)
                        _pool_disconnect(ip, int(port))
                    return True, ""
                if self.device_id and command[0] not in ["connect", "disconnect", "devices", "start-server", "kill-server"]:
                    rc, stdout, stderr = _pool_run_command(self.device_id, command, timeout=timeout)
                    if rc == 0:
                        return True, stdout
                    return False, stderr or stdout
            
            # Fallback
            cmd = [self.adb_path]
            if self.device_id and command[0] not in ["connect", "disconnect", "devices", "start-server", "kill-server"]:
                cmd.extend(["-s", self.device_id])
            cmd.extend(command)
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, startupinfo=startupinfo, encoding='utf-8', errors='ignore')
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, result.stderr.strip() or result.stdout.strip()
        except Exception as e:
            logger.error(f"执行ADB命令失败: {e}")
            return False, str(e)
    
    def connect(self, ip: str, port: int = 8787) -> Tuple[bool, str]:
        """连接设备 - 使用 adb_pool"""
        try:
            if _pool_connect:
                ok = _pool_connect(ip, port)
                target = f"{ip}:{port}"
                if ok:
                    return True, f"Connected to {target}"
                devs = _pool_list_devices()
                if target in devs:
                    return True, f"Connected to {target} (verified)"
                return False, "ADB connect failed"
            # Fallback: 尝试启用ADB
            try:
                import urllib.request
                url = f"http://{ip}:2007/debug/adb?enable=1"
                urllib.request.urlopen(url, timeout=2)
            except Exception:
                pass
            target = f"{ip}:{port}"
            success, output = self._run_adb_command(["connect", target])
            text = (output or "").lower()
            if "connected to" in text or "already connected" in text:
                return True, f"Connected to {target}"
            ok, devices_out = self._run_adb_command(["devices"])
            if ok and target in devices_out and 'device' in devices_out:
                return True, f"Connected to {target} (verified)"
            return False, f"ADB Output: {output}"
        except Exception as e:
            logger.error(f"连接设备失败: {e}")
            return False, str(e)

    def click(self, x: int, y: int, display_id: int = 0) -> bool:
        cmd = ["shell", "input"]
        if display_id > 0:
             # Support for multi-display input (Android 10+)
             cmd.extend(["-d", str(display_id)])
        cmd.extend(["tap", str(x), str(y)])
        
        success, _ = self._run_adb_command(cmd)
        return success
        
    def key_event(self, keycode: str) -> bool:
        success, _ = self._run_adb_command(["shell", "input", "keyevent", str(keycode)])
        return success

    def input_text(self, text: str) -> bool:
        escaped_text = text.replace(" ", "%s").replace("&", "\\&")
        success, _ = self._run_adb_command(["shell", "input", "text", escaped_text])
        return success
