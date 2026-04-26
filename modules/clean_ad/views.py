from flask import Blueprint, render_template, request, Response, stream_with_context
import pymysql
import requests
import subprocess
import threading
import time
import json
import os
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger

clean_ad_bp = Blueprint('clean_ad', __name__, template_folder='templates')
logger = setup_logger('clean_ad_module')

# 全局停止信号（使用字典存储多个任务的停止信号，key 为任务ID）
# 格式: {task_id: {'stop_event': Event, 'server_ip': str, 'start_time': float}}
CLEAN_TASKS = {}
CLEAN_TASKS_LOCK = threading.Lock()

# --- Core Logic copied and adapted from clean_ad_tool/main.py ---

# 使用 adb_pool 统一连接管理，避免多模块并发冲突
try:
    from core.adb_pool import list_devices as _pool_list_devices, connect as _pool_connect, disconnect as _pool_disconnect, run_command as _pool_run_command
except ImportError:
    _pool_list_devices = _pool_connect = _pool_disconnect = _pool_run_command = None


def run_adb_command(cmd, timeout=5):
    """Helper to run adb command - 使用 adb_pool 统一执行"""
    try:
        if _pool_run_command and isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "adb":
            if cmd[1] == "devices":
                devs = _pool_list_devices()
                out = "List of devices attached\n" + "\n".join(f"{d}\tdevice" for d in devs) + "\n"
                return 0, out, ""
            if cmd[1] == "connect" and len(cmd) >= 3:
                addr = cmd[2]
                if ":" in addr:
                    ip, port = addr.rsplit(":", 1)
                    ok = _pool_connect(ip, int(port))
                    return 0 if ok else 1, ("connected to " + addr if ok else ""), "" if ok else "connect failed"
            if cmd[1] == "disconnect" and len(cmd) >= 3:
                addr = cmd[2]
                if ":" in addr:
                    ip, port = addr.rsplit(":", 1)
                    _pool_disconnect(ip, int(port))
                return 0, "", ""
            if cmd[1] == "-s" and len(cmd) >= 4:
                device_id = cmd[2]
                shell_cmd = list(cmd[3:])
                rc, stdout, stderr = _pool_run_command(device_id, shell_cmd, timeout=timeout)
                return rc, stdout, stderr
        
        # Fallback
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            startupinfo=startupinfo, shell=False
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, stdout.decode('utf-8', errors='ignore'), stderr.decode('utf-8', errors='ignore')
        except subprocess.TimeoutExpired:
            process.kill()
            return -1, "", "Timeout"
        except Exception as e:
            return -1, "", f"Subprocess Error: {str(e)}"
    except Exception as e:
        return -1, "", str(e)

def process_box(ip, log_callback):
    try:
        # 1. Enable ADB via HTTP
        log_callback(f"  📡 [1/4] 正在启用ADB调试 (HTTP: {ip}:2007)...")
        url = f"http://{ip}:2007/debug/adb?enable=1"
        try:
            response = requests.get(url, timeout=2)
            log_callback(f"  ✅ ADB调试已启用 (HTTP状态码: {response.status_code})")
        except requests.exceptions.Timeout:
            log_callback(f"  ❌ HTTP请求超时，设备可能不在线或网络不通")
            return False
        except requests.exceptions.ConnectionError:
            log_callback(f"  ❌ HTTP连接失败，设备可能不在线")
            return False
        except Exception as e:
            log_callback(f"  ❌ HTTP请求失败: {str(e)}")
            return False
        
        # 2. ADB Connect
        target = f"{ip}:8787"
        log_callback(f"  🔌 [2/4] 正在连接ADB ({target})...")
        
        # Disconnect first to ensure clean state
        log_callback(f"  🔄 先断开旧连接（如果存在）...")
        run_adb_command(["adb", "disconnect", target])
        
        log_callback(f"  🔗 正在建立ADB连接...")
        ret_code, stdout, stderr = run_adb_command(["adb", "connect", target], timeout=5)
        
        output = stdout.strip().lower()
        if "connected to" not in output:
            log_callback(f"  ⚠️  首次连接失败，1秒后重试...")
            time.sleep(1)
            ret_code, stdout, stderr = run_adb_command(["adb", "connect", target], timeout=5)
            output = stdout.strip().lower()
            if "connected to" not in output:
                log_callback(f"  ❌ ADB连接失败: {output or stderr}")
                return False
        
        log_callback(f"  ✅ ADB连接成功")
            
        # 3. Delete files
        log_callback(f"  🗑️  [3/4] 正在删除广告文件 (/sdcard/thunder/ad/*)...")
        cmd = ["adb", "-s", target, "shell", "rm", "-rf", "/sdcard/thunder/ad/*"]
        ret_code, stdout, stderr = run_adb_command(cmd, timeout=5)
        
        if ret_code != 0:
             logger.warning(f'删除广告文件失败: {target}, 错误: {stderr}')
             log_callback(f"  ⚠️  删除指令执行非0退出: {stderr}")
        else:
            log_callback(f"  ✅ 广告文件删除完成")
        
        # Disconnect
        log_callback(f"  🔌 [4/4] 正在断开ADB连接...")
        run_adb_command(["adb", "disconnect", target])
        log_callback(f"  ✅ ADB连接已断开")
        
        logger.info(f'广告清理成功: {ip}')
        return True
        
    except Exception as e:
        logger.error(f'广告清理异常: {ip}, 错误: {e}', exc_info=True)
        log_callback(f"  ❌ 处理异常: {str(e)}")
        return False

# --- Web Views ---

@clean_ad_bp.route('/')
def index():
    return render_template('clean_ad_index.html')

@clean_ad_bp.route('/stream_clean')
def stream_clean():
    server_ip = request.args.get('server_ip')
    task_id = request.args.get('task_id', f'clean_{int(time.time())}')
    
    # 创建停止信号并存储任务信息
    stop_event = threading.Event()
    with CLEAN_TASKS_LOCK:
        CLEAN_TASKS[task_id] = {
            'stop_event': stop_event,
            'server_ip': server_ip,
            'start_time': time.time()
        }
    
    def generate():
        success_count = 0
        fail_count = 0
        processed_count = 0
        total_count = 0
        
        try:
            # 任务开始提示
            yield f"data: {json.dumps({'msg': '=' * 60, 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'msg': f'📋 任务ID: {task_id}', 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'msg': f'🖥️  目标服务器: {server_ip}', 'task_id': task_id})}\n\n"
            start_time_str = time.strftime("%Y-%m-%d %H:%M:%S")
            yield f"data: {json.dumps({'msg': f'⏰ 开始时间: {start_time_str}', 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'msg': '-' * 60, 'task_id': task_id})}\n\n"
            
            # 步骤1: 连接数据库
            yield f"data: {json.dumps({'msg': f'📡 [步骤 1/4] 正在连接数据库服务器 {server_ip}...', 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'msg': f'   数据库: karaok', 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'msg': f'   用户: root', 'task_id': task_id})}\n\n"
            
            try:
                conn = pymysql.connect(
                    host=server_ip,
                    user='root',
                    password='Thunder#123',
                    database='karaok',
                    connect_timeout=5,
                    charset='utf8mb4'
                )
                yield f"data: {json.dumps({'msg': '✅ 数据库连接成功！', 'task_id': task_id})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'msg': f'❌ 数据库连接失败: {str(e)}', 'task_id': task_id})}\n\n"
                raise
            
            # 步骤2: 查询设备列表
            yield f"data: {json.dumps({'msg': '📊 [步骤 2/4] 正在查询机顶盒设备列表...', 'task_id': task_id})}\n\n"
            cursor = conn.cursor()
            yield f"data: {json.dumps({'msg': '   执行SQL: SELECT room_ip FROM rooms', 'task_id': task_id})}\n\n"
            
            cursor.execute("SELECT room_ip FROM rooms")
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            yield f"data: {json.dumps({'msg': f'✅ 查询完成，获取到 {len(rows)} 条记录', 'task_id': task_id})}\n\n"
            
            # 步骤3: 处理数据
            yield f"data: {json.dumps({'msg': '🔧 [步骤 3/4] 正在处理设备列表...', 'task_id': task_id})}\n\n"
            ips = [row[0] for row in rows if row[0]]
            yield f"data: {json.dumps({'msg': f'   过滤空值后: {len(ips)} 个IP', 'task_id': task_id})}\n\n"
            
            # 去重
            ips = list(set(ips))
            yield f"data: {json.dumps({'msg': f'   去重后: {len(ips)} 个唯一IP', 'task_id': task_id})}\n\n"
            
            # 排序
            yield f"data: {json.dumps({'msg': '   正在排序IP地址...', 'task_id': task_id})}\n\n"
            try:
                ips.sort(key=lambda x: int(x.split('.')[-1]) if x.count('.')==3 else x)
                yield f"data: {json.dumps({'msg': '✅ IP地址排序完成', 'task_id': task_id})}\n\n"
            except Exception as e:
                ips.sort()
                yield f"data: {json.dumps({'msg': f'⚠️  使用默认排序（数字排序失败）', 'task_id': task_id})}\n\n"
                
            total = len(ips)
            total_count = total
            yield f"data: {json.dumps({'msg': '-' * 60, 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'msg': f'📦 准备清理 {total} 个机顶盒设备', 'total': total, 'task_id': task_id})}\n\n"
            
            # 步骤4: 开始清理
            yield f"data: {json.dumps({'msg': '🚀 [步骤 4/4] 开始执行清理任务...', 'task_id': task_id})}\n\n"
            yield f"data: {json.dumps({'msg': '-' * 60, 'task_id': task_id})}\n\n"
            
            # 检查停止信号
            if stop_event.is_set():
                yield f"data: {json.dumps({'msg': '任务已停止（在开始处理前）'})}\n\n"
                return
            
            for i, box_ip in enumerate(ips):
                # 检查停止信号（在处理下一个盒子之前）
                if stop_event.is_set():
                    yield f"data: {json.dumps({'msg': '⏹️  停止信号已接收，当前盒子清理完成后将停止'})}\n\n"
                    break  # 如果已经停止，不再处理新的盒子
                
                # 显示进度和当前处理的设备
                progress_percent = int((i + 1) / total * 100)
                progress_msg = f"\n📱 [{i+1}/{total}] ({progress_percent}%) 正在处理设备: {box_ip}"
                yield f"data: {json.dumps({'msg': progress_msg, 'current': i+1, 'total': total})}\n\n"
                
                # Callback to capture logs from process_box
                logs = []
                def log_cb(msg):
                    logs.append(msg)
                
                # 处理当前盒子
                box_success = process_box(box_ip, log_cb)
                processed_count += 1
                
                if box_success:
                    yield f"data: {json.dumps({'msg': '  ✅ 清理成功'})}\n\n"
                    success_count += 1
                else:
                    for l in logs:
                        yield f"data: {json.dumps({'msg': l})}\n\n"
                    yield f"data: {json.dumps({'msg': '  ❌ 处理失败'})}\n\n"
                    fail_count += 1
                
                # 当前盒子处理完成后，检查停止信号
                if stop_event.is_set():
                    yield f"data: {json.dumps({'msg': '当前盒子清理完成，任务已停止'})}\n\n"
                    break  # 退出循环，不再处理剩余盒子
                    
        except Exception as e:
            logger.error(f'广告清理任务严重错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'msg': f'❌ 严重错误: {str(e)}'})}\n\n"
        finally:
            # 清理任务记录
            with CLEAN_TASKS_LOCK:
                CLEAN_TASKS.pop(task_id, None)
        
        # 生成最终统计信息
        is_stopped = stop_event.is_set()
        remaining_count = total_count - processed_count
        
        logger.info(f'广告清理任务{"停止" if is_stopped else "完成"}: 已处理 {processed_count}, 成功 {success_count}, 失败 {fail_count}, 剩余 {remaining_count}')
        
        yield f"data: {json.dumps({'msg': '-' * 50})}\n\n"
        
        if is_stopped:
            summary = f"任务已停止。\n已处理: {processed_count}/{total_count}\n成功: {success_count}\n失败: {fail_count}\n剩余未处理: {remaining_count}"
        else:
            summary = f"任务完成。\n总计: {total_count}\n成功: {success_count}\n失败: {fail_count}"
        
        yield f"data: {json.dumps({'msg': summary, 'stats': {'total': total_count, 'processed': processed_count, 'success': success_count, 'failed': fail_count, 'remaining': remaining_count, 'stopped': is_stopped}})}\n\n"
        yield f"data: {json.dumps({'done': True, 'stopped': is_stopped})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@clean_ad_bp.route('/api/status', methods=['GET'])
def api_get_status():
    """查询正在运行的清理任务"""
    try:
        with CLEAN_TASKS_LOCK:
            running_tasks = []
            for task_id, task_info in CLEAN_TASKS.items():
                if task_info and task_info.get('stop_event'):
                    running_tasks.append({
                        'task_id': task_id,
                        'server_ip': task_info.get('server_ip', ''),
                        'start_time': task_info.get('start_time', 0),
                        'running_time': int(time.time() - task_info.get('start_time', time.time()))
                    })
            
            return success_response(data={
                'has_running_task': len(running_tasks) > 0,
                'running_tasks': running_tasks
            })
    except Exception as e:
        logger.error(f'查询任务状态失败: {e}', exc_info=True)
        return error_response(
            message='查询任务状态失败',
            error=str(e),
            status_code=500
        )

@clean_ad_bp.route('/api/stop', methods=['POST'])
def api_stop_clean():
    """停止广告清理任务"""
    try:
        data = request.get_json(force=True) or {}
        task_id = data.get('task_id')
        
        if not task_id:
            return error_response(
                message='缺少任务ID',
                error='task_id required',
                status_code=400
            )
        
        with CLEAN_TASKS_LOCK:
            task_info = CLEAN_TASKS.get(task_id)
            if task_info and task_info.get('stop_event'):
                task_info['stop_event'].set()
                logger.info(f'广告清理任务停止信号已发送: {task_id}')
                return success_response(message='停止信号已发送，当前盒子清理完成后将停止')
            else:
                return error_response(
                    message='未找到运行中的清理任务',
                    error='task not found',
                    status_code=404
                )
    except Exception as e:
        logger.error(f'停止清理任务失败: {e}', exc_info=True)
        return error_response(
            message='停止任务失败',
            error=str(e),
            status_code=500
        )
