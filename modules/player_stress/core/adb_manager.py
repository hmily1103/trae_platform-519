import time
import logging
import re
from typing import Optional, Tuple, Dict, List
from core.device.manager import get_device_manager

logger = logging.getLogger(__name__)

class AdbManager:
    def __init__(self, device_id: Optional[str] = None):
        self.device_id = device_id
        self.dm = get_device_manager()

    @staticmethod
    def list_devices() -> list:
        """列出所有已连接的设备ID（使用统一设备管理器）"""
        return get_device_manager().get_devices()

    def connect(self, ip: str, port: int = 8787) -> bool:
        """连接设备"""
        if self.dm.connect(ip, port):
            self.device_id = f"{ip}:{port}"
            return True
        return False

    def disconnect(self, ip: str, port: int = 8787) -> bool:
        """断开设备连接"""
        return self.dm.disconnect(ip, port)

    def _run_command(self, cmd: list, force_no_device_id: bool = False, timeout: int = 10, retry: int = 2) -> str:
        """
        执行ADB命令并返回输出 (适配 DeviceManager)
        """
        target_device_id = self.device_id
        
        # 自动设备锁定逻辑
        if not force_no_device_id and not target_device_id:
            devices = self.list_devices()
            ip_devices = [d for d in devices if "." in d and ":" in d]
            if ip_devices:
                target_device_id = ip_devices[0]
                self.device_id = target_device_id
                logger.info("自动锁定设备: %s", self.device_id)
            elif len(devices) == 1:
                target_device_id = devices[0]
                self.device_id = target_device_id
                logger.info("自动使用唯一设备: %s", self.device_id)
            elif len(devices) > 1:
                target_device_id = devices[0]
                self.device_id = target_device_id
                logger.warning("检测到多个设备，自动使用第一个: %s", self.device_id)

        if force_no_device_id:
            target_device_id = None

        last_error = None
        for attempt in range(retry + 1):
            try:
                # 使用 DeviceManager 执行
                # cmd 是列表，如 ["shell", "ls"]
                code, stdout, stderr = self.dm.run_adb_command(target_device_id, cmd, timeout=timeout)
                
                if code != 0:
                    error_msg = stderr.strip() if stderr else stdout.strip()
                    if "more than one device" in error_msg.lower():
                        return f"Error: {error_msg} (检测到多个设备，请明确指定 device_id)"
                    
                    if not error_msg and stdout:
                         error_msg = stdout.strip()

                    if not error_msg:
                        error_msg = f"Command failed with exit code {code}"
                        
                    return f"Error: {error_msg}"
                
                return stdout.strip()

            except Exception as e:
                last_error = str(e)
                if attempt < retry:
                    logger.warning("ADB 执行异常，第 %d 次重试: %s - %s", attempt + 1, cmd, e)
                    time.sleep(0.5 * (attempt + 1))
        
        return f"Error: {last_error}"

    def get_pid(self, package_name: str) -> Optional[int]:
        """获取应用PID"""
        output = self._run_command(["shell", "pidof", package_name])
        if output and output.isdigit():
            return int(output)
            
        cmd = ["shell", "ps", "-A"]
        output = self._run_command(cmd)
        for line in output.splitlines():
            if package_name in line:
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    return int(parts[1])
        return None

    def get_memory_info(self, package_name: str) -> Dict[str, float]:
        """获取内存信息 (PSS Total in MB)"""
        output = self._run_command(["shell", "dumpsys", "meminfo", package_name])
        pss = 0.0
        try:
            for line in output.splitlines():
                if "TOTAL" in line and ":" not in line:
                    parts = line.split()
                    for part in parts:
                        if part.isdigit():
                            pss = int(part) / 1024.0 
                            return {"pss_mb": round(pss, 2)}
                
                if "TOTAL PSS:" in line:
                     parts = line.split()
                     for part in parts:
                        if part.isdigit():
                            pss = int(part) / 1024.0
                            return {"pss_mb": round(pss, 2)}
        except Exception:
            pass
        return {"pss_mb": 0.0}

    def get_cpu_usage(self, package_name: str) -> float:
        """获取CPU使用率 (简易版)"""
        output = self._run_command(["shell", "dumpsys", "cpuinfo"])
        try:
            for line in output.splitlines():
                if package_name in line:
                    parts = line.split("%")
                    if parts[0].strip().replace(".", "").isdigit():
                         return float(parts[0].strip())
                    first_part = parts[0].split()[-1]
                    if first_part.replace(".", "").isdigit():
                        return float(first_part)
        except Exception:
            pass
        return 0.0

    def send_key_event(self, key_code: int):
        """发送按键事件"""
        self._run_command(["shell", "input", "keyevent", str(key_code)])

    def start_app(self, package_name: str, activity_name: Optional[str] = None):
        """启动应用"""
        real_pkg = package_name.split(':')[0]
        if activity_name:
            self._run_command(["shell", "am", "start", "-n", f"{real_pkg}/{activity_name}"], timeout=20)
        else:
            self._run_command(["shell", "monkey", "-p", real_pkg, "-c", "android.intent.category.LAUNCHER", "1"], timeout=20)

    def stop_app(self, package_name: str):
        """强制停止应用"""
        real_pkg = package_name.split(':')[0]
        self._run_command(["shell", "am", "force-stop", real_pkg])

    def is_device_online(self) -> bool:
        """检查设备是否在线"""
        output = self._run_command(["get-state"])
        return "device" in output

    def is_audio_active(self) -> bool:
        """检查是否有音频输出"""
        try:
            output = self._run_command(["shell", "dumpsys", "audio_flinger"])
            if "Can't find service" not in output:
                if "Active tracks:\n    " in output or "state: PLAYER_STATE_STARTED" in output:
                    return True
                if "started" in output.lower() and "audio" in output.lower():
                    return True

            output_service = self._run_command(["shell", "dumpsys", "audio"])
            if "state:started" in output_service:
                return True
            if "active? true" in output_service:
                 return True
            return False
        except Exception as e:
            logger.debug("is_audio_active 检查失败: %s", e)
            return False

    def take_screenshot(self, local_path: str, display_id: Optional[int] = None):
        """截图并保存到本地"""
        try:
            remote_path = f"/data/local/tmp/screen_temp_{int(time.time())}.png"
            cmd = ["shell", "screencap"]
            if display_id is not None:
                cmd.extend(["-d", str(display_id)])
            cmd.extend(["-p", remote_path])
            
            self._run_command(cmd)
            self._run_command(["pull", remote_path, local_path])
            self._run_command(["shell", "rm", remote_path])
        except Exception as e:
            logger.warning("截图失败: %s", e)

    def get_gfx_info(self, package_name: str) -> Dict[str, int]:
        """获取图形性能信息"""
        try:
            real_pkg = package_name.split(':')[0]
            output = self._run_command(["shell", "dumpsys", "gfxinfo", real_pkg])
            total_frames = 0
            janky_frames = 0
            for line in output.splitlines():
                if "Janky frames:" in line:
                    parts = line.split(":")
                    if len(parts) > 1:
                        val_str = parts[1].strip().split()[0]
                        if val_str.isdigit():
                            janky_frames = int(val_str)
                if "Total frames rendered:" in line:
                    parts = line.split(":")
                    if len(parts) > 1:
                         val_str = parts[1].strip()
                         if val_str.isdigit():
                             total_frames = int(val_str)
            return {"total_frames": total_frames, "janky_frames": janky_frames}
        except Exception as e:
            logger.debug("get_gfx_info 解析失败: %s", e)
            return {"total_frames": 0, "janky_frames": 0}

    def reset_gfx_info(self, package_name: str):
        try:
            real_pkg = package_name.split(':')[0]
            self._run_command(["shell", "dumpsys", "gfxinfo", real_pkg, "reset"])
        except Exception as e:
            logger.debug("reset_gfx_info 失败: %s", e)

    def get_media_metadata(self, package_name: str) -> Optional[str]:
        """从 dumpsys media_session 获取当前播放的元数据"""
        try:
            output = self._run_command(["shell", "dumpsys", "media_session"])
            real_pkg = package_name.split(':')[0]
            lines = output.splitlines()
            in_target_session = False
            for line in lines:
                if f"package={real_pkg}" in line:
                    in_target_session = True
                    continue
                if "Session " in line and "package=" in line and f"package={real_pkg}" not in line:
                    in_target_session = False
                
                if in_target_session and "metadata:" in line:
                    if "title=" in line:
                        try:
                            title_part = line.split("title=")[1]
                            song_title = title_part.split(",")[0].split("}")[0].strip()
                            if song_title and song_title != "null":
                                return song_title
                        except (IndexError, ValueError):
                            pass
                    if "description=" in line:
                         desc = line.split("description=")[1].strip()
                         return desc[:50] + "..." if len(desc) > 50 else desc
            return None
        except Exception as e:
            logger.debug("get_media_metadata 失败: %s", e)
            return None

    def get_top_heavy_processes(self, limit: int = 3) -> str:
        """获取CPU占用最高的进程列表"""
        try:
            cmd = ["shell", "top", "-b", "-n", "1"]
            output = self._run_command(cmd)
            lines = output.splitlines()
            candidates = []
            cpu_col_index = -1
            args_col_index = -1
            start_parsing = False
            
            for line in lines:
                line = line.strip()
                if not line: continue
                if "PID" in line and ("CPU" in line or "S[%CPU]" in line):
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if "CPU" in part:
                            cpu_col_index = i
                        if "ARGS" in part or "NAME" in part or "COMMAND" in part:
                            args_col_index = i
                    if args_col_index == -1:
                        args_col_index = len(parts) - 1
                    start_parsing = True
                    continue
                
                if start_parsing:
                    parts = line.split()
                    if len(parts) > cpu_col_index:
                        try:
                            cpu_str = parts[cpu_col_index].replace("%", "")
                            cpu_val = float(cpu_str)
                            proc_name = parts[-1]
                            if args_col_index < len(parts) - 1:
                                proc_name = " ".join(parts[args_col_index:])
                            elif args_col_index < len(parts):
                                proc_name = parts[args_col_index]
                            if "top" in proc_name:
                                continue
                            candidates.append((cpu_val, proc_name))
                        except (ValueError, IndexError, KeyError):
                            continue

            candidates.sort(key=lambda x: x[0], reverse=True)
            top_list = candidates[:limit]
            
            result = []
            for cpu, name in top_list:
                result.append(f"{name}({cpu}%)")
            
            return " | ".join(result)
        except Exception as e:
            logger.debug("get_top_heavy_processes 失败: %s", e)
            return f"Error: {str(e)}"
