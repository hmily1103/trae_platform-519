import os
import json
import time
import threading
from queue import Queue, Empty
from datetime import datetime
from .config import CONFIG, logger, REPORTS_PATH, append_log

# 报告写入队列与线程
REPORT_QUEUE = None
REPORT_WRITER_THREAD = None
REPORT_WRITER_STOP = threading.Event()
REPORT_LOG_PATH = None

def save_report(report):
    try:
        path = CONFIG.get('reports_path', REPORTS_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = []
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f) or []
                except Exception:
                    data = []
        data.append(report)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception('[报告] 保存失败')


def export_csv(task_id: str, summary_rows, detail_rows, csv_type: str):
    exports_dir = CONFIG.get('exports_dir')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = f"{csv_type}_{task_id}_{ts}.csv"
    fpath = os.path.join(exports_dir, fname)
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            if csv_type == 'summary':
                headers = [
                    'task_id','start_time','end_time','device_ip','adb_port','channel','duration_sec',
                    'reboot_count','execution_success_count','fail_adb_enable','fail_adb_connect',
                    'fail_process_check','fail_power_on','fail_power_off','notes'
                ]
                f.write(','.join(headers) + '\n')
                for r in summary_rows:
                    f.write(','.join(str(r.get(h,'')) for h in headers) + '\n')
            else:
                headers = [
                    'task_id','cycle_index','start_ts','end_ts','wait_after_on_sec','adb_enable_ok',
                    'adb_connect_ok','process_ok','power_off_ok','power_on_ok','ok','error_stage','error_message',
                    'tx_on_raw','tx_off_raw'
                ]
                f.write(','.join(headers) + '\n')
                for r in detail_rows:
                    f.write(','.join(str(r.get(h,'')) for h in headers) + '\n')
        return fpath
    except Exception:
        logger.exception('[导出] CSV 写入失败')
        return None

def _open_report_log(task_id):
    exports_dir = CONFIG.get('exports_dir')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = f"details_{task_id}_{ts}.jsonl"
    fpath = os.path.join(exports_dir, fname)
    os.makedirs(exports_dir, exist_ok=True)
    return fpath

def _rotate_if_needed(fpath):
    try:
        rotate_mb = int(CONFIG.get('reports', {}).get('rotate_mb', 20))
        if rotate_mb <= 0:
            return fpath
        if os.path.exists(fpath) and (os.path.getsize(fpath) > rotate_mb * 1024 * 1024):
            base, ext = os.path.splitext(fpath)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            new_path = f"{base}.{ts}{ext or ''}"
            os.replace(fpath, new_path)
    except Exception:
        logger.exception('[报告] 轮转失败')
    return fpath

def _report_writer_loop():
    global REPORT_LOG_PATH
    batch_size = int(CONFIG.get('reports', {}).get('batch_size', 50))
    flush_interval_ms = int(CONFIG.get('reports', {}).get('flush_interval_ms', 1000))
    fsync_every_batch = bool(CONFIG.get('reports', {}).get('fsync_every_batch', False))
    buf = []
    last_flush = time.time()
    while not REPORT_WRITER_STOP.is_set():
        try:
            item = REPORT_QUEUE.get(timeout=max(0.1, flush_interval_ms/1000.0))
            if item is None:
                # 哨兵
                break
            buf.append(item)
        except Empty:
            pass
        now = time.time()
        if len(buf) >= batch_size or ((now - last_flush) * 1000 >= flush_interval_ms and buf):
            try:
                REPORT_LOG_PATH = _rotate_if_needed(REPORT_LOG_PATH)
                with open(REPORT_LOG_PATH, 'a', encoding='utf-8') as f:
                    for r in buf:
                        f.write(json.dumps(r, ensure_ascii=False) + '\n')
                    f.flush()
                    if fsync_every_batch:
                        try:
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                buf.clear()
                last_flush = now
            except Exception:
                logger.exception('[报告] 写入失败')
    # 清理剩余缓冲
    if buf:
        try:
            with open(REPORT_LOG_PATH, 'a', encoding='utf-8') as f:
                for r in buf:
                    f.write(json.dumps(r, ensure_ascii=False) + '\n')
        except Exception:
            logger.exception('[报告] 收尾写入失败')

def start_report_writer(task_id):
    global REPORT_QUEUE, REPORT_WRITER_THREAD, REPORT_LOG_PATH
    if not bool(CONFIG.get('reports', {}).get('writer_enabled', True)):
        return
    if REPORT_WRITER_THREAD and REPORT_WRITER_THREAD.is_alive():
        return
    max_q = int(CONFIG.get('reports', {}).get('max_queue_size', 10000))
    REPORT_QUEUE = Queue(maxsize=max_q)
    REPORT_LOG_PATH = _open_report_log(task_id)
    REPORT_WRITER_STOP.clear()
    REPORT_WRITER_THREAD = threading.Thread(target=_report_writer_loop, daemon=True)
    REPORT_WRITER_THREAD.start()
    append_log('报告写入线程启动', {'path': REPORT_LOG_PATH})

def stop_report_writer():
    global REPORT_QUEUE
    if REPORT_QUEUE:
        try:
            REPORT_QUEUE.put_nowait(None)  # 发送哨兵
        except Exception:
            pass
    REPORT_WRITER_STOP.set()
    if REPORT_WRITER_THREAD:
        REPORT_WRITER_THREAD.join(timeout=3)
    append_log('报告写入线程停止', {'path': REPORT_LOG_PATH})

def enqueue_report(detail_row):
    try:
        if not bool(CONFIG.get('reports', {}).get('writer_enabled', True)):
            return
        if not REPORT_QUEUE:
            return
        on_overflow = str(CONFIG.get('reports', {}).get('on_overflow', 'block')).lower()
        if on_overflow == 'block':
            REPORT_QUEUE.put(detail_row)
        else:
            # drop_oldest 策略
            if REPORT_QUEUE.full():
                try:
                    REPORT_QUEUE.get_nowait()
                except Empty:
                    pass
            REPORT_QUEUE.put_nowait(detail_row)
    except Exception:
        logger.exception('[报告] 入队失败')
