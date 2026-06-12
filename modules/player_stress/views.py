
import os
import sys
import json
import re
import logging
import threading
import time
from datetime import datetime
from collections import deque
from flask import Blueprint, render_template, request, jsonify, current_app
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
            if failed:
                status = "failed"
                reason = "运行异常"
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
                "report_file": getattr(runner, "last_csv_file", "") or "",
                "summary_file": getattr(runner, "last_summary_file", "") or "",
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
        run_state.update({
            "elapsed_seconds": elapsed_seconds,
            "remaining_seconds": remaining_seconds,
            "progress_percent": round(progress_percent, 1),
        })
        
        # Get logs - 简单直接
        logs = []
        with LOG_LOCK:
            logs = [str(item) for item in LOG_BUFFER]
        
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
            'active_event': None,
            'recent_events': [],
        }
        if TEST_INSTANCE and hasattr(TEST_INSTANCE, 'monitor'):
            monitor = TEST_INSTANCE.monitor
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
            }
            watcher = getattr(TEST_INSTANCE, 'tv_playback_watcher', None)
            if watcher:
                active_event = getattr(watcher, '_active_event', None)

                def event_summary(event):
                    if not isinstance(event, dict):
                        return None
                    contention = event.get('cpu_contention') or {}
                    candidate = contention.get('top_candidate') or {}
                    return {
                        'event_id': event.get('event_id'),
                        'start_time': event.get('start_time'),
                        'end_time': event.get('end_time'),
                        'duration_ms': event.get('duration_ms', 0),
                        'reason': event.get('reason', ''),
                        'surface_name': event.get('surface_name', ''),
                        'max_frame_gap_ms': event.get('max_frame_gap_ms', 0),
                        'min_fps': event.get('min_fps', 0),
                        'evidence_dir': event.get('evidence_dir', ''),
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
                tv_monitor['active_event'] = event_summary(active_event)
                recent = list(getattr(monitor, 'tv_stall_events', []))[-5:]
                tv_monitor['recent_events'] = [
                    summary for summary in
                    (event_summary(event) for event in reversed(recent))
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
                    'video_fps_source': latest_snapshot.get('video_fps_source', ''),
                    'tv_display_id': latest_snapshot.get('tv_display_id'),
                    'tv_display_verified': latest_snapshot.get('tv_display_verified', False),
                    'tv_display_verification_reason': latest_snapshot.get('tv_display_verification_reason', ''),
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
                    'ignore_video_metrics': latest_snapshot.get('ignore_video_metrics', False),
                    'ignore_video_reason': latest_snapshot.get('ignore_video_reason', ''),
                    'tv_stall_count': len(getattr(monitor, 'tv_stall_events', [])),
                    'cpu_percent': latest_snapshot.get(
                        'system_cpu_percent',
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
                    'root_cause_evidence': latest_snapshot.get('root_cause_evidence', {}),
                    'top_consumers': latest_snapshot.get('top_consumers', ''),
                    'max_temperature_c': latest_snapshot.get('max_temperature_c', 0),
                    'min_cpu_frequency_ratio': latest_snapshot.get('min_cpu_frequency_ratio', 0),
                    'thermal_throttling': latest_snapshot.get('thermal_throttling', False),
                    'pss_mb': latest_snapshot.get('pss_mb', 0),
                    'gfx_jank_count': latest_snapshot.get('gfx_jank_count', 0),
                    'log_stutter_count': latest_snapshot.get('log_stutter_count', 0),
                    'audio_active': latest_snapshot.get('audio_active', False),
                    'timestamp': latest_snapshot.get('timestamp', '')
                }
                
                # Get recent history for charts (last 50 points)
                history = monitor.history[-50:] if len(monitor.history) > 50 else monitor.history
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
        })
    except Exception as e:
        logger.error(f'获取测试状态失败: {e}', exc_info=True)
        return error_response(
            message='获取状态失败',
            error=str(e),
            status_code=500
        )

@stress_bp.route('/api/reports', methods=['GET'])
def api_reports():
    try:
        report_dir = get_module_report_dir('player_stress')
        if not os.path.exists(report_dir):
            return success_response(data={'reports': []})

        entries_by_prefix = {}
        filename_pattern = re.compile(r'^(report|summary)_(\d{8}_\d{6})\.(csv|html|txt|json)$')

        try:
            for filename in os.listdir(report_dir):
                file_path = os.path.join(report_dir, filename)
                if not os.path.isfile(file_path):
                    continue
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
                        'meta': {},
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
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                summary_json = json.load(f)
                            meta = summary_json.get('meta') if isinstance(summary_json, dict) else None
                            decision = summary_json.get('decision') if isinstance(summary_json, dict) else None
                            stats = summary_json.get('stats') if isinstance(summary_json, dict) else None
                            entry['meta'] = {
                                'device_id': (meta or {}).get('device_id', ''),
                                'device_ip': (meta or {}).get('device_ip', ''),
                                'firmware_incremental': (meta or {}).get('firmware_incremental', ''),
                                'package_name': (meta or {}).get('package_name', ''),
                                'end_time': (meta or {}).get('end_time', ''),
                                'duration_sec': (meta or {}).get('duration_sec', 0),
                                'score': (decision or {}).get('score', None),
                                'grade': (decision or {}).get('grade', ''),
                                'tv_perceptual_score': ((decision or {}).get('metrics') or {}).get('perceptual_stutter', {}).get('score', None),
                                'log_stutter_count': (stats or {}).get('final_log_stutter_count', None),
                                'root_cause_top': (((stats or {}).get('root_cause_analysis') or {}).get('most_confident_cause') or {}).get('root_cause_type', ''),
                            }
                        except Exception as e:
                            logger.warning(f'读取 summary JSON 失败: {filename} | {e}')
        except Exception as e:
            logger.warning(f'读取报告目录失败: {e}')

        entries = sorted(entries_by_prefix.values(), key=lambda item: item.get('prefix', ''), reverse=True)
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
    from flask import send_from_directory
    import os
    if not filename or ".." in filename or "/" in filename.replace("\\", "/") or filename != os.path.basename(filename):
        return error_response(message="Invalid filename", status_code=400)
    report_dir = get_module_report_dir('player_stress')
    return send_from_directory(report_dir, os.path.basename(filename))
