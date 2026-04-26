import os
import time
import subprocess
import requests
import threading
from .config import CONFIG, append_log, SCREENSHOTS_DIR
from core.device.manager import get_device_manager

# 全局锁：防止多个设备同时重启 ADB 服务
ADB_RESTART_LOCK = threading.Lock()
# 记录上次重启时间，防止过于频繁重启
LAST_ADB_RESTART_TIME = 0.0


def _safe_device_target(s):
    """设备标识（如 IP:port 或 serial），仅允许安全字符，防止命令注入。"""
    if not s or not isinstance(s, str) or len(s) > 64:
        return False
    return all(c.isalnum() or c in ".:-_" for c in s)


def _safe_package(s):
    """包名仅允许字母数字与点、下划线，防止命令注入。"""
    if not s or not isinstance(s, str) or len(s) > 256:
        return False
    return all(c.isalnum() or c in "._" for c in s)


def run_cmd(cmd, timeout=10):
    """执行命令。cmd 为列表时使用 shell=False，避免命令注入。"""
    try:
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        if isinstance(cmd, (list, tuple)):
            p = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                shell=False,
                startupinfo=startupinfo,
            )
        else:
            p = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                shell=True,
                startupinfo=startupinfo,
            )
        raw_out = p.stdout or b''
        try:
            out = raw_out.decode('utf-8')
        except UnicodeDecodeError:
            try:
                import locale
                sys_enc = locale.getpreferredencoding() or 'mbcs'
                out = raw_out.decode(sys_enc)
            except Exception:
                out = raw_out.decode('utf-8', errors='ignore')
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, 'timeout'
    except Exception as e:
        return 1, f'error: {e}'


def probe_tcp(ip, port, timeout_ms=1500):
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(max(0.1, float(timeout_ms)/1000.0))
        start = time.time()
        result = sock.connect_ex((ip, int(port)))
        sock.close()
        ok = (result == 0)
        append_log('TCP端口探测', {'target': f"{ip}:{port}", 'ok': ok, 'cost_ms': int((time.time()-start)*1000)})
        return ok
    except Exception as e:
        append_log('TCP端口探测异常', {'target': f"{ip}:{port}", 'err': str(e)})
        return False


def take_screenshot(ip, adb_port, save_path=None, timeout=15, stage="before_off"):
    """
    在机顶盒端截屏，保存为 PNG。
    策略：优先尝试 exec-out (高效)，失败则回退到 shell+pull (兼容)。
    """
    try:
        import os
        device_id = f"{ip}:{int(adb_port)}"
        if not save_path:
            ts_str = time.strftime('%Y%m%d_%H%M%S')
            safe_dev = device_id.replace(':', '_')
            safe_stage = (stage or "screenshot").replace(" ", "_")
            fname = f"reboot_{safe_stage}_{safe_dev}_{ts_str}.png"
            save_path = os.path.join(SCREENSHOTS_DIR, fname)
        dir_path = os.path.dirname(save_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        _startup = None
        if os.name == 'nt':
            _startup = subprocess.STARTUPINFO()
            _startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        def _validate_png(path):
            if not os.path.exists(path) or os.path.getsize(path) < 12:
                return False, "size_too_small"
            try:
                with open(path, 'rb') as f:
                    head = f.read(8)
                    if head != b"\x89PNG\r\n\x1a\n":
                        return False, f"bad_header_{head.hex()}"
                return True, "ok"
            except Exception as e:
                return False, str(e)

        # 方案1: exec-out (直接流式传输)
        # 优点：速度快，无需SD卡读写权限，无临时文件
        # 缺点：部分旧设备(Android < 5.0)可能不支持
        try:
            with open(save_path, 'wb') as f:
                subprocess.run(
                    ['adb', '-s', device_id, 'exec-out', 'screencap', '-p'],
                    stdout=f,
                    timeout=timeout,
                    startupinfo=_startup
                )
            ok, reason = _validate_png(save_path)
            if ok:
                append_log('截屏成功(exec-out)', {'target': device_id, 'path': save_path})
                return True
            else:
                # 即使失败也记录一下，可能设备不支持 exec-out
                append_log('exec-out截屏无效', {'target': device_id, 'reason': reason})
        except Exception as e:
            append_log('exec-out尝试失败', {'target': device_id, 'err': str(e)})

        # 方案2: shell + pull (回退方案)
        remote_name = f"cap_{int(time.time())}_{os.urandom(2).hex()}.png"
        remote_path = f"/sdcard/{remote_name}"
        
        # 2.1) 截图到设备
        _p1 = subprocess.run(
            ['adb', '-s', device_id, 'shell', 'screencap', '-p', '-d', '0', remote_path],
            capture_output=True,
            timeout=timeout,
            startupinfo=_startup,
        )
        if _p1.returncode != 0:
            append_log('shell截屏失败', {'target': device_id, 'rc': _p1.returncode, 'out': (_p1.stdout or _p1.stderr or b'').decode('utf-8', errors='ignore')[:100]})
            return False

        # 2.2) Pull 到本地
        _p2 = subprocess.run(
            ['adb', '-s', device_id, 'pull', remote_path, save_path],
            capture_output=True,
            timeout=timeout,
            startupinfo=_startup,
        )
        
        # 2.3) 清理设备文件
        try:
            subprocess.run(['adb', '-s', device_id, 'shell', 'rm', remote_path], capture_output=True, timeout=5, startupinfo=_startup)
        except:
            pass

        if _p2.returncode != 0:
            append_log('pull截屏失败', {'target': device_id, 'rc': _p2.returncode, 'out': (_p2.stdout or _p2.stderr or b'').decode('utf-8', errors='ignore')[:100]})
            return False

        # 2.4) 校验
        ok, reason = _validate_png(save_path)
        if not ok:
            append_log('pull截屏校验失败', {'target': device_id, 'reason': reason})
            try:
                os.remove(save_path)
            except:
                pass
            return False

        append_log('截屏成功(pull)', {'target': device_id, 'path': save_path})
        return True

    except Exception as e:
        append_log('截屏全流程异常', {'target': f"{ip}:{adb_port}", 'err': str(e)})
        return False


def adb_enable(ip, enable_port=None):
    # 允许配置设备侧HTTP端口；若未指定，优先defaults.adb_enable_port，其次设备的adb_port，最后2007
    default_enable_port = int(CONFIG.get('defaults', {}).get('adb_enable_port', 2007))
    port = int(enable_port or default_enable_port)
    url = f"http://{ip}:{port}/debug/adb?enable=1"
    retries = int(CONFIG['defaults']['retries']['adb_enable'])
    timeout_sec = float(CONFIG.get('defaults', {}).get('adb_enable_timeout_sec', 5))
    for i in range(retries):
        try:
            r = requests.get(url, timeout=timeout_sec)
            append_log('ADB启用请求', {'url': url, 'status': r.status_code})
            if r.status_code == 200:
                return True
        except Exception as e:
            append_log('ADB启用失败', {'err': str(e), 'attempt': i+1, 'url': url})
        time.sleep(1)
    return False


def _do_connect_and_verify(target, ip, port, use_pool=True):
    """执行 adb connect 并校验 get-state，优先使用 DeviceManager 统一连接管理"""
    if not _safe_device_target(target):
        append_log('ADB连接', {'target': target, 'error': 'invalid target'})
        return False
    dm = get_device_manager()
    if use_pool and dm:
        ok = dm.connect(ip, int(port))
        append_log('ADB连接', {'target': target, 'ok': ok})
        if ok:
            rc, out, _ = dm.run_adb_command(target, ['get-state'], 5)
            append_log('ADB设备状态', {'target': target, 'rc': rc, 'state': out.strip()})
            return rc == 0 and out.strip() == 'device'
        return False
    rc, out = run_cmd(['adb', 'connect', target], timeout=10)
    append_log('ADB连接', {'target': target, 'rc': rc, 'out': out.strip()[:160]})
    if rc == 0:
        rc2, state = run_cmd(['adb', '-s', target, 'get-state'], timeout=5)
        append_log('ADB设备状态', {'target': target, 'rc': rc2, 'state': state.strip()})
        return rc2 == 0 and state.strip() == 'device'
    return False


def adb_connect(ip, port):
    retries = int(CONFIG['defaults']['retries']['adb_connect'])
    probe_timeout_ms = int(CONFIG.get('defaults', {}).get('adb_port_probe_timeout_ms', 1500))
    target = f"{ip}:{int(port)}"
    dm = get_device_manager()
    use_pool = bool(dm)

    # 先探测端口连通性，减少无效尝试
    if not probe_tcp(ip, port, timeout_ms=probe_timeout_ms):
        time.sleep(1)
        if not probe_tcp(ip, port, timeout_ms=probe_timeout_ms):
            append_log('ADB端口不可达，跳过连接', {'target': target})
            return False

    # 尝试连接并校验设备状态
    for i in range(retries):
        if _do_connect_and_verify(target, ip, port, use_pool):
            return True
        time.sleep(2)

    # 连接失败，尝试安全重启 ADB 服务
    global LAST_ADB_RESTART_TIME
    append_log('ADB连接失败，准备尝试重启服务', {'target': target})

    try:
        with ADB_RESTART_LOCK:
            now = time.time()
            if now - LAST_ADB_RESTART_TIME > 15:
                append_log('执行ADB服务重启', {'target': target})
                if use_pool and dm:
                    dm.run_adb_command(None, ['kill-server'], 10)
                    dm.run_adb_command(None, ['start-server'], 10)
                else:
                    run_cmd(['adb', 'kill-server'], timeout=10)
                    run_cmd(['adb', 'start-server'], timeout=10)
                LAST_ADB_RESTART_TIME = time.time()
                time.sleep(3)
            else:
                append_log('ADB服务近期已重启，跳过重复重启', {'target': target})
                time.sleep(2)
    except Exception as e:
        append_log('ADB服务重启异常', {'err': str(e)})

    # 重启后做最后一次连接尝试
    if _do_connect_and_verify(target, ip, port, use_pool):
        append_log('ADB服务重启后连接成功', {'target': target})
        return True
    return False


def check_process(ip, port, package):
    if not _safe_package(package):
        append_log('进程检测', {'pkg': package, 'error': 'invalid package'})
        return False
    retries = int(CONFIG['defaults']['retries']['process_check'])
    backoff_ms = int(CONFIG.get('defaults', {}).get('process_backoff_ms', 500))
    target = f"{ip}:{int(port)}"
    if not _safe_device_target(target):
        append_log('进程检测', {'target': target, 'error': 'invalid target'})
        return False
    dm = get_device_manager()
    use_pool = bool(dm)
    for i in range(retries):
        if use_pool:
            rc, out, _ = dm.run_adb_command(target, ['shell', 'pidof', package], 10)
        else:
            rc, out = run_cmd(['adb', '-s', target, 'shell', 'pidof', package], timeout=10)
        append_log('进程检测', {'target': target, 'pkg': package, 'rc': rc, 'out': (out or '').strip()[:160]})
        ok = (rc == 0 and bool((out or '').strip()))
        if not ok:
            # 备用：设备上执行 ps -A | grep package（package 已校验，仅含安全字符）
            shell_cmd = f"ps -A | grep {package}"
            if use_pool:
                rc2, out2, _ = dm.run_adb_command(target, ['shell', shell_cmd], 10)
            else:
                rc2, out2 = run_cmd(['adb', '-s', target, 'shell', shell_cmd], timeout=10)
            append_log('进程检测备用', {'target': target, 'pkg': package, 'rc': rc2, 'out': (out2 or '').strip()[:160]})
            ok = (rc2 == 0 and bool((out2 or '').strip()))
        if ok:
            return True
        wait_ms = backoff_ms * (2 ** i)
        time.sleep(max(0.1, float(wait_ms)/1000.0))
    return False

def check_process_with_cold_window(ip, port, package, window_sec):
    start = time.time()
    backoff_ms = int(CONFIG.get('defaults', {}).get('process_backoff_ms', 500))
    tries = 0
    while (time.time() - start) < max(0, int(window_sec)):
        if check_process(ip, port, package):
            return True
        wait_ms = backoff_ms * (2 ** tries)
        tries += 1
        time.sleep(max(0.1, float(wait_ms)/1000.0))
    # 窗口结束后再做一次最终检查
    return check_process(ip, port, package)
