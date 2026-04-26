
import os
import sys
import json
import logging
import subprocess
import threading
import time
import re
import uuid
import queue
from datetime import datetime
from collections import deque
from flask import Blueprint, render_template, request, jsonify, make_response, current_app
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from core.runtime.manager import get_runtime_manager as get_platform_runtime_manager, RuntimeStatus as PlatformRuntimeStatus

# Import TROM Manager
try:
    from shared.core.runtime_manager import get_runtime_manager
    from shared.core.trom import TaskType, StreamSource, BehaviorItem, PerformanceItem, EventItem, EventLevel
except ImportError:
    # Try absolute path import if relative fails
    try:
        from trae_platform.shared.core.runtime_manager import get_runtime_manager
        from trae_platform.shared.core.trom import TaskType, StreamSource, BehaviorItem, PerformanceItem, EventItem, EventLevel
    except ImportError:
        get_runtime_manager = None

try:
    from shared.unified.report_store import get_unified_report_store
except Exception:
    get_unified_report_store = None


# Optional: Try to import CollectorManager for performance monitoring
try:
    from modules.performance_monitor.core.collector_manager import CollectorManager
except ImportError:
    CollectorManager = None

monkey_bp = Blueprint('monkey', __name__, template_folder='templates', static_folder='static')

# Logger
logger = setup_logger('monkey_module')

# Globals
monkey_tests = {}
monkey_tests_lock = threading.Lock()
DEVICE_THREADS = {}
device_threads_lock = threading.Lock()
REPORTS = []
reports_lock = threading.Lock()
# 批次 Monkey：4 板同时 / 多轮循环
BATCHES = {}
batches_lock = threading.Lock()

# Config (Try to load from original monkey tool, else use defaults)
MONKEY_CONFIG_FILE = r'D:\trae-code\monkey\config.json'
DEFAULT_CONFIG = {
    'monkey': {
        'default_package': 'com.thunder.ktv',
        'default_events': 200000,
        'default_throttle': 1000,
        'default_timeout': 24300,
        'debug_port': 2007,
        'adb_port': 8787
    },
    'adb': {
        'default_port': 8787
    }
}

def load_config():
    try:
        if os.path.exists(MONKEY_CONFIG_FILE):
            with open(MONKEY_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load monkey config: {e}")
    return DEFAULT_CONFIG

CONFIG = load_config()


def _to_positive_int(value, default_value):
    try:
        v = int(value)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return int(default_value)


def _estimate_timeout_lower_bound(events_count, throttle_ms):
    events = max(1, int(events_count or 1))
    throttle = max(0, int(throttle_ms or 0))
    throttle_sec = throttle / 1000.0
    expected_sec_per_event = max(1.2, throttle_sec)
    estimated = int(events * expected_sec_per_event + 300)
    theoretical_floor = int(events * max(0.05, throttle_sec) + 120)
    return max(900, estimated, theoretical_floor)


def _normalize_timeout(events_count, throttle_ms, timeout_sec):
    requested = _to_positive_int(timeout_sec, CONFIG.get('monkey', {}).get('default_timeout', 3600))
    floor = _estimate_timeout_lower_bound(events_count, throttle_ms)
    if requested < floor:
        return floor, True, floor
    return requested, False, floor

# Helper Functions - 使用统一设备管理器
from core.device.manager import get_device_manager

def run_adb_command(cmd, timeout=15):
    """
    cmd: list or str
    Returns: stdout (str), returncode (int), stderr (str)
    使用 DeviceManager 统一执行
    """
    try:
        if isinstance(cmd, str):
            parts = cmd.split()
        else:
            parts = list(cmd)
            
        # Strip 'adb'
        if parts and parts[0] == 'adb':
            parts = parts[1:]
            
        device_id = None
        if len(parts) >= 2 and parts[0] == '-s':
            device_id = parts[1]
            parts = parts[2:]
            
        dm = get_device_manager()
        # run_adb_command returns (code, stdout, stderr)
        code, stdout, stderr = dm.run_adb_command(device_id, parts, timeout=timeout)
        return stdout, code, stderr
    except Exception as e:
        logger.error(f"run_adb_command error: {e}")
        return "", 1, str(e)

def kill_monkey_process(device_id):
    """
    Robustly kill monkey process on device
    """
    try:
        # 1. Try pkill (fastest, available on newer Androids)
        run_adb_command(["adb", "-s", device_id, "shell", "pkill", "-f", "com.android.commands.monkey"])
        
        # 2. Double check with ps and kill by PID
        # Some devices require -A to see all processes, others don't support it
        out, rc, _ = run_adb_command(["adb", "-s", device_id, "shell", "ps", "-A"]) 
        if not out or "bad pid" in out.lower():
             out, rc, _ = run_adb_command(["adb", "-s", device_id, "shell", "ps"])
        
        if out:
            for line in out.splitlines():
                if "com.android.commands.monkey" in line:
                    parts = line.split()
                    if len(parts) > 1:
                        # PID is usually the 2nd column
                        pid = parts[1]
                        if pid.isdigit():
                            run_adb_command(["adb", "-s", device_id, "shell", "kill", "-9", pid])
    except Exception as e:
        logger.error(f"Error killing monkey on {device_id}: {e}")

class MonkeyOutputReader:
    """
    Non-blocking output reader using a background thread
    """
    def __init__(self, process):
        self.process = process
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._reader_thread, daemon=True)
        self.thread.start()
    
    def _reader_thread(self):
        try:
            # Iterates lines until EOF
            for line in iter(self.process.stdout.readline, ''):
                self.queue.put(line)
            self.process.stdout.close()
        except Exception:
            pass
            
    def get_line(self):
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

def get_adb_devices():
    out, rc, err = run_adb_command(["adb", "devices"])
    devices = []
    if rc == 0 and out:
        lines = out.splitlines()[1:]
        for line in lines:
            if '\t' in line:
                device_id, status = line.split('\t', 1)
                if status.strip() == 'device':
                    devices.append(device_id.strip())
    return devices

def check_device_connection(ip, port):
    if ip == 'mock_device': return True
    try:
        adb_devices = get_adb_devices()
        device_id = f"{ip}:{port}"
        return device_id in adb_devices
    except Exception:
        return False

def connect_adb_device(ip, port):
    """
    连接 ADB 设备。返回 (成功, device_id或失败原因)。
    成功时 device_id 以 adb devices 实际为准（如 192.168.16.131:5555）。
    """
    try:
        # 1. 尝试通过 HTTP 启用 ADB（可选步骤，失败不影响后续连接）
        try:
            import urllib.request
            debug_port = CONFIG.get('monkey', {}).get('debug_port', 2007)
            enable_url = f"http://{ip}:{debug_port}/debug/adb?enable=1"
            urllib.request.urlopen(enable_url, timeout=2)
            logger.info(f'已发送 ADB 启用请求: {enable_url}')
        except Exception:
            pass

        # 2. ADB 连接
        addr = f"{ip}:{port}"
        out, rc, err = run_adb_command(["adb", "connect", addr])
        out_lower = (out or "").lower()
        err_lower = (err or "").lower()

        if rc == 0 and ('connected to' in out_lower or 'already connected' in out_lower):
            logger.info(f'设备连接成功: {addr}')
            # 以 adb devices 实际为准（可能带端口）
            devices = get_adb_devices()
            for d in devices:
                if d == addr or d.startswith(ip + ":"):
                    return (True, d)
            return (True, addr)

        # 给出可执行建议
        if "timeout" in err_lower or "timed out" in out_lower:
            return (False, "连接超时，请确认设备与电脑同网段、已开启 USB 调试（网络）")
        if "refused" in err_lower or "connection refused" in out_lower:
            return (False, "连接被拒绝，可尝试端口 5555 或 8787")
        if "cannot connect" in out_lower or "failed to connect" in out_lower:
            return (False, "无法连接，请确认设备已开 ADB 网络调试、端口正确")
        logger.warning(f'设备连接失败: {addr}, 输出: {out}, 错误: {err}')
        return (False, (out or err or "连接失败").strip()[:80])
    except Exception as e:
        logger.error(f'连接设备异常: {ip}:{port}, 错误: {e}')
        return (False, str(e)[:80])


def try_connect_ports(ip, ports=None):
    """只填 IP 时依次尝试常用端口连接。返回 (device_id, None) 或 (None, 错误原因)。"""
    if ports is None:
        ports = (5555, 8787)
    last_err = ""
    for port in ports:
        if check_device_connection(ip, port):
            devices = get_adb_devices()
            for d in devices:
                if d.startswith(ip + ":") or d == ip:
                    return (d, None)
            return (f"{ip}:{port}", None)
        ok, msg = connect_adb_device(ip, port)
        if ok:
            return (msg, None)
        last_err = msg or f"端口 {port} 失败"
    return (None, last_err)


def collect_process_pss_mb(device_id, package):
    try:
        out, rc, err = run_adb_command(["adb", "-s", device_id, "shell", "dumpsys", "meminfo", package])
        if rc != 0: return 0
        m = re.search(r"TOTAL\s+(\d+)", out)
        if m: return int(m.group(1)) // 1024
        m2 = re.search(r"Total\s+PSS\s*:\s*(\d+)\s*kB", out, re.I)
        if m2: return int(m2.group(1)) // 1024
        return 0
    except Exception:
        return 0

# Monkey Test Result Class
class MonkeyTestResult:
    STATUS_UNKNOWN="UNKNOWN"; STATUS_RUNNING="RUNNING"; STATUS_SUCCESS="SUCCESS"
    STATUS_FAILED="FAILED"; STATUS_TIMEOUT="TIMEOUT"; STATUS_ERROR="ERROR"; STATUS_STOPPED="STOPPED"
    
    def __init__(self, device_ip, device_port):
        self.report_id = str(uuid.uuid4())
        # unified runtime id (TROM runtime_id) if available
        self.runtime_id = None
        # Platform Runtime Center ID
        self.platform_runtime_id = None
        self.device_ip=device_ip
        self.device_port=device_port
        self.device_id=f"{device_ip}:{device_port}"
        self.status=self.STATUS_UNKNOWN
        self.status_reason=""
        self.start_time=None
        self.end_time=None
        self.duration=0
        self.events_planned=CONFIG.get('monkey', {}).get('default_events', 200000)
        self.events_executed=0
        self.crash_count=0
        self.anr_count=0
        self.exception_count=0
        self.error_count=0
        self.error_details=[]
        self.monkey_output=""
        self.monkey_process=None
        self.monkey_pid=None          # 设备端 Monkey 进程 PID
        self.monkey_local_pid=None   # 本机 adb 子进程 PID
        self.package_name=CONFIG.get('monkey', {}).get('default_package','com.thunder.ktv')
        self.before_mem_mb=0
        self.after_mem_mb=0
        self.delta_mem_mb=0
        self.leak=False
        
        # Performance Stats
        self.video_fps = 0.0
        self.mpp_active = 0
        self.mpp_sessions = 0
        self.cpu_app = 0.0
        # 批次测试
        self.batch_id = None
        self.round_index = None

    def get_coverage_percent(self):
        if self.events_planned==0: return 0
        return (self.events_executed/self.events_planned)*100
        
    def is_successful(self,min_coverage=80):
        return (self.status==self.STATUS_SUCCESS and self.get_coverage_percent()>=min_coverage
                and self.crash_count==0 and self.anr_count==0)
                
    def to_dict(self):
        return {
            'report_id': self.report_id,
            'runtime_id': self.runtime_id,
            'device_id': self.device_id,
            'device_ip': self.device_ip,
            'device_port': self.device_port,
            'status': self.status,
            'status_reason': self.status_reason,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'duration': self.duration,
            'events_planned': self.events_planned,
            'events_executed': self.events_executed,
            'coverage_percent': self.get_coverage_percent(),
            'crash_count': self.crash_count,
            'anr_count': self.anr_count,
            'package_name': self.package_name,
            'error_details': self.error_details,
            'monkey_output': self.monkey_output,
            'is_successful': self.is_successful(),
            'before_mem_mb': self.before_mem_mb,
            'after_mem_mb': self.after_mem_mb,
            'delta_mem_mb': self.delta_mem_mb,
            'leak': self.leak,
            'video_fps': self.video_fps,
            'mpp_active': self.mpp_active,
            'mpp_sessions': self.mpp_sessions,
            'cpu_app': self.cpu_app,
            'batch_id': getattr(self, 'batch_id', None),
            'round_index': getattr(self, 'round_index', None),
            'monkey_pid': getattr(self, 'monkey_pid', None),
            'monkey_local_pid': getattr(self, 'monkey_local_pid', None),
        }

class AdbAdapter:
    """Adapter to make run_adb_command compatible with CollectorManager's adb_controller"""
    def __init__(self, device_id):
        self.device_id = device_id
        
    def _run_command(self, cmd_list, timeout=3):
        full_cmd = ["adb", "-s", self.device_id] + cmd_list
        out, rc, err = run_adb_command(full_cmd, timeout=timeout)
        return out if rc == 0 else ""


def get_monkey_pid_on_device(device_id):
    """获取设备上 Monkey 进程的 PID，用于日志确认「monkey 已启动」。"""
    try:
        # 优先尝试 pidof（部分设备支持）
        out, rc, _ = run_adb_command(["adb", "-s", device_id, "shell", "pidof", "com.android.commands.monkey"], timeout=5)
        if rc == 0 and out and out.strip().isdigit():
            return int(out.strip().split()[0])
        # 回退：ps -A 解析
        out, rc, _ = run_adb_command(["adb", "-s", device_id, "shell", "ps", "-A"], timeout=5)
        if rc != 0 or not out:
            return None
        for line in out.splitlines():
            if "monkey" not in line.lower():
                continue
            parts = line.split()
            for i, p in enumerate(parts):
                if p.isdigit() and i > 0:
                    return int(p)
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
        return None
    except Exception as e:
        logger.debug("get_monkey_pid_on_device %s: %s", device_id, e)
        return None


def run_monkey_test_background(test_result, events_count, throttle, timeout):
    try:
        device_id = test_result.device_id
        pkg = test_result.package_name
        timeout, _, _ = _normalize_timeout(events_count, throttle, timeout)
        logger.info(f"Executing Monkey: {device_id} pkg={pkg}")
        
        # Device check
        out, rc, err = run_adb_command(["adb", "-s", device_id, "shell", "echo", "ok"])
        if rc != 0: raise Exception(f"Device check failed: {err}")
        
        # Process check
        out, rc, err = run_adb_command(["adb", "-s", device_id, "shell", "ps"])
        if pkg not in (out or ''):
            run_adb_command(["adb", "-s", device_id, "shell", "am", "start", "-n", f"{pkg}/.MainActivity"])
            time.sleep(2)
            
        try:
            test_result.before_mem_mb = int(collect_process_pss_mb(device_id, pkg) or 0)
        except:
            test_result.before_mem_mb = 0
            
        # Init CollectorManager if available
        collector_manager = None
        if CollectorManager:
            try:
                adb_adapter = AdbAdapter(device_id)
                collector_manager = CollectorManager(adb_adapter, pkg)
            except Exception as e:
                logger.warning(f"Failed to init CollectorManager: {e}")

        monkey_cmd = [
            "adb", "-s", device_id, "shell", "monkey",
            "-v", "-v", "-v",
            "-p", pkg,
            "-c", "android.intent.category.HMONKEY",
            "--ignore-crashes", "--ignore-timeouts",
            "--pct-touch", "70", "--pct-motion", "30",
            "--throttle", str(throttle),
            str(events_count),
            "--ignore-security-exceptions",
            "--kill-process-after-error",
            "--monitor-native-crashes"
        ]
        
        start_ts = time.time()
        last_mem_check_time = 0
        last_perf_check_time = 0
        last_device_check_time = 0
        device_check_interval = 30
        
        # TROM Initialization
        trom_runtime = None
        trom_last_save = 0
        if get_runtime_manager:
            try:
                manager = get_runtime_manager()
                trom_runtime = manager.create_runtime(
                    task_type=TaskType.MONKEY,
                    created_by="user_admin", # TODO: Get real user
                    device_id=device_id,
                    package_name=pkg
                )
                test_result.runtime_id = trom_runtime.runtime_id
                logger.info(f"TROM Runtime started: {trom_runtime.runtime_id}")
            except Exception as e:
                logger.error(f"Failed to start TROM runtime: {e}")

        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        proc = subprocess.Popen(
            monkey_cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            encoding='utf-8', 
            errors='ignore', 
            bufsize=1,
            startupinfo=startupinfo
        )
        test_result.monkey_process = proc
        test_result.monkey_local_pid = proc.pid

        # 等待设备上 Monkey 进程起来后取 PID，用于日志「monkey 已启动 + PID」
        time.sleep(1.5)
        try:
            device_pid = get_monkey_pid_on_device(device_id)
            if device_pid is not None:
                test_result.monkey_pid = device_pid
                logger.info("Monkey started on %s, device PID: %s, local PID: %s", device_id, device_pid, proc.pid)
        except Exception as e:
            logger.debug("Could not get monkey PID on device: %s", e)

        # 使用非阻塞读取器
        reader = MonkeyOutputReader(proc)
        
        output_lines = []
        stderr_lines = []
        
        # 实时读取输出
        while proc.poll() is None:
            now = time.time()
            try:
                # 定期设备存活检测（断网即退出）
                if now - last_device_check_time > device_check_interval:
                    _, rc, _ = run_adb_command(["adb", "-s", device_id, "shell", "echo", "ok"], timeout=5)
                    if rc != 0:
                        logger.warning(f"Device {device_id} disconnected (adb echo ok failed), stopping monkey...")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        kill_monkey_process(device_id)
                        test_result.status = MonkeyTestResult.STATUS_ERROR
                        test_result.status_reason = "设备断开 (Device disconnected)"
                        break
                    last_device_check_time = now

                # 定期检查内存 (每15秒)
                if now - last_mem_check_time > 15:
                    try:
                        mem = int(collect_process_pss_mb(device_id, pkg) or 0)
                        if mem > 0:
                            test_result.after_mem_mb = mem # 运行时使用 after_mem_mb 存储当前内存
                    except:
                        pass
                    last_mem_check_time = now

                # 定期检查性能 FPS/MPP (每2秒)
                if collector_manager and now - last_perf_check_time > 2:
                    try:
                        perf_data = collector_manager.collect_all()
                        test_result.video_fps = perf_data.get('video_fps', 0.0)
                        test_result.mpp_active = 1 if perf_data.get('usage', 0) > 0 else 0
                        # 简单的 MPP session 计数 (如果 collector 支持)
                        test_result.mpp_sessions = perf_data.get('session_count', 0)
                        # CPU
                        test_result.cpu_app = perf_data.get('cpu_app', 0.0)
                        
                        # TROM Perf Record
                        if trom_runtime:
                            perf_item = PerformanceItem(
                                ts=datetime.utcnow(),
                                cpu_percent=perf_data.get('cpu_app', 0.0),
                                mem_pss_mb=float(test_result.after_mem_mb),
                                fps=perf_data.get('video_fps', 0.0)
                            )
                            trom_runtime.streams.performance_stream.append(perf_item)
                            
                    except Exception as e:
                        logger.warning(f"Perf collection error: {e}")
                    last_perf_check_time = now

                # 从队列获取日志行 (非阻塞)
                line = reader.get_line()
                
                if line is None:
                    # 如果没有日志输出，检查是否超时
                    if now - start_ts > timeout:
                        logger.warning(f"Monkey test timeout ({timeout}s), killing process...")
                        try:
                            proc.kill()
                        except:
                            pass
                        kill_monkey_process(device_id) # 确保远程进程也被杀掉
                        
                        test_result.status = MonkeyTestResult.STATUS_TIMEOUT
                        test_result.status_reason = f"Timeout({timeout}s)"
                        break
                    
                    # 检查是否被手动停止
                    if test_result.status == MonkeyTestResult.STATUS_STOPPED:
                        try:
                            proc.kill()
                        except:
                            pass
                        kill_monkey_process(device_id)
                        break
                        
                    time.sleep(0.05)
                    continue
                
                # 移除换行符并添加到列表
                line_clean = line.rstrip('\n')
                output_lines.append(line_clean)
                
                # TROM Behavior Record
                if trom_runtime:
                    try:
                        # Simple parsing for Monkey actions
                        if ":Sending" in line_clean:
                            action_type = "unknown"
                            if "Touch" in line_clean: action_type = "touch"
                            elif "Trackball" in line_clean: action_type = "trackball"
                            elif "Key" in line_clean: action_type = "key"
                            elif "Flip" in line_clean: action_type = "flip"
                            
                            coords = None
                            if "(" in line_clean and ")" in line_clean:
                                # extract (x,y)
                                m_coord = re.search(r'\((\d+\.\d+),(\d+\.\d+)\)', line_clean)
                                if m_coord:
                                    coords = (int(float(m_coord.group(1))), int(float(m_coord.group(2))))
                            
                            behavior = BehaviorItem(
                                ts=datetime.utcnow(),
                                source=StreamSource.MONKEY,
                                action=action_type,
                                x=coords[0] if coords else None,
                                y=coords[1] if coords else None,
                                payload={"raw": line_clean}
                            )
                            trom_runtime.streams.behavior_stream.append(behavior)
                            
                        # TROM Event Record (Crash/ANR)
                        if "// CRASH:" in line_clean or "// NOT RESPONDING:" in line_clean:
                            event_type = "CRASH" if "// CRASH:" in line_clean else "ANR"
                            event = EventItem(
                                ts=datetime.utcnow(),
                                type=event_type,
                                source="monkey",
                                level=EventLevel.FATAL,
                                message=line_clean
                            )
                            trom_runtime.streams.event_stream.append(event)
                            
                        # Periodic Save (every 5s)
                        if now - trom_last_save > 5:
                            get_runtime_manager().save_runtime(trom_runtime)
                            trom_last_save = now
                            
                    except Exception as e:
                        pass

                # 进度解析
                # Monkey -v -v -v 会输出每一步操作，如 :Sending, :Switch 等
                if ":Sending" in line or ":Switch" in line:
                    test_result.events_executed += 1
                
                # 最终结果解析 (Events injected: 100)
                if "Events injected:" in line:
                    m = re.search(r'Events injected: (\d+)', line)
                    if m:
                        test_result.events_executed = int(m.group(1))

                # 超时检查 (即使有输出也要检查)
                if now - start_ts > timeout:
                    logger.warning(f"Monkey test timeout ({timeout}s) with active output, killing process...")
                    try:
                        proc.kill()
                    except:
                        pass
                    kill_monkey_process(device_id)
                    
                    test_result.status = MonkeyTestResult.STATUS_TIMEOUT
                    test_result.status_reason = f"Timeout({timeout}s)"
                    break
                    
            except Exception as read_err:
                logger.warning(f"Error in monkey monitoring loop: {read_err}")
                time.sleep(0.1)

        # 读取剩余输出和错误流
        # 注意: stdout 已经被 reader 消费了大部分，这里主要是 stderr
        # 为了避免 communicate 尝试读取已被 reader 线程占用的 stdout，将其设为 None
        proc.stdout = None
        rest_out, rest_err = proc.communicate(timeout=5)
        
        # 还要检查 queue 里是否还有残留的行
        while True:
            line = reader.get_line()
            if line is None: break
            output_lines.append(line.rstrip('\n'))
            
        if rest_out:
            output_lines.extend(rest_out.splitlines())
        if rest_err:
            stderr_lines = rest_err.splitlines()
        
        end_ts = time.time()
        test_result.duration = end_ts - start_ts
        test_result.end_time = datetime.now()
        test_result.monkey_output = '\n'.join(output_lines)
        
        full_out = '\n'.join(output_lines)
        parsed_ok = False
        
        if "Events injected" in full_out:
            m = re.search(r'Events injected: (\d+)', full_out)
            if m:
                test_result.events_executed = int(m.group(1))
                test_result.status = MonkeyTestResult.STATUS_SUCCESS
                parsed_ok = True
                
        if not parsed_ok:
            if test_result.status == MonkeyTestResult.STATUS_TIMEOUT:
                pass # Already set as timeout
            elif test_result.status == MonkeyTestResult.STATUS_STOPPED:
                pass # Already set as stopped by API
            else:
                test_result.status = MonkeyTestResult.STATUS_FAILED
                test_result.status_reason = "Events injected not found (Process exited)"

            
        # 使用更精准的匹配，避免日志中的普通 "crash" 单词被误判
        # Monkey 标准输出: // CRASH: com.package (pid)
        crashes = re.findall(r'// CRASH:.*', full_out)
        if crashes:
            test_result.crash_count = len(crashes)
            test_result.error_details.extend(crashes)
            
        # Monkey 标准输出: // NOT RESPONDING: com.package (pid)
        anrs = re.findall(r'// NOT RESPONDING:.*', full_out)
        if anrs:
            test_result.anr_count = len(anrs)
            test_result.error_details.extend(anrs)
            
        try:
            test_result.after_mem_mb = int(collect_process_pss_mb(device_id, pkg) or 0)
            test_result.delta_mem_mb = max(0, int(test_result.after_mem_mb) - int(test_result.before_mem_mb))
            test_result.leak = bool(int(test_result.delta_mem_mb) >= 50)
        except:
            pass
            
    except Exception as e:
        test_result.status = MonkeyTestResult.STATUS_ERROR
        test_result.status_reason = str(e)
        test_result.end_time = datetime.now()
        logger.error(f"Monkey execution error: {e}")
    finally:
        # Platform Runtime Finish
        if test_result.platform_runtime_id:
            try:
                platform_status = PlatformRuntimeStatus.COMPLETED
                if test_result.status in [MonkeyTestResult.STATUS_ERROR, MonkeyTestResult.STATUS_FAILED, MonkeyTestResult.STATUS_TIMEOUT]:
                    platform_status = PlatformRuntimeStatus.FAILED
                elif test_result.status == MonkeyTestResult.STATUS_STOPPED:
                    platform_status = PlatformRuntimeStatus.CANCELLED
                
                get_platform_runtime_manager().update_status(
                    test_result.platform_runtime_id,
                    platform_status,
                    result={
                        'status': test_result.status,
                        'is_successful': test_result.is_successful(),
                        'events_executed': test_result.events_executed,
                        'events_planned': test_result.events_planned,
                        'crash_count': test_result.crash_count,
                        'anr_count': test_result.anr_count
                    }
                )
                logger.info(f"Platform Runtime finished: {test_result.platform_runtime_id}")
            except Exception as e:
                logger.error(f"Failed to finish Platform Runtime: {e}")

        # TROM Finish
        if trom_runtime and get_runtime_manager:
            try:
                result_status = "pass" if test_result.is_successful() else "fail"
                # If status is ERROR, mark as FAIL
                if test_result.status in [MonkeyTestResult.STATUS_ERROR, MonkeyTestResult.STATUS_FAILED, MonkeyTestResult.STATUS_TIMEOUT]:
                    result_status = "fail"
                
                get_runtime_manager().finish_runtime(
                    trom_runtime.runtime_id, 
                    result=result_status,
                    conclusion=f"Status: {test_result.status}. Executed: {test_result.events_executed}/{test_result.events_planned}"
                )
                logger.info(f"TROM Runtime finished: {trom_runtime.runtime_id}")
            except Exception as e:
                logger.error(f"Failed to finish TROM runtime: {e}")

        with monkey_tests_lock:
            if test_result.device_id in monkey_tests:
                try:
                    finished = monkey_tests.pop(test_result.device_id)
                except KeyError:
                    finished = test_result
        with reports_lock:
            report_dict = test_result.to_dict()
            REPORTS.append(report_dict)
        batch_id = getattr(test_result, "batch_id", None)
        round_index = getattr(test_result, "round_index", None)
        if batch_id and round_index is not None:
            with batches_lock:
                if batch_id in BATCHES:
                    b = BATCHES[batch_id]
                    device_id = test_result.device_id
                    if device_id not in b["results"]:
                        b["results"][device_id] = []
                    b["results"][device_id].append({
                        "round_index": round_index,
                        "report_id": report_dict.get("report_id"),
                        "runtime_id": report_dict.get("runtime_id"),
                        "status": report_dict.get("status"),
                        "status_reason": report_dict.get("status_reason"),
                    })
                    # 本批次该设备已结束；若当前轮已是最后一轮且本批次所有设备都不在 monkey_tests 中，立即标记批次为 finished
                    device_ids = b.get("device_ids") or []
                    current_round = int(b.get("current_round") or 0)
                    total_rounds = int(b.get("rounds") or 1)
                    with monkey_tests_lock:
                        still_running = [d for d in device_ids if d.strip() in monkey_tests]
                    if not still_running and b.get("status") == "running" and current_round >= total_rounds:
                        BATCHES[batch_id]["status"] = "finished"
                        BATCHES[batch_id]["finished_at"] = time.time()
        with device_threads_lock:
            if test_result.device_id in DEVICE_THREADS:
                DEVICE_THREADS.pop(test_result.device_id, None)

        # Write unified report (additive; does not affect existing endpoints)
        if get_unified_report_store:
            try:
                unified_id = f"monkey_{test_result.report_id}"
                summary = {
                    "status": test_result.status,
                    "is_successful": test_result.is_successful(),
                    "events": {
                        "planned": test_result.events_planned,
                        "executed": test_result.events_executed,
                        "coverage_percent": test_result.get_coverage_percent(),
                    },
                    "crash_count": test_result.crash_count,
                    "anr_count": test_result.anr_count,
                    "mem_mb": {
                        "before": test_result.before_mem_mb,
                        "after": test_result.after_mem_mb,
                        "delta": test_result.delta_mem_mb,
                        "leak": test_result.leak,
                    },
                }
                details = {
                    "status_reason": test_result.status_reason,
                    "error_details": test_result.error_details,
                    "performance": {
                        "video_fps": test_result.video_fps,
                        "mpp_active": test_result.mpp_active,
                        "mpp_sessions": test_result.mpp_sessions,
                        "cpu_app": test_result.cpu_app,
                    },
                }
                raw = {"monkey": test_result.to_dict()}
                get_unified_report_store().save_report(
                    unified_id=unified_id,
                    module="monkey",
                    kind="monkey",
                    status=test_result.status,
                    summary=summary,
                    details=details,
                    device_id=test_result.device_id,
                    package_name=test_result.package_name,
                    legacy_id=test_result.report_id,
                    runtime_id=test_result.runtime_id,
                    started_at=test_result.start_time.isoformat() if test_result.start_time else None,
                    finished_at=test_result.end_time.isoformat() if test_result.end_time else None,
                    raw=raw,
                )
            except Exception as e:
                logger.warning(f"Failed to write unified report: {e}")

        # 一键任务：Monkey 完成后，若该 run 包含性能监控，则自动停止性能监控并写入报告
        unified_run_id = getattr(test_result, "unified_run_id", None)
        if unified_run_id:
            try:
                from modules.unified.hooks import on_unified_monkey_finished
                on_unified_monkey_finished(unified_run_id)
            except Exception as e:
                logger.warning("Unified monkey-finished hook: %s", e)

def _start_monkey_for_device(device_key, package_name, events_count, throttle, timeout, batch_id=None, round_index=None):
    """内部：为单台设备启动 Monkey，可选 batch_id/round_index。调用方需保证设备已连接。"""
    parts = device_key.split(":")
    ip = parts[0] if parts else ""
    port = int(parts[1]) if len(parts) > 1 else CONFIG.get("adb", {}).get("default_port", 8787)
    
    # 优化：智能连接（端口回退）
    # 如果指定端口未连接，尝试自动探测常用端口
    if not check_device_connection(ip, port):
        # 尝试智能连接
        real_id, err = try_connect_ports(ip)
        if real_id:
            # 更新为实际连接的端口
            if ":" in real_id:
                ip, port_str = real_id.split(":")
                port = int(port_str)
            device_key = real_id # 更新 Key 以匹配实际设备
        else:
            # 回退到原始逻辑以获取报错
            ok, msg = connect_adb_device(ip, port)
            if not ok:
                raise RuntimeError(msg or f"设备未连接: {device_key}")

    with monkey_tests_lock:
        if device_key in monkey_tests:
            raise RuntimeError(f"设备正在运行测试: {device_key}")

    # double check
    if not check_device_connection(ip, port):
         # Last attempt to connect specific port
         ok, msg = connect_adb_device(ip, port)
         if not ok:
            raise RuntimeError(f"设备无法连接: {ip}:{port}")

    tr = MonkeyTestResult(ip, port)
    tr.package_name = package_name
    tr.events_planned = int(events_count)
    tr.start_time = datetime.now()
    tr.status = MonkeyTestResult.STATUS_RUNNING
    tr.batch_id = batch_id
    tr.round_index = round_index
    try:
        runtime = get_platform_runtime_manager().create_runtime(
            name=f"Monkey Test: {device_key}",
            module="monkey",
            context={"device_id": device_key, "package_name": tr.package_name, "events": tr.events_planned, "throttle": throttle, "timeout": timeout}
        )
        tr.platform_runtime_id = runtime.runtime_id
        get_platform_runtime_manager().update_status(runtime.runtime_id, PlatformRuntimeStatus.RUNNING)
    except Exception as e:
        logger.error("Failed to create platform runtime: %s", e)
    with monkey_tests_lock:
        monkey_tests[device_key] = tr
    t = threading.Thread(target=run_monkey_test_background, args=(tr, tr.events_planned, throttle, timeout), daemon=True)
    with device_threads_lock:
        DEVICE_THREADS[device_key] = t
    t.start()
    return tr


def _run_batch_coordinator(batch_id, device_ids, package_name, events_count, throttle, timeout, rounds, sample_interval_minutes):
    """后台：多轮循环，每轮同时启动 N 台 Monkey，等全部结束后进入下一轮。"""
    try:
        for round_idx in range(1, int(rounds) + 1):
            with batches_lock:
                if batch_id not in BATCHES:
                    return
                BATCHES[batch_id]["current_round"] = round_idx
            
            active_keys = [] # 记录本轮实际启动成功的设备Key
            
            for device_key in device_ids:
                # 增加重试机制 (最多重试3次)
                max_retries = 3
                last_error = None
                
                for attempt in range(max_retries):
                    try:
                        tr = _start_monkey_for_device(
                            device_key.strip(),
                            package_name,
                            events_count,
                            throttle,
                            timeout,
                            batch_id=batch_id,
                            round_index=round_idx,
                        )
                        active_keys.append(tr.device_id)
                        last_error = None
                        break # 成功则跳出重试循环
                    except Exception as e:
                        last_error = e
                        if attempt < max_retries - 1:
                            time.sleep(3) # 失败等待后重试
                
                if last_error:
                    err_msg = str(last_error)
                    logger.warning("Batch %s round %s device %s start failed after retries: %s", batch_id, round_idx, device_key, err_msg)
                    with batches_lock:
                        if batch_id in BATCHES:
                            BATCHES[batch_id].setdefault("start_errors", {})[device_key.strip()] = err_msg
            
            # 如果本轮没有一台设备成功启动，且不是因为被停止，避免死循环太快
            if not active_keys:
                 time.sleep(5)

            # 等待本轮所有设备结束（或被用户停止）
            # 使用 active_keys 来判断是否还在运行
            wait_start = time.time()
            while time.time() - wait_start < timeout * len(device_ids) + 300:
                with batches_lock:
                    if BATCHES.get(batch_id, {}).get("status") == "stopped":
                        break
                with monkey_tests_lock:
                    # 检查 active_keys 中的设备是否还在 monkey_tests 中
                    still_running = [d for d in active_keys if d in monkey_tests]
                if not still_running:
                    break
                time.sleep(5)
            with batches_lock:
                if BATCHES.get(batch_id, {}).get("status") == "stopped":
                    break
            time.sleep(2)
        with batches_lock:
            if batch_id in BATCHES and BATCHES[batch_id].get("status") != "stopped":
                BATCHES[batch_id]["status"] = "finished"
                BATCHES[batch_id]["finished_at"] = time.time()
    except Exception as e:
        logger.exception("Batch coordinator %s error: %s", batch_id, e)
        with batches_lock:
            if batch_id in BATCHES:
                BATCHES[batch_id]["status"] = "failed"
                BATCHES[batch_id]["error"] = str(e)


from .scheduler import scheduler_instance
scheduler_instance.start()

# Routes
@monkey_bp.route('/')
def index():
    return render_template('monkey_index.html')

@monkey_bp.route('/api/start', methods=['POST'])
def api_start_monkey():
    try:
        data = request.get_json() or {}
        
        # 参数验证
        validation_error = validate_required(data, 'ip')
        if validation_error:
            return validation_error
        
        ip = data.get('ip')
        try:
            port = int(data.get('port') or CONFIG.get('adb', {}).get('default_port', 8787))
            if port <= 0 or port > 65535:
                return error_response(
                    message='端口号必须在 1-65535 之间',
                    error='invalid port',
                    status_code=400
                )
        except (ValueError, TypeError):
            return error_response(
                message='端口格式错误',
                error='invalid port format',
                status_code=400
            )
        
        device_key = f"{ip}:{port}"
            
        with monkey_tests_lock:
            if device_key in monkey_tests:
                return error_response(
                    message='设备正在运行测试，请先停止当前测试',
                    error='Device is already running test',
                    status_code=400
                )
                
        if not check_device_connection(ip, port):
            # 尝试智能连接（端口回退）
            real_id, err = try_connect_ports(ip)
            if real_id:
                if ":" in real_id:
                    ip_new, port_str = real_id.split(":")
                    port = int(port_str)
                    ip = ip_new
                device_key = f"{ip}:{port}" # 更新为实际连接的设备 Key
            else:
                ok, msg = connect_adb_device(ip, port)
                if not ok:
                    logger.warning(f'设备连接失败: {device_key}, {msg}')
                    return error_response(
                        message=msg or '设备未连接，请检查设备状态和网络',
                        error='Device not connected',
                        status_code=400
                    )
                 
        tr = MonkeyTestResult(ip, port)
        tr.package_name = data.get('package_name') or CONFIG.get('monkey', {}).get('default_package')
        tr.events_planned = _to_positive_int(data.get('events_count'), CONFIG.get('monkey', {}).get('default_events'))
        throttle = _to_positive_int(data.get('throttle'), CONFIG.get('monkey', {}).get('default_throttle'))
        raw_timeout = _to_positive_int(data.get('timeout'), CONFIG.get('monkey', {}).get('default_timeout'))
        timeout, timeout_adjusted, timeout_floor = _normalize_timeout(tr.events_planned, throttle, raw_timeout)
        tr.start_time = datetime.now()
        tr.status = MonkeyTestResult.STATUS_RUNNING
        
        # Create Platform Runtime
        try:
            runtime = get_platform_runtime_manager().create_runtime(
                name=f"Monkey Test: {device_key}",
                module="monkey",
                context={
                    'device_id': device_key,
                    'package_name': tr.package_name,
                    'events': tr.events_planned,
                    'throttle': throttle,
                    'timeout': timeout
                }
            )
            tr.platform_runtime_id = runtime.runtime_id
            get_platform_runtime_manager().update_status(runtime.runtime_id, PlatformRuntimeStatus.RUNNING)
        except Exception as e:
            logger.error(f"Failed to create platform runtime: {e}")

        with monkey_tests_lock:
            monkey_tests[device_key] = tr
            
        t = threading.Thread(target=run_monkey_test_background, args=(tr, tr.events_planned, throttle, timeout), daemon=True)
        with device_threads_lock:
            DEVICE_THREADS[device_key] = t
        t.start()
        
        logger.info(f'Monkey 测试启动: {device_key}, 包名: {tr.package_name}, 事件数: {tr.events_planned}')
        msg = f'Monkey 测试已启动，设备: {device_key}'
        if timeout_adjusted:
            msg += f'；超时已自动调整为 {timeout}s（原 {raw_timeout}s，建议下限 {timeout_floor}s）'
        return success_response(
            data={'test_info': tr.to_dict(), 'timeout_adjusted': timeout_adjusted, 'timeout_applied': timeout, 'timeout_floor': timeout_floor},
            message=msg
        )
    except Exception as e:
        logger.error(f'Monkey 测试启动失败: {e}', exc_info=True)
        return error_response(
            message='测试启动失败，请重试',
            error=str(e),
            status_code=500
        )

@monkey_bp.route('/api/stop', methods=['POST'])
def api_stop_monkey():
    try:
        data = request.get_json() or {}
        
        # 参数验证
        validation_error = validate_required(data, 'ip')
        if validation_error:
            return validation_error
        
        ip = data.get('ip')
        try:
            port = int(data.get('port') or CONFIG.get('adb', {}).get('default_port', 8787))
        except (ValueError, TypeError):
            return error_response(
                message='端口格式错误',
                error='invalid port format',
                status_code=400
            )
        
        device_key = f"{ip}:{port}"
        
        with monkey_tests_lock:
            tr = monkey_tests.get(device_key)
            if not tr:
                return error_response(
                    message='该设备没有运行中的测试',
                    error='No running test for device',
                    status_code=400
                )
            # Mark as manually stopped so the background thread knows
            tr.status = MonkeyTestResult.STATUS_STOPPED
            tr.status_reason = "用户手动停止 (User Stopped)"
            
            if tr and tr.monkey_process:
                tr.monkey_process.terminate()
                
        # Force kill via adb
        kill_monkey_process(device_key)
        
        logger.info(f'Monkey 测试停止: {device_key}')
        return success_response(message='测试已停止')
    except Exception as e:
        logger.error(f'Monkey 测试停止失败: {e}', exc_info=True)
        return error_response(
            message='停止测试失败，请重试',
            error=str(e),
            status_code=500
        )

@monkey_bp.route('/api/status', methods=['GET'])
def api_monkey_status():
    try:
        with monkey_tests_lock:
            tests = {k: v.to_dict() for k, v in monkey_tests.items()}
        
        # 查找当前正在运行的批次
        active_batch_id = None
        with batches_lock:
            # 按开始时间倒序，找到第一个 running 的批次
            sorted_batches = sorted(BATCHES.values(), key=lambda x: x.get('started_at', 0), reverse=True)
            for b in sorted_batches:
                if b.get('status') == 'running':
                    active_batch_id = b.get('batch_id')
                    break

        return success_response(data={
            'tests': tests,
            'active_batch_id': active_batch_id
        })
    except Exception as e:
        logger.error(f'获取 Monkey 状态失败: {e}', exc_info=True)
        return error_response(
            message='获取状态失败',
            error=str(e),
            status_code=500
        )

@monkey_bp.route('/api/reports', methods=['GET'])
def api_monkey_reports():
    try:
        # 尝试从磁盘加载历史报告
        load_reports_from_disk()
        
        with reports_lock:
            hist = list(REPORTS)
        return success_response(data={'reports': hist})
    except Exception as e:
        logger.error(f'获取 Monkey 报告失败: {e}', exc_info=True)
        return error_response(
            message='获取报告失败',
            error=str(e),
            status_code=500
        )

def _parse_timestamp(ts) -> float:
    """解析时间戳：支持 ISO 字符串或 epoch 秒数，返回 epoch 秒数，解析失败返回 0"""
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return float(ts) if ts > 0 else 0
    try:
        s = str(ts).strip()
        if not s:
            return 0
        # ISO 格式如 2024-01-15T10:30:00 或 2024-01-15 10:30:00
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(s[:26], fmt)
                return dt.timestamp()
            except ValueError:
                continue
        return 0
    except Exception:
        return 0


def load_reports_from_disk():
    """
    从 TROM 日志目录加载历史报告到内存
    """
    try:
        trom_dir = os.path.join(current_app.root_path, 'logs', 'trom_runs')
        if not os.path.exists(trom_dir):
            return

        with reports_lock:
            # 获取现有报告ID集合，避免重复加载
            existing_ids = {r.get('report_id') for r in REPORTS if r.get('report_id')}
            # also consider runtime_id for newer in-memory items
            existing_ids |= {r.get('runtime_id') for r in REPORTS if r.get('runtime_id')}
            
            # 遍历 TROM 文件
            for filename in os.listdir(trom_dir):
                if not filename.startswith('monkey_') or not filename.endswith('.json'):
                    continue
                    
                file_path = os.path.join(trom_dir, filename)
                try:
                    # 尝试读取文件
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        
                    runtime_id = data.get('runtime_id')
                    
                    # 如果已存在或不是 monkey 任务，跳过
                    if runtime_id in existing_ids or data.get('task_type') != 'monkey':
                        continue
                        
                    # 转换 TROM 数据为 MonkeyTestResult 格式
                    meta = data.get('meta', {})
                    context = data.get('context', {})
                    device = context.get('device', {})
                    app = context.get('app', {})
                    summary = data.get('summary') or {}
                    
                    # 尝试解析 summary.conclusion 中的 "Executed: x/y"
                    events_executed = 0
                    events_planned = 0
                    conclusion = summary.get('conclusion', '')
                    if conclusion:
                        m = re.search(r'Executed: (\d+)/(\d+)', conclusion)
                        if m:
                            events_executed = int(m.group(1))
                            events_planned = int(m.group(2))
                    
                    # 计算 duration：从 meta.created_at 和 meta.updated_at 解析时间戳
                    start_ts = _parse_timestamp(meta.get('created_at'))
                    end_ts = _parse_timestamp(meta.get('updated_at') or meta.get('created_at'))
                    duration_sec = max(0, end_ts - start_ts) if (start_ts and end_ts) else 0

                    # 构造报告对象
                    report = {
                        'report_id': runtime_id,
                        'runtime_id': runtime_id,
                        'device_id': device.get('device_id', 'unknown'),
                        'device_ip': device.get('device_id', 'unknown').split(':')[0] if ':' in device.get('device_id', '') else 'unknown',
                        'device_port': int(device.get('device_id', 'unknown').split(':')[1]) if ':' in device.get('device_id', '') else 0,
                        'status': 'SUCCESS' if data.get('status') == 'FINISHED' and summary.get('result') == 'pass' else 'FAILED',
                        'status_reason': conclusion,
                        'start_time': meta.get('created_at'),
                        'end_time': meta.get('updated_at') or meta.get('created_at'), # Fallback
                        'duration': duration_sec,
                        'events_planned': events_planned,
                        'events_executed': events_executed,
                        'coverage_percent': (events_executed / events_planned * 100) if events_planned > 0 else 0,
                        'crash_count': 0, # Need to parse event stream for exact count if needed
                        'anr_count': 0,
                        'package_name': app.get('package_name', 'unknown'),
                        'error_details': [],
                        'monkey_output': "Restored from TROM log",
                        'is_successful': summary.get('result') == 'pass',
                        'before_mem_mb': 0,
                        'after_mem_mb': 0,
                        'delta_mem_mb': 0,
                        'leak': False,
                        'video_fps': 0,
                        'mpp_active': 0,
                        'mpp_sessions': 0,
                        'cpu_app': 0
                    }
                    
                    REPORTS.append(report)
                    existing_ids.add(runtime_id)
                    
                except Exception as e:
                    logger.warning(f"Failed to load TROM report {filename}: {e}")
                    
    except Exception as e:
        logger.error(f"Error loading reports from disk: {e}")


@monkey_bp.route('/api/reports/<report_id>', methods=['GET'])
def api_monkey_report_detail(report_id):
    try:
        with reports_lock:
            report = next((r for r in REPORTS if r.get('report_id') == report_id), None)
        
        # 如果内存中未找到，尝试从磁盘加载
        if not report:
            load_reports_from_disk()
            with reports_lock:
                report = next((r for r in REPORTS if r.get('report_id') == report_id), None)
        
        if not report:
            return error_response(message='报告不存在', status_code=404)
            
        # 尝试从 TROM 文件加载详细性能数据
        performance_history = []
        trom_dir = os.path.join(current_app.root_path, 'logs', 'trom_runs')
        trom_file = os.path.join(trom_dir, f"{report_id}.json")
        # If report has runtime_id distinct from report_id, try it too (backward-compatible)
        if not os.path.exists(trom_file) and report and report.get("runtime_id"):
            trom_file = os.path.join(trom_dir, f"{report.get('runtime_id')}.json")
        
        if os.path.exists(trom_file):
            try:
                with open(trom_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    perf_stream = data.get('streams', {}).get('performance_stream', [])
                    # 转换格式为前端图表可用的数组
                    for item in perf_stream:
                        performance_history.append({
                            'ts': item.get('ts'),
                            'fps': item.get('fps', 0),
                            'cpu': item.get('cpu_percent', 0),
                            'mem': item.get('mem_pss_mb', 0)
                        })
            except Exception as e:
                logger.warning(f"Failed to load detailed performance data for {report_id}: {e}")
        
        return success_response(data={'report': report, 'performance_history': performance_history})
    except Exception as e:
        logger.error(f'获取报告详情失败: {e}', exc_info=True)
        return error_response(
            message='获取报告详情失败',
            error=str(e),
            status_code=500
        )


# ---------- 批次 Monkey：多轮循环 / 统一报告 / N 分钟采样 / 导出 PDF ----------
def _collect_batch_reports(batch_id):
    """从 REPORTS 中收集该批次下所有报告（含 batch_id 与 round_index）。"""
    with reports_lock:
        items = [r for r in REPORTS if r.get("batch_id") == batch_id]
    load_reports_from_disk()
    with reports_lock:
        items = [r for r in REPORTS if r.get("batch_id") == batch_id]
    return items


def _get_performance_history_for_report(report, current_app_root):
    """从 TROM 文件加载单条报告的 performance_stream，返回 [{ts, fps, cpu, mem}]。"""
    report_id = report.get("report_id")
    runtime_id = report.get("runtime_id") or report_id
    trom_dir = os.path.join(current_app_root, "logs", "trom_runs")
    for rid in (report_id, runtime_id):
        trom_file = os.path.join(trom_dir, f"{rid}.json")
        if os.path.exists(trom_file):
            try:
                with open(trom_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    perf_stream = data.get("streams", {}).get("performance_stream", [])
                    return [
                        {"ts": item.get("ts"), "fps": item.get("fps", 0), "cpu": item.get("cpu_percent", 0), "mem": item.get("mem_pss_mb", 0)}
                        for item in perf_stream
                    ]
            except Exception as e:
                logger.warning("Failed to load TROM %s: %s", rid, e)
    return []


def _parse_ts(ts):
    """将 ts 转为可比较的数值（秒时间戳）。"""
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0
    return 0


def _downsample_to_minutes(history, interval_minutes):
    """将 performance_history 按 interval_minutes 分钟聚合（取每段内最后一个点或均值）。"""
    if not history or interval_minutes <= 0:
        return history
    interval_sec = interval_minutes * 60
    out = []
    last_ts = None
    buf = []
    for p in history:
        ts = _parse_ts(p.get("ts"))
        if last_ts is None or (ts - last_ts) >= interval_sec:
            if buf:
                n = len(buf)
                out.append({
                    "ts": buf[-1].get("ts"),
                    "fps": sum(x.get("fps", 0) for x in buf) / n if n else 0,
                    "cpu": sum(x.get("cpu", 0) for x in buf) / n if n else 0,
                    "mem": sum(x.get("mem", 0) for x in buf) / n if n else 0,
                })
            buf = [p]
            last_ts = ts
        else:
            buf.append(p)
    if buf:
        n = len(buf)
        out.append({
            "ts": buf[-1].get("ts"),
            "fps": sum(x.get("fps", 0) for x in buf) / n if n else 0,
            "cpu": sum(x.get("cpu", 0) for x in buf) / n if n else 0,
            "mem": sum(x.get("mem", 0) for x in buf) / n if n else 0,
        })
    return out


def _batch_report_charts_base64(report_data):
    """为批次报告生成 matplotlib 曲线图，返回 base64 PNG 列表（供 PDF 嵌入）。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from io import BytesIO
        import base64
    except ImportError:
        return []
    out = []
    for r in report_data:
        hist = r.get("performance_history") or []
        if not hist:
            continue
        try:
            ts = [i for i in range(len(hist))]
            fps = [float(p.get("fps", 0)) for p in hist]
            cpu = [float(p.get("cpu", 0)) for p in hist]
            mem = [float(p.get("mem", 0)) for p in hist]
            fig, ax1 = plt.subplots(figsize=(8, 2.5))
            ax1.plot(ts, fps, "c-", label="FPS", linewidth=1)
            ax1.plot(ts, cpu, "m-", label="CPU%", linewidth=1)
            ax1.set_ylabel("FPS / CPU %")
            ax1.set_ylim(0, max(max(fps or [0]), max(cpu or [0])) * 1.1 or 1)
            ax2 = ax1.twinx()
            ax2.plot(ts, mem, "b-", label="Memory(MB)", linewidth=1)
            ax2.set_ylabel("Memory(MB)")
            ax2.set_ylim(0, max(mem or [0]) * 1.1 or 1)
            ax1.set_title(f"{r.get('device_id', '')} 第{r.get('round_index', 1)}轮")
            ax1.legend(loc="upper left", fontsize=8)
            ax2.legend(loc="upper right", fontsize=8)
            fig.tight_layout()
            buf = BytesIO()
            fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception as e:
            logger.warning("Batch chart for %s failed: %s", r.get("device_id"), e)
    return out


@monkey_bp.route("/api/batch/start", methods=["POST"])
def api_batch_start():
    """批量 Monkey：多台设备同时启动，支持多轮循环。"""
    try:
        data = request.get_json() or {}
        device_ids = data.get("device_ids") or []
        if isinstance(device_ids, str):
            device_ids = [s.strip() for s in device_ids.split(",") if s.strip()]
        if not device_ids or len(device_ids) > 20:
            return error_response(message="device_ids 需为 1~20 个设备", status_code=400)
        device_ids = [d.strip() for d in device_ids if d.strip()]

        # 连接+启动一条龙：只填 IP 时自动试 5555/8787，填 IP:端口 则试该端口；归一化 device_ids
        normalized = []
        failed = []
        for device_key in device_ids:
            parts = device_key.split(":", 1)
            ip = (parts[0] or "").strip()
            if not ip:
                continue
            if len(parts) > 1 and parts[1].strip().isdigit():
                port = int(parts[1].strip(), 10)
                if check_device_connection(ip, port):
                    devices = get_adb_devices()
                    dev_id = next((d for d in devices if d == f"{ip}:{port}" or d.startswith(ip + ":")), f"{ip}:{port}")
                    normalized.append(dev_id)
                else:
                    ok, msg = connect_adb_device(ip, port)
                    if ok:
                        normalized.append(msg)
                    else:
                        failed.append(f"{device_key}（{msg}）")
            else:
                dev_id, err = try_connect_ports(ip)
                if dev_id:
                    normalized.append(dev_id)
                else:
                    failed.append(f"{ip}（{err}）")
        if failed:
            return error_response(
                message="以下设备连接失败：" + "；".join(failed[:5]) + ("..." if len(failed) > 5 else ""),
                status_code=400
            )
        if not normalized:
            return error_response(message="没有可用的设备", status_code=400)
        device_ids = normalized

        package_name = data.get("package_name") or CONFIG.get("monkey", {}).get("default_package")
        events_count = _to_positive_int(data.get("events_count"), CONFIG.get("monkey", {}).get("default_events"))
        throttle = _to_positive_int(data.get("throttle"), CONFIG.get("monkey", {}).get("default_throttle"))
        raw_timeout = _to_positive_int(data.get("timeout"), CONFIG.get("monkey", {}).get("default_timeout"))
        timeout, timeout_adjusted, timeout_floor = _normalize_timeout(events_count, throttle, raw_timeout)
        sample_interval_minutes = max(1, int(data.get("sample_interval_minutes") or 2))
        rounds = max(1, int(data.get("rounds") or 1))
        batch_id = f"batch_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        with batches_lock:
            BATCHES[batch_id] = {
                "batch_id": batch_id,
                "device_ids": device_ids,
                "rounds": rounds,
                "current_round": 0,
                "status": "running",
                "results": {},
                "start_errors": {},
                "sample_interval_minutes": sample_interval_minutes,
                "created_at": time.time(),
                "started_at": time.time(),
                "package_name": package_name,
                "events_count": events_count,
                "throttle": throttle,
                "timeout": timeout,
            }
        t = threading.Thread(
            target=_run_batch_coordinator,
            args=(batch_id, device_ids, package_name, events_count, throttle, timeout, rounds, sample_interval_minutes),
            daemon=True,
        )
        t.start()
        msg = "批次已启动"
        if timeout_adjusted:
            msg += f"；超时已自动调整为 {timeout}s（原 {raw_timeout}s，建议下限 {timeout_floor}s）"
        return success_response(data={"batch_id": batch_id, "timeout_adjusted": timeout_adjusted, "timeout_applied": timeout, "timeout_floor": timeout_floor}, message=msg)
    except Exception as e:
        logger.exception("Batch start error: %s", e)
        return error_response(message=str(e), status_code=500)


@monkey_bp.route("/api/batch/<batch_id>/status", methods=["GET"])
def api_batch_status(batch_id):
    """查询批次状态。"""
    with batches_lock:
        b = BATCHES.get(batch_id)
    if not b:
        return error_response(message="批次不存在", status_code=404)
    return success_response(data=dict(b))


@monkey_bp.route("/api/batch/<batch_id>/stop", methods=["POST"])
def api_batch_stop(batch_id):
    """停止批次：标记为 stopped，并停止该批次下所有正在运行的 Monkey。"""
    with batches_lock:
        meta = BATCHES.get(batch_id)
    if not meta:
        return error_response(message="批次不存在", status_code=404)
    if meta.get("status") not in ("running", None):
        return success_response(data={"batch_id": batch_id}, message="批次已结束，无需停止")
    with batches_lock:
        BATCHES[batch_id]["status"] = "stopped"
        BATCHES[batch_id]["stopped_at"] = time.time()
    device_ids = meta.get("device_ids") or []
    for device_key in device_ids:
        device_key = device_key.strip()
        with monkey_tests_lock:
            tr = monkey_tests.get(device_key)
        if tr and getattr(tr, "batch_id", None) == batch_id:
            tr.status = MonkeyTestResult.STATUS_STOPPED
            tr.status_reason = "用户停止批次 (Batch stopped)"
            try:
                kill_monkey_process(device_key)
            except Exception as e:
                logger.warning("Kill monkey on %s for batch stop: %s", device_key, e)
    return success_response(data={"batch_id": batch_id}, message="已发送停止请求")

# ================= Scheduler Routes =================
@monkey_bp.route("/api/scheduler/list", methods=["GET"])
def api_scheduler_list():
    return success_response(data=scheduler_instance.get_tasks())

@monkey_bp.route("/api/scheduler/add", methods=["POST"])
def api_scheduler_add():
    data = request.get_json() or {}
    if not data.get("name") or not data.get("schedule_time"):
        return error_response(message="名称和执行时间不能为空", status_code=400)
    task = scheduler_instance.add_task(data)
    return success_response(data=task, message="添加成功")

@monkey_bp.route("/api/scheduler/update", methods=["POST"])
def api_scheduler_update():
    data = request.get_json() or {}
    task_id = data.get("id")
    if not task_id:
        return error_response(message="缺少任务 ID", status_code=400)
    task = scheduler_instance.update_task(task_id, data)
    if not task:
        return error_response(message="任务不存在", status_code=404)
    return success_response(data=task, message="更新成功")

@monkey_bp.route("/api/scheduler/delete", methods=["POST"])
def api_scheduler_delete():
    data = request.get_json() or {}
    task_id = data.get("id")
    if not task_id:
        return error_response(message="缺少任务 ID", status_code=400)
    scheduler_instance.delete_task(task_id)
    return success_response(message="删除成功")

@monkey_bp.route("/api/scheduler/toggle", methods=["POST"])
def api_scheduler_toggle():
    data = request.get_json() or {}
    task_id = data.get("id")
    enabled = data.get("enabled", True)
    if not task_id:
        return error_response(message="缺少任务 ID", status_code=400)
    task = scheduler_instance.update_task(task_id, {"enabled": enabled})
    if not task:
        return error_response(message="任务不存在", status_code=404)
    return success_response(data=task, message=f"已{'启用' if enabled else '停用'}")


@monkey_bp.route("/api/batch/<batch_id>/report", methods=["GET"])
def api_batch_report(batch_id):
    """批次报告：按 N 分钟采样，多设备曲线 + 汇总表，返回 HTML。"""
    with batches_lock:
        meta = BATCHES.get(batch_id)
    if not meta:
        return error_response(message="批次不存在", status_code=404)
    sample_interval_minutes = max(1, int(request.args.get("sample_interval_minutes") or meta.get("sample_interval_minutes") or 2))
    reports = _collect_batch_reports(batch_id)
    root = current_app.root_path
    by_device = {}
    for r in reports:
        dev = r.get("device_id") or r.get("device_ip", "") + ":" + str(r.get("device_port", ""))
        if dev not in by_device:
            by_device[dev] = []
        by_device[dev].append(r)
    devices_order = meta.get("device_ids") or list(by_device.keys())
    report_data = []
    for device_id in devices_order:
        device_reports = by_device.get(device_id, [])
        for r in sorted(device_reports, key=lambda x: (x.get("round_index") or 0, x.get("report_id") or "")):
            hist = _get_performance_history_for_report(r, root)
            hist = _downsample_to_minutes(hist, sample_interval_minutes)
            report_data.append({
                "device_id": device_id,
                "round_index": r.get("round_index"),
                "report_id": r.get("report_id"),
                "before_mem_mb": r.get("before_mem_mb"),
                "after_mem_mb": r.get("after_mem_mb"),
                "delta_mem_mb": r.get("delta_mem_mb"),
                "video_fps": r.get("video_fps"),
                "cpu_app": r.get("cpu_app"),
                "events_executed": r.get("events_executed"),
                "events_planned": r.get("events_planned"),
                "status": r.get("status"),
                "status_reason": r.get("status_reason"),
                "crash_count": r.get("crash_count", 0),
                "anr_count": r.get("anr_count", 0),
                "error_details": r.get("error_details") or [],
                "performance_history": hist,
            })
    html = render_template(
        "monkey_batch_report.html",
        batch_id=batch_id,
        meta=meta,
        report_data=report_data,
        report_data_json=json.dumps(report_data, ensure_ascii=False),
        sample_interval_minutes=sample_interval_minutes,
        chart_base64_list=None,
    )
    return make_response(html, 200, [("Content-Type", "text/html; charset=utf-8")])


@monkey_bp.route("/api/batch/<batch_id>/report.pdf", methods=["GET"])
def api_batch_report_pdf(batch_id):
    """批次报告导出 PDF。"""
    with batches_lock:
        meta = BATCHES.get(batch_id)
    if not meta:
        return error_response(message="批次不存在", status_code=404)
    sample_interval_minutes = max(1, int(request.args.get("sample_interval_minutes") or meta.get("sample_interval_minutes") or 2))
    reports = _collect_batch_reports(batch_id)
    root = current_app.root_path
    by_device = {}
    for r in reports:
        dev = r.get("device_id") or (str(r.get("device_ip", "")) + ":" + str(r.get("device_port", "")))
        if dev not in by_device:
            by_device[dev] = []
        by_device[dev].append(r)
    devices_order = meta.get("device_ids") or list(by_device.keys())
    report_data = []
    for device_id in devices_order:
        device_reports = by_device.get(device_id, [])
        for r in sorted(device_reports, key=lambda x: (x.get("round_index") or 0, x.get("report_id") or "")):
            hist = _get_performance_history_for_report(r, root)
            hist = _downsample_to_minutes(hist, sample_interval_minutes)
            report_data.append({
                "device_id": device_id,
                "round_index": r.get("round_index"),
                "report_id": r.get("report_id"),
                "before_mem_mb": r.get("before_mem_mb"),
                "after_mem_mb": r.get("after_mem_mb"),
                "delta_mem_mb": r.get("delta_mem_mb"),
                "video_fps": r.get("video_fps"),
                "cpu_app": r.get("cpu_app"),
                "events_executed": r.get("events_executed"),
                "events_planned": r.get("events_planned"),
                "status": r.get("status"),
                "status_reason": r.get("status_reason"),
                "crash_count": r.get("crash_count", 0),
                "anr_count": r.get("anr_count", 0),
                "error_details": r.get("error_details") or [],
                "performance_history": hist,
            })
    try:
        from weasyprint import HTML
        from io import BytesIO
        import base64
        chart_base64_list = _batch_report_charts_base64(report_data)
        html_content = render_template(
            "monkey_batch_report.html",
            batch_id=batch_id,
            meta=meta,
            report_data=report_data,
            report_data_json=json.dumps(report_data, ensure_ascii=False),
            sample_interval_minutes=sample_interval_minutes,
            chart_base64_list=chart_base64_list,
        )
        pdf_io = BytesIO()
        HTML(string=html_content, base_url=request.url_root).write_pdf(pdf_io)
        pdf_io.seek(0)
        return make_response(
            pdf_io.getvalue(),
            200,
            [("Content-Type", "application/pdf"), ("Content-Disposition", f"attachment; filename=monkey_batch_{batch_id}.pdf")],
        )
    except ImportError:
        return error_response(message="导出 PDF 需要安装 weasyprint: pip install weasyprint", status_code=501)
    except Exception as e:
        logger.exception("Batch report PDF error: %s", e)
        return error_response(message=str(e), status_code=500)


@monkey_bp.route('/api/devices', methods=['GET'])
def api_get_devices_list():
    try:
        devices = get_adb_devices()
        return success_response(data={'devices': devices})
    except Exception as e:
        logger.error(f'获取设备列表失败: {e}', exc_info=True)
        return error_response(
            message='获取设备列表失败，请检查 ADB 连接',
            error=str(e),
            status_code=500
        )

@monkey_bp.route('/api/connect', methods=['POST'])
def api_connect_device():
    """连接 ADB 设备"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'ip')
        if validation_error:
            return validation_error
        
        ip = data.get('ip')
        try:
            port = int(data.get('port') or CONFIG.get('adb', {}).get('default_port', 8787))
            if port <= 0 or port > 65535:
                return error_response(
                    message='端口号必须在 1-65535 之间',
                    error='invalid port',
                    status_code=400
                )
        except (ValueError, TypeError):
            return error_response(
                message='端口格式错误',
                error='invalid port format',
                status_code=400
            )
        
        device_id = f"{ip}:{port}"
        
        # 检查设备是否已连接
        if check_device_connection(ip, port):
            return success_response(
                data={'device_id': device_id},
                message=f'设备已连接: {device_id}'
            )
        
        # 尝试连接设备
        ok, msg = connect_adb_device(ip, port)
        if ok:
            actual_id = msg if ":" in str(msg) else device_id
            logger.info(f'设备连接成功: {actual_id}')
            return success_response(
                data={'device_id': actual_id},
                message=f'设备连接成功: {actual_id}'
            )
        return error_response(
            message=msg or '设备连接失败，请检查设备状态和网络',
            error='connection failed',
            status_code=400
        )
    except Exception as e:
        logger.error(f'连接设备失败: {e}', exc_info=True)
        return error_response(
            message='连接设备失败',
            error=str(e),
            status_code=500
        )

@monkey_bp.route('/api/disconnect', methods=['POST'])
def api_disconnect_device():
    """断开 ADB 设备连接"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'ip')
        if validation_error:
            return validation_error
        
        ip = data.get('ip')
        try:
            port = int(data.get('port') or CONFIG.get('adb', {}).get('default_port', 8787))
        except (ValueError, TypeError):
            return error_response(
                message='端口格式错误',
                error='invalid port format',
                status_code=400
            )
        
        device_id = f"{ip}:{port}"
        
        # 断开连接
        out, rc, err = run_adb_command(["adb", "disconnect", device_id])
        
        if rc == 0:
            logger.info(f'设备断开成功: {device_id}')
            return success_response(message=f'设备已断开: {device_id}')
        else:
            return error_response(
                message=f'断开设备失败: {err or out}',
                error='disconnect failed',
                status_code=400
            )
    except Exception as e:
        logger.error(f'断开设备失败: {e}', exc_info=True)
        return error_response(
            message='断开设备失败',
            error=str(e),
            status_code=500
        )
