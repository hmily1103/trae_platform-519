# -*- coding: utf-8 -*-
from flask import render_template, request, jsonify, current_app

from . import api_stress_bp
from .core.api_stress_manager import ApiStressManager
from .core.api_load_runner import DEFAULT_PRESETS
from .core.report_store import list_reports, load_report


def get_manager() -> ApiStressManager:
    mgr = ApiStressManager.get_instance()
    if hasattr(current_app, "root_path") and current_app.root_path:
        mgr.set_app_root(current_app.root_path)
    return mgr


@api_stress_bp.route('/')
def index():
    return render_template('api_stress_index.html')


@api_stress_bp.route('/api/presets', methods=['GET'])
def get_presets():
    """获取预设接口配置"""
    presets = get_manager().get_presets()
    return jsonify({'presets': presets})


@api_stress_bp.route('/api/stress/start', methods=['POST'])
def start_stress():
    """启动 API 压测"""
    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'message': '请填写接口 URL'}), 400

    method = (data.get('method') or 'POST').upper()
    query_params = (data.get('query_params') or '').strip()
    body = data.get('body', '').strip() or None
    concurrency = int(data.get('concurrency', 10))
    duration_sec = int(data.get('duration_sec', 60))
    timeout_sec = int(data.get('timeout_sec', 10))
    keywords = data.get('keywords')
    if isinstance(keywords, list):
        keywords = [str(k).strip() for k in keywords if k]
    else:
        keywords = None

    headers = {}
    raw_headers = data.get('headers')
    if isinstance(raw_headers, dict):
        headers = {str(k): str(v) for k, v in raw_headers.items()}
    elif not headers and method in ('POST', 'PUT', 'PATCH'):
        headers = {'Content-Type': 'application/json'}

    mgr = get_manager()
    success, msg = mgr.start(
        url=url,
        method=method,
        query_params=query_params,
        headers=headers if headers else None,
        body=body,
        concurrency=max(1, min(concurrency, 500)),
        duration_sec=max(5, min(duration_sec, 3600)),
        timeout_sec=max(1, min(timeout_sec, 120)),
        keywords=keywords,
    )
    return jsonify({'success': success, 'message': msg})


@api_stress_bp.route('/api/stress/stop', methods=['POST'])
def stop_stress():
    """停止压测"""
    success, msg, metrics, report_id = get_manager().stop()
    return jsonify({'success': success, 'message': msg, 'metrics': metrics, 'report_id': report_id})


@api_stress_bp.route('/api/stress/force_reset', methods=['POST'])
def force_reset():
    """清除压测状态（当出现「已有任务在运行」但实际无任务时使用）"""
    mgr = get_manager()
    had = mgr.force_reset()
    return jsonify({'success': True, 'message': '已清除压测状态' if had else '当前无运行中的任务'})


@api_stress_bp.route('/api/stress/status', methods=['GET'])
def stress_status():
    """获取压测状态和实时指标"""
    mgr = get_manager()
    running = mgr.is_running()
    metrics = mgr.get_metrics()
    last_report_id = getattr(mgr, '_last_report_id', None)
    return jsonify({
        'running': running,
        'metrics': metrics,
        'report_id': last_report_id,
    })


@api_stress_bp.route('/reports')
def reports_list():
    """报告列表"""
    reports = list_reports(limit=100, app_root=current_app.root_path)
    return render_template('api_stress_reports.html', reports=reports)


@api_stress_bp.route('/reports/<report_id>')
def report_detail(report_id):
    """报告详情"""
    report = load_report(report_id, app_root=current_app.root_path)
    if not report:
        return "报告不存在", 404
    return render_template('api_stress_report_detail.html', report=report)


@api_stress_bp.route('/api/server_top_processes', methods=['POST'])
def server_top_processes():
    """获取服务器 CPU/内存 Top10 进程（需 SSH）"""
    from .core.server_top_processes import fetch_top_processes
    data = request.json or {}
    host = (data.get('host') or data.get('ip') or '').strip()
    port = int(data.get('port', 222))
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    result = fetch_top_processes(
        host=host,
        port=port,
        username=username,
        password=password,
    )
    return jsonify(result)
