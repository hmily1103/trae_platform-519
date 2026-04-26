from flask import Blueprint, render_template, request, jsonify, Response
import time
import uuid
import threading
import os
import json
import glob
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from .core import (
    CONFIG, RUNTIME, STOP_EVENT, DEVICES_PATH,
    load_json, append_log,
    save_report, start_report_writer, stop_report_writer,
    device_worker, get_controller
)
from core.runtime.manager import get_runtime_manager, RuntimeStatus

# 模块日志
logger = setup_logger('reboot_module')

reboot_bp = Blueprint('reboot', __name__, template_folder='templates')

@reboot_bp.route('/')
def index():
    return render_template('reboot_index.html', prefix='/reboot')

@reboot_bp.route('/api/start', methods=['POST'])
def api_start():
    if RUNTIME.get('running'):
        return error_response(
            message='任务正在运行中，请先停止当前任务',
            error='task already running',
            status_code=400
        )
    
    payload = request.get_json(force=True) or {}
    
    # 参数验证
    try:
        duration_minutes = int(payload.get('duration_minutes', 60))
        if duration_minutes <= 0:
            return error_response(
                message='循环时长必须大于0',
                error='invalid duration_minutes',
                status_code=400
            )
    except (ValueError, TypeError):
        return error_response(
            message='循环时长格式错误',
            error='invalid duration_minutes format',
            status_code=400
        )
    defaults = CONFIG.get('defaults', {})
    if 'defaults' in payload:
        user_defaults = payload['defaults']
        defaults = {**defaults, **user_defaults}
        if 'retries' in user_defaults:
            defaults['retries'] = {**CONFIG['defaults'].get('retries', {}), **user_defaults['retries']}
    devices = payload.get('devices')
    if not devices:
        devices = load_json(DEVICES_PATH, [])
    if not devices:
        return error_response(
            message='未配置设备，请先添加设备',
            error='no devices configured',
            status_code=400
        )
    task_id = payload.get('task_id') or uuid.uuid4().hex[:8]
    
    RUNTIME['running'] = True
    RUNTIME['task_id'] = task_id
    RUNTIME['start_ts'] = int(time.time())
    RUNTIME['duration_sec'] = duration_minutes * 60
    RUNTIME['devices'] = {}
    RUNTIME['config_snapshot'] = {
        'duration_minutes': duration_minutes,
        'defaults': defaults,
        'devices': devices
    }
    STOP_EVENT.clear()
    start_report_writer(task_id)
    end_ts = RUNTIME['start_ts'] + RUNTIME['duration_sec']
    
    for d in devices:
        ip = d.get('ip')
        adb_port = int(d.get('adb_port', 8787))
        dev_key = f"{ip}:{adb_port}"
        
        # Create Runtime Object
        runtime = get_runtime_manager().create_runtime(
            module="reboot",
            name=f"Reboot Test - {ip}:{adb_port}",
            context={
                "device_id": f"{ip}:{adb_port}",
                "task_id": task_id,
                "config": d
            }
        )
        runtime_id = runtime.runtime_id
        
        RUNTIME['devices'][dev_key] = {
            'config': d,
            'runtime_id': runtime_id,
            'stats': {
                'reboot_count': 0,
                'execution_success_count': 0,
                'fail_adb_enable': 0,
                'fail_adb_connect': 0,
                'fail_process_check': 0,
                'fail_power_on': 0,
                'fail_power_off': 0,
                'consecutive_failures': 0,
                'current_status': '初始化'
            },
            'details': []
        }
        t = threading.Thread(target=device_worker, args=(task_id, dev_key, d, STOP_EVENT, end_ts, defaults, runtime_id), daemon=True)
        t.start()
    append_log('任务启动', {'task_id': task_id, 'devices': list(RUNTIME['devices'].keys())})
    logger.info(f'重启测试任务启动: {task_id}, 设备数: {len(RUNTIME["devices"])}')
    return success_response(
        data={'task_id': task_id},
        message=f'任务启动成功，共 {len(RUNTIME["devices"])} 个设备'
    )


@reboot_bp.route('/api/stop', methods=['POST'])
def api_stop():
    if not RUNTIME.get('running'):
        return error_response(
            message='当前没有运行中的任务',
            error='no running task',
            status_code=400
        )
    
    STOP_EVENT.set()
    RUNTIME['running'] = False
    
    # Update Runtime Status
    for dev_key, state in RUNTIME.get('devices', {}).items():
        rid = state.get('runtime_id')
        if rid:
            get_runtime_manager().update_status(rid, RuntimeStatus.CANCELLED)

    # 生成摘要报告
    try:
        duration = int(time.time() - RUNTIME.get('start_ts', int(time.time())))
        for dev_key, state in RUNTIME.get('devices', {}).items():
            cfg = state.get('config', {})
            stats = state.get('stats', {})
            report = {
                'task_id': RUNTIME.get('task_id'),
                'device_ip': cfg.get('ip'),
                'adb_port': cfg.get('adb_port'),
                'channel': cfg.get('channel'),
                'duration_sec': duration,
                'reboot_count': stats.get('reboot_count', 0),
                'execution_success_count': stats.get('execution_success_count', 0),
                'fail_adb_enable': stats.get('fail_adb_enable', 0),
                'fail_adb_connect': stats.get('fail_adb_connect', 0),
                'fail_process_check': stats.get('fail_process_check', 0),
                'fail_power_on': stats.get('fail_power_on', 0),
                'fail_power_off': stats.get('fail_power_off', 0),
                'mode': 'testreboot',
                'timestamp': int(time.time()),
                'note': '独立任务摘要'
            }
            save_report(report)
    except Exception:
        pass
    task_id = RUNTIME.get('task_id')
    append_log('任务停止', {'task_id': task_id})
    logger.info(f'重启测试任务停止: {task_id}')
    stop_report_writer()
    return success_response(message='任务已停止')


@reboot_bp.route('/api/status')
def api_status():
    # Debug logging for RUNTIME state
    print(f"[DEBUG] api_status called. Running: {RUNTIME.get('running')}, TaskID: {RUNTIME.get('task_id')}, Devices: {len(RUNTIME.get('devices', {}))}, RUNTIME_ID: {id(RUNTIME)}")
    logger.info(f"[DEBUG] api_status called. Running: {RUNTIME.get('running')}, TaskID: {RUNTIME.get('task_id')}, Devices: {len(RUNTIME.get('devices', {}))}, RUNTIME_ID: {id(RUNTIME)}")
    
    if not RUNTIME.get('running'):
        return success_response(data={
            'running': False,
            'task_id': RUNTIME.get('task_id'),
            'logs': list(RUNTIME.get('logs', []))[-50:]  # 返回最近日志
        })
        
    now = int(time.time())
    start_ts = RUNTIME.get('start_ts', now)
    duration_sec = RUNTIME.get('duration_sec', 0)
    elapsed = now - start_ts
    
    # 构造设备状态列表
    devices_status = {}
    for k, v in RUNTIME.get('devices', {}).items():
        devices_status[k] = {
            'stats': v['stats'],
            'config': v['config']
        }
        
    return success_response(data={
        'running': True,
        'task_id': RUNTIME.get('task_id'),
        'elapsed': elapsed,
        'progress': min(100, int(elapsed / duration_sec * 100)) if duration_sec > 0 else 0,
        'devices': devices_status,
        'config_snapshot': RUNTIME.get('config_snapshot', {}),
        'logs': list(RUNTIME.get('logs', []))[-50:]
    })



@reboot_bp.route('/api/reports/export', methods=['POST'])
def api_export_report():
    data = request.get_json(force=True) or {}
    csv_type = data.get('type', 'summary')
    task_id = RUNTIME.get('task_id')
    if not task_id:
        return error_response(
            message='未找到任务ID，请先启动任务',
            error='no task id found',
            status_code=400
        )
        
    try:
        # 这里需要构建 summary_rows 和 detail_rows
        # 暂时简化，直接从 RUNTIME 获取数据生成
        summary_rows = []
        detail_rows = []
        
        duration = int(time.time() - RUNTIME.get('start_ts', int(time.time())))
        for dev_key, state in RUNTIME.get('devices', {}).items():
            cfg = state.get('config', {})
            stats = state.get('stats', {})
            # Summary Row
            summary_rows.append({
                'Device': dev_key,
                'RebootCount': stats.get('reboot_count', 0),
                'SuccessCount': stats.get('execution_success_count', 0),
                'FailAdbEnable': stats.get('fail_adb_enable', 0),
                'FailAdbConnect': stats.get('fail_adb_connect', 0),
                'FailProcessCheck': stats.get('fail_process_check', 0),
                'FailPowerOn': stats.get('fail_power_on', 0),
                'FailPowerOff': stats.get('fail_power_off', 0)
            })
            # Details (simplified, as full details are in state['details'])
            for d in state.get('details', []):
                detail_rows.append(d)
                
        from .core import export_csv
        fpath = export_csv(task_id, summary_rows, detail_rows, csv_type)
        logger.info(f'报告导出成功: {fpath}')
        return success_response(
            data={'file': fpath},
            message='报告导出成功'
        )
    except Exception as e:
        logger.error(f'报告导出失败: {e}', exc_info=True)
        return error_response(
            message='报告导出失败，请重试',
            error=str(e),
            status_code=500
        )

# --- New Routes ---
@reboot_bp.route('/reports')
def reports_page():
    return render_template('reboot_reports.html', prefix='/reboot')

@reboot_bp.route('/reports/detail/<task_id>')
def report_detail_page(task_id):
    return render_template('reboot_report_detail.html', prefix='/reboot', task_id=task_id)

@reboot_bp.route('/api/reports/list')
def api_reports_list():
    try:
        path = CONFIG.get('reports_path')
        if not path or not os.path.exists(path):
            return success_response(data=[])
        
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Ensure data is a list
        if not isinstance(data, list):
            data = []
        # Sort by timestamp desc
        data.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
        return success_response(data=data)
    except Exception as e:
        logger.error(f'Get reports list failed: {e}')
        return error_response(message='获取报告列表失败')

@reboot_bp.route('/api/reports/detail/<task_id>')
def api_report_detail(task_id):
    try:
        exports_dir = CONFIG.get('exports_dir')
        if not exports_dir or not os.path.exists(exports_dir):
            return error_response(message='报告目录不存在')
            
        # Search for detail file: details_{task_id}_*.jsonl
        pattern = os.path.join(exports_dir, f"details_{task_id}_*.jsonl")
        files = glob.glob(pattern)
        if not files:
            return error_response(message='未找到详细报告文件')
            
        # Use the latest one if multiple (unlikely)
        files.sort(key=os.path.getmtime, reverse=True)
        fpath = files[0]
        
        details = []
        with open(fpath, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        details.append(json.loads(line))
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug('跳过无效 jsonl 行: %s', e)
                        
        # Construct summary from details if needed, or fetch from reports.json
        # Fetching from reports.json is better for summary consistency
        summary = []
        reports_path = CONFIG.get('reports_path')
        if reports_path and os.path.exists(reports_path):
            try:
                with open(reports_path, 'r', encoding='utf-8') as rf:
                    all_reports = json.load(rf)
                    task_reports = [r for r in all_reports if r.get('task_id') == task_id]
                    if task_reports:
                        summary = task_reports
            except (json.JSONDecodeError, OSError) as e:
                logger.debug('读取 reports.json 失败: %s', e)
        
        # If summary is empty but we have details (e.g. task crashed or not saved yet), generate it
        if not summary and details:
            temp_summary = {}
            start_time = float('inf')
            end_time = 0
            
            for row in details:
                dev = row.get('device') or row.get('device_ip') # Fallback
                if not dev: continue
                
                if dev not in temp_summary:
                    temp_summary[dev] = {
                        'task_id': task_id,
                        'device_ip': dev.split(':')[0] if ':' in dev else dev,
                        'reboot_count': 0,
                        'execution_success_count': 0,
                        'fail_adb_enable': 0,
                        'fail_adb_connect': 0,
                        'fail_process_check': 0,
                        'fail_power_on': 0,
                        'fail_power_off': 0,
                        'duration_sec': 0
                    }
                
                s = temp_summary[dev]
                ts = row.get('start_ts', 0)
                te = row.get('end_ts', 0)
                if ts: start_time = min(start_time, ts)
                if te: end_time = max(end_time, te)
                
                if row.get('ok'):
                    s['reboot_count'] += 1
                    s['execution_success_count'] += 1
                
                # Check errors based on row flags
                if row.get('adb_enable_ok') is False: s['fail_adb_enable'] += 1
                if row.get('adb_connect_ok') is False: s['fail_adb_connect'] += 1
                if row.get('process_ok') is False: s['fail_process_check'] += 1
                # power_on_ok is False means failed to power on (or power on after off)
                if row.get('power_on_ok') is False: s['fail_power_on'] += 1
                if row.get('power_off_ok') is False: s['fail_power_off'] += 1

            duration = end_time - start_time if (start_time != float('inf') and end_time > start_time) else 0
            for v in temp_summary.values():
                v['duration_sec'] = duration
                
            summary = list(temp_summary.values())

        return success_response(data={
            'summary': summary,
            'details': details
        })
    except Exception as e:
        logger.error(f'Get report detail failed: {e}')
        return error_response(message=f'获取详情失败: {str(e)}')
