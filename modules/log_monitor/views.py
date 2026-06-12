"""
Log Monitor Flask Views
提供日志监控的 Web API
"""
import json
import os
import threading
import time
from datetime import datetime
from flask import Blueprint, render_template, request, Response, stream_with_context, send_file
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from core.runtime.manager import get_runtime_manager
from core.runtime.model import RuntimeStatus
from .core.adb_controller import AdbController
from .core.log_analyzer import LogAnalyzer
from .alert_engine import AlertEngine, AlertRule
from .voice_tracker import VoiceCommandTracker
from .voice_tracker_store import (
    list_voice_sessions,
    load_voice_session,
    save_voice_session,
)
from .log_session_store import save_session_full_log, session_full_log_path
from .agent import get_agent

log_monitor_bp = Blueprint('log_monitor', __name__, template_folder='templates')
logger = setup_logger('log_monitor_module')

# 全局变量：管理多个监控任务
MONITOR_TASKS = {}  # {task_id: {'controller': AdbController, 'device_id': str, 'start_time': float, 'alert_engine': AlertEngine, 'voice_tracker': VoiceCommandTracker}}
MONITOR_TASKS_LOCK = threading.Lock()


def _get_context_around_alert(
    all_logs: list, alert_log_line: str, before: int = 25, after: int = 25, fallback: int = 50
) -> list:
    """
    获取告警前后的日志上下文。若找到告警行则取前后各 before/after 行；否则退回最近 fallback 条。
    """
    if not all_logs:
        return [alert_log_line] if alert_log_line else []
    # 查找告警行（支持子串匹配，日志可能被截断）
    alert_snippet = (alert_log_line or '')[:200]
    idx = -1
    alert_full = alert_log_line or ''
    for i, (_, log) in enumerate(all_logs):
        if alert_snippet and (alert_snippet in log or (alert_full and log in alert_full)):
            idx = i
            break
    if idx >= 0:
        start = max(0, idx - before)
        end = min(len(all_logs), idx + after + 1)
        return [log for _, log in all_logs[start:end]]
    # 未找到则退回最近 fallback 条，并确保包含告警行
    recent = [log for _, log in all_logs[-fallback:]]
    if alert_log_line and alert_log_line not in '\n'.join(recent):
        recent.append(alert_log_line)
    return recent


@log_monitor_bp.route('/')
def index():
    """主页面"""
    return render_template('log_monitor_index.html')


@log_monitor_bp.route('/file_analyze', methods=['GET', 'POST'])
def file_analyze_page():
    """日志文件分析页面（GET/POST 均返回页面，避免 405）"""
    return render_template('log_file_analyze.html')


@log_monitor_bp.route('/api/devices', methods=['GET'])
def api_get_devices():
    """获取设备列表"""
    try:
        controller = AdbController()
        devices = controller.get_connected_devices()
        return success_response(data={'devices': devices})
    except Exception as e:
        logger.error(f'获取设备列表失败: {e}', exc_info=True)
        return error_response(
            message='获取设备列表失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/connect', methods=['POST'])
def api_connect_device():
    """连接设备"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'ip')
        if validation_error:
            return validation_error
        
        ip = data.get('ip')
        port = int(data.get('port', 8787))
        
        controller = AdbController()
        success = controller.connect_device(ip, port)
        
        if success:
            return success_response(
                data={'device_id': f"{ip}:{port}"},
                message=f'设备连接成功: {ip}:{port}'
            )
        else:
            return error_response(
                message='设备连接失败，请检查设备状态和网络',
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


@log_monitor_bp.route('/api/disconnect', methods=['POST'])
def api_disconnect_device():
    """断开设备连接"""
    try:
        data = request.get_json() or {}
        device_id = data.get('device_id')
        
        controller = AdbController()
        controller.disconnect_device(device_id)
        
        return success_response(message='设备已断开连接')
    except Exception as e:
        logger.error(f'断开设备失败: {e}', exc_info=True)
        return error_response(
            message='断开设备失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/voice_tracker/history', methods=['GET'])
def api_voice_tracker_history():
    """获取语音指令追踪历史（运行中来自内存；结束后来自已保存会话文件）"""
    task_id = request.args.get('task_id')
    if not task_id:
        return error_response(message='缺少 task_id', status_code=400)

    with MONITOR_TASKS_LOCK:
        task_info = MONITOR_TASKS.get(task_id)
        if task_info:
            tracker = task_info.get('voice_tracker')
            if not tracker:
                return error_response(message='追踪器未初始化', status_code=500)
            history = list(tracker.get_history())
            history.reverse()
            return success_response(data=history)

    saved = load_voice_session(task_id)
    if saved and saved.get('items') is not None:
        history = list(saved['items'])
        history.reverse()
        return success_response(data=history)

    return error_response(message='任务不存在或无已保存的语音记录', status_code=404)


@log_monitor_bp.route('/api/voice_tracker/sessions', methods=['GET'])
def api_voice_tracker_sessions():
    """列出已保存的语音追踪会话（停止监控时写入）"""
    try:
        limit = int(request.args.get('limit', 50))
    except ValueError:
        limit = 50
    rows = list_voice_sessions(limit=limit)
    return success_response(data=rows)


@log_monitor_bp.route('/api/session_logs/download', methods=['GET'])
def api_session_logs_download():
    """下载停止监控时保存的完整 logcat 文本（与面板是否渲染无关）。"""
    task_id = request.args.get('task_id')
    if not task_id:
        return error_response(message='缺少 task_id', status_code=400)
    path = session_full_log_path(task_id)
    if not os.path.isfile(path):
        return error_response(
            message='未找到已保存的日志文件（请停止监控后再导出，或确认选择了正确的会话）',
            error='not_found',
            status_code=404,
        )
    safe_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in task_id) + '.log'
    return send_file(path, as_attachment=True, download_name=f'log_monitor_{safe_name}', mimetype='text/plain')

@log_monitor_bp.route('/api/start', methods=['POST'])
def api_start_monitor():
    """开始监控"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id')
        if validation_error:
            return validation_error
        
        device_id = data.get('device_id')
        task_id = data.get('task_id', f"log_monitor_{int(time.time())}")
        min_log_level = data.get('min_log_level', 'Verbose')
        
        # 检查是否已有任务在运行
        with MONITOR_TASKS_LOCK:
            if task_id in MONITOR_TASKS:
                return error_response(
                    message='监控任务已在运行',
                    error='task already running',
                    status_code=400
                )
        
        # Create Runtime
        runtime_id = None
        try:
            runtime = get_runtime_manager().create_runtime(
                name=f"Log Monitor: {device_id}",
                module="log_monitor",
                context={
                    'device_id': device_id,
                    'task_id': task_id,
                    'target_package': data.get('target_package', 'com.thunder.ktv')
                }
            )
            runtime_id = runtime.runtime_id
            get_runtime_manager().update_status(runtime_id, RuntimeStatus.RUNNING)
            logger.info(f"Runtime created for Log Monitor: {runtime_id}")
        except Exception as e:
            logger.warning(f"Failed to create Runtime for Log Monitor: {e}")

        # 创建控制器并开始监控
        controller = AdbController()
        
        # 创建日志队列（用于 SSE 流）
        log_queue = []
        log_queue_lock = threading.Lock()
        
        # 创建告警队列
        alert_queue = []
        alert_queue_lock = threading.Lock()
        
        # 创建告警引擎
        alert_engine = AlertEngine()
        
        # 创建语音指令追踪器
        voice_tracker = VoiceCommandTracker()
        
        def log_callback(log_line, analysis_result):
            """日志回调函数"""
            with log_queue_lock:
                log_queue.append({
                    'log': log_line,
                    'analysis': analysis_result[0] if analysis_result else None,
                    'timestamp': time.time()
                })
            
            # 更新语音指令状态机
            try:
                voice_tracker.process_log(log_line)
            except Exception as e:
                logger.error(f"处理语音指令状态失败: {e}")
            
            # 检查告警
            alerts = alert_engine.check_log(log_line, device_id, target_package)
            if alerts:
                with alert_queue_lock:
                    for alert in alerts:
                        alert_queue.append(alert.to_dict())
        
        target_package = data.get('target_package', 'com.thunder.ktv')
        
        controller.start_monitoring(
            device_id=device_id,
            log_callback=log_callback,
            min_log_level=min_log_level,
            target_package=target_package
        )
        
        # 保存任务信息
        with MONITOR_TASKS_LOCK:
            MONITOR_TASKS[task_id] = {
                'controller': controller,
                'device_id': device_id,
                'start_time': time.time(),
                'log_queue': log_queue,
                'log_queue_lock': log_queue_lock,
                'alert_queue': alert_queue,
                'alert_queue_lock': alert_queue_lock,
                'alert_engine': alert_engine,
                'voice_tracker': voice_tracker,
                'target_package': target_package,
                'runtime_id': runtime_id
            }
        
        logger.info(f'日志监控已启动: {task_id}, 设备: {device_id}')
        return success_response(
            data={'task_id': task_id},
            message='日志监控已启动'
        )
    except Exception as e:
        if 'runtime_id' in locals() and runtime_id:
            get_runtime_manager().update_status(runtime_id, RuntimeStatus.FAILED, error=str(e))
        logger.error(f'启动监控失败: {e}', exc_info=True)
        return error_response(
            message='启动监控失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/stop', methods=['POST'])
def api_stop_monitor():
    """停止监控"""
    try:
        data = request.get_json() or {}
        task_id = data.get('task_id')
        
        if not task_id:
            return error_response(
                message='缺少任务ID',
                error='task_id required',
                status_code=400
            )
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='未找到运行中的监控任务',
                    error='task not found',
                    status_code=404
                )
            
            # 持久化完整 logcat 文本（面板未渲染/专注语音模式时仍可导出）
            log_queue = task_info.get("log_queue") or []
            log_queue_lock = task_info.get("log_queue_lock")
            log_lines: list = []
            if log_queue_lock:
                with log_queue_lock:
                    for item in log_queue:
                        ln = item.get("log")
                        if ln is not None:
                            log_lines.append(str(ln))
            else:
                for item in log_queue:
                    ln = item.get("log")
                    if ln is not None:
                        log_lines.append(str(ln))
            try:
                save_session_full_log(task_id, log_lines)
            except Exception as e:
                logger.warning(f"保存完整日志失败: {e}", exc_info=True)

            # 停止监控
            controller = task_info['controller']
            controller.stop_monitoring()
            
            # 更新 Runtime 状态
            runtime_id = task_info.get('runtime_id')
            if runtime_id:
                get_runtime_manager().update_status(runtime_id, RuntimeStatus.CANCELLED)

            # 持久化语音追踪记录（停止后仍可从「语音记录历史」查看）
            voice_tracker = task_info.get('voice_tracker')
            device_id = task_info.get('device_id', '')
            if voice_tracker:
                try:
                    save_voice_session(task_id, device_id, list(voice_tracker.get_history()))
                except Exception as e:
                    logger.warning(f'保存语音追踪会话失败: {e}', exc_info=True)

            # 清理任务
            del MONITOR_TASKS[task_id]
        
        logger.info(f'日志监控已停止: {task_id}')
        return success_response(message='日志监控已停止')
    except Exception as e:
        logger.error(f'停止监控失败: {e}', exc_info=True)
        return error_response(
            message='停止监控失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/stream_logs')
def stream_logs():
    """SSE 日志流"""
    task_id = request.args.get('task_id')
    
    if not task_id:
        return error_response(
            message='缺少任务ID',
            error='task_id required',
            status_code=400
        )
    
    def generate():
        """生成 SSE 流"""
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                return
        
        log_queue = task_info['log_queue']
        log_queue_lock = task_info['log_queue_lock']
        last_index = 0
        
        try:
            while True:
                # 检查任务是否还在运行
                with MONITOR_TASKS_LOCK:
                    if task_id not in MONITOR_TASKS:
                        yield f"data: {json.dumps({'done': True})}\n\n"
                        break
                
                # 获取新日志
                with log_queue_lock:
                    if len(log_queue) > last_index:
                        new_logs = log_queue[last_index:]
                        last_index = len(log_queue)
                    else:
                        new_logs = []
                
                # 发送新日志
                for log_item in new_logs:
                    yield f"data: {json.dumps(log_item)}\n\n"
                
                # 短暂休眠，避免 CPU 占用过高
                time.sleep(0.1)
                
        except GeneratorExit:
            # 客户端断开连接
            pass
        except Exception as e:
            logger.error(f'SSE 流错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@log_monitor_bp.route('/stream_alerts')
def stream_alerts():
    """SSE 告警流"""
    task_id = request.args.get('task_id')
    
    if not task_id:
        return error_response(
            message='缺少任务ID',
            error='task_id required',
            status_code=400
        )
    
    def generate():
        """生成告警 SSE 流"""
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                return
        
        alert_queue = task_info.get('alert_queue', [])
        alert_queue_lock = task_info.get('alert_queue_lock')
        last_index = 0
        
        try:
            while True:
                # 检查任务是否还在运行
                with MONITOR_TASKS_LOCK:
                    if task_id not in MONITOR_TASKS:
                        yield f"data: {json.dumps({'done': True})}\n\n"
                        break
                
                # 获取新告警
                if alert_queue_lock:
                    with alert_queue_lock:
                        if len(alert_queue) > last_index:
                            new_alerts = alert_queue[last_index:]
                            last_index = len(alert_queue)
                        else:
                            new_alerts = []
                else:
                    new_alerts = []
                
                # 发送新告警
                for alert_item in new_alerts:
                    yield f"data: {json.dumps({'type': 'alert', 'data': alert_item})}\n\n"
                
                # 等待一段时间再检查
                time.sleep(0.5)
                
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f'告警流错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@log_monitor_bp.route('/api/alerts', methods=['GET'])
def api_get_alerts():
    """获取告警记录"""
    try:
        task_id = request.args.get('task_id')
        severity = request.args.get('severity')
        acknowledged = request.args.get('acknowledged')
        try:
            limit = max(1, min(500, int(request.args.get('limit', 100))))
        except (ValueError, TypeError):
            limit = 100
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return success_response(data={'alerts': []})
            
            # 获取告警
            alerts = alert_engine.get_alerts(
                severity=severity if severity else None,
                acknowledged=bool(acknowledged) if acknowledged else None,
                limit=limit
            )
            
            return success_response(data={
                'alerts': [alert.to_dict() for alert in alerts]
            })
    except Exception as e:
        logger.error(f'获取告警失败: {e}', exc_info=True)
        return error_response(
            message='获取告警失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alerts/<alert_id>/acknowledge', methods=['POST'])
def api_acknowledge_alert(alert_id):
    """确认告警"""
    try:
        task_id = request.args.get('task_id')
        data = request.get_json() or {}
        user = data.get('user', 'system')
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return error_response(
                    message='告警引擎不存在',
                    error='alert engine not found',
                    status_code=404
                )
            
            if alert_engine.acknowledge_alert(alert_id, user):
                return success_response(message='告警已确认')
            else:
                return error_response(
                    message='告警不存在',
                    error='alert not found',
                    status_code=404
                )
    except Exception as e:
        logger.error(f'确认告警失败: {e}', exc_info=True)
        return error_response(
            message='确认告警失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alerts/statistics', methods=['GET'])
def api_get_alert_statistics():
    """获取告警统计"""
    try:
        task_id = request.args.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return success_response(data={
                    'total': 0,
                    'by_severity': {'high': 0, 'medium': 0, 'low': 0},
                    'by_type': {},
                    'acknowledged': 0,
                    'unacknowledged': 0
                })
            
            stats = alert_engine.get_statistics(device_id=task_info['device_id'])
            return success_response(data={'statistics': stats})
    except Exception as e:
        logger.error(f'获取告警统计失败: {e}', exc_info=True)
        return error_response(
            message='获取告警统计失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/analyze', methods=['GET', 'POST', 'OPTIONS'])
def api_analyze_logs():
    """AI 日志分析 - 根因分析与排查建议"""
    if request.method == 'OPTIONS':
        return '', 204
    if request.method == 'GET':
        return success_response(message='请使用 POST 方法，传入 log_lines 或 task_id+alert_id', data={'usage': 'POST with log_lines or task_id+alert_id'})
    try:
        data = request.get_json() or {}
        task_id = data.get('task_id')
        alert_id = data.get('alert_id')
        log_lines = data.get('log_lines', [])

        # 方式1: 传入原始日志
        if log_lines:
            alert_context = data.get('alert_context')
            agent = get_agent()
            result = agent.analyze(log_lines=log_lines, alert_context=alert_context)
            return success_response(data=result.to_dict())

        # 方式2: 从运行中的任务获取告警 + 上下文日志
        if not task_id or not alert_id:
            return error_response(
                message='请提供 task_id 和 alert_id，或直接提供 log_lines',
                error='missing_params',
                status_code=400
            )

        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在或已停止',
                    error='task not found',
                    status_code=404
                )
            alert_engine = task_info.get('alert_engine')
            log_queue = task_info.get('log_queue', [])
            log_queue_lock = task_info.get('log_queue_lock')

        if not alert_engine:
            return error_response(
                message='告警引擎不可用',
                error='no alert engine',
                status_code=500
            )

        # 查找告警
        alerts = alert_engine.get_alerts(limit=500)
        alert_record = next((a for a in alerts if a.id == alert_id), None)
        if not alert_record:
            return error_response(
                message='告警不存在',
                error='alert not found',
                status_code=404
            )

        # 获取告警前后的日志作为上下文（优先告警周边，否则退回最近50条）
        with log_queue_lock:
            all_logs = [(i, item.get('log', '')) for i, item in enumerate(log_queue) if item.get('log')]
        context_logs = _get_context_around_alert(
            all_logs, alert_record.log_line, before=25, after=25, fallback=50
        )

        alert_context = {
            'rule_name': alert_record.rule_name,
            'severity': alert_record.severity,
            'type': alert_record.type,
            'log_line': alert_record.log_line,
        }

        agent = get_agent()
        result = agent.analyze(log_lines=context_logs, alert_context=alert_context)
        return success_response(data=result.to_dict())

    except FileNotFoundError as e:
        return error_response(
            message=str(e),
            error='llm_not_configured',
            status_code=400
        )
    except ValueError as e:
        return error_response(
            message=str(e),
            error='config_error',
            status_code=400
        )
    except Exception as e:
        logger.exception(f'AI 日志分析失败: {e}')
        return error_response(
            message=f'分析失败: {str(e)}',
            error='analysis_failed',
            status_code=500
        )


@log_monitor_bp.route('/api/runbook', methods=['POST'])
def api_generate_runbook():
    """根据告警类型生成简易排查 runbook（3～5 步）。"""
    try:
        data = request.get_json() or {}
        alert_type = (data.get('alert_type') or '').strip()
        alert_id = data.get('alert_id')
        task_id = data.get('task_id')
        log_snippet = data.get('log_snippet') or ''

        if not alert_type and (alert_id and task_id):
            with MONITOR_TASKS_LOCK:
                task_info = MONITOR_TASKS.get(task_id)
                if task_info:
                    alert_engine = task_info.get('alert_engine')
                    if alert_engine:
                        alerts = alert_engine.get_alerts(limit=500)
                        alert_record = next((a for a in alerts if a.id == alert_id), None)
                        if alert_record:
                            alert_type = alert_record.type or 'keyword'
                            if not log_snippet and alert_record.log_line:
                                log_snippet = alert_record.log_line[:400]

        if not alert_type:
            alert_type = 'keyword'

        agent = get_agent()
        runbook = agent.generate_runbook(alert_type=alert_type, log_snippet=log_snippet or None)
        return success_response(data={'runbook': runbook})
    except FileNotFoundError:
        return error_response(
            message='LLM 未配置，请先在用例管理中配置 LLM',
            error='llm_not_configured',
            status_code=400
        )
    except Exception as e:
        logger.exception(f'生成 runbook 失败: {e}')
        return error_response(
            message=f'生成失败: {str(e)}',
            error='runbook_failed',
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules', methods=['GET'])
def api_get_alert_rules():
    """获取告警规则列表"""
    try:
        task_id = request.args.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return success_response(data={'rules': []})
            
            rules = list(alert_engine.rules.values())
            return success_response(data={
                'rules': [
                    {
                        'id': r.id,
                        'name': r.name,
                        'type': r.type,
                        'pattern': r.pattern,
                        'severity': r.severity,
                        'enabled': r.enabled,
                        'description': r.description,
                        'action': r.action
                    }
                    for r in rules
                ]
            })
    except Exception as e:
        logger.error(f'获取告警规则失败: {e}', exc_info=True)
        return error_response(
            message='获取告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules', methods=['POST'])
def api_create_alert_rule():
    """创建告警规则"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'task_id', 'name', 'type', 'pattern')
        if validation_error:
            return validation_error
        
        task_id = data.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return error_response(
                    message='告警引擎不存在',
                    error='alert engine not found',
                    status_code=404
                )
            
            rule = AlertRule(
                id=data.get('id', f"rule_{int(time.time())}"),
                name=data['name'],
                type=data['type'],
                pattern=data['pattern'],
                severity=data.get('severity', 'medium'),
                enabled=data.get('enabled', True),
                description=data.get('description', ''),
                action=data.get('action', '')
            )
            
            if alert_engine.add_rule(rule):
                return success_response(
                    data={'rule': {
                        'id': rule.id,
                        'name': rule.name,
                        'type': rule.type,
                        'pattern': rule.pattern,
                        'severity': rule.severity,
                        'enabled': rule.enabled,
                        'description': rule.description,
                        'action': rule.action
                    }},
                    message='告警规则创建成功'
                )
            else:
                return error_response(
                    message='规则ID已存在',
                    error='rule id exists',
                    status_code=400
                )
    except Exception as e:
        logger.error(f'创建告警规则失败: {e}', exc_info=True)
        return error_response(
            message='创建告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules/<rule_id>', methods=['PUT'])
def api_update_alert_rule(rule_id):
    """更新告警规则"""
    try:
        data = request.get_json() or {}
        task_id = request.args.get('task_id') or data.get('task_id')
        
        if not task_id:
            return error_response(
                message='缺少任务ID',
                error='task_id required',
                status_code=400
            )
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return error_response(
                    message='告警引擎不存在',
                    error='alert engine not found',
                    status_code=404
                )
            
            # 获取现有规则或创建新规则对象
            existing_rule = alert_engine.rules.get(rule_id)
            if not existing_rule:
                return error_response(
                    message='规则不存在',
                    error='rule not found',
                    status_code=404
                )
            
            # 更新字段
            rule = AlertRule(
                id=rule_id,
                name=data.get('name', existing_rule.name),
                type=data.get('type', existing_rule.type),
                pattern=data.get('pattern', existing_rule.pattern),
                severity=data.get('severity', existing_rule.severity),
                enabled=data.get('enabled', existing_rule.enabled),
                description=data.get('description', existing_rule.description),
                action=data.get('action', existing_rule.action)
            )
            
            if alert_engine.update_rule(rule):
                return success_response(
                    data={'rule': {
                        'id': rule.id,
                        'name': rule.name,
                        'type': rule.type,
                        'pattern': rule.pattern,
                        'severity': rule.severity,
                        'enabled': rule.enabled,
                        'description': rule.description,
                        'action': rule.action
                    }},
                    message='告警规则更新成功'
                )
            else:
                return error_response(
                    message='更新规则失败',
                    error='update failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'更新告警规则失败: {e}', exc_info=True)
        return error_response(
            message='更新告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules/<rule_id>', methods=['DELETE'])
def api_delete_alert_rule(rule_id):
    """删除告警规则"""
    try:
        task_id = request.args.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return error_response(
                    message='告警引擎不存在',
                    error='alert engine not found',
                    status_code=404
                )
            
            if alert_engine.delete_rule(rule_id):
                return success_response(message='告警规则删除成功')
            else:
                return error_response(
                    message='规则不存在',
                    error='rule not found',
                    status_code=404
                )
    except Exception as e:
        logger.error(f'删除告警规则失败: {e}', exc_info=True)
        return error_response(
            message='删除告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/status', methods=['GET'])
def api_get_status():
    """获取监控任务状态"""
    try:
        task_id = request.args.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            if task_id:
                # 获取特定任务状态
                task_info = MONITOR_TASKS.get(task_id)
                if not task_info:
                    return error_response(
                        message='任务不存在',
                        error='task not found',
                        status_code=404
                    )
                
                return success_response(data={
                    'task_id': task_id,
                    'device_id': task_info['device_id'],
                    'start_time': task_info['start_time'],
                    'running_time': int(time.time() - task_info['start_time']),
                    'is_running': True
                })
            else:
                # 获取所有任务状态
                tasks = []
                for tid, info in MONITOR_TASKS.items():
                    tasks.append({
                        'task_id': tid,
                        'device_id': info['device_id'],
                        'start_time': info['start_time'],
                        'running_time': int(time.time() - info['start_time'])
                    })
                
                return success_response(data={
                    'has_running_task': len(tasks) > 0,
                    'tasks': tasks
                })
    except Exception as e:
        logger.error(f'获取状态失败: {e}', exc_info=True)
        return error_response(
            message='获取状态失败',
            error=str(e),
            status_code=500
        )
