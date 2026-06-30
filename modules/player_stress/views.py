
import os
import sys
import json
import re
import logging
import threading
import time
from datetime import datetime
from collections import deque
from flask import Blueprint, render_template, request, jsonify, current_app, send_from_directory
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from utils.report_paths import get_module_report_dir
from utils.config_loader import get_config_section

# Import core logic from the copied module
# Note: Since this file will be in modules/player_stress/views.py, 
# and core is in modules/player_stress/core, we can use relative import.
from .core.runner import TestRunner
from .core.adb_manager import AdbManager
from .core.log_monitor import LogMonitor

stress_bp = Blueprint('player_stress', __name__, template_folder='templates', static_folder='static', url_prefix='/player_stress')
logger = setup_logger('player_stress_module')

# Globals
TEST_INSTANCE = None
TEST_THREAD = None
TEST_LOCK = threading.Lock()
LOG_BUFFER = deque(maxlen=2000) # Keep last 2000 lines
LOG_LOCK = threading.Lock()
TEST_STATE_LOCK = threading.Lock()
TEST_RUN_STATE = {
    "status": "idle",
    "started_at": None,
    "planned_end_at": None,
    "finished_at": None,
    "duration_seconds": 0,
    "completion_reason": "",
    "report_file": "",
    "summary_file": "",
    "report_csv_file": "",
    "summary_txt_file": "",
    "summary_json_file": "",
    "report_meta": {},
}

REPORT_META_CACHE = {}


def _existing_file_or_empty(path: str) -> str:
    try:
        return path if path and os.path.isfile(path) else ""
    except Exception:
        return ""


def _safe_file_token(value: str) -> str:
    token = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '').strip())
    return token.strip('._') or 'device'


def _safe_event_token(value: str) -> str:
    token = re.sub(r'[^A-Za-z0-9_-]+', '', str(value or '').strip())
    return token or ''


def _current_evidence_root() -> str:
    try:
        watcher = getattr(TEST_INSTANCE, 'tv_playback_watcher', None)
        root = getattr(watcher, 'evidence_root', '') if watcher else ''
        if root and os.path.isdir(root):
            return root
    except Exception:
        pass
    fallback = os.path.join(get_module_report_dir('player_stress'), 'tv_stall_events')
    return fallback


def _resolve_tv_event_evidence_dir(event_token: str) -> str:
    event_token = _safe_event_token(event_token)
    if not event_token:
        return ''
    roots = []
    current_root = _current_evidence_root()
    if current_root:
        roots.append(current_root)
    fallback = os.path.join(get_module_report_dir('player_stress'), 'tv_stall_events')
    if fallback not in roots:
        roots.append(fallback)
    for root in roots:
        candidate = os.path.join(root, event_token)
        try:
            if os.path.isdir(candidate):
                return candidate
        except Exception:
            continue
    return ''


def _build_tv_event_evidence_manifest(event_dir: str, event_token: str) -> dict:
    if not event_dir or not os.path.isdir(event_dir):
        return {}
    event_json_data = {}
    files = []
    for name in sorted(os.listdir(event_dir)):
        file_path = os.path.join(event_dir, name)
        if not os.path.isfile(file_path):
            continue
        ext = os.path.splitext(name)[1].lower()
        is_image = ext in {'.png', '.jpg', '.jpeg', '.webp'}
        is_text = ext in {'.txt', '.json', '.jsonl', '.log'}
        preview_text = ''
        if is_text:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    preview_text = f.read(1200)
            except Exception:
                preview_text = ''
        if name == 'event.json':
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    event_json_data = json.load(f)
            except Exception:
                event_json_data = {}
        files.append({
            'name': name,
            'size': os.path.getsize(file_path),
            'is_image': is_image,
            'is_text': is_text,
            'preview_text': preview_text,
            'url': f'/player_stress/evidence/{event_token}/{name}',
        })
    key_files = {item['name']: item['url'] for item in files}
    diagnosis = _build_tv_event_evidence_diagnosis(event_json_data)
    return {
        'event_token': event_token,
        'event_dir': event_dir,
        'files': files,
        'screenshots': [item for item in files if item['is_image']],
        'downloads': files,
        'diagnosis': diagnosis,
        'event_json_url': key_files.get('event.json', ''),
        'event_summary_url': key_files.get('event_summary.txt', ''),
        'time_window_logcat_url': key_files.get('time_window_logcat.txt', ''),
        'time_window_summary_url': key_files.get('time_window_summary.json', ''),
        'decoder_window_url': key_files.get('decoder_window.txt', ''),
        'top_before_url': key_files.get('top_before.txt', ''),
        'top_after_url': key_files.get('top_after.txt', ''),
        'cpu_during_url': key_files.get('cpu_during.jsonl', ''),
    }


def _build_tv_event_evidence_diagnosis(event_data: dict) -> dict:
    event_data = event_data if isinstance(event_data, dict) else {}
    reason = str(event_data.get('reason', '') or '')
    confirmed = bool(event_data.get('confirmed', False))
    confidence = str(event_data.get('confidence_level', 'risk') or 'risk')
    assessment_reason = str(event_data.get('assessment_reason', '') or '')
    signals = list(event_data.get('corroboration_signals', []) or [])
    cpu_contention = event_data.get('cpu_contention') or {}
    cpu_candidate = cpu_contention.get('top_candidate') or {}
    cpu_detected = bool(cpu_contention.get('detected'))
    max_gap_ms = float(event_data.get('max_frame_gap_ms', 0) or 0.0)
    min_fps = float(event_data.get('min_fps', 0) or 0.0)

    if cpu_detected:
        process_name = str(cpu_candidate.get('process', '') or '高负载进程')
        statement = f"这条事件更像系统/固件侧 CPU 资源竞争，优先排查 {process_name}。"
        basis = (
            f"卡顿期间命中 CPU 争抢信号，嫌疑进程 {process_name}，"
            f"最大帧间隔 {max_gap_ms:.0f} ms。"
        )
        next_action = f"先看 {process_name} 的拉起/保活逻辑，再结合 top_before/top_after.txt 确认是否重复抢占 CPU。"
        owner = "系统/固件侧"
    elif (
        'decoder_confirmed' in signals
        or 'decode_drop' in signals
        or 'decoder' in reason.lower()
    ):
        statement = "这条事件更像播放器/解码链路停顿，优先排查硬件解码器、码流和驱动。"
        basis = (
            f"事件带有解码侧互证信号（{', '.join(signals) if signals else 'decoder'}），"
            f"最低 FPS {min_fps:.1f}。"
        )
        next_action = "先看 event.json 与解码器相关日志，再核对码率、分辨率和芯片解码能力上限。"
        owner = "播放器/解码侧"
    elif confirmed:
        statement = "这条事件已确认是电视端卡顿，但当前还不能单独锁定到系统侧或播放器侧。"
        basis = f"事件已达确认级，最大帧间隔 {max_gap_ms:.0f} ms，最低 FPS {min_fps:.1f}。"
        next_action = "先看 event.json、截图和 cpu_during.jsonl，把时间线与播放器异常、系统负载一起交叉确认。"
        owner = "待继续定责"
    else:
        statement = "这条事件目前更像风险提示，还不是一锤定音的问题结论。"
        basis = assessment_reason or "当前仅有局部异常信号，互证还不够完整。"
        next_action = "继续复测并补齐 Surface、FPS、日志和 CPU 证据后再定责。"
        owner = "待补证据"

    return {
        'statement': statement,
        'basis': basis,
        'next_action': next_action,
        'owner': owner,
        'confidence': confidence,
        'confirmed': confirmed,
    }


def _pick_existing_report_files(runner) -> dict:
    html_file = _existing_file_or_empty(getattr(runner, "last_html_file", "") or "")
    csv_file = _existing_file_or_empty(getattr(runner, "last_csv_file", "") or "")
    summary_json_file = _existing_file_or_empty(getattr(runner, "last_summary_json_file", "") or "")
    summary_txt_file = _existing_file_or_empty(getattr(runner, "last_summary_file", "") or "")
    return {
        "report_file": html_file or csv_file,
        "summary_file": summary_json_file or summary_txt_file,
        "report_csv_file": csv_file,
        "summary_txt_file": summary_txt_file,
        "summary_json_file": summary_json_file,
    }

def logger_callback(msg):
    """线程安全的日志回调函数"""
    try:
        msg_str = str(msg)
        with LOG_LOCK:
            LOG_BUFFER.append(msg_str)
            logger.info("[LOG] %s", msg_str)
    except Exception as e:
        logger.exception("logger_callback 失败: %s", e)


def _load_report_meta_from_summary_json(summary_json_path: str) -> dict:
    if not summary_json_path:
        return {}
    try:
        with open(summary_json_path, 'r', encoding='utf-8', errors='replace') as f:
            summary_json = json.load(f)
        if not isinstance(summary_json, dict):
            return {}
        meta = summary_json.get('meta') if isinstance(summary_json.get('meta'), dict) else {}
        decision = summary_json.get('decision') if isinstance(summary_json.get('decision'), dict) else {}
        stats = summary_json.get('stats') if isinstance(summary_json.get('stats'), dict) else {}
        metrics = decision.get('metrics') if isinstance(decision.get('metrics'), dict) else {}
        perceptual = metrics.get('perceptual_stutter') if isinstance(metrics.get('perceptual_stutter'), dict) else {}
        root_cause_analysis = stats.get('root_cause_analysis') if isinstance(stats.get('root_cause_analysis'), dict) else {}
        most_confident = root_cause_analysis.get('most_confident_cause') if isinstance(root_cause_analysis.get('most_confident_cause'), dict) else {}
        return {
            'device_id': meta.get('device_id', ''),
            'device_ip': meta.get('device_ip', ''),
            'firmware_incremental': meta.get('firmware_incremental', ''),
            'package_name': meta.get('package_name', ''),
            'start_time': meta.get('start_time', ''),
            'end_time': meta.get('end_time', ''),
            'duration_sec': meta.get('duration_sec', 0),
            'score': decision.get('score', None),
            'grade': decision.get('grade', ''),
            'tv_perceptual_score': perceptual.get('score', None),
            'log_stutter_count': stats.get('final_log_stutter_count', None),
            'root_cause_top': most_confident.get('root_cause_type', ''),
        }
    except Exception as e:
        logger.warning(f'读取 summary JSON 失败: {summary_json_path} | {e}')
        return {}


def _load_report_meta_from_summary_txt(summary_txt_path: str) -> dict:
    if not summary_txt_path:
        return {}
    try:
        with open(summary_txt_path, 'r', encoding='utf-8', errors='ignore') as f:
            head_lines = []
            for _ in range(120):
                line = f.readline()
                if not line:
                    break
                head_lines.append(line.rstrip('\n'))
        head = "\n".join(head_lines)

        device_id = ""
        device_ip = ""
        firmware_incremental = ""
        package_name = ""
        start_time = ""
        end_time = ""
        duration_sec = 0
        score = None
        grade = ""
        tv_perceptual_score = None
        log_stutter_count = None
        root_cause_top = ""

        m = re.search(r'测试设备:\s*(.+)', head)
        if m:
            device_id = m.group(1).strip()
        m = re.search(r'机顶盒\s*IP:\s*(.+)', head)
        if m:
            device_ip = m.group(1).strip()
        m = re.search(r'固件版本:\s*(.+)', head)
        if m:
            firmware_incremental = m.group(1).strip()
        m = re.search(r'测试包名:\s*(.+)', head)
        if m:
            package_name = m.group(1).strip()
        m = re.search(r'生成时间:\s*(.+)', head)
        if m:
            end_time = m.group(1).strip()
        m = re.search(r'实际运行时长:\s*([0-9]+)小时\s*([0-9]+)分钟\s*([0-9]+)秒', head)
        if m:
            h = int(m.group(1) or 0)
            mi = int(m.group(2) or 0)
            s = int(m.group(3) or 0)
            duration_sec = (h * 3600) + (mi * 60) + s
        m = re.search(r'稳定性评分:\s*([0-9]+)\s*/\s*100\s*\\(等级:\s*([A-Z])\\)', head)
        if m:
            score = int(m.group(1))
            grade = m.group(2)
        m = re.search(r'首要根因:\s*([A-Z0-9_]+)', head)
        if m:
            root_cause_top = m.group(1).strip()
        m = re.search(r'卡顿事件:\s*([0-9]+)\s*次', head)
        if m:
            log_stutter_count = int(m.group(1))
        m = re.search(r'Perceptual Score:\s*([0-9]+)', head)
        if m:
            tv_perceptual_score = int(m.group(1))

        return {
            'device_id': device_id,
            'device_ip': device_ip,
            'firmware_incremental': firmware_incremental,
            'package_name': package_name,
            'start_time': start_time,
            'end_time': end_time,
            'duration_sec': duration_sec,
            'score': score,
            'grade': grade,
            'tv_perceptual_score': tv_perceptual_score,
            'log_stutter_count': log_stutter_count,
            'root_cause_top': root_cause_top,
        }
    except Exception as e:
        logger.warning(f'读取 summary TXT 失败: {summary_txt_path} | {e}')
        return {}


def _load_report_meta_cached(summary_txt_path: str, summary_json_path: str) -> dict:
    meta = {}
    try:
        cache_key = ""
        mtime = None
        if summary_txt_path and os.path.isfile(summary_txt_path):
            cache_key = summary_txt_path
            mtime = os.path.getmtime(summary_txt_path)
        elif summary_json_path and os.path.isfile(summary_json_path):
            cache_key = summary_json_path
            mtime = os.path.getmtime(summary_json_path)
        if cache_key and mtime is not None:
            cached = REPORT_META_CACHE.get(cache_key)
            if cached and cached.get('mtime') == mtime:
                return cached.get('meta') or {}
        if summary_txt_path:
            meta = _load_report_meta_from_summary_txt(summary_txt_path)
        if not meta and summary_json_path:
            meta = _load_report_meta_from_summary_json(summary_json_path)
        if cache_key and mtime is not None:
            REPORT_META_CACHE[cache_key] = {'mtime': mtime, 'meta': meta}
    except Exception:
        pass
    return meta or {}

def run_test_background(config):
    """后台运行测试的线程函数"""
    global TEST_INSTANCE, TEST_RUN_STATE
    log_monitor = None
    runner = None
    failed = False
    try:
        now = datetime.now().strftime('%H:%M:%S')
        logger_callback(f"[{now}] 🚀 测试线程已启动，正在初始化...")
        logger.debug("run_test_background: 线程已启动")
        
        output_dir = config.get('report', {}).get('output_dir', 'reports')
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] [THREAD] 创建报告目录: {output_dir}")
        
        device_id = config.get('device_id')
        song_change_keywords = config.get('test_strategy', {}).get('song_change_keywords', [])
        adb = AdbManager(device_id=device_id)
        log_monitor = LogMonitor(adb, device_id, log_callback=logger_callback, song_change_patterns=song_change_keywords)
        log_monitor.start()
        logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] [THREAD] LogMonitor 已启动" + (f"（切歌关键字: {song_change_keywords}）" if song_change_keywords else ""))
        
        logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] [THREAD] 正在创建 TestRunner 实例...")
        logger.debug("run_test_background: 正在创建 TestRunner...")
        runner = TestRunner(config, log_monitor=log_monitor, logger_callback=logger_callback)
        TEST_INSTANCE = runner
        logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] [THREAD] TestRunner 实例已创建")
        
        logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] [THREAD] 开始执行测试...")
        logger.debug("run_test_background: 开始执行 runner.run()...")
        runner.run()
        logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] [THREAD] 测试执行完成")
    except Exception as e:
        failed = True
        error_msg = f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Test Thread Error: {e}"
        logger_callback(error_msg)
        import traceback
        traceback_str = traceback.format_exc()
        logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] Traceback:\n{traceback_str}")
        logger.error("测试线程异常: %s", e, exc_info=True)
    finally:
        if log_monitor:
            try:
                log_monitor.stop()
            except Exception:
                pass
        logger_callback(f"[{datetime.now().strftime('%H:%M:%S')}] 🏁 测试线程已完成")
        finished_at = time.time()
        with TEST_STATE_LOCK:
            report_files = _pick_existing_report_files(runner)
            runner_failure = (
                getattr(runner, "failure_reason", "") if runner else ""
            )
            if failed or runner_failure:
                status = "failed"
                reason = runner_failure or "运行异常"
            elif runner and getattr(runner, "stop_flag", False):
                status = "stopped"
                reason = "用户手动停止"
            else:
                status = "completed"
                reason = "已达到计划时长"
            TEST_RUN_STATE.update({
                "status": status,
                "finished_at": finished_at,
                "completion_reason": reason,
                "report_file": report_files["report_file"],
                "summary_file": report_files["summary_file"],
                "report_csv_file": report_files["report_csv_file"],
                "summary_txt_file": report_files["summary_txt_file"],
                "summary_json_file": report_files["summary_json_file"],
                "report_meta": _load_report_meta_cached(
                    report_files["summary_txt_file"],
                    report_files["summary_json_file"],
                ),
            })
        logger.debug("run_test_background: 线程已完成")

@stress_bp.route('/')
def index():
    return render_template('stress_index.html')

@stress_bp.route('/api/devices', methods=['GET'])
def api_get_devices():
    try:
        devices = AdbManager.list_devices()
        return success_response(data={'devices': devices})
    except Exception as e:
        logger.error(f'获取设备列表失败: {e}', exc_info=True)
        return error_response(
            message='获取设备列表失败，请检查 ADB 连接',
            error=str(e),
            status_code=500
        )

@stress_bp.route('/api/environment', methods=['GET'])
def api_check_environment():
    """检查环境：Root权限、Display检测"""
    import subprocess
    import shutil
    import re
    import os
    
    # 获取设备ID参数（可选）- 强制清除空格
    device_id = request.args.get('device_id', None)
    if device_id:
        device_id = str(device_id).strip()
    logger.debug("API调用: device_id参数=%r, 所有参数=%s", device_id, dict(request.args))
    
    # 清理僵尸连接：如果检测到多个设备，先断开所有连接再重新连接
    try:
        devices = AdbManager.list_devices()
        if len(devices) > 1:
            logger.info(f'检测到多个设备: {devices}，尝试清理僵尸连接')
            logger.debug("检测到多个设备: %s", devices)
            # 只断开离线或重复的设备，保留在线设备
            for dev in devices:
                if ":" in dev and dev != device_id:
                    # 断开非目标设备（可选，谨慎操作）
                    pass  # 暂时不自动断开，避免影响其他设备
    except Exception as e:
        logger.debug(f'清理僵尸连接失败: {e}')
    
    # 强制要求 device_id：如果未提供，尝试自动获取
    if not device_id:
        logger.warning('环境检查: device_id 参数为空，尝试自动获取设备')
        logger.debug("警告: device_id 参数为空，尝试自动获取设备")
        devices = AdbManager.list_devices()
        # 优先选择 IP 格式的设备（KTV 机顶盒特征）
        ip_devices = [d for d in devices if "." in d and ":" in d]
        if ip_devices:
            device_id = ip_devices[0]
            logger.info(f'自动选择设备: {device_id}')
            logger.debug("自动选择设备: %s", device_id)
        elif len(devices) == 1:
            device_id = devices[0]
            logger.info(f'自动使用唯一设备: {device_id}')
            logger.debug("自动使用唯一设备: %s", device_id)
        elif len(devices) > 1:
            # 多个设备但未指定，返回错误
            return error_response(
                message='检测到多个设备，请明确指定 device_id 参数',
                error=f'Multiple devices found: {devices}',
                status_code=400
            )
        else:
            return error_response(
                message='未检测到任何设备，请确保设备已连接',
                error='No devices found',
                status_code=400
            )
    
    # 检查ADB是否可用
    adb_path = shutil.which('adb')
    if not adb_path:
        return error_response(
            message='ADB未找到，请确保ADB已安装并在PATH中',
            error='ADB not found',
            status_code=500
        )
    
    # 构建ADB命令前缀 - 强制清除空格
    adb_cmd = ["adb"]
    if device_id:
        # 清除首尾空格，防止 Flask 接收参数时的换行符干扰
        device_id = str(device_id).strip()
        adb_cmd.extend(["-s", device_id])
        # 先验证设备是否在线
        try:
            import platform
            creation_flags = 0
            if platform.system() == 'Windows':
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            check_cmd = adb_cmd + ["get-state"]
            logger.info(f'设备状态检查命令: {" ".join(check_cmd)}')
            logger.debug("设备状态检查命令: %s", " ".join(check_cmd))
            
            # 关键点：清除环境变量干扰
            import os
            current_env = os.environ.copy()
            current_env.pop("ANDROID_SERIAL", None)
            
            check_result = subprocess.run(
                check_cmd,
                capture_output=True,
                text=True,
                timeout=3,
                encoding='utf-8',
                errors='ignore',
                env=current_env,  # 使用清理后的环境变量
                creationflags=creation_flags if creation_flags else 0,
                shell=False
            )
            device_state = (check_result.stdout or "").strip()
            stderr_state = (check_result.stderr or "").strip()
            logger.info(f'设备状态检查: {device_id} -> stdout={repr(device_state)}, stderr={repr(stderr_state)}, returncode={check_result.returncode}')
            logger.debug("设备状态检查: %s -> stdout=%r, stderr=%r, returncode=%s", device_id, device_state, stderr_state, check_result.returncode)
            logger.debug("完整命令列表: %s", check_cmd)
            logger.debug("环境变量 ANDROID_SERIAL: %s", current_env.get("ANDROID_SERIAL", "已清除"))
            
            # 检查 stderr 中是否有 "more than one device" 错误
            if stderr_state and "more than one device" in stderr_state.lower():
                logger.error(f'设备状态检查失败: 检测到多个设备 - {stderr_state}')
                logger.debug("设备状态检查失败: 检测到多个设备错误")
                logger.debug("完整 stderr: %s", stderr_state)
                return error_response(
                    message=f'检测到多个设备，请确保设备ID正确: {device_id}。错误: {stderr_state}',
                    error=f'Multiple devices: {stderr_state}',
                    status_code=400
                )
            
            # 检查 stdout 中是否也有错误信息（某些情况下错误可能在stdout）
            if device_state and "more than one device" in device_state.lower():
                logger.error(f'设备状态检查失败: 检测到多个设备（在stdout中） - {device_state}')
                return error_response(
                    message=f'检测到多个设备，请确保设备ID正确: {device_id}。错误: {device_state}',
                    error=f'Multiple devices: {device_state}',
                    status_code=400
                )
            
            if device_state != "device":
                logger.warning(f'设备状态异常: {device_state}')
                return error_response(
                    message=f'设备 {device_id} 不在线或状态异常: {device_state}',
                    error=f'Device state: {device_state}',
                    status_code=400
                )
            
            logger.info(f'设备状态检查通过: {device_id} 在线')
            logger.debug("设备状态检查通过: %s 在线", device_id)
            logger.debug("继续执行 Root 和 Display 检查...")
        except Exception as e:
            logger.warning(f'设备状态检查失败: {e}', exc_info=True)
            logger.debug("设备状态检查异常: %s", e)
            # 继续执行，让后续命令来验证
    else:
        logger.debug("设备ID为空，跳过设备状态检查")
    
    logger.info(f'环境检查开始: device_id={device_id}, adb_cmd={adb_cmd}')
    logger.debug("环境检查开始: device_id=%s, adb_cmd=%s", device_id, adb_cmd)
    
    try:
        # 检查Root权限 - 使用多种方法
        root_available = False
        root_error = None
        
        # 方法1: 使用 su 0 id
        try:
            import platform
            # Windows 下需要特殊处理
            creation_flags = 0
            if platform.system() == 'Windows':
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            # 使用 sh -c 包装，避免 ADB 参数解析歧义
            # 方法1: 尝试 su 0 id (标准方式)
            cmd = adb_cmd + ["shell", "su", "0", "id"]
            cmd_str = " ".join(f'"{arg}"' if " " in arg else arg for arg in cmd)
            logger.info(f'执行Root检查命令: {cmd_str}')
            logger.info(f'命令列表: {cmd}')
            logger.debug("Root检查命令: %s", " ".join(cmd))
            
            # 关键点：清除环境变量干扰
            import os
            current_env = os.environ.copy()
            current_env.pop("ANDROID_SERIAL", None)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=8,
                encoding='utf-8',
                errors='ignore',
                env=current_env,  # 使用清理后的环境变量
                creationflags=creation_flags if creation_flags else 0,
                shell=False  # 明确指定不使用shell
            )
            # 分别处理stdout和stderr
            stdout_text = (result.stdout or "").strip()
            stderr_text = (result.stderr or "").strip()
            output_text = stdout_text + stderr_text
            
            logger.info(f'Root检查方法1: returncode={result.returncode}')
            logger.info(f'Root检查方法1 stdout: {repr(stdout_text[:300])}')
            logger.info(f'Root检查方法1 stderr: {repr(stderr_text[:300])}')
            logger.debug("Root检查方法1: returncode=%s, stdout=%r, stderr=%r", result.returncode, stdout_text[:100], stderr_text[:100])
            
            # 如果stderr包含"more than one device"，说明设备ID有问题
            if "more than one device" in stderr_text.lower():
                # 即使指定了 device_id 仍然报错，可能是 device_id 格式问题
                root_error = f"ADB检测到多个设备。当前 device_id={device_id}。请检查：1) 设备ID格式是否正确（如 192.168.16.105:8787）；2) 设备是否在线。错误: {stderr_text[:200]}"
                logger.error(f'Root检查失败（方法1）: {root_error}')
                logger.debug("Root检查失败: 多个设备错误 - device_id=%s, stderr=%s", device_id, stderr_text[:100])
                # 不立即返回，继续尝试其他方法
            # 检查是否包含 uid=0
            elif "uid=0" in output_text or ("uid=0(root)" in output_text):
                root_available = True
                logger.info('Root检查成功（方法1）- 检测到uid=0')
                logger.debug("Root检查成功: 检测到uid=0")
            elif "permission denied" in stderr_text.lower() or "access denied" in stderr_text.lower():
                root_error = "Root权限被拒绝。可能原因：1) 设备未Root；2) 首次运行时需要在设备屏幕上点击'允许'授权；3) su命令路径不正确。请手动执行 'adb shell su' 测试。"
                logger.warning(f'Root检查失败（方法1）: 权限被拒绝')
                logger.debug("Root检查失败: 权限被拒绝 - %s", stderr_text[:100])
            elif "not found" in stderr_text.lower() and "su" in stderr_text.lower():
                root_error = "su命令未找到。可能原因：1) 设备未Root；2) su命令不在PATH中。请检查设备是否已正确Root。"
                logger.warning(f'Root检查失败（方法1）: su命令未找到')
                logger.debug("Root检查失败: su命令未找到 - %s", stderr_text[:100])
            elif result.returncode == 0:
                # 返回码为0但没有uid=0，可能是其他问题
                root_error = f"命令执行成功但未检测到root权限。输出: {output_text[:200]}。提示：请手动执行 'adb -s {device_id} shell su 0 id' 验证。"
                logger.warning(f'Root检查失败（方法1）: {root_error}')
                logger.debug("Root检查失败: 返回码0但无uid=0 - %s", output_text[:100])
            else:
                root_error = f"返回码: {result.returncode}。错误: {stderr_text[:200] if stderr_text else stdout_text[:200]}。提示：请手动执行 'adb -s {device_id} shell su 0 id' 验证。"
                logger.warning(f'Root检查失败（方法1）: {root_error}')
                logger.debug("Root检查失败: 返回码%s - %s", result.returncode, stderr_text[:100] if stderr_text else stdout_text[:100])
        except subprocess.TimeoutExpired:
            root_error = "Root权限检查超时（超过8秒）"
            logger.warning(f'Root检查超时（方法1）')
        except Exception as e:
            root_error = f"Root检查异常（方法1）: {str(e)}"
            logger.error(f'Root检查异常（方法1）: {e}', exc_info=True)
        
        # 如果方法1失败，尝试方法2: 使用 su -c "id"
        if not root_available:
            try:
                import platform
                creation_flags = 0
                if platform.system() == 'Windows':
                    creation_flags = subprocess.CREATE_NO_WINDOW
                
                result2 = subprocess.run(
                    adb_cmd + ["shell", "su", "-c", "id"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    encoding='utf-8',
                    errors='ignore',
                    creationflags=creation_flags if creation_flags else 0,
                    shell=False
                )
                output_text2 = (result2.stdout or "") + (result2.stderr or "")
                logger.debug(f'Root检查方法2输出: returncode={result2.returncode}, output={output_text2[:200]}')
                
                if "uid=0" in output_text2:
                    root_available = True
                    root_error = None  # 清除之前的错误
                    logger.info('Root检查成功（方法2）')
            except Exception as e:
                logger.debug(f'Root检查方法2失败: {e}')
        
        # 如果还是失败，尝试方法3: 直接检查 su 是否可用
        if not root_available:
            try:
                import platform
                creation_flags = 0
                if platform.system() == 'Windows':
                    creation_flags = subprocess.CREATE_NO_WINDOW
                
                result3 = subprocess.run(
                    adb_cmd + ["shell", "su", "-v"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    encoding='utf-8',
                    errors='ignore',
                    creationflags=creation_flags if creation_flags else 0,
                    shell=False
                )
                if result3.returncode == 0 or result3.stdout:
                    # su 命令可用，但可能没有root权限
                    root_error = root_error or "su命令可用但无法获取root权限"
                    logger.warning(f'Root检查: su可用但无法获取权限')
            except Exception as e:
                logger.debug(f'Root检查方法3失败: {e}')
        
        # 检测电视端Display ID
        available_displays = []
        display_error = None
        try:
            import platform
            # Windows 下需要特殊处理
            creation_flags = 0
            if platform.system() == 'Windows':
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            # 方法1: dumpsys display
            import os
            cmd = adb_cmd + ["shell", "dumpsys", "display"]
            cmd_str = " ".join(f'"{arg}"' if " " in arg else arg for arg in cmd)
            logger.info(f'执行Display检测命令: {cmd_str}')
            logger.info(f'命令列表: {cmd}')
            logger.debug("Display检测命令: %s", " ".join(cmd))
            
            # 关键点：清除环境变量干扰
            current_env = os.environ.copy()
            current_env.pop("ANDROID_SERIAL", None)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                encoding='utf-8',
                errors='ignore',
                env=current_env,  # 使用清理后的环境变量
                creationflags=creation_flags if creation_flags else 0,
                shell=False  # 明确指定不使用shell
            )
            logger.info(f'Display检测: returncode={result.returncode}')
            logger.info(f'Display检测 stdout长度: {len(result.stdout or "")}')
            logger.info(f'Display检测 stderr: {repr((result.stderr or "")[:200])}')
            
            # 如果stderr包含"more than one device"，说明设备ID有问题
            if result.stderr and "more than one device" in result.stderr.lower():
                # 即使指定了 device_id 仍然报错，可能是 device_id 格式问题
                display_error = f"ADB检测到多个设备。当前 device_id={device_id}。请检查：1) 设备ID格式是否正确（如 192.168.16.105:8787）；2) 设备是否在线。错误: {result.stderr[:200]}"
                logger.error(f'Display检测失败: {display_error}')
                logger.debug("Display检测失败: 多个设备错误 - device_id=%s, stderr=%s", device_id, result.stderr[:100])
                # 不立即返回，继续尝试其他方法（如默认 Display 1）
            elif result.returncode == 0:
                output = result.stdout
                logger.info(f'Display检测输出长度: {len(output)} 字符')
                logger.debug(f'Display检测输出前500字符: {output[:500]}')
                # 更全面的Display检测
                # 匹配多种格式: Display id=1, mDisplayId=1, displayId=1, Display 1等
                for line in output.splitlines():
                    # 匹配 displayId=1 (驼峰格式)
                    camel_match = re.search(r'displayId\s*=\s*(\d+)', line, re.IGNORECASE)
                    if camel_match:
                        did = int(camel_match.group(1))
                        if did > 0 and did not in available_displays:
                            available_displays.append(did)
                    
                    # 匹配 Display id=1, mDisplayId=1 等格式
                    matches = re.findall(r'(?:Display|display)[\s_]*id[\s=:]*(\d+)', line, re.IGNORECASE)
                    for match in matches:
                        did = int(match)
                        if did > 0 and did not in available_displays:
                            available_displays.append(did)
                    
                    # 也匹配简单的 "Display 1" 格式
                    simple_match = re.search(r'Display\s+(\d+)', line, re.IGNORECASE)
                    if simple_match:
                        did = int(simple_match.group(1))
                        if did > 0 and did not in available_displays:
                            available_displays.append(did)
                    
                    # 匹配 DisplayViewport 中的 displayId=1
                    viewport_match = re.search(r'displayId\s*=\s*(\d+)', line, re.IGNORECASE)
                    if viewport_match:
                        did = int(viewport_match.group(1))
                        if did > 0 and did not in available_displays:
                            available_displays.append(did)
                
                # 方法2: 如果没有检测到，尝试通过SurfaceFlinger检测
                if not available_displays:
                    logger.info('方法1未检测到Display，尝试方法2: SurfaceFlinger')
                    try:
                        import platform
                        creation_flags = 0
                        if platform.system() == 'Windows':
                            creation_flags = subprocess.CREATE_NO_WINDOW
                        
                        # 尝试列出所有display
                        sf_cmd = adb_cmd + ["shell", "dumpsys", "SurfaceFlinger", "--display-id"]
                        logger.info(f'执行Display检测命令(方法2): {" ".join(sf_cmd)}')
                        sf_result = subprocess.run(
                            sf_cmd,
                            capture_output=True,
                            text=True,
                            timeout=5,
                            encoding='utf-8',
                            errors='ignore',
                            creationflags=creation_flags if creation_flags else 0,
                            shell=False
                        )
                        logger.info(f'SurfaceFlinger检测: returncode={sf_result.returncode}, 输出长度={len(sf_result.stdout or "")}')
                        if sf_result.returncode == 0:
                            # 解析SurfaceFlinger输出
                            for line in sf_result.stdout.splitlines():
                                # 查找display相关信息
                                matches = re.findall(r'display[_\s]*id[=:]?\s*(\d+)', line, re.IGNORECASE)
                                for match in matches:
                                    did = int(match)
                                    if did > 0 and did not in available_displays:
                                        available_displays.append(did)
                    except Exception as e:
                        logger.debug("SurfaceFlinger Display 解析失败(可忽略): %s", e)
                
                # 方法3: 尝试直接测试 --display-id 参数
                if not available_displays:
                    logger.info('方法2未检测到Display，尝试方法3: 直接测试--display-id 1')
                    try:
                        import platform
                        creation_flags = 0
                        if platform.system() == 'Windows':
                            creation_flags = subprocess.CREATE_NO_WINDOW
                        
                        # 测试Display 1是否存在
                        test_cmd = adb_cmd + ["shell", "dumpsys", "SurfaceFlinger", "--display-id", "1"]
                        logger.info(f'执行Display检测命令(方法3): {" ".join(test_cmd)}')
                        import os
                        current_env_test = os.environ.copy()
                        current_env_test.pop("ANDROID_SERIAL", None)
                        
                        test_result = subprocess.run(
                            test_cmd,
                            capture_output=True,
                            text=True,
                            timeout=3,
                            encoding='utf-8',
                            errors='ignore',
                            env=current_env_test,  # 使用清理后的环境变量
                            creationflags=creation_flags if creation_flags else 0,
                            shell=False
                        )
                        logger.info(f'Display 1测试: returncode={test_result.returncode}')
                        if test_result.returncode == 0:
                            available_displays.append(1)
                            logger.info('Display检测成功（方法3）: Display 1可用')
                    except Exception as e:
                        logger.debug(f'Display检测方法3异常: {e}')
                
                # 输出最终结果
                if available_displays:
                    logger.info(f'Display检测最终结果: 找到 {available_displays}')
                    logger.debug("Display检测成功: %s", available_displays)
                else:
                    logger.warning(f'Display检测: 所有方法都未检测到Display')
                    logger.debug("Display检测失败: 所有方法都未检测到")
                    # 根据用户反馈，如果 --display 1 是正常的，我们可以假设Display 1存在
                    # 但提供明确的提示
                    display_error = "未检测到Display。可能原因：1) 电视端Display未激活；2) 厂商深度定制导致Display ID不标准。提示：如果确定电视端是Display 1，可以手动选择。"
            else:
                display_error = f"dumpsys display失败，返回码: {result.returncode}, 错误: {result.stderr[:200] if result.stderr else '无错误信息'}"
                logger.error(f'Display检测失败: {display_error}')
                logger.debug("Display检测失败: %s", display_error)
        except subprocess.TimeoutExpired:
            display_error = "Display检测超时"
            available_displays = [1]  # 默认值
        except Exception as e:
            display_error = str(e)
            available_displays = [1]  # 默认值
        
        # 返回结果，包含错误信息（如果有）
        # 即使检测失败，也提供回退建议
        result_data = {
            'root_available': root_available,
            'root_error': root_error,
            'available_displays': available_displays if available_displays else [],  # 确保是列表
            'display_error': display_error,
            'device_id': device_id,
            'device_ip': '',
            'firmware_incremental': '',
            'suggestions': []
        }
        try:
            identity_adb = AdbManager(device_id=device_id)
            result_data['device_ip'] = identity_adb.get_device_ip()
            result_data['firmware_incremental'] = (
                identity_adb.get_firmware_incremental()
            )
        except Exception as identity_error:
            logger.debug("设备身份信息获取失败: %s", identity_error)
        
        # 添加建议
        if not root_available:
            result_data['suggestions'].append({
                'type': 'root',
                'message': 'Root权限不可用，但可以使用"极低功耗模式"进行监控（仅监控FPS和日志卡顿）',
                'action': '可以使用极低功耗模式继续测试'
            })
        
        if not available_displays:
            result_data['suggestions'].append({
                'type': 'display',
                'message': '未检测到Display，如果确定电视端是Display 1，可以手动选择',
                'action': '可以手动选择Display 1进行测试'
            })
        
        logger.info(f'环境检查完成: root={root_available}, displays={available_displays}, device_id={device_id}')
        logger.debug("环境检查完成: root=%s, displays=%s", root_available, available_displays)
        
        return success_response(data=result_data)
    except Exception as e:
        logger.error(f'环境检查失败: {e}', exc_info=True)
        return error_response(
            message='环境检查失败',
            error=str(e),
            status_code=500
        )

@stress_bp.route('/api/fps_probe', methods=['GET'])
def api_fps_probe():
    try:
        device_id = request.args.get('device_id', None)
        if device_id:
            device_id = str(device_id).strip()
        package_name = request.args.get('package_name', 'com.thunder.ktv:media')
        package_name = str(package_name).strip()
        display_id = request.args.get('display_id', '1')
        try:
            display_id = int(display_id)
        except (TypeError, ValueError):
            display_id = 1

        if not device_id:
            devices = AdbManager.list_devices()
            if len(devices) == 1:
                device_id = devices[0]
            else:
                return error_response(message='缺少 device_id', error='device_id is required', status_code=400)

        import re
        if not re.fullmatch(r'[A-Za-z0-9._:-]+', package_name):
            return error_response(message='package_name 非法', error='invalid package_name', status_code=400)

        from .core.monitor import PerformanceMonitor
        adb = AdbManager(device_id=device_id)
        pm = PerformanceMonitor(adb, package_name, monitor_config={"tv_display_id": display_id})

        main_pkg = package_name.split(':')[0]
        media_pkg = package_name if ':' in package_name else None

        def clip_text(s: str, limit: int = 2000) -> str:
            if not s:
                return ""
            s = str(s)
            return s if len(s) <= limit else (s[:limit] + "\n...(truncated)")

        out_gfx_main = adb._run_command(["shell", "dumpsys", "gfxinfo", main_pkg], timeout=3)
        out_fs_main = adb._run_command(["shell", "dumpsys", "gfxinfo", main_pkg, "framestats"], timeout=3)
        out_gfx_media = ""
        out_fs_media = ""
        if media_pkg:
            out_gfx_media = adb._run_command(["shell", "dumpsys", "gfxinfo", media_pkg], timeout=3)
            out_fs_media = adb._run_command(["shell", "dumpsys", "gfxinfo", media_pkg, "framestats"], timeout=3)

        sf_list = adb._run_command(["shell", "dumpsys", "SurfaceFlinger", "--list"], timeout=3)
        sf_list_d = adb._run_command(["shell", "dumpsys", "SurfaceFlinger", "--display-id", str(display_id), "--list"], timeout=3)

        framestats_main_fps = pm._get_fps_from_framestats(main_pkg)
        framestats_media_fps = pm._get_fps_from_framestats(media_pkg) if media_pkg else 0.0
        gfxinfo_fps = pm._get_fps_from_gfxinfo()
        sf_fps = pm._get_fps_from_surfaceflinger(display_id=display_id)

        candidates = []
        if sf_list and "Error:" not in sf_list:
            for line in sf_list.splitlines():
                line_l = line.lower()
                if main_pkg in line or package_name in line or 'video' in line_l or 'media' in line_l or 'surfaceview' in line or 'textureview' in line:
                    candidates.append(line.strip())
                if len(candidates) >= 20:
                    break

        return success_response(data={
            "device_id": device_id,
            "package_name": package_name,
            "display_id": display_id,
            "fps": {
                "framestats_main_fps": framestats_main_fps,
                "framestats_media_fps": framestats_media_fps,
                "gfxinfo_fps": gfxinfo_fps,
                "surfaceflinger_fps": sf_fps
            },
            "surfaceflinger_candidates": candidates,
            "raw": {
                "gfxinfo_main": clip_text(out_gfx_main),
                "framestats_main": clip_text(out_fs_main),
                "gfxinfo_media": clip_text(out_gfx_media),
                "framestats_media": clip_text(out_fs_media),
                "sf_list": clip_text(sf_list),
                "sf_list_display": clip_text(sf_list_d)
            }
        })
    except Exception as e:
        logger.error("fps_probe 失败: %s", e, exc_info=True)
        return error_response(message='fps_probe 失败', error=str(e), status_code=500)


@stress_bp.route('/api/display_snapshot', methods=['GET'])
def api_display_snapshot():
    try:
        device_id = request.args.get('device_id', None)
        if device_id:
            device_id = str(device_id).strip()

        display_id = request.args.get('display_id', '1')
        try:
            display_id = int(display_id)
        except (TypeError, ValueError):
            return error_response(
                message='display_id 格式错误',
                error='invalid display_id',
                status_code=400,
            )

        if not device_id:
            devices = AdbManager.list_devices()
            if len(devices) == 1:
                device_id = devices[0]
            else:
                return error_response(
                    message='缺少 device_id',
                    error='device_id is required',
                    status_code=400,
                )

        adb = AdbManager(device_id=device_id)
        report_dir = get_module_report_dir('player_stress')
        os.makedirs(report_dir, exist_ok=True)

        filename = f"display_probe_{_safe_file_token(device_id)}_d{display_id}.png"
        local_path = os.path.join(report_dir, filename)
        adb.take_screenshot(local_path, display_id=display_id)

        if not os.path.isfile(local_path) or os.path.getsize(local_path) <= 0:
            return error_response(
                message='截图失败，请确认设备在线且支持该 Display',
                error='snapshot capture failed',
                status_code=500,
            )

        return success_response(data={
            'device_id': device_id,
            'display_id': display_id,
            'filename': filename,
            'captured_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'image_url': f'/player_stress/reports/{filename}?t={int(time.time())}',
        })
    except Exception as e:
        logger.error("display_snapshot 失败: %s", e, exc_info=True)
        return error_response(
            message='抓取截图失败',
            error=str(e),
            status_code=500,
        )

@stress_bp.route('/api/start', methods=['POST'])
def api_start_test():
    global TEST_THREAD, TEST_INSTANCE, TEST_RUN_STATE
    
    try:
        with TEST_LOCK:
            if TEST_THREAD and TEST_THREAD.is_alive():
                return error_response(
                    message='测试正在运行中，请先停止当前测试',
                    error='Test is already running',
                    status_code=400
                )
            
            data = request.json or {}
            
            # 参数验证
            validation_error = validate_required(data, 'device_id')
            if validation_error:
                return validation_error
            
            device_id = data.get('device_id')
            package_name = data.get('package_name', 'com.thunder.ktv:media') # Default
            try:
                duration = int(data.get('duration', 60))
                if duration <= 0:
                    return error_response(
                        message='测试时长必须大于0',
                        error='invalid duration',
                        status_code=400
                    )
            except (ValueError, TypeError):
                return error_response(
                    message='测试时长格式错误',
                    error='invalid duration format',
                    status_code=400
                )
            
            mode = data.get('mode', 'monitor_only')
            song_change_keywords = data.get('song_change_keywords', [])
            if isinstance(song_change_keywords, str):
                song_change_keywords = [k.strip() for k in song_change_keywords.split(',') if k.strip()]
            
            # 监控模式配置（默认标准模式以采集 FPS 和趋势图数据）
            # 默认值从 config/platform.yaml 读取
            cfg = get_config_section('player_stress') or {}
            default_interval = cfg.get('interval_seconds', 5)
            performance_mode = data.get('performance_mode', '标准模式')
            interval_seconds = data.get('interval_seconds', default_interval)
            try:
                tv_display_id = int(data.get('tv_display_id', 1))
                screen_check_interval_seconds = max(
                    1.0,
                    float(data.get('screen_check_interval_seconds', 5)),
                )
                tv_freeze_threshold_seconds = max(
                    1.0,
                    float(data.get('tv_freeze_threshold_seconds', 3)),
                )
                tv_poll_interval_seconds = max(
                    0.2,
                    float(data.get('tv_poll_interval_seconds', 0.5)),
                )
                tv_stall_frame_gap_threshold_ms = max(
                    100.0,
                    float(data.get('tv_stall_frame_gap_threshold_ms', 250)),
                )
                tv_stall_start_confirmations = max(
                    2,
                    int(data.get('tv_stall_start_confirmations', 3)),
                )
                tv_stall_recovery_confirmations = max(
                    1,
                    int(data.get('tv_stall_recovery_confirmations', 2)),
                )
            except (TypeError, ValueError):
                return error_response(
                    message='电视端监控参数格式错误',
                    error='invalid tv display monitor settings',
                    status_code=400,
                )
            
            # 根据监控模式设置参数
            if performance_mode == '极低功耗':
                enable_screenshot = False
                enable_fps = False
                interval_seconds = cfg.get('interval_ultra', 5)
            elif performance_mode == '标准模式':
                enable_screenshot = True
                enable_fps = True
                interval_seconds = data.get('interval_seconds', cfg.get('interval_standard', 5))
            else:  # 深度压测
                enable_screenshot = True
                enable_fps = True
                interval_seconds = cfg.get('interval_deep', 1)

            # Construct config
            # Based on config.yaml structure
            config = {
                'device_id': device_id,
                'target_app': {
                    'package_name': package_name,
                    # 'main_activity': '...' # Optional
                },
                'test_strategy': {
                    'mode': mode,
                    'duration_minutes': duration,
                    'skip_interval_seconds': data.get('skip_interval_seconds', 300),
                    'song_change_keywords': song_change_keywords  # 纯监控模式：Logcat 切歌关键字
                },
                'monitor': {
                    'interval_seconds': interval_seconds,
                    'enable_screenshot': enable_screenshot,
                    'enable_fps': enable_fps,
                    'tv_display_id': tv_display_id,
                    'auto_detect_tv_display': bool(data.get('auto_detect_tv_display', True)),
                    'allow_display0_fallback': False,
                    'screen_check_interval_seconds': screen_check_interval_seconds,
                    'tv_freeze_threshold_seconds': tv_freeze_threshold_seconds,
                    'tv_poll_interval_seconds': tv_poll_interval_seconds,
                    'tv_stall_frame_gap_threshold_ms': tv_stall_frame_gap_threshold_ms,
                    'tv_stall_start_confirmations': tv_stall_start_confirmations,
                    'tv_stall_recovery_confirmations': tv_stall_recovery_confirmations,
                    'tv_cpu_baseline_interval_seconds': max(
                        1.0,
                        float(data.get('tv_cpu_baseline_interval_seconds', 2)),
                    ),
                    'tv_cpu_during_interval_seconds': max(
                        0.5,
                        float(data.get('tv_cpu_during_interval_seconds', 1)),
                    ),
                },
                'report': {
                    'output_dir': get_module_report_dir('player_stress')
                },
                'http_vod': data.get('http_vod', {})  # 从请求中获取，如果没有则为空
            }

            started_at = time.time()
            duration_seconds = duration * 60
            with TEST_STATE_LOCK:
                TEST_RUN_STATE = {
                    "status": "running",
                    "started_at": started_at,
                    "planned_end_at": started_at + duration_seconds,
                    "finished_at": None,
                    "duration_seconds": duration_seconds,
                    "completion_reason": "",
                    "report_file": "",
                    "summary_file": "",
                    "report_csv_file": "",
                    "summary_txt_file": "",
                    "summary_json_file": "",
                    "report_meta": {},
                }
            
            # Clear previous logs and add initial log IMMEDIATELY
            now = datetime.now().strftime('%H:%M:%S')
            with LOG_LOCK:
                old_len = len(LOG_BUFFER)
                LOG_BUFFER.clear()
            logger.debug("api_start_test: 清空 LOG_BUFFER，旧长度=%s, LOG_BUFFER id=%s", old_len, id(LOG_BUFFER))
            # 立即添加启动日志，确保前端能看到
            initial_logs = [
                f"[{now}] [START] 正在启动测试...",
                f"[{now}] [INFO] 设备: {device_id}, 包名: {package_name}",
                f"[{now}] [INFO] 监控模式: {performance_mode}, 采样间隔: {interval_seconds}秒"
            ]
            for log_msg in initial_logs:
                LOG_BUFFER.append(log_msg)
            new_len = len(LOG_BUFFER)
            logger.debug("api_start_test: 已添加 %s 条初始日志到 LOG_BUFFER", new_len)
            logger.debug("api_start_test: LOG_BUFFER 内容=%s", list(LOG_BUFFER))
            # 验证日志确实写入
            verify_logs = list(LOG_BUFFER)
            logger.debug("api_start_test: 验证 - LOG_BUFFER 中有 %s 条日志", len(verify_logs))
        
            # Reset TEST_INSTANCE before starting
            TEST_INSTANCE = None
            TEST_THREAD = None  # 确保重置线程状态
                
            # 先快速验证设备是否在线（避免在后台线程中卡住）
            # 注意：即使设备检查失败，也继续启动线程，让线程自己处理错误并记录日志
            # 设备检查 - 即使失败也继续启动线程
            try:
                with LOG_LOCK:
                    LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] [INFO] 正在检查设备连接...")
                    logger.debug("设备检查前 LOG_BUFFER 长度: %s", len(LOG_BUFFER))
                
                adb = AdbManager(device_id=device_id)
                # 设置超时，避免卡住
                import signal
                device_online = False
                try:
                    device_online = adb.is_device_online()
                except Exception as check_error:
                    logger.debug("设备检查异常: %s", check_error)
                    with LOG_LOCK:
                        LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] [WARNING] 设备检查异常: {check_error}")
                
                with LOG_LOCK:
                    if device_online:
                        LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] 设备连接正常")
                        logger.debug("设备检查后 LOG_BUFFER 长度: %s", len(LOG_BUFFER))
                    else:
                        LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] [WARNING] 设备未连接或不在线，但继续启动测试线程...")
                        logger.debug("设备离线，但继续启动，LOG_BUFFER 长度: %s", len(LOG_BUFFER))
                        # 不阻止启动，让后台线程处理并记录详细错误
            except Exception as e:
                logger.warning(f'设备连接检查失败: {e}')
                with LOG_LOCK:
                    LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] [WARNING] 设备检查异常: {e}，继续尝试启动...")
                    logger.debug("设备检查异常，LOG_BUFFER 长度: %s", len(LOG_BUFFER))
                # 不阻止启动，让后台线程处理
            
            try:
                with LOG_LOCK:
                    LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] 🔧 正在创建测试线程...")
                    logger.debug("创建线程前 LOG_BUFFER 长度: %s", len(LOG_BUFFER))
                
                TEST_THREAD = threading.Thread(target=run_test_background, args=(config,))
                TEST_THREAD.daemon = True
                TEST_THREAD.start()
                logger.debug("测试线程已启动，线程ID: %s, 是否存活: %s", TEST_THREAD.ident, TEST_THREAD.is_alive())
                
                # 等待一小段时间，确保线程启动并输出初始日志
                time.sleep(1.0)  # 增加到1秒，确保线程有时间写入日志
                
                with LOG_LOCK:
                    buffer_len = len(LOG_BUFFER)
                    logger.debug("线程启动后 LOG_BUFFER 长度: %s", buffer_len)
                    if buffer_len > 0:
                        logger.debug("LOG_BUFFER 前3条: %s", list(LOG_BUFFER)[:3])
                
                # 检查线程是否还在运行
                if not TEST_THREAD.is_alive():
                    with LOG_LOCK:
                        LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 测试线程启动失败，可能立即退出了")
                        logger.debug("线程已退出，LOG_BUFFER 长度: %s", len(LOG_BUFFER))
                    logger.error('测试线程启动后立即退出')
                    return error_response(
                        message='测试启动失败，请查看日志了解详情',
                        error='Test thread exited immediately',
                        status_code=500
                    )
                
                now = datetime.now().strftime('%H:%M:%S')
                with LOG_LOCK:
                    LOG_BUFFER.append(f"[{now}] [SUCCESS] 测试线程已启动")
                    final_count = len(LOG_BUFFER)
                    # 确保日志被正确复制
                    logs_copy = [str(item) for item in LOG_BUFFER]
                    logger.debug("最终 LOG_BUFFER 长度: %s, logs_copy 长度: %s", final_count, len(logs_copy))
                
                logger.info(f'播放器压测启动: device={device_id}, package={package_name}, duration={duration}分钟, mode={mode}')
                
                # 立即返回，让前端可以获取日志
                with LOG_LOCK:
                    logs_copy = [str(item) for item in LOG_BUFFER]
                
                response_data = {
                    'logs_count': len(logs_copy),
                    'logs': logs_copy
                }
                logger.debug("准备返回的数据: logs_count=%s, logs长度=%s", response_data["logs_count"], len(response_data["logs"]))
                if len(response_data["logs"]) > 0:
                    logger.debug("返回的第一条日志: %s", response_data["logs"][0][:80])
                
                result = success_response(
                    data=response_data,
                    message=f'测试已启动，预计运行 {duration} 分钟'
                )
                logger.debug("success_response 返回类型: %s", type(result))
                return result
            except Exception as e:
                logger.error(f'启动测试线程失败: {e}', exc_info=True)
                with LOG_LOCK:
                    LOG_BUFFER.append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 启动失败: {str(e)}")
                return error_response(
                    message=f'测试启动失败: {str(e)}',
                    error=str(e),
                    status_code=500
                )
    except Exception as outer_e:
        logger.error(f'api_start_test 外层异常: {outer_e}', exc_info=True)
        import traceback
        traceback.print_exc()
        return error_response(
            message=f'测试启动失败: {str(outer_e)}',
            error=str(outer_e),
            status_code=500
        )

@stress_bp.route('/api/stop', methods=['POST'])
def api_stop_test():
    global TEST_INSTANCE, TEST_THREAD, TEST_RUN_STATE
    with TEST_LOCK:
        if not TEST_INSTANCE or not TEST_THREAD or not TEST_THREAD.is_alive():
            return error_response(
                message='当前没有运行中的测试',
                error='No running test instance',
                status_code=400
            )
        with TEST_STATE_LOCK:
            TEST_RUN_STATE["status"] = "stopping"
            TEST_RUN_STATE["completion_reason"] = "正在停止监控并生成报告"
        logger_callback(
            f"[{datetime.now().strftime('%H:%M:%S')}] [STOP] 收到停止请求"
        )
        TEST_INSTANCE.stop()
        thread = TEST_THREAD

    # Do not keep the HTTP request blocked while the current ADB sample and
    # report generation finish. The UI polls /api/status until the thread exits.
    thread.join(timeout=1.5)
    stopped = not thread.is_alive()
    if stopped:
        logger.info('播放器压测已停止')
        return success_response(
            message='监控已停止，报告已保存',
            data={'stopped': True, 'status': 'stopped'},
        )
    logger.warning('播放器压测仍在执行收尾')
    return success_response(
        message='停止信号已生效，正在等待当前采集结束并生成报告',
        data={'stopped': False, 'status': 'stopping'},
    )

@stress_bp.route('/api/status', methods=['GET'])
def api_status():
    try:
        global TEST_THREAD, TEST_INSTANCE, LOG_BUFFER, LOG_LOCK, TEST_RUN_STATE
        include_logs = request.args.get('include_logs', '1') != '0'

        def _compact_history_snapshot(snapshot):
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            return {
                'timestamp': snapshot.get('timestamp', ''),
                'video_fps': snapshot.get('video_fps', 0),
                'mpp_work_count': snapshot.get('mpp_work_count', 0),
                'player_cpu_percent': snapshot.get(
                    'player_cpu_percent',
                    snapshot.get('cpu_percent', 0),
                ),
                'system_cpu_percent': snapshot.get('system_cpu_percent', 0),
                'decode_fps_estimate': snapshot.get('decode_fps_estimate', 0),
                'decode_drop_ratio': snapshot.get('decode_drop_ratio', 0),
                'decoder_stuck': snapshot.get('decoder_stuck', False),
                'decoder_stuck_confirmed': snapshot.get('decoder_stuck_confirmed', False),
                'tv_stutter_detected': snapshot.get('tv_stutter_detected', False),
                'video_fps_source': snapshot.get('video_fps_source', ''),
                'tv_surface_name': snapshot.get('tv_surface_name', ''),
            }

        def _compact_root_cause_evidence(evidence):
            evidence = evidence if isinstance(evidence, dict) else {}
            return {
                'stutter_cpu': evidence.get('stutter_cpu', 0),
                'instance_count': evidence.get('instance_count', 0),
                'reason': evidence.get('reason', ''),
            }

        def _compact_responsibility_summary(summary):
            summary = summary if isinstance(summary, dict) else {}
            evidence_items = summary.get('evidence_items', [])
            return {
                'category': summary.get('category', ''),
                'owner': summary.get('owner', ''),
                'suspect_process': summary.get('suspect_process', ''),
                'confidence': summary.get('confidence', ''),
                'key_basis': summary.get('key_basis', ''),
                'evidence_items': list(evidence_items[:3]) if isinstance(evidence_items, list) else [],
            }

        is_running = TEST_THREAD is not None and TEST_THREAD.is_alive()
        with TEST_STATE_LOCK:
            run_state = dict(TEST_RUN_STATE)
        if is_running and run_state.get("status") != "stopping":
            run_state["status"] = "running"
        now = time.time()
        started_at = float(run_state.get("started_at") or 0)
        planned_end_at = float(run_state.get("planned_end_at") or 0)
        finished_at = float(run_state.get("finished_at") or 0)
        duration_seconds = int(run_state.get("duration_seconds") or 0)
        elapsed_end = now if is_running or not finished_at else finished_at
        elapsed_seconds = (
            max(0, int(elapsed_end - started_at))
            if started_at
            else 0
        )
        remaining_seconds = (
            max(0, int(planned_end_at - now))
            if is_running and planned_end_at
            else 0
        )
        progress_percent = (
            min(100.0, (elapsed_seconds / duration_seconds) * 100.0)
            if duration_seconds > 0
            else 0.0
        )
        if run_state.get("status") == "completed":
            progress_percent = 100.0
        report_meta = dict(run_state.get("report_meta") or {})
        run_state.update({
            "elapsed_seconds": elapsed_seconds,
            "remaining_seconds": remaining_seconds,
            "progress_percent": round(progress_percent, 1),
            "report_meta": report_meta,
        })
        
        # Get logs - 简单直接
        logs = []
        if include_logs:
            with LOG_LOCK:
                logs = [str(item) for item in list(LOG_BUFFER)[-120:]]
        
        # Get performance metrics
        metrics = None
        history = []
        performance_mode = None  # 供前端显示提示
        device_info = {
            'device_id': '',
            'device_ip': '',
            'firmware_incremental': '',
        }
        tv_monitor = {
            'watcher_running': False,
            'display_id': None,
            'surface_name': '',
            'active_event': None,
            'recent_events': [],
        }
        if TEST_INSTANCE and hasattr(TEST_INSTANCE, 'monitor'):
            monitor = TEST_INSTANCE.monitor
            pid_loss_events = [
                event for event in getattr(monitor, 'pid_events', [])
                if event.get('type') == 'PID_LOST'
            ]
            base_process_failure_summary = {}
            if hasattr(monitor, '_build_process_failure_summary'):
                try:
                    base_process_failure_summary = monitor._build_process_failure_summary()
                except Exception:
                    base_process_failure_summary = {}
            error_stats = {"crash_count": 0, "anr_count": 0, "error_events": []}
            if getattr(TEST_INSTANCE, 'log_monitor', None):
                try:
                    error_stats = TEST_INSTANCE.log_monitor.get_error_stats()
                except Exception:
                    error_stats = {"crash_count": 0, "anr_count": 0, "error_events": []}
            live_summary_stub = {
                "restart_count": int(getattr(monitor, 'restart_count', 0) or 0),
                "pid_loss_count": len(pid_loss_events),
                "process_failure_summary": base_process_failure_summary,
            }
            live_process_failure_summary = {}
            live_process_failure_actions = []
            live_tv_process_correlation_summary = {}
            if hasattr(TEST_INSTANCE, '_build_process_failure_summary'):
                try:
                    live_process_failure_summary = TEST_INSTANCE._build_process_failure_summary(
                        live_summary_stub,
                        error_stats,
                    )
                    live_summary_stub["process_failure_summary"] = live_process_failure_summary
                    live_process_failure_actions = TEST_INSTANCE._build_process_failure_actions(
                        live_summary_stub
                    )
                    live_summary_stub["tv_stall_events"] = list(getattr(monitor, 'tv_stall_events', []))
                    live_summary_stub["pid_events"] = list(getattr(monitor, 'pid_events', []))
                    live_tv_process_correlation_summary = TEST_INSTANCE._build_tv_process_correlation_summary(
                        live_summary_stub,
                        error_stats,
                    )
                except Exception:
                    live_process_failure_summary = base_process_failure_summary or {}
                    live_process_failure_actions = []
                    live_tv_process_correlation_summary = {}
            device_info = {
                'device_id': str(
                    getattr(TEST_INSTANCE, 'config', {}).get('device_id', '')
                    or ''
                ),
                'device_ip': str(
                    getattr(TEST_INSTANCE, 'device_ip', '') or ''
                ),
                'firmware_incremental': str(
                    getattr(TEST_INSTANCE, 'firmware_incremental', '') or ''
                ),
                'platform_identity': str(
                    getattr(TEST_INSTANCE, 'platform_identity', '') or ''
                ),
            }
            watcher = getattr(TEST_INSTANCE, 'tv_playback_watcher', None)
            if watcher:
                active_event = getattr(watcher, '_active_event', None)

                def event_summary(event):
                    if not isinstance(event, dict):
                        return None
                    contention = event.get('cpu_contention') or {}
                    candidate = contention.get('top_candidate') or {}
                    reason = str(event.get('reason', '') or '')
                    signals = list(event.get('corroboration_signals', []) or [])
                    confirmed = bool(event.get('confirmed', False))
                    if bool(contention.get('detected')):
                        attribution = {
                            'category': '系统/固件侧',
                            'owner': str(candidate.get('process', '') or 'CPU 资源竞争'),
                            'confidence': 'high' if confirmed else 'medium',
                        }
                    elif (
                        'decoder' in reason.lower()
                        or 'decoder_confirmed' in signals
                        or 'decode_drop' in signals
                    ):
                        attribution = {
                            'category': '播放器/解码侧',
                            'owner': '硬件解码链路',
                            'confidence': 'high' if confirmed else 'medium',
                        }
                    elif confirmed:
                        attribution = {
                            'category': '电视端已确认卡顿',
                            'owner': '待继续定责',
                            'confidence': 'medium',
                        }
                    else:
                        attribution = {
                            'category': '风险提示',
                            'owner': '待补证据',
                            'confidence': 'low',
                        }
                    return {
                        'event_id': event.get('event_id'),
                        'event_token': _safe_event_token(event.get('event_id')),
                        'type': event.get('type', 'TV_STALL'),
                        'start_time': event.get('start_time'),
                        'end_time': event.get('end_time'),
                        'duration_ms': event.get('duration_ms', 0),
                        'reason': event.get('reason', ''),
                        'surface_name': event.get('surface_name', ''),
                        'max_frame_gap_ms': event.get('max_frame_gap_ms', 0),
                        'min_fps': event.get('min_fps', 0),
                        'evidence_dir': event.get('evidence_dir', ''),
                        'confirmed': bool(event.get('confirmed', False)),
                        'confidence_level': event.get('confidence_level', 'risk'),
                        'assessment_reason': event.get('assessment_reason', ''),
                        'corroboration_count': int(event.get('corroboration_count', 0) or 0),
                        'corroboration_signals': list(event.get('corroboration_signals', []) or []),
                        'attribution': attribution,
                        'cpu_contention_detected': bool(contention.get('detected')),
                        'cpu_candidate': {
                            'process': candidate.get('process', ''),
                            'baseline_cpu_percent': candidate.get('baseline_cpu_percent', 0),
                            'peak_cpu_percent': candidate.get('peak_cpu_percent', 0),
                            'after_cpu_percent': candidate.get('after_cpu_percent', 0),
                            'confidence': candidate.get('confidence', 0),
                        } if candidate else None,
                    }

                tv_monitor['watcher_running'] = bool(watcher.running)
                tv_monitor['display_id'] = getattr(watcher, 'display_id', None)
                tv_monitor['surface_name'] = getattr(monitor, '_last_tv_surface_name', '')
                tv_monitor['active_event'] = event_summary(active_event)
                recent_confirmed = list(getattr(monitor, 'tv_stall_events', []))[-3:]
                recent_risk = list(getattr(monitor, 'tv_stall_risk_events', []))[-3:]
                tv_monitor['recent_events'] = [
                    summary for summary in
                    (event_summary(event) for event in reversed(recent_confirmed))
                    if summary
                ]
                tv_monitor['recent_risk_events'] = [
                    summary for summary in
                    (event_summary(event) for event in reversed(recent_risk))
                    if summary
                ]
            # 从 config 获取当前模式（用于前端提示）
            if hasattr(TEST_INSTANCE, 'config') and TEST_INSTANCE.config:
                mon_cfg = TEST_INSTANCE.config.get('monitor', {})
                performance_mode = '极低功耗' if not mon_cfg.get('enable_fps', True) else ('深度压测' if mon_cfg.get('interval_seconds') == 1 else '标准模式')
            if monitor and hasattr(monitor, 'history') and monitor.history:
                # Get latest snapshot
                latest_snapshot = monitor.history[-1]
                
                # Extract key metrics
                metrics = {
                    'video_fps': latest_snapshot.get('video_fps', 0),
                    'video_fps_collected': latest_snapshot.get('video_fps_collected', False),
                    'video_fps_source': latest_snapshot.get('video_fps_source', ''),
                    'tv_display_id': latest_snapshot.get('tv_display_id'),
                    'tv_display_verified': latest_snapshot.get('tv_display_verified', False),
                    'tv_display_verification_reason': latest_snapshot.get('tv_display_verification_reason', ''),
                    'tv_display_recommendation': latest_snapshot.get('tv_display_recommendation', {}),
                    'tv_surface_name': latest_snapshot.get('tv_surface_name', ''),
                    'expected_stream_fps': latest_snapshot.get('expected_stream_fps', 0),
                    'mpp_work_count': latest_snapshot.get('mpp_work_count', 0),
                    'mpp_work_count_delta': latest_snapshot.get('mpp_work_count_delta', 0),
                    'mpp_work_count_delta_time_sec': latest_snapshot.get('mpp_work_count_delta_time_sec', 0),
                    'decoder_stuck': latest_snapshot.get('decoder_stuck', False),
                    'decoder_stuck_duration_sec': latest_snapshot.get('decoder_stuck_duration_sec', 0),
                    'tv_stutter_detected': latest_snapshot.get('tv_stutter_detected', False),
                    'decode_fps_estimate': latest_snapshot.get('decode_fps_estimate', 0),
                    'decode_slowdown_detected': latest_snapshot.get('decode_slowdown_detected', False),
                    'decode_drop_estimate': latest_snapshot.get('decode_drop_estimate', 0),
                    'decode_drop_ratio': latest_snapshot.get('decode_drop_ratio', 0),
                    'fps_unavailable_reason': latest_snapshot.get('fps_unavailable_reason', ''),
                    'ignore_video_metrics': latest_snapshot.get('ignore_video_metrics', False),
                    'ignore_video_reason': latest_snapshot.get('ignore_video_reason', ''),
                    'tv_stall_count': len(getattr(monitor, 'tv_stall_events', [])),
                    'tv_stall_risk_count': len(getattr(monitor, 'tv_stall_risk_events', [])),
                    'cpu_percent': latest_snapshot.get(
                        'player_cpu_percent',
                        latest_snapshot.get('cpu_percent', 0),
                    ),
                    'player_cpu_percent': latest_snapshot.get(
                        'player_cpu_percent',
                        latest_snapshot.get('cpu_percent', 0),
                    ),
                    'system_cpu_percent': latest_snapshot.get('system_cpu_percent', 0),
                    'system_cpu_pressure': latest_snapshot.get('system_cpu_pressure', False),
                    'root_cause_type': latest_snapshot.get('root_cause_type', ''),
                    'suspect_process': latest_snapshot.get('suspect_process', ''),
                    'root_cause_confidence': latest_snapshot.get('root_cause_confidence', 0),
                    'root_cause_evidence': _compact_root_cause_evidence(
                        latest_snapshot.get('root_cause_evidence', {})
                    ),
                    'decoder_stuck_confirmed': latest_snapshot.get('decoder_stuck_confirmed', False),
                    'decoder_stuck_risk': latest_snapshot.get('decoder_stuck_risk', False),
                    'observer_pid': latest_snapshot.get('observer_pid', 0),
                    'observer_cpu_percent': latest_snapshot.get('observer_cpu_percent', 0),
                    'observer_memory_mb': latest_snapshot.get('observer_memory_mb', 0),
                    'observer_sampling_mode': latest_snapshot.get('observer_sampling_mode', ''),
                    'max_temperature_c': latest_snapshot.get('max_temperature_c', 0),
                    'min_cpu_frequency_ratio': latest_snapshot.get('min_cpu_frequency_ratio', 0),
                    'thermal_throttling': latest_snapshot.get('thermal_throttling', False),
                    'pss_mb': latest_snapshot.get('pss_mb', 0),
                    'gfx_jank_count': latest_snapshot.get('gfx_jank_count', 0),
                    'log_stutter_count': latest_snapshot.get('log_stutter_count', 0),
                    'audio_active': latest_snapshot.get('audio_active', False),
                    'timestamp': latest_snapshot.get('timestamp', '')
                    ,'target_process_available': latest_snapshot.get('target_process_available', True)
                    ,'pid_missing_duration_sec': latest_snapshot.get('pid_missing_duration_sec', 0)
                    ,'process_failure_summary': live_process_failure_summary
                    ,'process_failure_actions': live_process_failure_actions
                    ,'tv_process_correlation_summary': live_tv_process_correlation_summary
                    ,'responsibility_summary': _compact_responsibility_summary(
                        TEST_INSTANCE._build_responsibility_summary(
                            {
                                **live_summary_stub,
                                "process_failure_summary": live_process_failure_summary,
                                "tv_process_correlation_summary": live_tv_process_correlation_summary,
                                "confirmed_decoder_stuck_count": latest_snapshot.get('confirmed_decoder_stuck_count', 0),
                                "avg_system_cpu_percent": latest_snapshot.get('system_cpu_percent', 0),
                                "max_system_cpu_percent": latest_snapshot.get('system_cpu_percent', 0),
                                "avg_player_cpu_percent": latest_snapshot.get(
                                    'player_cpu_percent',
                                    latest_snapshot.get('cpu_percent', 0),
                                ),
                                "tv_surface_locked": bool(latest_snapshot.get('tv_surface_name', '')),
                                "avg_video_fps": latest_snapshot.get('video_fps', 0),
                                "decode_drop_ratio": latest_snapshot.get('decode_drop_ratio', 0),
                            },
                            getattr(TEST_INSTANCE, 'last_summary_data', {}).get('root_cause_analysis', {}) or {},
                        )
                        if hasattr(TEST_INSTANCE, '_build_responsibility_summary') else {}
                    )
                    ,'dev_priority_summary': (
                        TEST_INSTANCE._build_dev_priority_summary(
                            {
                                **live_summary_stub,
                                "process_failure_summary": live_process_failure_summary,
                                "tv_process_correlation_summary": live_tv_process_correlation_summary,
                                "decoder_stuck_summary": getattr(TEST_INSTANCE, 'last_summary_data', {}).get('decoder_stuck_summary', {}) or {},
                                "confirmed_decoder_stuck_count": latest_snapshot.get('confirmed_decoder_stuck_count', 0),
                                "avg_system_cpu_percent": latest_snapshot.get('system_cpu_percent', 0),
                                "max_system_cpu_percent": latest_snapshot.get('system_cpu_percent', 0),
                                "avg_player_cpu_percent": latest_snapshot.get(
                                    'player_cpu_percent',
                                    latest_snapshot.get('cpu_percent', 0),
                                ),
                                "tv_surface_locked": bool(latest_snapshot.get('tv_surface_name', '')),
                                "avg_video_fps": latest_snapshot.get('video_fps', 0),
                                "decode_drop_ratio": latest_snapshot.get('decode_drop_ratio', 0),
                            },
                            getattr(TEST_INSTANCE, 'last_summary_data', {}).get('root_cause_analysis', {}) or {},
                        )
                        if hasattr(TEST_INSTANCE, '_build_dev_priority_summary') else {}
                    )
                    ,'platform_support_summary': (
                        TEST_INSTANCE._build_platform_support_summary(
                            {
                                **live_summary_stub,
                                "platform_identity": str(getattr(TEST_INSTANCE, 'platform_identity', '') or ''),
                                "firmware_incremental": str(getattr(TEST_INSTANCE, 'firmware_incremental', '') or ''),
                                "confirmed_decoder_stuck_count": latest_snapshot.get('confirmed_decoder_stuck_count', 0),
                                "decoder_stuck_risk_count": 1 if latest_snapshot.get('decoder_stuck_risk', False) else 0,
                                "tv_display_verified": bool(latest_snapshot.get('tv_display_verified', False)),
                                "tv_display_id": latest_snapshot.get('tv_display_id', 0),
                                "tv_surface_locked": bool(latest_snapshot.get('tv_surface_name', '')),
                                "avg_video_fps": latest_snapshot.get('video_fps', 0),
                                "video_fps_unavailable_reason": latest_snapshot.get('fps_unavailable_reason', ''),
                                "tv_display_recommendation": latest_snapshot.get('tv_display_recommendation', {}),
                            }
                        )
                        if hasattr(TEST_INSTANCE, '_build_platform_support_summary') else {}
                    )
                    ,'evidence_strength': (
                        (
                            (
                                getattr(TEST_INSTANCE, 'last_summary_data', {}).get('root_cause_analysis', {}) or {}
                            ).get('final_diagnosis', {}) or {}
                        ).get('evidence_strength', {})
                    )
                    ,'observer_overhead_summary': {
                        'pid': latest_snapshot.get('observer_pid', 0),
                        'avg_cpu_percent': latest_snapshot.get('observer_cpu_percent', 0),
                        'peak_cpu_percent': latest_snapshot.get('observer_cpu_percent', 0),
                        'avg_memory_mb': latest_snapshot.get('observer_memory_mb', 0),
                        'peak_memory_mb': latest_snapshot.get('observer_memory_mb', 0),
                        'sampling_mode': latest_snapshot.get('observer_sampling_mode', 'unknown'),
                    }
                }
                
                # Get recent history for charts (last 20 points), but keep API payload compact.
                recent_history = monitor.history[-20:] if len(monitor.history) > 20 else monitor.history
                history = [
                    _compact_history_snapshot(item)
                    for item in recent_history
                ]
        elif report_meta:
            device_info = {
                'device_id': str(report_meta.get('device_id', '') or ''),
                'device_ip': str(report_meta.get('device_ip', '') or ''),
                'firmware_incremental': str(report_meta.get('firmware_incremental', '') or ''),
                'platform_identity': str(report_meta.get('platform_identity', '') or ''),
            }
        else:
            # Debug: log why metrics are not available
            if not TEST_INSTANCE:
                logger.debug('TEST_INSTANCE is None')
            elif not hasattr(TEST_INSTANCE, 'monitor'):
                logger.debug('TEST_INSTANCE has no monitor attribute')
            elif not hasattr(TEST_INSTANCE.monitor, 'history'):
                logger.debug('monitor has no history attribute')
            elif not TEST_INSTANCE.monitor.history:
                logger.debug('monitor.history is empty (length: 0)')
        
        song_count = TEST_INSTANCE.song_count if (TEST_INSTANCE and hasattr(TEST_INSTANCE, 'song_count')) else 0
        return success_response(data={
            'running': is_running,
            'logs': logs,
            'metrics': metrics,
            'history': history,
            'performance_mode': performance_mode,
            'song_count': song_count,
            'tv_monitor': tv_monitor,
            'run_state': run_state,
            'device_info': device_info,
            'server_info': {
                'pid': os.getpid(),
                'started_at': run_state.get('started_at') if is_running else None,
            },
        })
    except Exception as e:
        logger.error(f'获取测试状态失败: {e}', exc_info=True)
        return error_response(
            message='获取状态失败',
            error=str(e),
            status_code=500
        )

@stress_bp.route('/api/logs', methods=['GET'])
def api_logs():
    try:
        global LOG_BUFFER, LOG_LOCK
        limit = int(request.args.get('limit', 120) or 120)
        limit = max(20, min(limit, 300))
        with LOG_LOCK:
            logs = [str(item) for item in list(LOG_BUFFER)[-limit:]]
        return success_response(data={
            'logs': logs,
            'count': len(logs),
        })
    except Exception as e:
        logger.error(f'鑾峰彇瀹炴椂鏃ュ織澶辫触: {e}', exc_info=True)
        return error_response(
            message='鑾峰彇瀹炴椂鏃ュ織澶辫触',
            error=str(e),
            status_code=500
        )

@stress_bp.route('/api/reports', methods=['GET'])
def api_reports():
    try:
        limit = int(request.args.get('limit', 20) or 20)
        limit = max(1, min(limit, 200))

        report_dir = get_module_report_dir('player_stress')
        if not os.path.exists(report_dir):
            return success_response(data={'reports': []})

        entries_by_prefix = {}
        filename_pattern = re.compile(r'^(report|summary)_(\d{8}_\d{6})\.(csv|html|txt|json)$')

        try:
            for item in os.scandir(report_dir):
                if not item.is_file():
                    continue
                filename = item.name
                match = filename_pattern.match(filename)
                if not match:
                    continue

                kind, prefix, ext = match.groups()
                entry = entries_by_prefix.get(prefix)
                if not entry:
                    entry = {
                        'prefix': prefix,
                        'csv': '',
                        'html': '',
                        'txt': '',
                        'json': '',
                    }
                    entries_by_prefix[prefix] = entry

                if kind == 'report':
                    if ext == 'csv':
                        entry['csv'] = filename
                    elif ext == 'html':
                        entry['html'] = filename
                elif kind == 'summary':
                    if ext == 'txt':
                        entry['txt'] = filename
                    elif ext == 'json':
                        entry['json'] = filename
        except Exception as e:
            logger.warning(f'读取报告目录失败: {e}')

        prefixes = sorted(entries_by_prefix.keys(), reverse=True)[:limit]
        entries = []
        for prefix in prefixes:
            entry = entries_by_prefix.get(prefix) or {}
            downloadable = []
            for key in ('html', 'txt', 'json', 'csv'):
                if entry.get(key):
                    downloadable.append({
                        'kind': key,
                        'filename': entry[key],
                    })
            summary_json_filename = entry.get('json') or ''
            summary_txt_filename = entry.get('txt') or ''
            summary_json_path = os.path.join(report_dir, summary_json_filename) if summary_json_filename else ''
            summary_txt_path = os.path.join(report_dir, summary_txt_filename) if summary_txt_filename else ''
            entry_out = {
                'prefix': prefix,
                'csv': entry.get('csv', ''),
                'html': entry.get('html', ''),
                'txt': entry.get('txt', ''),
                'json': entry.get('json', ''),
                'downloadable': downloadable,
                'meta': _load_report_meta_cached(summary_txt_path, summary_json_path),
            }
            entries.append(entry_out)

        return success_response(data={'reports': entries})
    except Exception as e:
        logger.error(f'获取报告列表失败: {e}', exc_info=True)
        return error_response(
            message='获取报告列表失败',
            error=str(e),
            status_code=500
        )

@stress_bp.route('/reports/<path:filename>')
def view_report(filename):
    if not filename or ".." in filename or "/" in filename.replace("\\", "/") or filename != os.path.basename(filename):
        return error_response(message="Invalid filename", status_code=400)
    report_dir = get_module_report_dir('player_stress')
    return send_from_directory(report_dir, os.path.basename(filename))


@stress_bp.route('/api/tv_event_evidence/<event_token>', methods=['GET'])
def api_tv_event_evidence(event_token):
    event_token = _safe_event_token(event_token)
    if not event_token:
        return error_response(message='invalid event token', status_code=400)
    event_dir = _resolve_tv_event_evidence_dir(event_token)
    if not event_dir:
        return error_response(message='evidence not found', status_code=404)
    manifest = _build_tv_event_evidence_manifest(event_dir, event_token)
    return success_response(data=manifest)


@stress_bp.route('/evidence/<event_token>/<path:filename>')
def view_tv_event_evidence_file(event_token, filename):
    event_token = _safe_event_token(event_token)
    safe_name = os.path.basename(filename or '')
    if not event_token or not safe_name or safe_name != filename:
        return error_response(message='invalid evidence path', status_code=400)
    event_dir = _resolve_tv_event_evidence_dir(event_token)
    if not event_dir:
        return error_response(message='evidence not found', status_code=404)
    file_path = os.path.join(event_dir, safe_name)
    if not os.path.isfile(file_path):
        return error_response(message='file not found', status_code=404)
    return send_from_directory(event_dir, safe_name)
