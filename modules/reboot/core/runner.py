import time
import threading
from .config import CONFIG, RUNTIME, STOP_EVENT, append_log
from .serial_controller import get_controller
from .utils import adb_enable, adb_connect, check_process_with_cold_window, take_screenshot, probe_tcp
from .report import enqueue_report
from core.runtime.manager import get_runtime_manager, RuntimeStatus

def power_on_cycle(dev_cfg):
    com = dev_cfg.get('com_port', 'COM13')
    baud = int(dev_cfg.get('baud', 9600))
    addr = dev_cfg.get('addr_hex', '01')
    channel = int(dev_cfg.get('channel', 1))
    ctrl, lock = get_controller(com, baud, addr)
    
    with lock:
        try:
            ctrl.open()
        except Exception as e:
            append_log('串口打开失败', {'com': com, 'baud': baud, 'addr': addr, 'err': str(e)})
            return False, None

        try:
            ctrl.power_on(channel, addr)
            tx = getattr(ctrl, 'last_tx', None)
            append_log('通电', {'channel': channel, 'tx_raw': tx})
            return True, tx
        except Exception as e:
            append_log('通电失败', {'channel': channel, 'err': str(e)})
            return False, None

def power_off_cycle(dev_cfg):
    com = dev_cfg.get('com_port', 'COM13')
    baud = int(dev_cfg.get('baud', 9600))
    addr = dev_cfg.get('addr_hex', '01')
    channel = int(dev_cfg.get('channel', 1))
    ctrl, lock = get_controller(com, baud, addr)
    
    with lock:
        try:
            ctrl.open()
        except Exception as e:
            append_log('串口打开失败', {'com': com, 'baud': baud, 'addr': addr, 'err': str(e)})
            return False, None

        try:
            ctrl.power_off(channel, addr)
            tx = getattr(ctrl, 'last_tx', None)
            append_log('断电', {'channel': channel, 'tx_raw': tx})
            return True, tx
        except Exception as e:
            append_log('断电失败', {'channel': channel, 'err': str(e)})
            return False, None

def device_worker(task_id, dev_key, dev_cfg, stop_event, end_ts, defaults, runtime_id=None):
    ip = dev_cfg.get('ip')
    adb_port = int(dev_cfg.get('adb_port', 8787))
    package = dev_cfg.get('package', 'com.thunder.ktv')
    wait_after_on = int(defaults.get('wait_after_on', 70))
    power_off_wait = int(defaults.get('power_off_wait', 10))
    failure_threshold = int(defaults.get('failure_threshold', 5))
    enable_adb = bool(defaults.get('enable_adb', True))
    verify_poweroff = bool(defaults.get('verify_poweroff', True))
    retries = defaults.get('retries', {}) or {}
    retry_on = int(retries.get('serial_on', CONFIG['defaults']['retries'].get('serial_on', 3)))
    retry_off = int(retries.get('serial_off', CONFIG['defaults']['retries'].get('serial_off', 3)))
    stats = RUNTIME['devices'][dev_key]['stats']
    details = RUNTIME['devices'][dev_key]['details']
    cycle_index = 0
    # 可选截图开关（不影响默认流程）
    screenshot_before_reboot = bool(defaults.get('screenshot_before_reboot', False))
    screenshot_after_on = bool(defaults.get('screenshot_after_on', False))
    screenshot_after_on_delay_sec = int(defaults.get('screenshot_after_on_delay_sec', 45) or 45)
    
    try:
        append_log('DEVICE_WORKER_START', {'version': 'new_timestamp_format_v2', 'task_id': task_id})
        if runtime_id:
            get_runtime_manager().update_status(runtime_id, RuntimeStatus.RUNNING)

        while not stop_event.is_set() and int(time.time()) < end_ts:
            cycle_index += 1
            start_ts = int(time.time())
            power_on_ts = None
            ok_on = False
            tx_on = None
            stats['current_status'] = '正在通电'
            for _ in range(max(1, retry_on)):
                ok_on, tx_on = power_on_cycle(dev_cfg)
                if ok_on:
                    break
            if not ok_on:
                stats['fail_power_on'] += 1
                stats['consecutive_failures'] += 1
                stats['current_status'] = '通电失败'
                row = {'task_id': task_id, 'device': dev_key, 'cycle_index': cycle_index, 'start_ts': start_ts, 'end_ts': int(time.time()), 'wait_after_on_sec': 0, 'adb_enable_ok': False, 'adb_connect_ok': False, 'process_ok': False, 'power_off_ok': False, 'power_on_ok': False, 'ok': False, 'error_stage': 'power_on', 'error_message': 'power on failed', 'tx_on_raw': tx_on or '', 'tx_off_raw': ''}
                details.append(row)
                enqueue_report(row)
                if stats['consecutive_failures'] >= failure_threshold:
                    append_log('连续失败停止设备任务', {'device': dev_key, 'threshold': failure_threshold})
                    break
                time.sleep(1)
                continue
            power_on_ts = time.time()
            
            # --- 优化：支持“通电后X秒”优先截图（避免与断电前截图时间重叠） ---
            has_taken_early_shot = False
            # 仅当配置了截图，且截图延时小于总启动等待时间时，拆分等待
            if screenshot_after_on and screenshot_after_on_delay_sec > 0 and screenshot_after_on_delay_sec < wait_after_on:
                # 1. 等待至截图时间点
                first_wait = screenshot_after_on_delay_sec
                stats['current_status'] = f'等待截图({first_wait}s)'
                time.sleep(first_wait)
                
                # 2. 尝试连接并截图 (Best Effort，不阻塞后续流程)
                try:
                    # 尝试启用ADB（忽略错误）
                    enable_port = dev_cfg.get('adb_enable_port') or defaults.get('adb_enable_port') or dev_cfg.get('adb_port', 2007)
                    if enable_adb:
                        try: adb_enable(ip, enable_port)
                        except: pass
                    
                    # 尝试连接 - 增加短暂重试以提高命中率
                    is_connected = False
                    for _ in range(3):
                        if adb_connect(ip, adb_port):
                            is_connected = True
                            break
                        time.sleep(1)

                    if is_connected:
                        safe_dev = f"{ip}:{adb_port}".replace(":", "_")
                        # 优化文件名：reboot_task{id}_c{cycle}_after_on_{delay}s_{ts}.png
                        ts_str = time.strftime('%Y%m%d_%H%M%S')
                        shot_path = f"reboot_{task_id}_c{cycle_index}_after_on_{screenshot_after_on_delay_sec}s_{safe_dev}_{ts_str}.png"
                        import os
                        from .config import SCREENSHOTS_DIR
                        full_path = os.path.join(SCREENSHOTS_DIR, shot_path)
                        # 调用截图
                        if take_screenshot(ip, adb_port, save_path=full_path, stage="after_on"):
                             has_taken_early_shot = True
                except Exception as e:
                    # 仅记录日志，不影响主流程
                    append_log('早期截图尝试失败', {'device': dev_key, 'err': str(e)})

                # 3. 等待剩余时间 (轮询尝试截图)
                rest_wait = wait_after_on - first_wait
                if rest_wait > 0:
                    stats['current_status'] = f'等待启动剩余({rest_wait}s)'
                    start_rest = time.time()
                    while time.time() - start_rest < rest_wait:
                        if not has_taken_early_shot:
                            try:
                                # 探测端口是否通了
                                if probe_tcp(ip, adb_port, timeout_ms=500):
                                    # 尝试连接
                                    if adb_connect(ip, adb_port):
                                        # 关键优化：连接成功后等待几秒，确保 SurfaceFlinger/UI 也就绪
                                        # 解决“端口通了但截图失败”或“截屏全黑”的问题
                                        time.sleep(3)
                                        
                                        safe_dev = f"{ip}:{adb_port}".replace(":", "_")
                                        ts_str = time.strftime('%Y%m%d_%H%M%S')
                                        shot_path = f"reboot_{task_id}_c{cycle_index}_after_on_{screenshot_after_on_delay_sec}s_{safe_dev}_{ts_str}.png"
                                        import os
                                        from .config import SCREENSHOTS_DIR
                                        full_path = os.path.join(SCREENSHOTS_DIR, shot_path)
                                        
                                        # 尝试截图 (带一次内部重试)
                                        if take_screenshot(ip, adb_port, save_path=full_path, stage="after_on"):
                                             has_taken_early_shot = True
                                        else:
                                             # 如果失败，稍等再试一次
                                             time.sleep(2)
                                             if take_screenshot(ip, adb_port, save_path=full_path, stage="after_on"):
                                                 has_taken_early_shot = True
                            except Exception:
                                pass
                        
                        # 计算剩余睡眠时间
                        remaining = rest_wait - (time.time() - start_rest)
                        if remaining <= 0:
                            break
                        time.sleep(min(1, remaining))
            else:
                # 标准逻辑：直接等待全部时间
                stats['current_status'] = f'等待启动({wait_after_on}s)'
                time.sleep(wait_after_on)
            
            adb_enabled = False
            adb_connected = False
            proc_ok = True if not enable_adb else False
            if enable_adb:
                stats['current_status'] = '检查ADB/进程'
                # 选择ADB启用端口：设备字段adb_enable_port > defaults.adb_enable_port > 设备adb_port > 2007
                enable_port = dev_cfg.get('adb_enable_port') or defaults.get('adb_enable_port') or dev_cfg.get('adb_port', 2007)
                try:
                    adb_enabled = adb_enable(ip, enable_port)
                except Exception as e:
                    append_log('ADB启用异常', {'device': dev_key, 'err': str(e)})
                if not adb_enabled:
                    stats['fail_adb_enable'] += 1
                adb_connected = adb_connect(ip, adb_port)
                if not adb_connected:
                    stats['fail_adb_connect'] += 1
                if adb_connected:
                    # 注意：移除“通电后截图”的晚期重试逻辑，防止与“断电前截图”重复。
                    # 如果早期截图失败（如ADB未准备好），则本次循环缺失该截图，
                    # 这样比生成两张一模一样的截图更合理，也符合“两张图差距很大”的预期。
                    
                    cold_window = int(defaults.get('cold_boot_window', CONFIG.get('defaults', {}).get('cold_boot_window', 30)))
                    proc_ok = check_process_with_cold_window(ip, adb_port, package, cold_window)
                    if not proc_ok:
                        stats['fail_process_check'] += 1
            if (verify_poweroff and proc_ok) or (not verify_poweroff):
                # 可选项：断电前截一张机顶盒端图，失败不影响后续断电
                if screenshot_before_reboot and adb_connected:
                    try:
                        safe_dev = f"{ip}:{adb_port}".replace(":", "_")
                        # 优化文件名：reboot_task{id}_c{cycle}_before_off_{ts}.png
                        ts_str = time.strftime('%Y%m%d_%H%M%S')
                        shot_path = f"reboot_{task_id}_c{cycle_index}_before_off_{safe_dev}_{ts_str}.png"
                        import os
                        from .config import SCREENSHOTS_DIR
                        take_screenshot(ip, adb_port, save_path=os.path.join(SCREENSHOTS_DIR, shot_path), stage="before_off")
                    except Exception:
                        pass
                ok_off = False
                tx_off = None
                stats['current_status'] = '正在断电'
                for _ in range(max(1, retry_off)):
                    ok_off, tx_off = power_off_cycle(dev_cfg)
                    if ok_off:
                        break
                if not ok_off:
                    stats['fail_power_off'] += 1
                    stats['consecutive_failures'] += 1
                    stats['current_status'] = '断电失败'
                    row = {'task_id': task_id, 'device': dev_key, 'cycle_index': cycle_index, 'start_ts': start_ts, 'end_ts': int(time.time()), 'wait_after_on_sec': wait_after_on, 'adb_enable_ok': adb_enabled, 'adb_connect_ok': adb_connected, 'process_ok': proc_ok, 'power_off_ok': False, 'power_on_ok': True, 'ok': False, 'error_stage': 'power_off', 'error_message': 'power off failed', 'tx_on_raw': tx_on or '', 'tx_off_raw': tx_off or ''}
                    details.append(row)
                    enqueue_report(row)
                    if stats['consecutive_failures'] >= failure_threshold:
                        append_log('连续失败停止设备任务', {'device': dev_key, 'threshold': failure_threshold})
                        break
                    time.sleep(1)
                    continue
                stats['current_status'] = f'等待断电({power_off_wait}s)'
                time.sleep(power_off_wait)
                ok_on2 = False
                tx_on2 = None
                stats['current_status'] = '再次通电'
                for _ in range(max(1, retry_on)):
                    ok_on2, tx_on2 = power_on_cycle(dev_cfg)
                    if ok_on2:
                        break
                if ok_on2:
                    stats['reboot_count'] += 1
                    stats['execution_success_count'] += 1
                    stats['consecutive_failures'] = 0
                    stats['current_status'] = '完成'
                    row = {'task_id': task_id, 'device': dev_key, 'cycle_index': cycle_index, 'start_ts': start_ts, 'end_ts': int(time.time()), 'wait_after_on_sec': wait_after_on, 'adb_enable_ok': adb_enabled, 'adb_connect_ok': adb_connected, 'process_ok': proc_ok, 'power_off_ok': True, 'power_on_ok': True, 'ok': True, 'error_stage': '', 'error_message': '', 'tx_on_raw': tx_on or '', 'tx_off_raw': tx_off or ''}
                    details.append(row)
                    enqueue_report(row)
                else:
                    stats['fail_power_on'] += 1
                    stats['consecutive_failures'] += 1
                    stats['current_status'] = '再次通电失败'
                    row = {'task_id': task_id, 'device': dev_key, 'cycle_index': cycle_index, 'start_ts': start_ts, 'end_ts': int(time.time()), 'wait_after_on_sec': wait_after_on, 'adb_enable_ok': adb_enabled, 'adb_connect_ok': adb_connected, 'process_ok': proc_ok, 'power_off_ok': True, 'power_on_ok': False, 'ok': False, 'error_stage': 'power_on_after_off', 'error_message': 'power on after off failed', 'tx_on_raw': tx_on or '', 'tx_off_raw': tx_off or ''}
                    details.append(row)
                    enqueue_report(row)
                    if stats['consecutive_failures'] >= failure_threshold:
                        append_log('连续失败停止设备任务', {'device': dev_key, 'threshold': failure_threshold})
                        break
            else:
                stats['consecutive_failures'] += 1
                stats['current_status'] = '检查失败'
                row = {'task_id': task_id, 'device': dev_key, 'cycle_index': cycle_index, 'start_ts': start_ts, 'end_ts': int(time.time()), 'wait_after_on_sec': wait_after_on, 'adb_enable_ok': adb_enabled, 'adb_connect_ok': adb_connected, 'process_ok': False, 'power_off_ok': False, 'power_on_ok': True, 'ok': False, 'error_stage': 'process_check', 'error_message': 'process not found or adb not connected', 'tx_on_raw': tx_on or '', 'tx_off_raw': ''}
                details.append(row)
                enqueue_report(row)
                if stats['consecutive_failures'] >= failure_threshold:
                    append_log('连续失败停止设备任务', {'device': dev_key, 'threshold': failure_threshold})
                    break
    except Exception as e:
        if runtime_id:
            get_runtime_manager().update_status(runtime_id, RuntimeStatus.FAILED, error=str(e))
        raise
    finally:
        if runtime_id:
            if stop_event.is_set():
                get_runtime_manager().update_status(runtime_id, RuntimeStatus.CANCELLED)
            elif stats['consecutive_failures'] >= failure_threshold:
                get_runtime_manager().update_status(runtime_id, RuntimeStatus.FAILED, error="Failure threshold reached")
            elif int(time.time()) >= end_ts:
                 get_runtime_manager().update_status(runtime_id, RuntimeStatus.COMPLETED)
            else:
                 get_runtime_manager().update_status(runtime_id, RuntimeStatus.FAILED, error="Unknown termination")
