import time
import logging
import re
import threading
from datetime import datetime
from typing import Optional, Tuple, Dict, List
from core.device.manager import get_device_manager

logger = logging.getLogger(__name__)

class AdbManager:
    def __init__(self, device_id: Optional[str] = None):
        self.device_id = device_id
        self.dm = get_device_manager()
        self._cpu_stat_lock = threading.Lock()
        self._previous_cpu_stat = None
        self._cancel_event = None

    def set_cancel_event(self, cancel_event) -> None:
        self._cancel_event = cancel_event

    def _cancel_requested(self) -> bool:
        return bool(
            self._cancel_event is not None
            and self._cancel_event.is_set()
        )

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
            if self._cancel_requested():
                return "Error: command cancelled"
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
                    delay = 0.5 * (attempt + 1)
                    if (
                        self._cancel_event is not None
                        and self._cancel_event.wait(delay)
                    ):
                        return "Error: command cancelled"
        
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

    def get_firmware_incremental(self) -> str:
        """获取设备增量固件版本。"""
        output = self._run_command([
            "shell",
            "getprop",
            "ro.build.version.incremental",
        ])
        value = str(output or "").strip()
        if value and not value.startswith("Error:"):
            return value

        output = self._run_command(["shell", "getprop"])
        match = re.search(
            r"\[ro\.build\.version\.incremental\]\s*:\s*\[([^\]]+)\]",
            str(output or ""),
        )
        return match.group(1).strip() if match else ""

    def get_device_ip(self) -> str:
        """从ADB设备ID或设备属性中获取机顶盒IP。"""
        device_id = str(self.device_id or "").strip()
        if device_id:
            host = device_id.rsplit(":", 1)[0] if ":" in device_id else device_id
            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
                return host

        output = self._run_command([
            "shell",
            "getprop",
            "dhcp.wlan0.ipaddress",
        ])
        value = str(output or "").strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value):
            return value
        return ""

    def get_platform_identity(self) -> str:
        """获取芯片/平台标识，用于报告中的平台支持等级说明。"""
        prop_candidates = [
            "ro.soc.model",
            "ro.board.platform",
            "ro.hardware",
            "ro.product.board",
            "ro.product.device",
        ]
        values = []
        seen = set()
        for prop in prop_candidates:
            output = self._run_command(["shell", "getprop", prop])
            value = str(output or "").strip()
            if not value or value.startswith("Error:") or value in seen:
                continue
            seen.add(value)
            values.append(value)

        if values:
            primary = values[0]
            aliases = [item for item in values[1:] if item.lower() != primary.lower()]
            if aliases:
                return f"{primary} ({', '.join(aliases[:2])})"
            return primary

        firmware = self.get_firmware_incremental()
        return firmware or ""

    @staticmethod
    def _parse_proc_stat_cpu(output: str) -> Optional[Tuple[int, int]]:
        """Return cumulative (total, idle) CPU jiffies from /proc/stat."""
        first_line = str(output or "").splitlines()[0:1]
        if not first_line:
            return None
        parts = first_line[0].split()
        if not parts or parts[0] != "cpu":
            return None
        try:
            values = [int(value) for value in parts[1:]]
        except (TypeError, ValueError):
            return None
        if len(values) < 4:
            return None
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle

    def get_system_cpu_usage(self) -> float:
        """获取整机CPU使用率，基于连续两次 /proc/stat 差分。"""
        current = self._parse_proc_stat_cpu(
            self._run_command(["shell", "cat", "/proc/stat"])
        )
        if current is None:
            return 0.0

        lock = getattr(self, "_cpu_stat_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._cpu_stat_lock = lock

        with lock:
            previous = getattr(self, "_previous_cpu_stat", None)
            self._previous_cpu_stat = current

        if previous is None:
            return 0.0
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return 0.0
        usage = (1.0 - (max(0, idle_delta) / float(total_delta))) * 100.0
        return round(max(0.0, min(100.0, usage)), 2)

    @staticmethod
    def _parse_sysfs_values(output: str) -> List[Tuple[str, int]]:
        values = []
        for raw_line in str(output or "").splitlines():
            if "=" not in raw_line:
                continue
            path, raw_value = raw_line.split("=", 1)
            try:
                value = int(raw_value.strip())
            except (TypeError, ValueError):
                continue
            values.append((path.strip(), value))
        return values

    def get_thermal_status(self) -> Dict:
        """采集温度与CPU频率，用于识别发热降频。"""
        thermal_output = self._run_command([
            "shell", "sh", "-c",
            "for f in /sys/class/thermal/thermal_zone*/temp; "
            "do [ -r \"$f\" ] && echo \"$f=$(cat \"$f\")\"; done",
        ])
        current_freq_output = self._run_command([
            "shell", "sh", "-c",
            "for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq; "
            "do [ -r \"$f\" ] && echo \"$f=$(cat \"$f\")\"; done",
        ])
        max_freq_output = self._run_command([
            "shell", "sh", "-c",
            "for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/cpuinfo_max_freq; "
            "do [ -r \"$f\" ] && echo \"$f=$(cat \"$f\")\"; done",
        ])

        temperatures = []
        for path, raw_value in self._parse_sysfs_values(thermal_output):
            celsius = raw_value / 1000.0 if abs(raw_value) >= 1000 else float(raw_value)
            if -20.0 <= celsius <= 150.0:
                temperatures.append({
                    "zone": path.rsplit("/", 2)[-2],
                    "celsius": round(celsius, 1),
                })

        current_by_cpu = {
            path.split("/cpu/")[-1].split("/", 1)[0]: value
            for path, value in self._parse_sysfs_values(current_freq_output)
            if value > 0
        }
        max_by_cpu = {
            path.split("/cpu/")[-1].split("/", 1)[0]: value
            for path, value in self._parse_sysfs_values(max_freq_output)
            if value > 0
        }
        frequency_ratios = []
        frequencies = []
        for cpu, current_khz in current_by_cpu.items():
            max_khz = max_by_cpu.get(cpu, 0)
            ratio = (float(current_khz) / float(max_khz)) if max_khz > 0 else 0.0
            if ratio > 0:
                frequency_ratios.append(ratio)
            frequencies.append({
                "cpu": cpu,
                "current_khz": current_khz,
                "max_khz": max_khz,
                "ratio": round(ratio, 3) if ratio > 0 else 0.0,
            })

        max_temperature = max(
            [item["celsius"] for item in temperatures],
            default=0.0,
        )
        min_frequency_ratio = min(frequency_ratios, default=0.0)
        thermal_throttling = bool(
            max_temperature >= 80.0
            or (
                max_temperature >= 70.0
                and min_frequency_ratio > 0
                and min_frequency_ratio <= 0.65
            )
        )
        return {
            "available": bool(temperatures or frequencies),
            "max_temperature_c": round(max_temperature, 1),
            "min_frequency_ratio": round(min_frequency_ratio, 3),
            "thermal_throttling": thermal_throttling,
            "temperatures": temperatures,
            "cpu_frequencies": frequencies,
        }

    @staticmethod
    def _extract_decoder_name(text: str) -> str:
        raw = str(text or "")
        patterns = [
            r"(OMX\.[\w\.\-]+)",
            r"(c2\.[\w\.\-]+)",
            r"(rk[\w\.\-]*decoder[\w\.\-]*)",
            r"(rkvdec[\w\.\-]*)",
            r"(vdec[\w\.\-]*)",
            r"(MediaCodec[\w\.\-:/]*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw, re.IGNORECASE)
            if match:
                return str(match.group(1)).strip()
        return ""

    def get_decoder_diagnostics(self, package_name: str = "") -> Dict:
        package_hint = str(package_name or "").split(":")[0].lower()
        outputs = [
            self._run_command(["shell", "dumpsys", "media.codec"], timeout=4),
            self._run_command(
                ["shell", "sh", "-c", "cat /proc/mpp_service/sessions-summary 2>/dev/null"],
                timeout=3,
            ),
            self._run_command(
                ["shell", "sh", "-c", "cat /sys/kernel/debug/mpp_service/stats 2>/dev/null"],
                timeout=3,
            ),
        ]
        relevant_lines: List[str] = []
        decoder_name = ""
        for output in outputs:
            for line in str(output or "").splitlines():
                line_s = line.strip()
                if not line_s:
                    continue
                line_l = line_s.lower()
                if package_hint and package_hint in line_l:
                    relevant_lines.append(line_s)
                elif any(
                    key in line_l
                    for key in ["codec", "omx", "c2.", "decoder", "rkvdec", "mpp", "session"]
                ):
                    relevant_lines.append(line_s)
                if not decoder_name:
                    decoder_name = self._extract_decoder_name(line_s)
        if not decoder_name:
            decoder_name = self._extract_decoder_name("\n".join(relevant_lines))
        return {
            "decoder_name": decoder_name,
            "codec_lines": relevant_lines[:12],
            "codec_output_excerpt": "\n".join(relevant_lines[:12]),
        }

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
            remote_path = (
                f"/data/local/tmp/screen_temp_{time.time_ns()}_"
                f"{threading.get_ident()}.png"
            )
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

    @staticmethod
    def _normalize_top_process_name(command: str) -> str:
        value = re.sub(r"\s+", " ", str(command or "").strip())
        if not value:
            return ""
        parts = value.split()
        shell_names = {"sh", "/system/bin/sh", "bash", "/system/bin/bash"}
        if len(parts) >= 2 and parts[0] in shell_names:
            if parts[1] == "-c" and len(parts) >= 3:
                value = " ".join(parts[2:])
            else:
                value = " ".join(parts[1:])
        lowered = value.lower()
        if "dex2oat" in lowered:
            if "--compilation-reason=install" in lowered:
                return "dex2oat (install)"
            return "dex2oat"
        if len(value) > 80:
            return value[:77] + "..."
        return value

    @classmethod
    def _parse_top_processes(cls, output: str) -> List[Dict]:
        """Parse Android toybox top output and aggregate duplicate commands."""
        grouped = {}
        parsing = False
        states = {"R", "S", "D", "I", "T", "Z", "X"}
        for raw_line in str(output or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "PID" in line and ("CPU" in line or "%CPU" in line):
                parsing = True
                continue
            if not parsing:
                continue

            parts = line.split()
            if not parts or not parts[0].isdigit():
                continue
            state_index = next(
                (
                    index
                    for index in range(5, min(len(parts), 10))
                    if parts[index].upper() in states
                ),
                -1,
            )
            cpu_index = state_index + 1
            if state_index < 0 or cpu_index >= len(parts):
                continue
            try:
                cpu_percent = float(parts[cpu_index].rstrip("%"))
            except (TypeError, ValueError):
                continue

            command_index = cpu_index + 3
            command = (
                " ".join(parts[command_index:])
                if command_index < len(parts)
                else parts[-1]
            )
            name = cls._normalize_top_process_name(command)
            if not name or name.lower() == "top" or name.lower().startswith("top "):
                continue

            item = grouped.setdefault(
                name,
                {
                    "name": name,
                    "cpu_percent": 0.0,
                    "instance_count": 0,
                    "pids": [],
                },
            )
            item["cpu_percent"] += cpu_percent
            item["instance_count"] += 1
            item["pids"].append(int(parts[0]))

        processes = list(grouped.values())
        for item in processes:
            item["cpu_percent"] = round(item["cpu_percent"], 2)
        processes.sort(key=lambda item: item["cpu_percent"], reverse=True)
        return processes

    def get_top_heavy_processes(self, limit: int = 3) -> str:
        """获取CPU占用最高的进程，并聚合同名脚本实例。"""
        try:
            output = self._run_command(["shell", "top", "-b", "-n", "1"])
            processes = [
                process
                for process in self._parse_top_processes(output)
                if not self._is_monitor_tool_process(process.get("name", ""))
            ]
            selected = list(processes[:max(1, int(limit))])
            selected_names = {process["name"] for process in selected}
            for process in processes:
                if (
                    int(process.get("instance_count", 1) or 1) >= 5
                    and process["name"] not in selected_names
                ):
                    selected.append(process)
                    selected_names.add(process["name"])
            result = []
            for process in selected:
                name = process["name"]
                count = int(process.get("instance_count", 1) or 1)
                if count > 1:
                    name = f"{name} x{count}"
                result.append(f"{name}({process['cpu_percent']}%)")
            return " | ".join(result)
        except Exception as e:
            logger.debug("get_top_heavy_processes 失败: %s", e)
            return f"Error: {str(e)}"

    @staticmethod
    def _is_monitor_tool_process(name: str) -> bool:
        value = str(name or "").lower()
        return bool(
            "screencap" in value
            or value == "top"
            or value.startswith("top ")
            or "screen_temp_" in value
        )

    def get_cpu_evidence(self, limit: int = 10) -> Dict:
        """Collect structured CPU evidence for a stall event."""
        captured_at = time.time()
        system_cpu_percent = self.get_system_cpu_usage()
        top_output = self._run_command(["shell", "top", "-b", "-n", "1"])
        loadavg_output = self._run_command(["shell", "cat", "/proc/loadavg"])
        process_count_output = self._run_command(
            ["shell", "sh", "-c", "ps -A | wc -l"]
        )

        processes = [
            process
            for process in self._parse_top_processes(top_output)
            if not self._is_monitor_tool_process(process.get("name", ""))
        ]
        try:
            process_count = int(str(process_count_output).strip())
        except (TypeError, ValueError):
            process_count = 0

        load_average = []
        for value in str(loadavg_output or "").split()[:3]:
            try:
                load_average.append(float(value))
            except (TypeError, ValueError):
                break

        return {
            "timestamp": captured_at,
            "time": datetime.fromtimestamp(captured_at).isoformat(
                timespec="milliseconds"
            ),
            "process_count": process_count,
            "system_cpu_percent": system_cpu_percent,
            "load_average": load_average,
            "top_processes": processes[:max(1, int(limit))],
            "duplicate_processes": [
                process
                for process in processes
                if int(process.get("instance_count", 1) or 1) >= 3
            ],
            "raw_top": top_output or "",
        }
