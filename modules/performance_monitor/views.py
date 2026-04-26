"""
性能监控 Flask Views
提供性能监控的 Web API
"""
import json
import os
import io
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from flask import Blueprint, render_template, request, Response, stream_with_context, send_from_directory, send_file
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from utils.report_paths import get_module_report_dir
from .service import PerformanceMonitorService
from .baseline import PerformanceBaseline
from .alert_engine import PerformanceAlertRule, PerformanceAlert
from modules.log_monitor.core.models.analysis_models import PerformanceSnapshot, ProcessSnapshot

performance_monitor_bp = Blueprint('performance_monitor', __name__, template_folder='templates')
logger = setup_logger('performance_monitor_module')

# 全局服务实例
PERFORMANCE_SERVICE = PerformanceMonitorService()
PERFORMANCE_SERVICE_LOCK = threading.Lock()


@performance_monitor_bp.route('/')
def index():
    """主页面"""
    return render_template('performance_monitor_index.html')


@performance_monitor_bp.route('/api/devices', methods=['GET'])
def api_get_devices():
    """获取设备列表"""
    try:
        from modules.log_monitor.core.adb_controller import AdbController
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


@performance_monitor_bp.route('/api/connect', methods=['POST'])
def api_connect_device():
    """连接设备"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'ip')
        if validation_error:
            return validation_error
        
        ip = data.get('ip')
        port = int(data.get('port', 8787))
        
        from modules.log_monitor.core.adb_controller import AdbController
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


def _run_adb(device_id: str, cmd: list, timeout: int = 30) -> tuple:
    """执行 ADB 命令，返回 (returncode, stdout, stderr)"""
    try:
        from core.adb_pool import run_command
        return run_command(device_id, cmd, timeout=timeout)
    except Exception:
        pass
    base = ["adb", "-s", device_id] + cmd
    try:
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        r = subprocess.run(base, capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace', creationflags=creationflags)
        return (r.returncode, r.stdout or '', r.stderr or '')
    except Exception as e:
        logger.exception('ADB 执行失败: %s', e)
        return (-1, '', str(e))


@performance_monitor_bp.route('/api/heap_dump', methods=['POST'])
def api_heap_dump():
    """抓取堆转储（不改 App，通过 ADB）- 用于内存泄漏分析"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id', 'package_name')
        if validation_error:
            return validation_error
        device_id = data.get('device_id', '').strip()
        package_name = data.get('package_name', '').strip()
        if not device_id or not package_name:
            return error_response(message='请提供 device_id 和 package_name', error='missing_params', status_code=400)

        # 1. 获取进程 PID
        rc, stdout, stderr = _run_adb(device_id, ['shell', 'pidof', package_name], timeout=10)
        if rc != 0 or not stdout.strip():
            return error_response(
                message=f'应用 {package_name} 未运行或无法获取 PID，请确保应用已启动',
                error='app_not_running',
                status_code=400
            )
        pid = stdout.strip().split()[0] if stdout.strip() else ''
        if not pid or not pid.isdigit():
            return error_response(message='无法解析进程 PID', error='invalid_pid', status_code=400)

        # 2. 在设备上执行 dumpheap
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        remote_path = f'/data/local/tmp/heap_{package_name.replace(".", "_")}_{ts}.hprof'
        rc, stdout, stderr = _run_adb(device_id, ['shell', 'am', 'dumpheap', pid, remote_path], timeout=60)
        if rc != 0:
            err_msg = (stderr or stdout or '未知错误').strip()[:300]
            return error_response(
                message=f'抓取堆转储失败: {err_msg}。部分设备需应用为 debuggable 或 root 权限。',
                error='dumpheap_failed',
                status_code=500
            )

        # 3. 拉取到本地
        heap_dir = get_module_report_dir('performance_monitor')
        heap_subdir = os.path.join(heap_dir, 'heap_dumps')
        os.makedirs(heap_subdir, exist_ok=True)
        local_name = f'heap_{package_name.replace(".", "_")}_{ts}.hprof'
        local_path = os.path.join(heap_subdir, local_name)
        rc, stdout, stderr = _run_adb(device_id, ['pull', remote_path, local_path], timeout=120)
        _run_adb(device_id, ['shell', 'rm', '-f', remote_path], timeout=5)
        if rc != 0 or not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            return error_response(
                message='拉取堆转储文件失败，请检查设备存储空间与权限',
                error='pull_failed',
                status_code=500
            )

        download_url = f'/performance_monitor/heap_dumps/{local_name}'
        return success_response(
            data={'download_url': download_url, 'filename': local_name, 'size_bytes': os.path.getsize(local_path)},
            message='堆转储已抓取，可下载后用 Android Studio Profiler 打开分析'
        )
    except Exception as e:
        logger.exception('堆转储抓取失败: %s', e)
        return error_response(message=f'抓取失败: {str(e)}', error='exception', status_code=500)


@performance_monitor_bp.route('/heap_dumps/<path:filename>')
def serve_heap_dump(filename):
    """下载堆转储文件"""
    if not filename or '..' in filename or '/' in filename.replace('\\', '/'):
        return error_response(message='无效文件名', status_code=400)
    heap_dir = os.path.join(get_module_report_dir('performance_monitor'), 'heap_dumps')
    if not os.path.exists(os.path.join(heap_dir, filename)):
        return error_response(message='文件不存在', status_code=404)
    return send_from_directory(heap_dir, filename, as_attachment=True, download_name=filename)


@performance_monitor_bp.route('/api/meminfo_analyze', methods=['POST'])
def api_meminfo_analyze():
    """内存分析（dumpsys meminfo + LLM）- 直接给出分析结果，不改 App"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id', 'package_name')
        if validation_error:
            return validation_error
        device_id = data.get('device_id', '').strip()
        package_name = data.get('package_name', '').strip()
        if not device_id or not package_name:
            return error_response(message='请提供 device_id 和 package_name', error='missing_params', status_code=400)

        rc, stdout, stderr = _run_adb(device_id, ['shell', 'dumpsys', 'meminfo', package_name], timeout=30)
        if rc != 0 or not stdout.strip():
            return error_response(
                message=f'无法获取应用 {package_name} 的内存信息，请确保应用已启动',
                error='meminfo_failed',
                status_code=400
            )
        meminfo_text = (stdout or '')[:8000]
        if not meminfo_text.strip():
            return error_response(message='内存信息为空', error='empty', status_code=400)

        try:
            from utils.llm_client import call_llm, load_llm_config
            load_llm_config(None)
        except FileNotFoundError:
            return error_response(
                message='请先在「用例管理 → 全局配置」中配置 LLM API Key',
                error='llm_not_configured',
                status_code=400
            )
        except Exception as e:
            return error_response(message=f'LLM 配置错误: {str(e)}', error='llm_error', status_code=400)

        system_prompt = """你是一名 Android 内存分析专家。根据 dumpsys meminfo 输出，分析应用内存状况。
请用中文回答，输出格式必须严格遵循以下 JSON（不要包含其他文字或 markdown 标记）：
{
  "summary": "一句话总结内存状况",
  "suspected_leak": true或false,
  "findings": ["发现1", "发现2"],
  "suggestions": ["建议1", "建议2", "建议3"]
}
要求：
- summary: 简要概括（如：内存正常 / 疑似泄漏 / 对象数量异常）
- suspected_leak: 若 Activities、Views、AppContexts 等数量异常偏多，或 Java heap 持续增长，则为 true
- findings: 2-5 条具体发现（如：检测到 3 个 Activity 实例，通常只需 1 个）
- suggestions: 3-5 条可操作的排查建议"""

        user_content = f"""请分析以下 Android dumpsys meminfo 输出，判断是否存在内存泄漏或异常，并给出排查建议。

【应用包名】{package_name}

【meminfo 输出】
```
{meminfo_text}
```
"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        try:
            response = call_llm(messages, config_path=None, timeout=60)
        except Exception as e:
            logger.exception('LLM 调用失败: %s', e)
            return error_response(message=f'LLM 分析失败: {str(e)}', error='llm_failed', status_code=500)

        content = (response or '').strip()
        if content.startswith('```'):
            lines = content.split('\n')
            if lines[0].startswith('```'):
                lines = lines[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            content = '\n'.join(lines)
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            obj = {
                "summary": content[:300] if content else "解析失败",
                "suspected_leak": False,
                "findings": [],
                "suggestions": ["请检查 LLM 返回格式"]
            }
        return success_response(
            data=obj,
            message='内存分析完成'
        )
    except Exception as e:
        logger.exception('内存分析失败: %s', e)
        return error_response(message=f'分析失败: {str(e)}', error='exception', status_code=500)


@performance_monitor_bp.route('/api/start', methods=['POST'])
def api_start_monitor():
    """开始性能监控"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id', 'package_name')
        if validation_error:
            return validation_error
        
        device_id = data.get('device_id')
        package_name = data.get('package_name')
        task_id = data.get('task_id', f"perf_{int(time.time())}")
        description = data.get('description', '')
        polling_interval = float(data.get('polling_interval', 3.0))
        monitor_type = data.get('monitor_type', 'video')  # 默认监控视频播放卡顿
        save_matched_logs = bool(data.get('save_matched_logs', False))
        log_keywords = data.get('log_keywords', [])
        save_full_logs = bool(data.get('save_full_logs', False))
        try:
            full_log_rotate_size_mb = float(data.get('full_log_rotate_size_mb', 20) or 20)
        except (TypeError, ValueError):
            full_log_rotate_size_mb = 20.0
        full_log_rotate_size_mb = max(1.0, min(full_log_rotate_size_mb, 200.0))
        # Display ID（用于 FPS / SurfaceFlinger 采集），默认 1
        try:
            display_id = int(data.get('display_id', 1) or 1)
        except (TypeError, ValueError):
            display_id = 1
        # 监控时长（分钟）转换为秒；0 或缺省表示不限时，需要手动停止
        try:
            duration_minutes = float(data.get('duration_minutes', 0) or 0)
        except (TypeError, ValueError):
            duration_minutes = 0.0
        duration_seconds = int(duration_minutes * 60) if duration_minutes > 0 else 0
        
        with PERFORMANCE_SERVICE_LOCK:
            if PERFORMANCE_SERVICE.start_monitoring(
                task_id=task_id,
                device_id=device_id,
                package_name=package_name,
                description=description,
                polling_interval=polling_interval,
                monitor_type=monitor_type,
                duration_seconds=duration_seconds,
                display_id=display_id,
                save_matched_logs=save_matched_logs,
                log_keywords=log_keywords,
                save_full_logs=save_full_logs,
                full_log_rotate_size_mb=full_log_rotate_size_mb,
            ):
                task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
                
                # 设置告警回调（用于SSE推送）
                alert_queue = []
                alert_queue_lock = threading.Lock()
                
                def alert_callback(alert: PerformanceAlert):
                    with alert_queue_lock:
                        alert_queue.append(alert.to_dict())
                
                task_info['alert_callback'] = alert_callback
                task_info['alert_queue'] = alert_queue
                task_info['alert_queue_lock'] = alert_queue_lock
                
                return success_response(
                    data={
                        'task_id': task_id,
                        'session_id': task_info['session_id'],
                        'save_matched_logs': bool(task_info.get('save_matched_logs')),
                        'log_keywords': task_info.get('log_keywords', []),
                        'save_full_logs': bool(task_info.get('save_full_logs')),
                        'full_log_rotate_size_mb': task_info.get('full_log_rotate_size_mb', 20),
                    },
                    message='性能监控已启动'
                )
            else:
                return error_response(
                    message='启动监控失败，任务可能已存在',
                    error='task exists',
                    status_code=400
                )
    except Exception as e:
        logger.error(f'启动监控失败: {e}', exc_info=True)
        return error_response(
            message='启动监控失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/stop', methods=['POST'])
def api_stop_monitor():
    """停止性能监控"""
    try:
        data = request.get_json() or {}
        task_id = data.get('task_id')
        
        if not task_id:
            return error_response(
                message='缺少任务ID',
                error='task_id required',
                status_code=400
            )
        
        with PERFORMANCE_SERVICE_LOCK:
            if PERFORMANCE_SERVICE.stop_monitoring(task_id):
                return success_response(message='性能监控已停止')
            else:
                return error_response(
                    message='未找到运行中的监控任务',
                    error='task not found',
                    status_code=404
                )
    except Exception as e:
        logger.error(f'停止监控失败: {e}', exc_info=True)
        return error_response(
            message='停止监控失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/stream_performance')
def stream_performance():
    """SSE 性能数据流"""
    task_id = request.args.get('task_id')
    
    if not task_id:
        return error_response(
            message='缺少任务ID',
            error='task_id required',
            status_code=400
        )
    
    def generate():
        """生成性能数据 SSE 流"""
        with PERFORMANCE_SERVICE_LOCK:
            task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
            if not task_info:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                return
            
            session_id = task_info['session_id']
            last_count = 0
        
        try:
            while True:
                # 检查任务是否还在运行
                with PERFORMANCE_SERVICE_LOCK:
                    if task_id not in PERFORMANCE_SERVICE.tasks:
                        yield f"data: {json.dumps({'done': True})}\n\n"
                        break
                
                # 获取新的性能数据
                session = PERFORMANCE_SERVICE.storage.get_session(session_id)
                if session:
                    snapshots = session.get('snapshots', [])
                    if len(snapshots) > last_count:
                        new_snapshots = snapshots[last_count:]
                        last_count = len(snapshots)
                        
                        for snap in new_snapshots:
                            yield f"data: {json.dumps({'type': 'performance', 'data': snap})}\n\n"
                
                # 等待一段时间再检查
                time.sleep(1.0)
                
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f'性能数据流错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@performance_monitor_bp.route('/api/sessions', methods=['GET'])
def api_list_sessions():
    """列出所有会话（优化：支持分页和限制）"""
    try:
        package_name = request.args.get('package_name')
        device_id = request.args.get('device_id')
        limit = request.args.get('limit', type=int, default=50)  # 默认限制50条
        offset = request.args.get('offset', type=int, default=0)
        
        sessions = PERFORMANCE_SERVICE.storage.list_sessions(
            package_name=package_name,
            device_id=device_id
        )
        
        # 按时间倒序排序（最新的在前）
        sessions.sort(key=lambda x: x.get('start_time', ''), reverse=True)
        
        # 分页处理
        total = len(sessions)
        paginated_sessions = sessions[offset:offset + limit]
        
        # 简化数据，移除快照详情（减少传输量）
        simplified_sessions = []
        for session in paginated_sessions:
            simplified = {
                'session_id': session.get('session_id'),
                'package_name': session.get('package_name'),
                'device_id': session.get('device_id'),
                'start_time': session.get('start_time'),
                'description': session.get('description'),
                'snapshot_count': len(session.get('snapshots', []))
            }
            simplified_sessions.append(simplified)
        
        return success_response(data={
            'sessions': simplified_sessions,
            'total': total,
            'limit': limit,
            'offset': offset
        })
    except Exception as e:
        logger.error(f'获取会话列表失败: {e}', exc_info=True)
        return error_response(
            message='获取会话列表失败',
            error=str(e),
            status_code=500
        )


def _compute_memory_trend(snapshots: list) -> dict:
    """根据 PSS 曲线判断是否疑似内存泄漏（不改 App 方案）"""
    if not snapshots or len(snapshots) < 10:
        return {'suspected_leak': False, 'reason': '样本不足（需至少 10 个快照）', 'first_mb': 0, 'last_mb': 0}
    pss_list = [(s.get('total_pss') or 0) / 1024.0 for s in snapshots]
    first_mb = sum(pss_list[:len(pss_list) // 4]) / max(1, len(pss_list) // 4)
    last_mb = sum(pss_list[-len(pss_list) // 4:]) / max(1, len(pss_list) // 4)
    delta_mb = last_mb - first_mb
    delta_pct = (delta_mb / first_mb * 100) if first_mb > 0 else 0
    if delta_mb >= 30 and delta_pct >= 15:
        return {
            'suspected_leak': True,
            'reason': f'内存从 {first_mb:.1f} MB 增至 {last_mb:.1f} MB（+{delta_mb:.1f} MB，+{delta_pct:.0f}%），建议用堆转储进一步分析',
            'first_mb': round(first_mb, 2),
            'last_mb': round(last_mb, 2),
        }
    return {
        'suspected_leak': False,
        'reason': f'内存趋势正常（{first_mb:.1f} → {last_mb:.1f} MB）',
        'first_mb': round(first_mb, 2),
        'last_mb': round(last_mb, 2),
    }


@performance_monitor_bp.route('/api/sessions/<session_id>', methods=['GET'])
def api_get_session(session_id):
    """获取会话详情"""
    try:
        session = PERFORMANCE_SERVICE.storage.get_session(session_id)
        if not session:
            return error_response(
                message='会话不存在',
                error='session not found',
                status_code=404
            )
        
        statistics = PERFORMANCE_SERVICE.storage.get_statistics(session_id)
        snapshots = session.get('snapshots', [])
        memory_trend = _compute_memory_trend(snapshots)
        
        return success_response(data={
            'session': session,
            'statistics': statistics,
            'memory_trend': memory_trend
        })
    except Exception as e:
        logger.error(f'获取会话详情失败: {e}', exc_info=True)
        return error_response(
            message='获取会话详情失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/sessions/<session_id>/snapshots', methods=['GET'])
def api_get_snapshots(session_id):
    """获取性能快照列表"""
    try:
        limit = request.args.get('limit', type=int, default=50)
        if limit is not None:
            limit = max(1, min(500, limit))
        else:
            limit = 50
        start_time_str = request.args.get('start_time')
        end_time_str = request.args.get('end_time')
        
        start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else None
        
        snapshots = PERFORMANCE_SERVICE.storage.get_snapshots(
            session_id=session_id,
            limit=limit,
            start_time=start_time,
            end_time=end_time
        )
        
        return success_response(data={'snapshots': snapshots})
    except Exception as e:
        logger.error(f'获取快照列表失败: {e}', exc_info=True)
        return error_response(
            message='获取快照列表失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/sessions/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """删除会话"""
    try:
        if PERFORMANCE_SERVICE.storage.delete_session(session_id):
            return success_response(message='会话已删除')
        else:
            return error_response(
                message='会话不存在',
                error='session not found',
                status_code=404
            )
    except Exception as e:
        logger.error(f'删除会话失败: {e}', exc_info=True)
        return error_response(
            message='删除会话失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/sessions/<session_id>/export', methods=['GET'])
def api_export_session(session_id):
    """导出会话数据"""
    try:
        export_format = request.args.get('format', 'csv').lower()
        
        session = PERFORMANCE_SERVICE.storage.get_session(session_id)
        if not session:
            return error_response(
                message='会话不存在',
                error='session not found',
                status_code=404
            )
        
        snapshots = session.get('snapshots', [])
        metadata = session.get('metadata', {})
        
        if export_format == 'csv':
            # 导出为 CSV
            import csv
            import io
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            # 写入表头
            writer.writerow([
                '时间', 'CPU (%)', '内存 (MB)', 'FPS', '标准 Jank', 
                '网络 RX (KB/s)', '网络 TX (KB/s)', 'GC 次数',
                '人眼感知卡顿评分', '卡顿事件数', '总卡顿时长 (ms)', 
                '是否正在卡顿', '卡顿严重程度', '帧时间方差'
            ])
            
            # 写入数据
            for snap in snapshots:
                timestamp = snap.get('timestamp', '')
                cpu = snap.get('cpu_usage', 0)
                memory = (snap.get('total_pss', 0) / 1024)  # KB to MB
                fps = snap.get('fps', 0)
                jank = snap.get('jank_count', 0)
                network_rx = snap.get('network_rx_kb', 0)
                network_tx = snap.get('network_tx_kb', 0)
                gc_count = snap.get('gc_count', 0)
                
                # 人眼感知卡顿数据
                stall_score = snap.get('perceptual_stall_score', 0)
                stall_events = snap.get('perceptual_stall_events', 0)
                stall_duration = snap.get('perceptual_stall_duration_ms', 0)
                is_stalling = snap.get('is_perceptual_stalling', False)
                stall_severity = snap.get('perceptual_stall_severity', '')
                frame_variance = snap.get('frame_time_variance', 0)
                
                writer.writerow([
                    timestamp, f'{cpu:.2f}', f'{memory:.2f}', fps, jank,
                    f'{network_rx:.2f}', f'{network_tx:.2f}', gc_count,
                    f'{stall_score:.2f}', stall_events, f'{stall_duration:.2f}',
                    '是' if is_stalling else '否', stall_severity or '-', f'{frame_variance:.2f}'
                ])
            
            csv_content = output.getvalue()
            output.close()
            
            from flask import Response
            return Response(
                csv_content,
                mimetype='text/csv',
                headers={
                    'Content-Disposition': f'attachment; filename=performance_{session_id}_{datetime.now().strftime("%Y%m%d")}.csv'
                }
            )
        
        elif export_format == 'json':
            # 导出为 JSON
            export_data = {
                'session_id': session_id,
                'metadata': metadata,
                'snapshots': snapshots,
                'statistics': PERFORMANCE_SERVICE.storage.get_statistics(session_id),
                'export_time': datetime.now().isoformat()
            }
            
            from flask import Response
            return Response(
                json.dumps(export_data, ensure_ascii=False, indent=2),
                mimetype='application/json',
                headers={
                    'Content-Disposition': f'attachment; filename=performance_{session_id}_{datetime.now().strftime("%Y%m%d")}.json'
                }
            )
        
        else:
            return error_response(
                message='不支持的导出格式',
                error='unsupported format',
                status_code=400
            )
            
    except Exception as e:
        logger.error(f'导出会话数据失败: {e}', exc_info=True)
        return error_response(
            message='导出会话数据失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/sessions/<session_id>/full-logs/<int:part_index>', methods=['GET'])
def api_download_full_log_part(session_id, part_index):
    """下载全量日志分片"""
    try:
        session = PERFORMANCE_SERVICE.storage.get_session(session_id)
        if not session:
            return error_response(message='会话不存在', error='session not found', status_code=404)
        file_path = PERFORMANCE_SERVICE.storage.get_full_log_part_path(session_id, part_index)
        if not file_path or not os.path.exists(file_path):
            return error_response(message='日志分片不存在', error='part not found', status_code=404)
        return send_file(
            file_path,
            as_attachment=True,
            download_name=os.path.basename(file_path),
            mimetype='text/plain'
        )
    except Exception as e:
        logger.error(f'下载日志分片失败: {e}', exc_info=True)
        return error_response(message='下载日志分片失败', error=str(e), status_code=500)


@performance_monitor_bp.route('/api/sessions/<session_id>/full-logs/export', methods=['GET'])
def api_export_full_logs_zip(session_id):
    """导出全量日志 ZIP"""
    try:
        session = PERFORMANCE_SERVICE.storage.get_session(session_id)
        if not session:
            return error_response(message='会话不存在', error='session not found', status_code=404)
        parts = PERFORMANCE_SERVICE.storage.list_full_log_parts(session_id)
        if not parts:
            return error_response(message='当前会话没有全量日志分片', error='no full logs', status_code=404)
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for part in parts:
                file_path = part.get('file_path')
                file_name = part.get('file_name')
                if file_path and file_name and os.path.exists(file_path):
                    zf.write(file_path, arcname=file_name)
        memory_file.seek(0)
        return send_file(
            memory_file,
            as_attachment=True,
            download_name=f'{session_id}_full_logs.zip',
            mimetype='application/zip'
        )
    except Exception as e:
        logger.error(f'导出全量日志 ZIP 失败: {e}', exc_info=True)
        return error_response(message='导出全量日志失败', error=str(e), status_code=500)


@performance_monitor_bp.route('/api/baselines', methods=['GET'])
def api_list_baselines():
    """列出所有基线"""
    try:
        baselines = PERFORMANCE_SERVICE.baseline.list_baselines()
        return success_response(data={'baselines': baselines})
    except Exception as e:
        logger.error(f'获取基线列表失败: {e}', exc_info=True)
        return error_response(
            message='获取基线列表失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/baselines', methods=['POST'])
def api_create_baseline():
    """创建性能基线"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'name', 'session_id', 'snapshot_index')
        if validation_error:
            return validation_error
        
        name = data['name']
        session_id = data['session_id']
        snapshot_index = int(data['snapshot_index'])
        description = data.get('description', '')
        
        # 获取快照
        session = PERFORMANCE_SERVICE.storage.get_session(session_id)
        if not session:
            return error_response(
                message='会话不存在',
                error='session not found',
                status_code=404
            )
        
        snapshots = session.get('snapshots', [])
        if snapshot_index >= len(snapshots):
            return error_response(
                message='快照索引超出范围',
                error='snapshot index out of range',
                status_code=400
            )
        
        snap_dict = snapshots[snapshot_index]
        
        # 转换为 PerformanceSnapshot
        processes = [
            ProcessSnapshot(
                pid=p['pid'],
                process_name=p['process_name'],
                cpu_usage=p['cpu_usage'],
                rss_kb=p['rss_kb'],
                pss_kb=p['pss_kb'],
                gc_count=p['gc_count']
            )
            for p in snap_dict.get('processes', [])
        ]
        
        snapshot = PerformanceSnapshot(
            timestamp=datetime.fromisoformat(snap_dict['timestamp']),
            total_pss=snap_dict['total_pss'],
            gc_count=snap_dict['gc_count'],
            cpu_usage=snap_dict['cpu_usage'],
            fps=snap_dict.get('fps', 0),
            jank_count=snap_dict.get('jank_count', 0),
            network_rx_kb=snap_dict.get('network_rx_kb', 0.0),
            network_tx_kb=snap_dict.get('network_tx_kb', 0.0),
            device_info=snap_dict.get('device_info', ''),
            processes=processes
        )
        
        PERFORMANCE_SERVICE.baseline.save_baseline(name, snapshot, description)
        
        return success_response(message='基线创建成功')
    except Exception as e:
        logger.error(f'创建基线失败: {e}', exc_info=True)
        return error_response(
            message='创建基线失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/baselines/<baseline_name>', methods=['DELETE'])
def api_delete_baseline(baseline_name):
    """删除基线"""
    try:
        if PERFORMANCE_SERVICE.baseline.delete_baseline(baseline_name):
            return success_response(message='基线已删除')
        else:
            return error_response(
                message='基线不存在',
                error='baseline not found',
                status_code=404
            )
    except Exception as e:
        logger.error(f'删除基线失败: {e}', exc_info=True)
        return error_response(
            message='删除基线失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/baselines/<baseline_name>/compare', methods=['POST'])
def api_compare_baseline(baseline_name):
    """与基线对比"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'session_id', 'snapshot_index')
        if validation_error:
            return validation_error
        
        session_id = data['session_id']
        snapshot_index = int(data['snapshot_index'])
        
        # 获取快照
        session = PERFORMANCE_SERVICE.storage.get_session(session_id)
        if not session:
            return error_response(
                message='会话不存在',
                error='session not found',
                status_code=404
            )
        
        snapshots = session.get('snapshots', [])
        if snapshot_index >= len(snapshots):
            return error_response(
                message='快照索引超出范围',
                error='snapshot index out of range',
                status_code=400
            )
        
        snap_dict = snapshots[snapshot_index]
        
        # 转换为 PerformanceSnapshot
        processes = [
            ProcessSnapshot(
                pid=p['pid'],
                process_name=p['process_name'],
                cpu_usage=p['cpu_usage'],
                rss_kb=p['rss_kb'],
                pss_kb=p['pss_kb'],
                gc_count=p['gc_count']
            )
            for p in snap_dict.get('processes', [])
        ]
        
        snapshot = PerformanceSnapshot(
            timestamp=datetime.fromisoformat(snap_dict['timestamp']),
            total_pss=snap_dict['total_pss'],
            gc_count=snap_dict['gc_count'],
            cpu_usage=snap_dict['cpu_usage'],
            fps=snap_dict.get('fps', 0),
            jank_count=snap_dict.get('jank_count', 0),
            network_rx_kb=snap_dict.get('network_rx_kb', 0.0),
            network_tx_kb=snap_dict.get('network_tx_kb', 0.0),
            device_info=snap_dict.get('device_info', ''),
            processes=processes
        )
        
        comparison = PERFORMANCE_SERVICE.baseline.compare_with_baseline(baseline_name, snapshot)
        
        if 'error' in comparison:
            return error_response(
                message=comparison['error'],
                error='baseline not found',
                status_code=404
            )
        
        return success_response(data={'comparison': comparison})
    except Exception as e:
        logger.error(f'对比基线失败: {e}', exc_info=True)
        return error_response(
            message='对比基线失败',
            error=str(e),
            status_code=500
        )


@performance_monitor_bp.route('/api/status', methods=['GET'])
def api_get_status():
    """获取监控任务状态"""
    try:
        task_id = request.args.get('task_id')
        
        with PERFORMANCE_SERVICE_LOCK:
            if task_id:
                task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
                if not task_info:
                    return error_response(
                        message='任务不存在',
                        error='task not found',
                        status_code=404
                    )
                
                return success_response(data={
                    'task_id': task_id,
                    'device_id': task_info['device_id'],
                    'package_name': task_info['package_name'],
                    'session_id': task_info['session_id'],
                    'start_time': task_info['start_time'],
                    'running_time': int(time.time() - task_info['start_time']),
                    'is_running': True
                })
            else:
                tasks = PERFORMANCE_SERVICE.list_tasks()
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


# ==================== 性能告警相关 API ====================

@performance_monitor_bp.route('/api/alert-rules', methods=['GET'])
def api_get_alert_rules():
    """获取告警规则列表"""
    try:
        task_id = request.args.get('task_id')
        
        with PERFORMANCE_SERVICE_LOCK:
            task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
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
                        'metric': r.metric,
                        'operator': r.operator,
                        'threshold': r.threshold,
                        'severity': r.severity,
                        'enabled': r.enabled,
                        'description': r.description,
                        'duration': r.duration
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


@performance_monitor_bp.route('/api/alert-rules', methods=['POST'])
def api_create_alert_rule():
    """创建告警规则"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'task_id', 'name', 'metric', 'operator', 'threshold')
        if validation_error:
            return validation_error
        
        task_id = data.get('task_id')
        
        with PERFORMANCE_SERVICE_LOCK:
            task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
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
            
            rule = PerformanceAlertRule(
                id=data.get('id', f"rule_{int(time.time())}"),
                name=data['name'],
                metric=data['metric'],
                operator=data['operator'],
                threshold=float(data['threshold']),
                severity=data.get('severity', 'medium'),
                enabled=data.get('enabled', True),
                description=data.get('description', ''),
                duration=int(data.get('duration', 0))
            )
            
            if alert_engine.add_rule(rule):
                return success_response(
                    data={'rule': {
                        'id': rule.id,
                        'name': rule.name,
                        'metric': rule.metric,
                        'operator': rule.operator,
                        'threshold': rule.threshold,
                        'severity': rule.severity,
                        'enabled': rule.enabled,
                        'description': rule.description,
                        'duration': rule.duration
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


@performance_monitor_bp.route('/api/alert-rules/<rule_id>', methods=['DELETE'])
def api_delete_alert_rule(rule_id):
    """删除告警规则"""
    try:
        task_id = request.args.get('task_id')
        
        with PERFORMANCE_SERVICE_LOCK:
            task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
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


@performance_monitor_bp.route('/api/alerts', methods=['GET'])
def api_get_alerts():
    """获取告警列表"""
    try:
        task_id = request.args.get('task_id')
        severity = request.args.get('severity')
        acknowledged = request.args.get('acknowledged')
        limit = int(request.args.get('limit', 100))
        
        with PERFORMANCE_SERVICE_LOCK:
            task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return success_response(data={'alerts': []})
            
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


@performance_monitor_bp.route('/api/alerts/<alert_id>/acknowledge', methods=['POST'])
def api_acknowledge_alert(alert_id):
    """确认告警"""
    try:
        task_id = request.args.get('task_id')
        data = request.get_json() or {}
        user = data.get('user', 'system')
        
        with PERFORMANCE_SERVICE_LOCK:
            task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
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


@performance_monitor_bp.route('/stream_alerts')
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
        with PERFORMANCE_SERVICE_LOCK:
            task_info = PERFORMANCE_SERVICE.get_task_info(task_id)
            if not task_info:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                return
            
            alert_queue = task_info.get('alert_queue', [])
            alert_queue_lock = task_info.get('alert_queue_lock')
            last_index = 0
        
        try:
            while True:
                # 检查任务是否还在运行
                with PERFORMANCE_SERVICE_LOCK:
                    if task_id not in PERFORMANCE_SERVICE.tasks:
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
