"""
组合测试模块 - 多模块联动编排
"""
from flask import Blueprint, render_template, request, send_from_directory
import os
import time
import uuid
import threading
from utils.response import success_response, error_response
from utils.logger import setup_logger
from utils.report_paths import get_module_report_dir
from .core.pipeline import run_pipeline, PIPELINE_TYPES
from .core.report import save_report as save_combined_report

logger = setup_logger('combined_test')

combined_test_bp = Blueprint('combined_test', __name__, template_folder='templates')

# 运行状态
RUNTIME = {
    'running': False,
    'pipeline_id': None,
    'pipeline_type': None,
    'start_ts': 0,
    'logs': [],
    'result': None,
    'stop_event': None,
    'config': None,
}
RUNTIME_LOCK = threading.Lock()
MAX_LOGS = 200


def _append_log(msg: str):
    with RUNTIME_LOCK:
        RUNTIME['logs'].append({'ts': int(time.time()), 'msg': msg})
        if len(RUNTIME['logs']) > MAX_LOGS:
            RUNTIME['logs'] = RUNTIME['logs'][-MAX_LOGS:]


def _run_pipeline_thread(pipeline_id: str, pipeline_type: str, base_url: str, config: dict):
    """后台线程执行流水线"""
    stop_event = threading.Event()
    with RUNTIME_LOCK:
        RUNTIME['stop_event'] = stop_event

    def on_log(msg: str):
        _append_log(msg)
        logger.info(msg)

    try:
        result = run_pipeline(pipeline_type, base_url, config, on_log, stop_event)
        with RUNTIME_LOCK:
            RUNTIME['result'] = result
            RUNTIME['running'] = False
            logs = list(RUNTIME.get('logs', []))
        try:
            path = save_combined_report(pipeline_id, pipeline_type, result, logs, config)
            if path:
                _append_log(f'[PIPELINE] 报告已保存: {os.path.basename(path)}')
        except Exception as ex:
            logger.warning('保存报告失败: %s', ex)
    except Exception as e:
        logger.exception('流水线执行异常: %s', e)
        _append_log(f'[PIPELINE] 异常: {e}')
        with RUNTIME_LOCK:
            RUNTIME['running'] = False
            RUNTIME['result'] = {'success': False, 'message': str(e)}


@combined_test_bp.route('/')
def index():
    return render_template('combined_index.html', prefix='/combined_test')


@combined_test_bp.route('/api/pipeline_types', methods=['GET'])
def api_pipeline_types():
    """获取支持的流水线类型"""
    types_list = [
        {'id': k, 'name': v['name'], 'desc': v['desc'], 'steps': v['steps']}
        for k, v in PIPELINE_TYPES.items()
    ]
    return success_response(data={'types': types_list})


@combined_test_bp.route('/api/start', methods=['POST'])
def api_start():
    """启动组合测试"""
    with RUNTIME_LOCK:
        if RUNTIME.get('running'):
            return error_response(
                message='组合测试正在运行中，请先停止',
                error='task already running',
                status_code=400
            )

    data = request.get_json(force=True) or {}
    pipeline_type = data.get('pipeline_type')
    if not pipeline_type or pipeline_type not in PIPELINE_TYPES:
        return error_response(
            message='请选择有效的流水线类型',
            error='invalid pipeline_type',
            status_code=400
        )

    devices = data.get('devices', [])
    if not devices:
        return error_response(
            message='请配置至少一个设备',
            error='no devices',
            status_code=400
        )

    # 获取 base_url（当前请求的 host）
    base_url = request.host_url.rstrip('/')
    if not base_url.startswith('http'):
        base_url = f'http://{base_url}'

    config = {
        'devices': devices,
        'reboot_duration_minutes': int(data.get('reboot_duration_minutes', 5)),
        'player_stress_duration': int(data.get('player_stress_duration', 30)),
        'device_id': data.get('device_id'),
        'mode': data.get('mode', 'monitor_only'),
        'package_name': data.get('package_name', 'com.thunder.ktv:media'),
        'reboot_defaults': data.get('reboot_defaults', {}),
        'precision_analysis_id': data.get('precision_analysis_id') or request.args.get('precision_analysis_id'),
        'precision_test_point_id': data.get('precision_test_point_id') or request.args.get('precision_test_point_id'),
        'precision_execution_id': data.get('precision_execution_id') or request.args.get('precision_execution_id'),
    }

    pipeline_id = data.get('pipeline_id') or uuid.uuid4().hex[:8]
    config['start_ts'] = int(time.time())
    with RUNTIME_LOCK:
        RUNTIME['running'] = True
        RUNTIME['pipeline_id'] = pipeline_id
        RUNTIME['pipeline_type'] = pipeline_type
        RUNTIME['start_ts'] = config['start_ts']
        RUNTIME['logs'] = []
        RUNTIME['result'] = None
        RUNTIME['config'] = config

    t = threading.Thread(
        target=_run_pipeline_thread,
        args=(pipeline_id, pipeline_type, base_url, config),
        daemon=True
    )
    t.start()

    _append_log(f'[PIPELINE] 启动: {PIPELINE_TYPES[pipeline_type]["name"]}')
    logger.info('组合测试启动: %s, id=%s', pipeline_type, pipeline_id)

    return success_response(
        data={'pipeline_id': pipeline_id, 'pipeline_type': pipeline_type},
        message='组合测试已启动'
    )


@combined_test_bp.route('/api/stop', methods=['POST'])
def api_stop():
    """停止组合测试"""
    with RUNTIME_LOCK:
        stop_event = RUNTIME.get('stop_event')
        if not RUNTIME.get('running'):
            return error_response(
                message='当前没有运行中的组合测试',
                error='no running task',
                status_code=400
            )

    if stop_event:
        stop_event.set()
    with RUNTIME_LOCK:
        RUNTIME['running'] = False

    _append_log('[PIPELINE] 已发送停止信号')
    logger.info('组合测试已停止')
    return success_response(message='已发送停止信号')


@combined_test_bp.route('/api/status', methods=['GET'])
def api_status():
    """获取运行状态"""
    with RUNTIME_LOCK:
        return success_response(data={
            'running': RUNTIME.get('running', False),
            'pipeline_id': RUNTIME.get('pipeline_id'),
            'pipeline_type': RUNTIME.get('pipeline_type'),
            'start_ts': RUNTIME.get('start_ts', 0),
            'logs': list(RUNTIME.get('logs', []))[-80:],
            'result': RUNTIME.get('result'),
        })


@combined_test_bp.route('/api/reports', methods=['GET'])
def api_reports():
    """获取报告列表"""
    try:
        report_dir = get_module_report_dir('combined_test')
        if not os.path.exists(report_dir):
            return success_response(data={'reports': []})
        files = []
        for f in os.listdir(report_dir):
            if f.endswith('.html'):
                fp = os.path.join(report_dir, f)
                mtime = os.path.getmtime(fp)
                item = {'name': f, 'mtime': mtime, 'report_url': f'/combined_test/reports/{f}'}
                json_name = f[:-5] + '.json'
                json_path = os.path.join(report_dir, json_name)
                if os.path.exists(json_path):
                    try:
                        import json
                        with open(json_path, 'r', encoding='utf-8') as jf:
                            report = json.load(jf)
                        item.update({
                            'json_name': json_name,
                            'success': bool(report.get('success')),
                            'status': 'success' if report.get('success') else 'failed',
                            'summary': report.get('message') or '',
                            'pipeline_id': report.get('pipeline_id'),
                            'pipeline_type': report.get('pipeline_type'),
                            'steps_done': report.get('steps_done') or [],
                            'steps_failed': report.get('steps_failed') or [],
                            'precision_analysis_id': report.get('precision_analysis_id') or '',
                            'precision_test_point_id': report.get('precision_test_point_id') or '',
                            'precision_execution_id': report.get('precision_execution_id') or '',
                        })
                    except Exception as ex:
                        item['parse_error'] = str(ex)
                files.append(item)
        files.sort(key=lambda x: x['mtime'], reverse=True)
        return success_response(data={'reports': files[:50]})
    except Exception as e:
        logger.error('获取报告列表失败: %s', e)
        return error_response(message='获取报告列表失败', error=str(e), status_code=500)


@combined_test_bp.route('/reports')
def reports_page():
    """报告列表页面"""
    return render_template('combined_reports.html', prefix='/combined_test')


@combined_test_bp.route('/reports/<path:filename>')
def view_report(filename):
    """查看报告文件"""
    if not filename or ".." in filename or "/" in filename.replace("\\", "/") or filename != os.path.basename(filename):
        return error_response(message="Invalid filename", status_code=400)
    report_dir = get_module_report_dir('combined_test')
    return send_from_directory(report_dir, os.path.basename(filename))
