from flask import render_template, request, Response, stream_with_context
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from . import remote_control_bp
from .core.device_controller import RemoteDeviceController
from .core.screen_streamer import ScreenStreamer
from modules.ui_automation.core.scrcpy_manager import ScrcpyManager

logger = setup_logger('remote_control_views')

@remote_control_bp.route('/')
def index():
    """远程控制主页"""
    return render_template('remote_control_index.html')

@remote_control_bp.route('/api/connect', methods=['POST'])
def api_connect():
    """连接设备"""
    try:
        data = request.get_json() or {}
        ip_input = data.get('ip')
        logger.info(f"Connect request: ip_input={ip_input}, port_input={data.get('port')}")
        
        if not ip_input:
            return error_response(message='请输入设备IP')
        port_input = data.get('port')
        ip_token = ip_input.strip().split()[0]
        ip = ip_token
        port = int(port_input) if port_input else 8787
        if ':' in ip_token:
            parts = ip_token.split(':', 1)
            ip = parts[0].strip()
            try:
                port = int(parts[1])
            except:
                port = 8787
        
        controller = RemoteDeviceController("")
        target = f"{ip}:{port}"
        logger.info(f"Target device: {target}")
        
        ok, out = controller._run_adb_command(['devices'])
        logger.info(f"Devices check: ok={ok}, out={out}")
        
        if ok and target in out and 'device' in out:
            logger.info("Device already in devices list")
            return success_response(data={'device_id': target}, message=f'已连接到 {target}')
            
        success, msg = controller.connect(ip, port)
        logger.info(f"Connect result: success={success}, msg={msg}")
        
        if success:
            return success_response(data={'device_id': target}, message=msg)
        return error_response(message=f'连接失败: {msg}')
    except Exception as e:
        logger.error(f"连接异常: {e}", exc_info=True)
        return error_response(message=str(e))

@remote_control_bp.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    """断开连接并停止所有相关进程"""
    try:
        data = request.get_json() or {}
        device_id = data.get('device_id')
        
        if not device_id:
            return error_response(message='未指定设备ID')
            
        logger.info(f"Disconnect request for {device_id}")
            
        # 1. Stop Scrcpy processes
        scrcpy_manager = ScrcpyManager()
        scrcpy_manager.stop(device_id)
        
        # 2. Stop ADB connection (Optional but requested "Exit")
        # Just logging out is enough, but we can try to disconnect if it was a tcpip device
        if ':' in device_id:
             controller = RemoteDeviceController(device_id)
             # Don't strictly check result, just try
             controller._run_adb_command(['disconnect', device_id])
        
        return success_response(message='已断开连接')
    except Exception as e:
        logger.error(f"断开连接异常: {e}")
        return error_response(message=str(e))

@remote_control_bp.route('/api/control', methods=['POST'])
def api_control():
    """发送控制指令"""
    try:
        data = request.get_json() or {}
        logger.info(f"Control request received: {data}")
        
        device_id = data.get('device_id')
        action = data.get('action')
        
        if not device_id or not action:
            return error_response(message='参数不完整')
            
        controller = RemoteDeviceController(device_id)
        
        if action == 'click':
            x = data.get('x')
            y = data.get('y')
            display_id = data.get('display_id', 0)
            logger.info(f"Processing click: x={x}, y={y}, display_id={display_id} on {device_id}")
            if controller.click(x, y, display_id):
                return success_response(message='点击成功')
                
        elif action == 'key':
            keycode = data.get('keycode')
            if controller.key_event(keycode):
                return success_response(message='按键发送成功')
                
        elif action == 'text':
            text = data.get('text')
            if controller.input_text(text):
                return success_response(message='文本输入成功')
                
        return error_response(message='操作失败')
    except Exception as e:
        logger.error(f"控制异常: {e}")
        return error_response(message=str(e))

@remote_control_bp.route('/stream_video')
def stream_video():
    """MJPEG 视频流"""
    import sys
    print(f"DEBUG: /stream_video request received. device_id={request.args.get('device_id')}")
    sys.stdout.flush()
    
    device_id = request.args.get('device_id')
    display_id = request.args.get('display_id', 0, type=int)
    
    if not device_id:
        return "Device ID required", 400
        
    streamer = ScreenStreamer()
    
    return Response(
        stream_with_context(streamer.stream_frames(device_id, display_id)),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@remote_control_bp.route('/api/open_scrcpy', methods=['POST'])
def open_scrcpy():
    """使用Scrcpy打开设备窗口"""
    try:
        data = request.get_json() or {}
        device_id = data.get('device_id')
        display_id = data.get('display_id')  # Optional
        
        if not device_id:
            return error_response(message='未指定设备ID')
            
        manager = ScrcpyManager()
        # Ensure display_id is int if provided
        if display_id is not None:
            try:
                display_id = int(display_id)
            except:
                display_id = None
                
        window_x = data.get('window_x')
        window_y = data.get('window_y')
        
        if manager.start_window(device_id, display_id=display_id, window_x=window_x, window_y=window_y):
            return success_response(message='Scrcpy启动成功')
        else:
            return error_response(message='Scrcpy启动失败')
    except Exception as e:
        logger.error(f"Scrcpy启动异常: {e}")
        return error_response(message=str(e))
