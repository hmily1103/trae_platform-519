"""
UI自动化 Flask Views
提供UI自动化的 Web API
"""
import json
import threading
import time
from datetime import datetime
from flask import Blueprint, render_template, request, Response, stream_with_context
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from .service import UIAutomationService
from .storage import RecordingStorage

ui_automation_bp = Blueprint('ui_automation', __name__, template_folder='templates')
logger = setup_logger('ui_automation_module')

# 全局服务实例
UI_AUTOMATION_SERVICE = UIAutomationService()
UI_AUTOMATION_SERVICE_LOCK = threading.Lock()

def _is_device_not_connected_error(output: str) -> bool:
    s = (output or "").lower()
    return (
        "device not found" in s
        or "no devices/emulators found" in s
        or "no devices/emulators" in s
        or "device offline" in s
        or "offline" in s
        or "unauthorized" in s
    )


@ui_automation_bp.route('/')
@ui_automation_bp.route('/dashboard')
def index():
    """工作台"""
    return render_template('ui_automation_dashboard.html')


@ui_automation_bp.route('/recorder')
def recorder_page():
    """录制"""
    # 服务端预渲染设备列表，避免纯靠前端 JS 拉取导致下拉为空（网络/脚本异常时仍可见）
    initial_all_devices = []
    try:
        from utils.adb_helper import AdbHelper
        initial_all_devices = AdbHelper.get_devices_with_status() or []
    except Exception as e:
        logger.warning(f'预加载设备列表失败（不影响页面其余功能）: {e}')
        initial_all_devices = []
    return render_template(
        'ui_automation_recorder.html',
        initial_all_devices=initial_all_devices
    )


@ui_automation_bp.route('/cases')
def cases_page():
    """用例管理"""
    return render_template('ui_automation_cases.html')


@ui_automation_bp.route('/reports')
def reports_page():
    """执行记录"""
    return render_template('ui_automation_reports.html')


@ui_automation_bp.route('/ai')
def ai_page():
    """AI 探索生成用例（自然语言 / 分步用例）"""
    initial_all_devices = []
    try:
        from utils.adb_helper import AdbHelper
        initial_all_devices = AdbHelper.get_devices_with_status() or []
    except Exception as e:
        logger.warning(f'预加载设备列表失败: {e}')
    return render_template(
        'ui_automation_ai.html',
        initial_all_devices=initial_all_devices,
    )


@ui_automation_bp.route('/report/<job_id>')
def report_detail_page(job_id):
    """执行详情页"""
    return render_template('ui_automation_report_detail.html', job_id=job_id)


@ui_automation_bp.route('/api/trace', methods=['POST'])
def api_save_trace():
    """保存执行Trace"""
    try:
        data = request.get_json() or {}
        # 简单校验
        if not data.get('run_id') or not data.get('device_id'):
            return error_response(message='缺少必要参数 (run_id/device_id)')
            
        success = UI_AUTOMATION_SERVICE.save_execution_trace(data)
        if success:
            return success_response()
        else:
            return error_response(message='保存 Trace 失败')
    except Exception as e:
        logger.error(f"保存Trace异常: {e}")
        return error_response(message=str(e))


@ui_automation_bp.route('/api/trace/analyze', methods=['POST'])
def api_analyze_stability():
    """分析执行稳定性"""
    try:
        data = request.get_json() or {}
        run_ids = data.get('run_ids', [])
        
        if not run_ids:
            return error_response(message='缺少 run_ids 参数')
            
        report = UI_AUTOMATION_SERVICE.analyze_execution_stability(run_ids)
        return success_response(data=report)
    except Exception as e:
        logger.error(f"分析稳定性异常: {e}")
        return error_response(message=str(e))



@ui_automation_bp.route('/api/devices', methods=['GET'])
def api_get_devices():
    """获取设备列表（含所有连接状态的设备，便于前端标注需授权/离线）"""
    try:
        from modules.log_monitor.core.adb_controller import AdbController
        from utils.adb_helper import AdbHelper
        controller = AdbController()
        devices = controller.get_connected_devices()
        all_devices = AdbHelper.get_devices_with_status()
        logger.info(f'获取到 {len(devices)} 个可用设备，共 {len(all_devices)} 个连接设备')
        return success_response(data={'devices': devices, 'all_devices': all_devices})
    except Exception as e:
        logger.error(f'获取设备列表失败: {e}', exc_info=True)
        return error_response(
            message='获取设备列表失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/connect', methods=['POST'])
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
        
        # 先尝试启用ADB（通过HTTP）
        enable_success = False
        try:
            import urllib.request
            url = f"http://{ip}:2007/debug/adb?enable=1"
            urllib.request.urlopen(url, timeout=2)
            enable_success = True
            logger.info(f"ADB启用成功: {ip}:2007")
        except Exception as e:
            logger.warning(f"ADB启用失败（可能设备不支持HTTP启用）: {e}")
        
        # ADB连接
        success = controller.connect_device(ip, port)
        
        if success:
            # 验证连接
            devices = controller.get_connected_devices()
            device_id = f"{ip}:{port}"
            if device_id in devices:
                return success_response(
                    data={'device_id': device_id},
                    message=f'设备连接成功: {device_id}'
                )
            else:
                return error_response(
                    message='设备连接成功但未在设备列表中，请检查ADB状态',
                    error='device not in list',
                    status_code=400
                )
        else:
            # 获取详细错误信息
            import subprocess
            try:
                result = subprocess.run(
                    ["adb", "connect", f"{ip}:{port}"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                error_msg = result.stderr or result.stdout
            except:
                error_msg = "无法执行ADB命令"
            
            return error_response(
                message=f'设备连接失败，请检查：\n1. 设备IP和端口是否正确\n2. 设备是否已开启ADB调试\n3. 设备与电脑是否在同一网络\n4. ADB服务是否正常运行\n\n错误详情: {error_msg}',
                error='connection failed',
                status_code=400
            )
    except Exception as e:
        logger.error(f'连接设备失败: {e}', exc_info=True)
        return error_response(
            message=f'连接设备失败: {str(e)}',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/video/start', methods=['POST'])
def api_start_video():
    """启动视频流"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id')
        if validation_error:
            return validation_error
        
        device_id = data.get('device_id')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            if UI_AUTOMATION_SERVICE.start_video_stream(device_id):
                return success_response(message='视频流已启动')
            else:
                return error_response(
                    message='启动视频流失败',
                    error='start failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'启动视频流失败: {e}', exc_info=True)
        return error_response(
            message='启动视频流失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/stream_video')
def stream_video():
    """WebSocket视频流（SSE方式）"""
    device_id = request.args.get('device_id')
    
    if not device_id:
        return error_response(
            message='缺少设备ID',
            error='device_id required',
            status_code=400
        )
    
    def generate():
        """生成视频流"""
        frame_queue = []
        frame_lock = threading.Lock()
        
        def video_callback(image_base64):
            """视频帧回调"""
            with frame_lock:
                frame_queue.append(image_base64)
        
        # 启动视频流
        with UI_AUTOMATION_SERVICE_LOCK:
            UI_AUTOMATION_SERVICE.start_video_stream(device_id, callback=video_callback)
        
        try:
            while True:
                # 检查任务是否还在运行
                # 优化：移除循环中的全局锁，直接检查 video_tasks
                # Python 字典的 'in' 操作是原子性的，且此处只读，风险极低
                if device_id not in UI_AUTOMATION_SERVICE.video_tasks:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    break
                
                # 获取新帧
                with frame_lock:
                    if frame_queue:
                        frame = frame_queue.pop(0)
                        # 只保留最新一帧
                        frame_queue.clear()
                    else:
                        frame = None
                
                if frame:
                    yield f"data: {json.dumps({'type': 'frame', 'data': frame})}\n\n"
                
                time.sleep(0.1)  # 控制发送频率
                
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f'视频流错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            # 停止视频流
            with UI_AUTOMATION_SERVICE_LOCK:
                UI_AUTOMATION_SERVICE.stop_video_stream(device_id)
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@ui_automation_bp.route('/api/device/screenshot/latest', methods=['GET'])
def api_device_screenshot_latest():
    """
    获取最新设备截图 (独立刷新接口)
    用于前端轮询预览，与控制操作解耦
    返回 image + device_width/height，供点击坐标映射。
    force=1（默认）强制重截，避免返回点击前的旧画面。
    """
    device_id = (request.args.get('device_id') or '').replace('：', ':').strip()
    if not device_id:
        return error_response(message='缺少 device_id 参数', status_code=400)

    force_raw = (request.args.get('force') or '1').strip().lower()
    force = force_raw not in ('0', 'false', 'no')
    
    try:
        if force:
            UI_AUTOMATION_SERVICE.invalidate_preview_cache(device_id)
        payload = UI_AUTOMATION_SERVICE.get_latest_screenshot_payload(device_id, force=force)
        
        if payload and payload.get('image'):
            return success_response(data=payload)

        # 附带控制器错误信息，方便前端展示
        detail = ''
        try:
            controller = UI_AUTOMATION_SERVICE.get_device_controller(device_id)
            detail = (controller.last_output or '').strip()
        except Exception:
            detail = ''
        msg = '获取截图失败' + (f'：{detail}' if detail else '')
        return error_response(message=msg, status_code=500)
    except Exception as e:
        logger.error(f"获取最新截图失败: {e}", exc_info=True)
        return error_response(message='获取最新截图失败', error=str(e), status_code=500)


# --- Suite API ---

@ui_automation_bp.route('/api/suites', methods=['GET'])
def api_list_suites():
    """列出测试套件"""
    try:
        project_id = request.args.get('project_id')
        suites = UI_AUTOMATION_SERVICE.list_suites(project_id)
        return success_response(data={'suites': [s.to_dict() for s in suites]})
    except Exception as e:
        logger.error(f'获取测试套件列表失败: {e}', exc_info=True)
        return error_response(message='获取测试套件列表失败', error=str(e))

@ui_automation_bp.route('/api/suites', methods=['POST'])
def api_create_suite():
    """创建测试套件"""
    try:
        data = request.json or {}
        name = data.get('name')
        project_id = data.get('project_id')
        case_ids = data.get('case_ids', [])
        description = data.get('description', '')
        
        if not name or not project_id:
            return error_response(message='名称和项目ID不能为空')
            
        suite = UI_AUTOMATION_SERVICE.create_suite(name, project_id, case_ids, description)
        if suite:
            return success_response(data={'suite': suite.to_dict()})
        return error_response(message='创建失败')
    except Exception as e:
        logger.error(f'创建测试套件失败: {e}', exc_info=True)
        return error_response(message='创建测试套件失败', error=str(e))

@ui_automation_bp.route('/api/suite/<suite_id>', methods=['GET'])
def api_get_suite(suite_id):
    """获取测试套件"""
    try:
        suite = UI_AUTOMATION_SERVICE.get_suite(suite_id)
        if suite:
            return success_response(data={'suite': suite.to_dict()})
        return error_response(message='未找到测试套件', status_code=404)
    except Exception as e:
        logger.error(f'获取测试套件失败: {e}', exc_info=True)
        return error_response(message='获取测试套件失败', error=str(e))

@ui_automation_bp.route('/api/suite/<suite_id>', methods=['PUT'])
def api_update_suite(suite_id):
    """更新测试套件"""
    try:
        data = request.json or {}
        if UI_AUTOMATION_SERVICE.update_suite(
            suite_id,
            name=data.get('name'),
            case_ids=data.get('case_ids'),
            description=data.get('description')
        ):
            return success_response(message='更新成功')
        return error_response(message='更新失败')
    except Exception as e:
        logger.error(f'更新测试套件失败: {e}', exc_info=True)
        return error_response(message='更新测试套件失败', error=str(e))

@ui_automation_bp.route('/api/suite/<suite_id>', methods=['DELETE'])
def api_delete_suite(suite_id):
    """删除测试套件"""
    try:
        if UI_AUTOMATION_SERVICE.delete_suite(suite_id):
            return success_response(message='删除成功')
        return error_response(message='删除失败')
    except Exception as e:
        logger.error(f'删除测试套件失败: {e}', exc_info=True)
        return error_response(message='删除测试套件失败', error=str(e))

@ui_automation_bp.route('/api/suite/<suite_id>/run', methods=['POST'])
def api_run_suite(suite_id):
    """运行测试套件"""
    try:
        data = request.json or {}
        device_id = data.get('device_id')
        
        if not device_id:
            return error_response(message='设备ID不能为空')
            
        precision_context = {
            'precision_analysis_id': (data.get('precision_analysis_id') or request.args.get('precision_analysis_id') or '').strip(),
            'precision_test_point_id': (data.get('precision_test_point_id') or request.args.get('precision_test_point_id') or '').strip(),
            'precision_execution_id': (data.get('precision_execution_id') or request.args.get('precision_execution_id') or '').strip(),
        }
        job_id = UI_AUTOMATION_SERVICE.run_suite(suite_id, device_id, precision_context=precision_context)
        if job_id:
            return success_response(data={'job_id': job_id})
        return error_response(message='启动失败')
    except Exception as e:
        logger.error(f'运行测试套件失败: {e}', exc_info=True)
        return error_response(message='运行测试套件失败', error=str(e))


@ui_automation_bp.route('/api/case/quick-run', methods=['POST'])
def api_quick_run_case():
    """快速执行单个用例"""
    try:
        data = request.json or {}
        case_id = data.get('case_id')
        device_ids = data.get('device_ids')

        if not case_id:
            return error_response(message='请选择用例')
        if not device_ids:
            return error_response(message='请选择执行设备')

        precision_context = {
            'precision_analysis_id': (data.get('precision_analysis_id') or request.args.get('precision_analysis_id') or '').strip(),
            'precision_test_point_id': (data.get('precision_test_point_id') or request.args.get('precision_test_point_id') or '').strip(),
            'precision_execution_id': (data.get('precision_execution_id') or request.args.get('precision_execution_id') or '').strip(),
        }
        job_id = UI_AUTOMATION_SERVICE.quick_run_case(case_id, device_ids, precision_context=precision_context)
        if not job_id:
            return error_response(message='用例不存在或执行失败')
        return success_response(data={'job_id': job_id})
    except Exception as e:
        logger.error(f'快速执行用例失败: {e}', exc_info=True)
        return error_response(message='执行失败', error=str(e))


@ui_automation_bp.route('/api/suite/jobs/running', methods=['GET'])
def api_get_running_suite_jobs():
    """获取运行中的套件任务，用于页面切换后恢复"""
    try:
        jobs = UI_AUTOMATION_SERVICE.get_running_suite_jobs()
        return success_response(data={'jobs': jobs})
    except Exception as e:
        logger.error(f'获取运行中任务失败: {e}', exc_info=True)
        return error_response(message='获取失败', error=str(e))


@ui_automation_bp.route('/api/suite/job/<job_id>', methods=['GET'])
def api_get_suite_job(job_id):
    """获取套件任务状态"""
    try:
        status = UI_AUTOMATION_SERVICE.get_suite_job_status(job_id)
        if status:
            return success_response(data={'status': status})
        return error_response(message='未找到任务', status_code=404)
    except Exception as e:
        logger.error(f'获取任务状态失败: {e}', exc_info=True)
        return error_response(message='获取任务状态失败', error=str(e))


@ui_automation_bp.route('/api/suite/job/<job_id>/stop', methods=['POST'])
def api_stop_suite_job(job_id):
    """停止套件任务"""
    try:
        if UI_AUTOMATION_SERVICE.stop_suite_job(job_id):
            return success_response(message='任务已停止')
        return error_response(message='停止失败，任务不存在')
    except Exception as e:
        logger.error(f'停止任务失败: {e}', exc_info=True)
        return error_response(message='停止任务失败', error=str(e))


@ui_automation_bp.route('/api/video/stop', methods=['POST'])
def api_stop_video():
    """停止视频流"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id')
        if validation_error:
            return validation_error
        
        device_id = data.get('device_id')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            if UI_AUTOMATION_SERVICE.stop_video_stream(device_id):
                return success_response(message='视频流已停止')
            else:
                return error_response(
                    message='停止视频流失败',
                    error='stop failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'停止视频流失败: {e}', exc_info=True)
        return error_response(
            message='停止视频流失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/control/click', methods=['POST'])
def api_control_click():
    """点击控制"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id', 'x', 'y')
        if validation_error:
            return validation_error
        
        device_id = (data.get('device_id') or '').replace('：', ':').strip()
        x = int(data.get('x'))
        y = int(data.get('y'))
        
        # 使用共享的设备控制器
        with UI_AUTOMATION_SERVICE_LOCK:
            controller = UI_AUTOMATION_SERVICE.get_device_controller(device_id)
        
        if controller.click(x, y):
            try:
                UI_AUTOMATION_SERVICE.invalidate_preview_cache(device_id)
            except Exception:
                pass
            return success_response(message='点击成功')
        else:
            if _is_device_not_connected_error(controller.last_output):
                return error_response(
                    message=f'设备未连接: {device_id}\n请先连接设备',
                    error='device not connected',
                    status_code=400
                )
            return error_response(
                message=f'点击失败: {controller.last_output or "未知原因"}',
                error=controller.last_output or 'click failed',
                status_code=500
            )
    except Exception as e:
        logger.error(f'点击控制失败: {e}', exc_info=True)
        return error_response(
            message='点击控制失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/control/swipe', methods=['POST'])
def api_control_swipe():
    """滑动控制"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id', 'x1', 'y1', 'x2', 'y2')
        if validation_error:
            return validation_error
        
        device_id = (data.get('device_id') or '').replace('：', ':').strip()
        x1 = int(data.get('x1'))
        y1 = int(data.get('y1'))
        x2 = int(data.get('x2'))
        y2 = int(data.get('y2'))
        duration = int(data.get('duration', 300))
        
        # 使用共享的设备控制器
        with UI_AUTOMATION_SERVICE_LOCK:
            controller = UI_AUTOMATION_SERVICE.get_device_controller(device_id)
        
        if controller.swipe(x1, y1, x2, y2, duration):
            try:
                UI_AUTOMATION_SERVICE.invalidate_preview_cache(device_id)
            except Exception:
                pass
            return success_response(message='滑动成功')
        else:
            if _is_device_not_connected_error(controller.last_output):
                return error_response(
                    message=f'设备未连接: {device_id}\n请先连接设备',
                    error='device not connected',
                    status_code=400
                )
            return error_response(
                message=f'滑动失败: {controller.last_output or "未知原因"}',
                error=controller.last_output or 'swipe failed',
                status_code=500
            )
    except Exception as e:
        logger.error(f'滑动控制失败: {e}', exc_info=True)
        return error_response(
            message='滑动控制失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/control/input', methods=['POST'])
def api_control_input():
    """输入控制"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id', 'text')
        if validation_error:
            return validation_error
        
        device_id = data.get('device_id')
        text = data.get('text')
        
        # 使用共享的设备控制器
        with UI_AUTOMATION_SERVICE_LOCK:
            controller = UI_AUTOMATION_SERVICE.get_device_controller(device_id)
        
        if controller.input_text(text):
            return success_response(message='输入成功')
        else:
            if _is_device_not_connected_error(controller.last_output):
                return error_response(
                    message=f'设备未连接: {device_id}\n请先连接设备',
                    error='device not connected',
                    status_code=400
                )
            return error_response(
                message=f'输入失败: {controller.last_output or "未知原因"}',
                error=controller.last_output or 'input failed',
                status_code=500
            )
    except Exception as e:
        logger.error(f'输入控制失败: {e}', exc_info=True)
        return error_response(
            message='输入控制失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/control/key', methods=['POST'])
def api_control_key():
    """按键控制"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id', 'key_code')
        if validation_error:
            return validation_error
        
        device_id = data.get('device_id')
        key_code = data.get('key_code')
        
        # 使用共享的设备控制器
        with UI_AUTOMATION_SERVICE_LOCK:
            controller = UI_AUTOMATION_SERVICE.get_device_controller(device_id)
        
        if controller.press_key(key_code):
            try:
                UI_AUTOMATION_SERVICE.invalidate_preview_cache(device_id)
            except Exception:
                pass
            return success_response(message='按键成功')
        else:
            if _is_device_not_connected_error(controller.last_output):
                return error_response(
                    message=f'设备未连接: {device_id}\n请先连接设备',
                    error='device not connected',
                    status_code=400
                )
            return error_response(
                message=f'按键失败: {controller.last_output or "未知原因"}',
                error=controller.last_output or 'key press failed',
                status_code=500
            )
    except Exception as e:
        logger.error(f'按键控制失败: {e}', exc_info=True)
        return error_response(
            message='按键控制失败',
            error=str(e),
            status_code=500
        )


def _normalize_device_id(device_id: str) -> str:
    """规范化设备ID（全角冒号转半角）"""
    if not device_id:
        return device_id
    return device_id.replace('：', ':').strip()


@ui_automation_bp.route('/api/recording/start', methods=['POST'])
def api_start_recording():
    """开始录制"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id')
        if validation_error:
            return validation_error
        
        device_id = _normalize_device_id(data.get('device_id', ''))
        recording_id = data.get('recording_id', f"recording_{int(time.time())}")
        package_name = data.get('package_name', '')
        description = data.get('description', '')
        project_id = data.get('project_id', '')
        name = data.get('name', '')
        
        # 预先检查设备是否在连接列表中
        try:
            from modules.log_monitor.core.adb_controller import AdbController
            adb = AdbController()
            connected = adb.get_connected_devices()
            if device_id not in connected:
                return error_response(
                    message=f'设备未连接: {device_id}\n请先连接设备（当前已连接: {", ".join(connected) if connected else "无"}）',
                    error='device not connected',
                    status_code=400
                )
        except Exception as e:
            logger.warning(f"预检设备失败: {e}")
        
        with UI_AUTOMATION_SERVICE_LOCK:
            try:
                started = UI_AUTOMATION_SERVICE.start_recording(
                    device_id=device_id,
                    recording_id=recording_id,
                    package_name=package_name,
                    description=description,
                    project_id=project_id,
                    name=name
                )
            except Exception as e:
                logger.error(f'开始录制失败: {e}', exc_info=True)
                if _is_device_not_connected_error(str(e)):
                    return error_response(
                        message=f'设备未连接: {device_id}\n请先连接设备',
                        error='device not connected',
                        status_code=400
                    )
                return error_response(
                    message=f'开始录制失败: {str(e)}',
                    error=str(e),
                    status_code=500
                )

            if started:
                return success_response(
                    data={'recording_id': recording_id},
                    message='录制已开始'
                )

            return error_response(
                message=f'开始录制失败: 录制ID已存在 ({recording_id})，请先停止当前录制',
                error='recording already exists',
                status_code=400
            )
    except Exception as e:
        logger.error(f'开始录制失败: {e}', exc_info=True)
        return error_response(
            message='开始录制失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/stop', methods=['POST'])
def api_stop_recording():
    """停止录制"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        save = data.get('save', True)
        
        with UI_AUTOMATION_SERVICE_LOCK:
            recorder = UI_AUTOMATION_SERVICE.get_recorder(recording_id)
            action_count = len(recorder.session.actions) if recorder and getattr(recorder, 'session', None) else 0
            if UI_AUTOMATION_SERVICE.stop_recording(recording_id, save=save):
                return success_response(message='录制已停止', data={'action_count': action_count})
            return error_response(
                message=f'停止录制失败: 录制不存在或已停止 ({recording_id})',
                error='recording not running',
                status_code=400
            )
    except Exception as e:
        logger.error(f'停止录制失败: {e}', exc_info=True)
        return error_response(
            message='停止录制失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/list', methods=['GET'])
def api_list_recordings():
    """列出所有录制"""
    try:
        project_id = request.args.get('project_id')
        recordings = UI_AUTOMATION_SERVICE.storage.list_recordings(project_id=project_id)
        return success_response(data={'recordings': recordings})
    except Exception as e:
        logger.error(f'获取录制列表失败: {e}', exc_info=True)
        return error_response(
            message='获取录制列表失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/<recording_id>', methods=['GET'])
def api_get_recording(recording_id):
    """获取录制详情"""
    try:
        session = UI_AUTOMATION_SERVICE.storage.load_recording(recording_id)
        if not session:
            return error_response(
                message='录制不存在',
                error='recording not found',
                status_code=404
            )

        recording_dict = session.to_dict()
        recording_dict['insights'] = UI_AUTOMATION_SERVICE.get_case_insights(recording_id) or {}
        artifact_step_count = UI_AUTOMATION_SERVICE.storage.get_artifact_step_count(recording_id)
        if len(recording_dict.get('actions') or []) == 0 and artifact_step_count > 0:
            recording_dict['artifact_step_count'] = artifact_step_count
            recording_dict['warning'] = '录制步骤未写入，发现截图/UI树文件，可能是录制时发生异常'

        return success_response(data={'recording': recording_dict})
    except Exception as e:
        logger.error(f'获取录制详情失败: {e}', exc_info=True)
        return error_response(
            message='获取录制详情失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/<recording_id>/insights', methods=['GET'])
def api_get_recording_insights(recording_id):
    """获取用例治理洞察：质量体检 + 最近失败归因"""
    try:
        insights = UI_AUTOMATION_SERVICE.get_case_insights(recording_id)
        if insights is None:
            return error_response(
                message='录制不存在',
                error='recording not found',
                status_code=404
            )
        return success_response(data={'insights': insights})
    except Exception as e:
        logger.error(f'获取用例洞察失败: {e}', exc_info=True)
        return error_response(
            message='获取用例洞察失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/<recording_id>', methods=['DELETE'])
def api_delete_recording(recording_id):
    """删除录制"""
    try:
        # Use service method to ensure recording is stopped before deletion
        # Add lock for thread safety
        with UI_AUTOMATION_SERVICE_LOCK:
            if UI_AUTOMATION_SERVICE.delete_recording(recording_id):
                return success_response(message='录制已删除')
            else:
                return error_response(
                    message='删除录制失败',
                    error='delete failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'删除录制失败: {e}', exc_info=True)
        return error_response(
            message='删除录制失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/click', methods=['POST'])
def api_recording_click():
    """录制点击操作"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id', 'x', 'y')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        x = int(data.get('x'))
        y = int(data.get('y'))
        description = data.get('description', '')
        
        # 优化：只在获取 recorder 时加锁
        recorder = None
        with UI_AUTOMATION_SERVICE_LOCK:
            recorder = UI_AUTOMATION_SERVICE.get_recorder(recording_id)
        
        if not recorder:
            return error_response(
                message=f'录制点击失败: 录制未开始或已停止 ({recording_id})',
                error='recording not running',
                status_code=400
            )
        if not getattr(recorder, 'is_recording', False):
            return error_response(
                message=f'录制点击失败: 录制器状态异常 ({recording_id})',
                error='recorder not recording',
                status_code=400
            )
            
        # 调用 recorder.record_click (内部已实现异步和瞬发)
        # 注意：ActionRecorder 已重构为内部处理异步逻辑，这里只需调用即可
        if recorder.record_click(x, y, description):
            # 立即返回，告知前端已接收指令
            return success_response(
                message='点击指令已下发，后台正在分析界面...',
                data={'async': True}
            )
        else:
            return error_response(
                message=f'点击执行失败: {getattr(recorder, "last_error", "")}',
                error='click failed',
                status_code=500
            )

    except Exception as e:
        logger.error(f'录制点击失败: {e}', exc_info=True)
        return error_response(
            message='录制点击失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/step', methods=['POST'])
def api_recording_step():
    """
    异步录制步骤 (纯录制，不操作设备)
    配合 /api/control/click 使用，实现"控制"与"录制"的完全解耦
    """
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id', 'x', 'y')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        x = int(data.get('x'))
        y = int(data.get('y'))
        description = data.get('description', '')
        action_type = data.get('action_type', 'click')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            recorder = UI_AUTOMATION_SERVICE.get_recorder(recording_id)
            if not recorder:
                return error_response(
                    message=f'录制步骤失败: 录制未开始 ({recording_id})',
                    error='recording not running',
                    status_code=400
                )
            
            if UI_AUTOMATION_SERVICE.record_step_async(recording_id, x, y, description, action_type):
                return success_response(
                    message='步骤已加入处理队列',
                    data={'async': True}
                )
            else:
                return error_response(
                    message='录制步骤失败',
                    error='record failed',
                    status_code=500
                )

    except Exception as e:
        logger.error(f'录制步骤失败: {e}', exc_info=True)
        return error_response(
            message='录制步骤失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/swipe', methods=['POST'])
def api_recording_swipe():
    """录制滑动操作"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id', 'x1', 'y1', 'x2', 'y2')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        x1 = int(data.get('x1'))
        y1 = int(data.get('y1'))
        x2 = int(data.get('x2'))
        y2 = int(data.get('y2'))
        duration = int(data.get('duration', 300))
        description = data.get('description', '')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            recorder = UI_AUTOMATION_SERVICE.get_recorder(recording_id)
            if not recorder:
                return error_response(
                    message=f'录制滑动失败: 录制未开始 ({recording_id})',
                    error='recording not running',
                    status_code=400
                )
            
            if recorder.record_swipe(x1, y1, x2, y2, duration, description):
                return success_response(message='滑动已录制')
            else:
                return error_response(
                    message=f'滑动录制失败: {recorder.last_error}',
                    error='record failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'录制滑动失败: {e}', exc_info=True)
        return error_response(
            message='录制滑动失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/key', methods=['POST'])
def api_recording_key():
    """录制按键操作"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id', 'key_code')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        key_code = data.get('key_code')
        description = data.get('description', '')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            recorder = UI_AUTOMATION_SERVICE.get_recorder(recording_id)
            if not recorder:
                return error_response(
                    message=f'录制按键失败: 录制未开始 ({recording_id})',
                    error='recording not running',
                    status_code=400
                )
            
            if recorder.record_key(key_code, description):
                return success_response(message='按键已录制')
            else:
                return error_response(
                    message=f'按键录制失败: {recorder.last_error}',
                    error='record failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'录制按键失败: {e}', exc_info=True)
        return error_response(
            message='录制按键失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/input', methods=['POST'])
def api_recording_input():
    """录制输入操作"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id', 'x', 'y', 'text')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        x = int(data.get('x'))
        y = int(data.get('y'))
        text = data.get('text')
        description = data.get('description', '')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            recorder = UI_AUTOMATION_SERVICE.get_recorder(recording_id)
            if not recorder:
                return error_response(
                    message=f'录制输入失败: 录制未开始或已停止 ({recording_id})',
                    error='recording not running',
                    status_code=400
                )
            if not getattr(recorder, 'is_recording', False):
                return error_response(
                    message=f'录制输入失败: 录制器状态异常 ({recording_id})',
                    error='recorder not recording',
                    status_code=400
                )
            if UI_AUTOMATION_SERVICE.record_input(recording_id, x, y, text, description):
                return success_response(message='输入已录制')
            return error_response(
                message=f'录制输入失败: {getattr(recorder, "last_error", "") or "未知原因"}',
                error='record failed',
                status_code=500
            )
    except Exception as e:
        logger.error(f'录制输入失败: {e}', exc_info=True)
        return error_response(
            message='录制输入失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/assertion', methods=['POST'])
def api_recording_assertion():
    """录制断言操作"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id', 'x', 'y', 'type')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        x = int(data.get('x'))
        y = int(data.get('y'))
        assertion_type = data.get('type')
        expected_value = data.get('expected', '')
        description = data.get('description', '')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            recorder = UI_AUTOMATION_SERVICE.get_recorder(recording_id)
            if not recorder:
                return error_response(
                    message=f'录制断言失败: 录制未开始或已停止 ({recording_id})',
                    error='recording not running',
                    status_code=400
                )
            if not getattr(recorder, 'is_recording', False):
                return error_response(
                    message=f'录制断言失败: 录制器状态异常 ({recording_id})',
                    error='recorder not recording',
                    status_code=400
                )
            if UI_AUTOMATION_SERVICE.record_assertion(recording_id, x, y, assertion_type, expected_value, description):
                return success_response(message='断言已录制')
            return error_response(
                message=f'录制断言失败: {getattr(recorder, "last_error", "") or "未知原因"}',
                error='record failed',
                status_code=500
            )
    except Exception as e:
        logger.error(f'录制断言失败: {e}', exc_info=True)
        return error_response(
            message='录制断言失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/recording/action', methods=['DELETE'])
def api_delete_recording_action():
    """删除录制操作"""
    try:
        data = request.get_json() or {}
        recording_id = data.get('recording_id')
        index = data.get('index')
        
        if not recording_id or index is None:
            return error_response(message='缺少必要参数')
            
        with UI_AUTOMATION_SERVICE_LOCK:
            if UI_AUTOMATION_SERVICE.delete_recording_action(recording_id, int(index)):
                return success_response(message='删除成功')
            else:
                return error_response(message='删除失败')
    except Exception as e:
        logger.error(f'删除录制操作失败: {e}', exc_info=True)
        return error_response(message='删除录制操作失败', error=str(e))


@ui_automation_bp.route('/api/recording/action', methods=['PUT'])
def api_update_recording_action():
    """更新录制操作"""
    try:
        data = request.get_json() or {}
        recording_id = data.get('recording_id')
        index = data.get('index')
        
        if not recording_id or index is None:
            return error_response(message='缺少必要参数')
            
        action_type = data.get('action_type')
        value = data.get('value')
        description = data.get('description')
        wait_after = data.get('wait_after')
        
        with UI_AUTOMATION_SERVICE_LOCK:
            if UI_AUTOMATION_SERVICE.update_recording_action(
                recording_id, int(index), action_type, value, description, wait_after
            ):
                return success_response(message='更新成功')
            else:
                return error_response(message='更新失败')
    except Exception as e:
        logger.error(f'更新录制操作失败: {e}', exc_info=True)
        return error_response(message='更新录制操作失败', error=str(e))


# --- Project APIs ---

@ui_automation_bp.route('/api/projects', methods=['GET'])
def api_list_projects():
    """获取项目列表"""
    try:
        projects = UI_AUTOMATION_SERVICE.list_projects()
        return success_response(data={'projects': [p.to_dict() for p in projects]})
    except Exception as e:
        logger.error(f'获取项目列表失败: {e}', exc_info=True)
        return error_response(message='获取项目列表失败', error=str(e))

@ui_automation_bp.route('/api/projects', methods=['POST'])
def api_create_project():
    """创建项目"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'name')
        if validation_error:
            return validation_error
        
        name = data.get('name')
        description = data.get('description', '')
        
        project = UI_AUTOMATION_SERVICE.create_project(name, description)
        if project:
            return success_response(data={'project': project.to_dict()}, message='项目创建成功')
        else:
            return error_response(message='项目创建失败', error='创建失败')
    except Exception as e:
        logger.error(f'创建项目失败: {e}', exc_info=True)
        return error_response(message='创建项目失败', error=str(e))

@ui_automation_bp.route('/api/projects/<project_id>', methods=['GET'])
def api_get_project(project_id):
    """获取项目详情"""
    try:
        project = UI_AUTOMATION_SERVICE.get_project(project_id)
        if project:
            return success_response(data={'project': project.to_dict()})
        return error_response(message='项目不存在', status_code=404)
    except Exception as e:
        logger.error(f'获取项目失败: {e}', exc_info=True)
        return error_response(message='获取项目失败', error=str(e))

@ui_automation_bp.route('/api/projects/<project_id>', methods=['DELETE'])
def api_delete_project(project_id):
    """删除项目"""
    try:
        if UI_AUTOMATION_SERVICE.delete_project(project_id):
            return success_response(message='项目删除成功')
        return error_response(message='项目删除失败', error='删除失败')
    except Exception as e:
        logger.error(f'删除项目失败: {e}', exc_info=True)
        return error_response(message='删除项目失败', error=str(e))


@ui_automation_bp.route('/api/screenshot', methods=['GET'])
def api_screenshot():
    """获取设备截图"""
    try:
        device_id = request.args.get('device_id')
        if not device_id:
            return error_response(
                message='缺少设备ID',
                error='device_id required',
                status_code=400
            )
        
        from .core.device_controller import DeviceController
        import tempfile
        import os
        
        # 使用共享的设备控制器
        with UI_AUTOMATION_SERVICE_LOCK:
            controller = UI_AUTOMATION_SERVICE.get_device_controller(device_id)

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        temp_path = temp_file.name
        temp_file.close()
        
        # 尝试截图
        if controller.screenshot(temp_path):
            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                from flask import send_file
                response = send_file(temp_path, mimetype='image/png')
                # 设置缓存头，避免浏览器缓存
                response.cache_control.no_cache = True
                return response
            else:
                return error_response(
                    message='截图文件不存在或为空',
                    error='screenshot file invalid',
                    status_code=500
                )
        else:
            # 检查设备是否连接
            from modules.log_monitor.core.adb_controller import AdbController
            adb_controller = AdbController()
            devices = adb_controller.get_connected_devices()
            
            if device_id not in devices:
                return error_response(
                    message=f'设备未连接: {device_id}\n请先连接设备',
                    error='device not connected',
                    status_code=400
                )
            else:
                return error_response(
                    message='截图失败，请检查设备状态',
                    error='screenshot failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'获取截图失败: {e}', exc_info=True)
        return error_response(
            message=f'获取截图失败: {str(e)}',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/script/generate', methods=['POST'])
def api_generate_script():
    """生成脚本"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'recording_id')
        if validation_error:
            return validation_error
        
        recording_id = data.get('recording_id')
        device_id = data.get('device_id')
        session = UI_AUTOMATION_SERVICE.storage.load_recording(recording_id)
        if not session:
            return error_response(
                message='录制不存在',
                error='recording not found',
                status_code=404
            )
        if (session.platform or 'android') != 'android':
            return error_response(
                message='当前仅支持为 Android 用例生成脚本，Web 用例执行器请后续接入 Playwright',
                error='unsupported platform',
                status_code=400
            )
        
        script_content = UI_AUTOMATION_SERVICE.generate_script(recording_id, device_id)
        
        if script_content:
            return success_response(data={'script': script_content})
        else:
            return error_response(
                message='生成脚本失败',
                error='generate failed',
                status_code=500
            )
    except Exception as e:
        logger.error(f'生成脚本失败: {e}', exc_info=True)
        return error_response(
            message='生成脚本失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/script/execute', methods=['POST'])
def api_execute_script():
    """执行脚本"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'script', 'device_id')
        if validation_error:
            return validation_error
        
        script_content = data.get('script')
        device_id = data.get('device_id')
        execution_id = data.get('execution_id')
        
        # 创建输出队列（用于SSE推送）
        output_queue = []
        output_lock = threading.Lock()
        
        def output_callback(line: str):
            """输出回调"""
            with output_lock:
                output_queue.append(line)
        
        execution_id = UI_AUTOMATION_SERVICE.execute_script(
            script_content=script_content,
            device_id=device_id,
            execution_id=execution_id,
            output_callback=output_callback
        )
        
        # 保存输出队列到任务信息（用于SSE）
        with UI_AUTOMATION_SERVICE_LOCK:
            if execution_id not in UI_AUTOMATION_SERVICE.script_executor.executions:
                return error_response(
                    message='执行启动失败',
                    error='execute failed',
                    status_code=500
                )
            
            UI_AUTOMATION_SERVICE.script_executor.executions[execution_id]['output_queue'] = output_queue
            UI_AUTOMATION_SERVICE.script_executor.executions[execution_id]['output_lock'] = output_lock
        
        return success_response(
            data={'execution_id': execution_id},
            message='脚本执行已启动'
        )
    except Exception as e:
        logger.error(f'执行脚本失败: {e}', exc_info=True)
        return error_response(
            message='执行脚本失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/script/<execution_id>/stop', methods=['POST'])
def api_stop_execution(execution_id):
    """停止脚本执行"""
    try:
        with UI_AUTOMATION_SERVICE_LOCK:
            if UI_AUTOMATION_SERVICE.stop_execution(execution_id):
                return success_response(message='脚本执行已停止')
            else:
                return error_response(
                    message='停止执行失败',
                    error='stop failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'停止执行失败: {e}', exc_info=True)
        return error_response(
            message='停止执行失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/script/<execution_id>/status', methods=['GET'])
def api_get_execution_status(execution_id):
    """获取执行状态"""
    try:
        status = UI_AUTOMATION_SERVICE.get_execution_status(execution_id)
        if status:
            return success_response(data={'status': status})
        else:
            return error_response(
                message='执行不存在',
                error='execution not found',
                status_code=404
            )
    except Exception as e:
        logger.error(f'获取执行状态失败: {e}', exc_info=True)
        return error_response(
            message='获取执行状态失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/report/<job_id>/detail', methods=['GET'])
def api_report_detail(job_id):
    """获取执行报告详情（含完整 trace）"""
    try:
        detail = UI_AUTOMATION_SERVICE.get_report_detail(job_id)
        if detail is None:
            return error_response(
                message='报告不存在',
                error='report not found',
                status_code=404
            )
        return success_response(data={'detail': detail})
    except Exception as e:
        logger.error(f'获取报告详情失败: {e}', exc_info=True)
        return error_response(message='获取失败', error=str(e))


@ui_automation_bp.route('/api/reports', methods=['GET'])
def api_list_reports():
    """获取所有执行报告"""
    try:
        reports = UI_AUTOMATION_SERVICE.storage.list_reports()
        return success_response(data={'reports': reports})
    except Exception as e:
        logger.error(f'获取执行报告失败: {e}', exc_info=True)
        return error_response(message='获取失败', error=str(e))


@ui_automation_bp.route('/api/cases/execution-status', methods=['POST'])
def api_cases_execution_status():
    """批量查询用例最近执行状态"""
    try:
        data = request.json or {}
        case_ids = data.get('case_ids', [])
        if not case_ids:
            return success_response(data={'status_map': {}})
        status_map = UI_AUTOMATION_SERVICE.get_cases_execution_status(case_ids)
        return success_response(data={'status_map': status_map})
    except Exception as e:
        logger.error(f'查询用例执行状态失败: {e}', exc_info=True)
        return error_response(message='查询失败', error=str(e))


@ui_automation_bp.route('/api/stats', methods=['GET'])
def api_dashboard_stats():
    """获取工作台统计数据"""
    try:
        # 获取统计数据
        stats = UI_AUTOMATION_SERVICE.get_dashboard_stats()
        return success_response(data=stats)
    except Exception as e:
        logger.error(f'获取工作台数据失败: {e}', exc_info=True)
        return error_response(
            message='获取工作台数据失败',
            error=str(e),
            status_code=500
        )


@ui_automation_bp.route('/api/ai/explore', methods=['POST'])
def api_ai_explore():
    """
    AI 闭环探索生成用例。
    默认 async=true：立即返回 job_id，用 GET /api/ai/explore/<job_id> 拉进度。
    async=false：同步等待（可能很长）。
    """
    try:
        data = request.get_json() or {}
        device_id = (data.get('device_id') or '').strip()
        case_text = (data.get('case_text') or data.get('prompt') or '').strip()
        if not device_id:
            return error_response(message='缺少 device_id', error='device_id required', status_code=400)
        if not case_text:
            return error_response(message='缺少 case_text', error='case_text required', status_code=400)

        closed_loop = bool(data.get('closed_loop', True))
        auto_diagnose = bool(data.get('auto_diagnose', True))
        async_mode = bool(data.get('async', True))
        try:
            timeout_sec = float(data.get('timeout_sec') or 600)
        except (TypeError, ValueError):
            timeout_sec = 600.0
        timeout_sec = max(60.0, min(timeout_sec, 3600.0))
        kwargs = dict(
            device_id=device_id,
            case_text=case_text,
            name=(data.get('name') or '').strip(),
            package_name=(data.get('package_name') or '').strip(),
            project_id=(data.get('project_id') or '').strip(),
            description=(data.get('description') or '').strip(),
            execute_while_exploring=bool(data.get('execute_while_exploring', True)),
            closed_loop=closed_loop,
            auto_diagnose=auto_diagnose,
        )

        if async_mode:
            job_id = UI_AUTOMATION_SERVICE.start_ai_explore_job(
                **kwargs, timeout_sec=timeout_sec
            )
            return success_response(
                data={
                    'job_id': job_id,
                    'async': True,
                    'closed_loop': closed_loop,
                    'timeout_sec': timeout_sec,
                },
                message='探索任务已启动，请轮询进度',
            )

        result = UI_AUTOMATION_SERVICE.ai_explore_case(
            **kwargs, timeout_sec=timeout_sec
        )
        return success_response(data=result, message=result.get('message') or 'AI 探索完成')
    except ValueError as e:
        return error_response(message=str(e), error=str(e), status_code=400)
    except Exception as e:
        logger.error(f'AI 探索失败: {e}', exc_info=True)
        return error_response(message='AI 探索失败', error=str(e), status_code=500)


@ui_automation_bp.route('/api/ai/explore-jobs', methods=['GET'])
def api_ai_explore_jobs():
    """列出近期探索任务（含落盘恢复）。"""
    try:
        limit = request.args.get('limit', '20')
        try:
            limit_i = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit_i = 20
        jobs = UI_AUTOMATION_SERVICE.list_ai_explore_jobs(limit=limit_i)
        return success_response(data={'jobs': jobs})
    except Exception as e:
        logger.error(f'列出探索任务失败: {e}', exc_info=True)
        return error_response(message='列出探索任务失败', error=str(e), status_code=500)


@ui_automation_bp.route('/api/ai/explore/<job_id>', methods=['GET'])
def api_ai_explore_job(job_id: str):
    """查询闭环探索任务进度与结果。"""
    try:
        since = request.args.get('since', '0')
        try:
            since_i = max(0, int(since))
        except (TypeError, ValueError):
            since_i = 0
        job = UI_AUTOMATION_SERVICE.get_ai_explore_job(job_id, since=since_i)
        if not job:
            return error_response(message='任务不存在', error='job not found', status_code=404)
        return success_response(data=job)
    except Exception as e:
        logger.error(f'查询探索任务失败: {e}', exc_info=True)
        return error_response(message='查询探索任务失败', error=str(e), status_code=500)


@ui_automation_bp.route('/api/ai/explore/<job_id>/cancel', methods=['POST'])
def api_ai_explore_cancel(job_id: str):
    """取消正在运行的 AI 探索任务。"""
    try:
        ok = UI_AUTOMATION_SERVICE.cancel_ai_explore_job(job_id)
        if not ok:
            return error_response(
                message='无法取消（任务不存在或已结束）',
                error='cancel failed',
                status_code=400,
            )
        return success_response(data={'job_id': job_id, 'cancel_requested': True}, message='已请求取消')
    except Exception as e:
        logger.error(f'取消探索任务失败: {e}', exc_info=True)
        return error_response(message='取消失败', error=str(e), status_code=500)


@ui_automation_bp.route('/api/ai/diagnose', methods=['POST'])
def api_ai_diagnose():
    """失败归因：返回根因与建议补丁，needs_human_approval=true，不自动改用例。"""
    try:
        data = request.get_json() or {}
        case_id = (data.get('case_id') or '').strip()
        if not case_id:
            return error_response(message='缺少 case_id', error='case_id required', status_code=400)

        failed_step = data.get('failed_step')
        if failed_step is not None:
            try:
                failed_step = int(failed_step)
            except (TypeError, ValueError):
                failed_step = None

        result = UI_AUTOMATION_SERVICE.ai_diagnose_failure(
            case_id,
            device_id=(data.get('device_id') or '').strip(),
            failed_step=failed_step,
            error=(data.get('error') or '').strip(),
            step_details=data.get('step_details') or [],
            dump_live_ui=bool(data.get('dump_live_ui', True)),
        )
        return success_response(data=result, message='归因完成（需人工确认后才可应用补丁）')
    except ValueError as e:
        return error_response(message=str(e), error=str(e), status_code=400)
    except Exception as e:
        logger.error(f'AI 归因失败: {e}', exc_info=True)
        return error_response(message='AI 归因失败', error=str(e), status_code=500)


@ui_automation_bp.route('/api/ai/apply-patch', methods=['POST'])
def api_ai_apply_patch():
    """人工确认后应用建议补丁到用例。必须 approved=true。"""
    try:
        data = request.get_json() or {}
        case_id = (data.get('case_id') or '').strip()
        suggested_patch = data.get('suggested_patch')
        approved = bool(data.get('approved', False))
        if not case_id:
            return error_response(message='缺少 case_id', error='case_id required', status_code=400)
        if not isinstance(suggested_patch, dict):
            return error_response(message='缺少 suggested_patch', error='suggested_patch required', status_code=400)
        if not approved:
            return error_response(
                message='未人工确认，拒绝修改用例',
                error='needs_human_approval',
                status_code=403,
            )

        result = UI_AUTOMATION_SERVICE.ai_apply_patch(
            case_id,
            suggested_patch,
            approved=True,
        )
        return success_response(data=result, message=result.get('message') or '补丁已应用')
    except PermissionError as e:
        return error_response(message=str(e), error='needs_human_approval', status_code=403)
    except ValueError as e:
        return error_response(message=str(e), error=str(e), status_code=400)
    except Exception as e:
        logger.error(f'应用补丁失败: {e}', exc_info=True)
        return error_response(message='应用补丁失败', error=str(e), status_code=500)


@ui_automation_bp.route('/stream_script_output')
def stream_script_output():
    """SSE脚本输出流"""
    execution_id = request.args.get('execution_id')
    
    if not execution_id:
        return error_response(
            message='缺少执行ID',
            error='execution_id required',
            status_code=400
        )
    
    def generate():
        """生成输出流"""
        with UI_AUTOMATION_SERVICE_LOCK:
            if execution_id not in UI_AUTOMATION_SERVICE.script_executor.executions:
                yield f"data: {json.dumps({'error': '执行不存在'})}\n\n"
                return
            
            execution = UI_AUTOMATION_SERVICE.script_executor.executions[execution_id]
            output_queue = execution.get('output_queue', [])
            output_lock = execution.get('output_lock')
            last_count = 0
        
        try:
            while True:
                # 检查执行是否还在运行
                status = UI_AUTOMATION_SERVICE.get_execution_status(execution_id)
                if not status or status['status'] in ['completed', 'failed', 'stopped', 'error']:
                    # 发送最后的状态
                    yield f"data: {json.dumps({'type': 'status', 'data': status})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    break
                
                # 获取新输出
                if output_lock:
                    with output_lock:
                        if len(output_queue) > last_count:
                            new_outputs = output_queue[last_count:]
                            last_count = len(output_queue)
                        else:
                            new_outputs = []
                else:
                    new_outputs = []
                
                # 发送新输出
                for output_line in new_outputs:
                    yield f"data: {json.dumps({'type': 'output', 'data': output_line})}\n\n"
                
                time.sleep(0.1)
                
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f'输出流错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@ui_automation_bp.route('/api/recording/<recording_id>/update', methods=['POST'])
def api_update_recording_info(recording_id):
    """更新录制元信息（名称、描述、包名、项目、平台等）"""
    try:
        data = request.get_json() or {}
        session = UI_AUTOMATION_SERVICE.storage.load_recording(recording_id)
        if not session:
            return error_response(
                message='录制不存在',
                error='recording not found',
                status_code=404
            )
        if 'name' in data:
            session.name = data['name']
        if 'description' in data:
            session.description = data['description']
        if 'package_name' in data:
            session.package_name = data['package_name']
        if 'project_id' in data:
            session.project_id = data['project_id']
        if 'platform' in data:
            session.platform = (data['platform'] or 'android').strip() or 'android'
        if 'target' in data:
            session.target = (data['target'] or '').strip()
        if 'entry_url' in data:
            session.entry_url = (data['entry_url'] or '').strip()
        if (session.platform or 'android') == 'android':
            session.target = session.target or session.device_id or session.package_name
            session.entry_url = ''
        if UI_AUTOMATION_SERVICE.storage.save_recording(session):
            return success_response(message='用例已保存')
        else:
            return error_response(message='保存失败', error='保存失败', status_code=500)
    except Exception as e:
        logger.error(f'更新录制失败: {e}', exc_info=True)
        return error_response(message='更新录制失败', error=str(e), status_code=500)
