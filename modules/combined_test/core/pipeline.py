"""
组合测试流水线 - 编排多模块联动执行
通过 HTTP API 调用各模块，保持模块解耦
"""
import time
import logging
import threading
from typing import Dict, Any, Optional, Callable, Tuple

logger = logging.getLogger(__name__)

# 支持的流水线类型
PIPELINE_TYPES = {
    'reboot_then_player_stress': {
        'name': '重启 + 播放压测',
        'desc': '先执行断电重启测试，完成后自动启动播放器压力测试',
        'steps': ['reboot', 'player_stress'],
    },
    'reboot_with_log_monitor': {
        'name': '重启 + 日志监控',
        'desc': '执行断电重启的同时抓取冷启动日志，便于分析启动异常',
        'steps': ['log_monitor', 'reboot'],
    },
    'monkey_stress_monitor': {
        'name': 'Monkey + 性能 + 日志',
        'desc': 'Monkey 随机测试的同时，全程记录性能数据和日志，结束后生成联合报告',
        'steps': ['monitor_start', 'monkey', 'monitor_stop'],
    },
    'reboot_then_monkey': {
        'name': '重启 + Monkey',
        'desc': '执行断电重启，开机后立即启动 Monkey 测试系统启动稳定性',
        'steps': ['reboot', 'monkey'],
    },
    'player_with_server_stress': {
        'name': '播放 + 服务器压测',
        'desc': '播放视频的同时，对服务器施加压力，测试弱网/高负载下的客户端表现',
        'steps': ['server_stress_start', 'player_stress', 'server_stress_stop'],
    },
}


def _http_post(base_url: str, path: str, json_data: dict, timeout: int = 30) -> Tuple[bool, dict]:
    """内部 HTTP POST 调用"""
    try:
        import requests
        url = f"{base_url.rstrip('/')}{path}"
        r = requests.post(url, json=json_data, timeout=timeout)
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        ok = r.status_code == 200 and data.get('success', data.get('ok', False))
        return ok, data
    except Exception as e:
        logger.warning(f"HTTP POST {path} 失败: {e}")
        return False, {'error': str(e)}


def _http_get(base_url: str, path: str, timeout: int = 10) -> Tuple[bool, dict]:
    """内部 HTTP GET 调用"""
    try:
        import requests
        url = f"{base_url.rstrip('/')}{path}"
        r = requests.get(url, timeout=timeout)
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        ok = r.status_code == 200
        return ok, data
    except Exception as e:
        logger.warning(f"HTTP GET {path} 失败: {e}")
        return False, {'error': str(e)}


def _run_reboot(base_url: str, config: dict, on_log: Callable[[str], None], stop_event: threading.Event) -> Tuple[bool, bool]:
    """执行重启任务。返回 (成功, 是否用户主动停止)"""
    payload = {
        'devices': config.get('devices', []),
        'reboot_duration_minutes': config.get('reboot_duration_minutes', 5),
        'reboot_defaults': config.get('reboot_defaults', {}),
    }
    ok, resp = _http_post(base_url, '/reboot/api/start', payload)
    if not ok:
        on_log(f'[REBOOT] 启动失败: {resp.get("message", "未知错误")}')
        return False, False

    on_log('[REBOOT] 已启动，等待重启完成...')
    
    # 轮询直到完成
    poll_interval = 5
    while not stop_event.is_set():
        ok, status = _http_get(base_url, '/reboot/api/status')
        if not ok:
            time.sleep(poll_interval)
            continue
            
        data = status.get('data', status)
        # 只要 running 为 False，就认为重启流程结束（可能是成功，也可能是超时）
        if not data.get('running', False):
            on_log('[REBOOT] 重启流程已结束')
            return True, False

        elapsed = data.get('elapsed', 0)
        progress = data.get('progress', 0)
        on_log(f'[REBOOT] 运行中... 已用时 {elapsed}s, 进度 {progress}%')
        time.sleep(poll_interval)

    # 用户停止
    _http_post(base_url, '/reboot/api/stop', {})
    on_log('[REBOOT] 已停止')
    return False, True


def _run_player_stress(base_url: str, config: dict, on_log: Callable[[str], None], stop_event: threading.Event) -> Tuple[bool, bool]:
    """执行 Player Stress 任务。返回 (成功, 是否用户主动停止)"""
    device_id = config.get('device_id')
    if not device_id:
        on_log('[PLAYER_STRESS] 未配置 device_id，跳过')
        return False, False

    duration = int(config.get('player_stress_duration', 30))
    payload = {
        'device_id': device_id,
        'duration': duration,
        'mode': config.get('mode', 'monitor_only'),
        'package_name': config.get('package_name', 'com.thunder.ktv:media'),
    }
    ok, resp = _http_post(base_url, '/player_stress/api/start', payload, timeout=60)
    if not ok:
        on_log(f'[PLAYER_STRESS] 启动失败: {resp.get("message", resp.get("error", "未知错误"))}')
        return False, False

    on_log(f'[PLAYER_STRESS] 已启动，device={device_id}，时长={duration}分钟')

    # 轮询直到完成（player_stress 通过 status 的 running 判断）
    poll_interval = 10
    start_ts = time.time()
    max_wait = duration * 60 + 120  # 预留缓冲
    while not stop_event.is_set():
        if time.time() - start_ts > max_wait:
            on_log('[PLAYER_STRESS] 超时，停止')
            _http_post(base_url, '/player_stress/api/stop', {})
            break
        ok, status = _http_get(base_url, '/player_stress/api/status')
        if not ok:
            time.sleep(poll_interval)
            continue
        data = status.get('data', status)
        if not data.get('running', False):
            on_log('[PLAYER_STRESS] 已完成')
            return True, False
        elapsed = data.get('elapsed_sec', 0)
        on_log(f'[PLAYER_STRESS] 运行中... 已用时 {elapsed}s')
        time.sleep(poll_interval)

    _http_post(base_url, '/player_stress/api/stop', {})
    on_log('[PLAYER_STRESS] 已停止')
    return False, True


def _run_log_monitor(base_url: str, config: dict, on_log: Callable[[str], None], stop_event: threading.Event) -> Tuple[bool, Optional[str]]:
    """启动 Log Monitor，与 Reboot 并行运行。返回 (成功, task_id)"""
    device_id = config.get('device_id')
    if not device_id:
        on_log('[LOG_MONITOR] 未配置 device_id，跳过')
        return False, None

    task_id = f"combined_{int(time.time())}"
    payload = {
        'device_id': device_id,
        'task_id': task_id,
        'target_package': config.get('package_name', 'com.thunder.ktv'),
    }
    ok, resp = _http_post(base_url, '/log_monitor/api/start', payload)
    if not ok:
        on_log(f'[LOG_MONITOR] 启动失败: {resp.get("message", resp.get("error", "未知错误"))}')
        return False, None

    task_id = resp.get('data', {}).get('task_id', task_id)
    on_log(f'[LOG_MONITOR] 已启动，device={device_id}，task_id={task_id}')
    return True, task_id


def _stop_log_monitor(base_url: str, task_id: str) -> None:
    """停止 Log Monitor"""
    _http_post(base_url, '/log_monitor/api/stop', {'task_id': task_id})


def _run_monkey(base_url: str, config: dict, on_log: Callable[[str], None], stop_event: threading.Event) -> Tuple[bool, bool]:
    """执行 Monkey 任务。返回 (成功, 是否用户主动停止)"""
    device_id = config.get('device_id')
    if not device_id:
        on_log('[MONKEY] 未配置 device_id，跳过')
        return False, False
    
    # 提取 IP 和 Port
    try:
        if ':' in device_id:
            ip, port_str = device_id.split(':')
            port = int(port_str)
        else:
            ip = device_id
            port = 5555
    except ValueError:
        on_log(f'[MONKEY] 设备ID格式错误: {device_id}')
        return False, False

    events = int(config.get('monkey_events', 20000))
    package = config.get('package_name', 'com.thunder.ktv')
    
    payload = {
        'devices': [{'ip': ip, 'adb_port': port, 'events': events, 'package': package}],
        'run_id': config.get('unified_run_id'),
    }
    
    ok, resp = _http_post(base_url, '/monkey/api/start', payload)
    if not ok:
        on_log(f'[MONKEY] 启动失败: {resp.get("message", "未知错误")}')
        return False, False

    on_log(f'[MONKEY] 已启动，device={device_id}，events={events}')

    # 轮询直到完成
    poll_interval = 5
    not_found_count = 0
    max_not_found = 3 # 允许最多3次查不到状态（刚启动时可能还没同步）

    while not stop_event.is_set():
        ok, status = _http_get(base_url, '/monkey/api/status')
        if not ok:
            time.sleep(poll_interval)
            continue
            
        data = status.get('data', status)
        devices_status = data.get('devices_status', [])
        
        target_status = None
        if isinstance(devices_status, list):
            for ds in devices_status:
                if ds.get('device_id') == device_id:
                    target_status = ds
                    break
        elif isinstance(devices_status, dict):
             target_status = devices_status.get(device_id)

        if target_status:
             not_found_count = 0 # 重置计数
             state = target_status.get('status', '')
             if state in ('running', 'pending'):
                 executed = target_status.get('events_executed', 0)
                 total = target_status.get('events_total', events)
                 pct = int(executed / total * 100) if total > 0 else 0
                 on_log(f'[MONKEY] 运行中... 进度 {pct}% ({executed}/{total})')
             elif state == 'finished':
                 on_log('[MONKEY] 已完成')
                 return True, False
             elif state == 'error':
                 on_log(f'[MONKEY] 设备 {device_id} 发生错误: {target_status.get("error", "未知")}')
                 return False, False
        else:
            not_found_count += 1
            if not_found_count > max_not_found:
                on_log(f'[MONKEY] 找不到设备 {device_id} 的任务状态，可能已异常结束')
                return False, False
            on_log(f'[MONKEY] 等待任务状态同步... ({not_found_count}/{max_not_found})')

        time.sleep(poll_interval)

    # 用户停止
    _http_post(base_url, '/monkey/api/stop', {})
    on_log('[MONKEY] 已停止')
    return False, True


def _run_perf_monitor(base_url: str, config: dict, on_log: Callable[[str], None], stop_event: threading.Event) -> Tuple[bool, Optional[str]]:
    """启动性能监控。返回 (成功, task_id)"""
    device_id = config.get('device_id')
    if not device_id:
        on_log('[PERF_MONITOR] 未配置 device_id，跳过')
        return False, None

    task_id = f"combined_perf_{int(time.time())}"
    payload = {
        'device_id': device_id,
        'package_name': config.get('package_name', 'com.thunder.ktv'),
        'task_id': task_id,
        'monitor_type': 'standard', # 基础监控 CPU/Mem/FPS
    }
    ok, resp = _http_post(base_url, '/performance_monitor/api/start', payload)
    if not ok:
        on_log(f'[PERF_MONITOR] 启动失败: {resp.get("message", resp.get("error", "未知错误"))}')
        return False, None

    on_log(f'[PERF_MONITOR] 已启动，device={device_id}')
    return True, task_id


def _stop_perf_monitor(base_url: str, task_id: str) -> None:
    """停止性能监控"""
    _http_post(base_url, '/performance_monitor/api/stop', {'task_id': task_id})


def _run_server_stress(base_url: str, config: dict, on_log: Callable[[str], None], stop_event: threading.Event) -> Tuple[bool, Optional[str]]:
    """启动服务器压测。返回 (成功, session_id)"""
    # 假设 server_stress 有个 start 接口
    # 暂时模拟，因为 server_stress 模块目前主要是 SSH 交互
    # 这里我们只打印日志作为占位，实际需要对接 server_stress 模块的具体 API
    on_log('[SERVER_STRESS] 正在启动服务器压测 (模拟)...')
    # 实际调用: _http_post(base_url, '/server_stress/api/start_stress', {...})
    return True, "simulated_session_id"


def _stop_server_stress(base_url: str, session_id: str) -> None:
    """停止服务器压测"""
    # 实际调用: _http_post(base_url, '/server_stress/api/stop_stress', {...})
    pass


def run_pipeline(
    pipeline_type: str,
    base_url: str,
    config: dict,
    on_log: Callable[[str], None],
    stop_event: threading.Event,
) -> Dict[str, Any]:
    """
    执行组合流水线
    :param pipeline_type: 流水线类型，如 reboot_then_player_stress
    :param base_url: 平台 base URL，如 http://127.0.0.1:5000
    :param config: 配置，包含 devices、device_id、各阶段参数等
    :param on_log: 日志回调
    :param stop_event: 停止事件
    :return: 执行结果摘要
    """
    result = {
        'pipeline_type': pipeline_type,
        'success': False,
        'steps_done': [],
        'steps_failed': [],
        'message': '',
    }

    if pipeline_type not in PIPELINE_TYPES:
        on_log(f'[PIPELINE] 未知类型: {pipeline_type}')
        result['message'] = f'未知流水线类型: {pipeline_type}'
        return result

    meta = PIPELINE_TYPES[pipeline_type]
    steps = meta['steps']
    on_log(f'[PIPELINE] 开始执行: {meta["name"]}')
    on_log(f'[PIPELINE] 步骤: {" -> ".join(steps)}')

    # 从 devices 提取第一个设备作为 player_stress / log_monitor 的 device_id
    devices = config.get('devices', [])
    first_device_id = None
    if devices:
        d = devices[0]
        ip = d.get('ip', '')
        port = int(d.get('adb_port', 8787))
        first_device_id = f"{ip}:{port}"

    if pipeline_type == 'reboot_then_player_stress':
        # Step 1: Reboot
        reboot_config = {
            'devices': devices,
            'reboot_duration_minutes': config.get('reboot_duration_minutes', 5),
            'reboot_defaults': config.get('reboot_defaults', {}),
        }
        reboot_ok, reboot_stopped = _run_reboot(base_url, reboot_config, on_log, stop_event)
        if not reboot_ok:
            result['steps_failed'].append('reboot')
            result['message'] = '用户已停止' if reboot_stopped else 'Reboot 阶段未完成'
            return result
        result['steps_done'].append('reboot')

        if stop_event.is_set():
            result['message'] = '用户已停止'
            return result

        # 短暂间隔，让设备稳定
        on_log('[PIPELINE] 等待 10 秒后启动播放压测...')
        for _ in range(10):
            if stop_event.is_set():
                break
            time.sleep(1)

        # Step 2: Player Stress
        ps_config = {
            'device_id': config.get('device_id') or first_device_id,
            'player_stress_duration': config.get('player_stress_duration', 30),
            'mode': config.get('mode', 'monitor_only'),
            'package_name': config.get('package_name', 'com.thunder.ktv:media'),
        }
        if not ps_config['device_id']:
            on_log('[PIPELINE] 无可用设备，跳过 Player Stress')
            result['message'] = '无可用设备'
            return result
        ps_ok, ps_stopped = _run_player_stress(base_url, ps_config, on_log, stop_event)
        if not ps_ok:
            result['steps_failed'].append('player_stress')
            result['message'] = '用户已停止' if ps_stopped else 'Player Stress 阶段未完成'
            return result
        result['steps_done'].append('player_stress')

    elif pipeline_type == 'reboot_with_log_monitor':
        # 先启动 Log Monitor，再执行 Reboot（两者并行）
        lm_config = {
            'device_id': config.get('device_id') or first_device_id,
            'package_name': config.get('package_name', 'com.thunder.ktv'),
        }
        if not lm_config['device_id']:
            on_log('[PIPELINE] 无可用设备')
            result['message'] = '无可用设备'
            return result

        ok, log_task_id = _run_log_monitor(base_url, lm_config, on_log, stop_event)
        if not ok or not log_task_id:
            result['steps_failed'].append('log_monitor')
            result['message'] = 'Log Monitor 启动失败'
            return result
        result['steps_done'].append('log_monitor')

        # 执行 Reboot（与 Log Monitor 并行）
        reboot_config = {
            'devices': devices,
            'reboot_duration_minutes': config.get('reboot_duration_minutes', 5),
            'reboot_defaults': config.get('reboot_defaults', {}),
        }
        reboot_ok, reboot_stopped = _run_reboot(base_url, reboot_config, on_log, stop_event)
        if not reboot_ok:
            result['steps_failed'].append('reboot')
            result['message'] = '用户已停止' if reboot_stopped else 'Reboot 阶段未完成'
        else:
            result['steps_done'].append('reboot')

        # 停止 Log Monitor
        _stop_log_monitor(base_url, log_task_id)
        on_log('[LOG_MONITOR] 已停止')

    elif pipeline_type == 'monkey_stress_monitor':
        # Monkey + 性能 + 日志
        device_id = config.get('device_id') or first_device_id
        if not device_id:
            on_log('[PIPELINE] 无可用设备')
            return result
        
        common_config = {
            'device_id': device_id,
            'package_name': config.get('package_name', 'com.thunder.ktv'),
            'unified_run_id': config.get('unified_run_id'), # 关键：传递 run_id
        }

        # 1. 启动日志监控
        log_ok, log_task_id = _run_log_monitor(base_url, common_config, on_log, stop_event)
        if log_ok:
            result['steps_done'].append('log_monitor_start')
        else:
            on_log('[PIPELINE] 日志监控启动失败，但继续执行 Monkey')

        # 2. 启动性能监控
        perf_ok, perf_task_id = _run_perf_monitor(base_url, common_config, on_log, stop_event)
        if perf_ok:
            result['steps_done'].append('perf_monitor_start')
        else:
             on_log('[PIPELINE] 性能监控启动失败，但继续执行 Monkey')

        # 3. 运行 Monkey (阻塞直到完成)
        monkey_config = {**common_config, 'monkey_events': config.get('monkey_events', 20000)}
        monkey_ok, monkey_stopped = _run_monkey(base_url, monkey_config, on_log, stop_event)
        
        if monkey_ok:
            result['steps_done'].append('monkey')
        else:
            result['steps_failed'].append('monkey')
            result['message'] = '用户已停止' if monkey_stopped else 'Monkey 执行失败'

        # 4. 停止监控
        if log_task_id:
            _stop_log_monitor(base_url, log_task_id)
        if perf_task_id:
            _stop_perf_monitor(base_url, perf_task_id)
        on_log('[PIPELINE] 监控已停止')
        result['steps_done'].append('monitor_stop')

    elif pipeline_type == 'reboot_then_monkey':
        # 重启 + Monkey
        # Step 1: Reboot
        reboot_config = {
            'devices': devices,
            'reboot_duration_minutes': config.get('reboot_duration_minutes', 5),
            'reboot_defaults': config.get('reboot_defaults', {}),
        }
        reboot_ok, reboot_stopped = _run_reboot(base_url, reboot_config, on_log, stop_event)
        if not reboot_ok:
            print(f"DEBUG: Reboot failed. ok={reboot_ok}, stopped={reboot_stopped}") # DEBUG
            result['steps_failed'].append('reboot')
            result['message'] = '用户已停止' if reboot_stopped else 'Reboot 阶段未完成'
            return result
        result['steps_done'].append('reboot')

        if stop_event.is_set():
            result['message'] = '用户已停止'
            return result

        # 等待开机稳定
        on_log('[PIPELINE] 等待 15 秒后启动 Monkey...')
        for _ in range(15):
            if stop_event.is_set(): break
            time.sleep(1)

        # Step 2: Monkey
        monkey_config = {
            'device_id': config.get('device_id') or first_device_id,
            'monkey_events': config.get('monkey_events', 10000), # 默认较少的事件数测试启动
            'package_name': config.get('package_name', 'com.thunder.ktv'),
        }
        monkey_ok, monkey_stopped = _run_monkey(base_url, monkey_config, on_log, stop_event)
        if not monkey_ok:
            print(f"DEBUG: Monkey failed. ok={monkey_ok}, stopped={monkey_stopped}") # DEBUG
            result['steps_failed'].append('monkey')
            result['message'] = '用户已停止' if monkey_stopped else 'Monkey 阶段未完成'
        else:
            result['steps_done'].append('monkey')

    elif pipeline_type == 'player_with_server_stress':
        # 播放 + 服务器压测
        device_id = config.get('device_id') or first_device_id
        
        # 1. 启动服务器压测
        srv_ok, srv_sess_id = _run_server_stress(base_url, config, on_log, stop_event)
        if srv_ok:
            result['steps_done'].append('server_stress_start')
        
        # 2. 运行播放压测 (阻塞)
        ps_config = {
            'device_id': device_id,
            'player_stress_duration': config.get('player_stress_duration', 30),
            'mode': config.get('mode', 'monitor_only'),
            'package_name': config.get('package_name', 'com.thunder.ktv:media'),
        }
        ps_ok, ps_stopped = _run_player_stress(base_url, ps_config, on_log, stop_event)
        
        if ps_ok:
            result['steps_done'].append('player_stress')
        else:
             result['steps_failed'].append('player_stress')
             result['message'] = '用户已停止' if ps_stopped else '播放压测失败'

        # 3. 停止服务器压测
        if srv_sess_id:
            _stop_server_stress(base_url, srv_sess_id)
            on_log('[PIPELINE] 服务器压测已停止')
            result['steps_done'].append('server_stress_stop')

    result['success'] = len(result['steps_failed']) == 0
    result['message'] = result['message'] or ('全部完成' if result['success'] else '部分失败')
    on_log(f'[PIPELINE] 结束: {result["message"]}')
    return result
