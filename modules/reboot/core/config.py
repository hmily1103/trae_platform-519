import os
import json
import logging
from collections import deque
import threading

# 适配 Trae Platform 的目录结构
# D:\trae-code\trae_platform\modules\reboot\core\config.py
# BASE_DIR 应该是 D:\trae-code\trae_platform\modules\reboot
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Trae Platform Root (D:\trae-code\trae_platform)
PLATFORM_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
# Use a directory outside the source tree to prevent auto-reload loops
# D:\trae-code\trae_platform -> D:\trae-code\trae_logs\reboot_data
SAFE_DATA_DIR = os.path.join(os.path.dirname(PLATFORM_ROOT), 'trae_logs', 'reboot_data')

CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
DEVICES_PATH = os.path.join(BASE_DIR, 'devices.json')
# Reports and Exports moved to safe directory to avoid auto-reload loops
REPORTS_PATH = os.path.join(SAFE_DATA_DIR, 'reports.json')

DEFAULT_CONFIG = {
    "log_dir": os.path.join(SAFE_DATA_DIR, 'logs'),
    "reports_path": REPORTS_PATH,
    "exports_dir": os.path.join(SAFE_DATA_DIR, 'exports'),
    "defaults": {
        "wait_after_on": 70,
        "power_off_wait": 10,
        "failure_threshold": 5,
        "screenshot_before_reboot": False,
        "screenshot_after_on": False,
        "screenshot_after_on_delay_sec": 45,
        "adb_enable_port": 2007,
        "adb_enable_timeout_sec": 5,
        "adb_port_probe_timeout_ms": 1500,
        "adb_connect_timeout_sec": 10,
        "process_backoff_ms": 500,
        "cold_boot_window": 30,
        "retries": {
            "adb_enable": 5,
            "adb_connect": 5,
            "serial_on": 3,
            "serial_off": 3,
            "process_check": 3
        }
    }
}

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return default

CONFIG = load_json(CONFIG_PATH, DEFAULT_CONFIG)

# Ensure critical paths are present (merge defaults if missing in loaded config)
if CONFIG is not DEFAULT_CONFIG:
    for key in ['log_dir', 'reports_path', 'exports_dir']:
        if key not in CONFIG:
            CONFIG[key] = DEFAULT_CONFIG[key]

# Ensure directories exist
os.makedirs(CONFIG.get('log_dir', os.path.join(SAFE_DATA_DIR, 'logs')), exist_ok=True)
os.makedirs(CONFIG.get('exports_dir', os.path.join(SAFE_DATA_DIR, 'exports')), exist_ok=True)
SCREENSHOTS_DIR = os.path.join(CONFIG.get('exports_dir', os.path.join(SAFE_DATA_DIR, 'exports')), 'screenshots')
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Logger setup（使用 RotatingFileHandler 轮转）
logger = logging.getLogger('reboot_module')
if not logger.handlers:
    from logging.handlers import RotatingFileHandler
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    
    log_file = os.path.join(CONFIG.get('log_dir'), 'app.log')
    try:
        max_bytes = int(os.environ.get('LOG_MAX_BYTES', '10485760'))  # 10MB
        backup_count = int(os.environ.get('LOG_BACKUP_COUNT', '5'))
        file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception:
        pass  # 可能没权限写文件
    
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

# Global Runtime State
RUNTIME = {
    'running': False,
    'task_id': None,
    'start_ts': 0,
    'duration_sec': 0,
    'devices': {},
    'logs': deque(maxlen=1000)
}
print(f"DEBUG: config.py loaded. Name: {__name__}, RUNTIME id: {id(RUNTIME)}")
logger.info(f"DEBUG: config.py loaded. Name: {__name__}, RUNTIME id: {id(RUNTIME)}")
STOP_EVENT = threading.Event()

def append_log(msg, ctx=None):
    import time
    item = {'ts': int(time.time()), 'msg': msg, 'ctx': ctx or {}}
    RUNTIME['logs'].append(item)
    logger.info(f"[日志] {msg} | {ctx or {}}")
