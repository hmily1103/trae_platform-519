import os
import json
import threading
import time
from datetime import datetime
from utils.logger import setup_logger

logger = setup_logger('monkey_scheduler')

SCHEDULER_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scheduler_data.json')

class MonkeyScheduler:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(MonkeyScheduler, cls).__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self.tasks = []
        self.running = False
        self.thread = None
        self._load_tasks()

    def _load_tasks(self):
        try:
            if os.path.exists(SCHEDULER_DATA_FILE):
                with open(SCHEDULER_DATA_FILE, 'r', encoding='utf-8') as f:
                    self.tasks = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load scheduler tasks: {e}")
            self.tasks = []

    def _save_tasks(self):
        try:
            with open(SCHEDULER_DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.tasks, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Failed to save scheduler tasks: {e}")

    def add_task(self, task):
        # task should have: id, name, schedule_time(HH:MM), devices, package, events, throttle, enabled
        task['id'] = str(int(time.time() * 1000))
        task.setdefault('enabled', True)
        task.setdefault('last_run_date', '')
        self.tasks.append(task)
        self._save_tasks()
        return task

    def update_task(self, task_id, updates):
        for task in self.tasks:
            if task.get('id') == task_id:
                task.update(updates)
                self._save_tasks()
                return task
        return None

    def delete_task(self, task_id):
        self.tasks = [t for t in self.tasks if t.get('id') != task_id]
        self._save_tasks()

    def get_tasks(self):
        return self.tasks

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Monkey Scheduler started.")

    def stop(self):
        self.running = False

    def _run_loop(self):
        while self.running:
            try:
                now = datetime.now()
                current_time_str = now.strftime("%H:%M")
                today_str = now.strftime("%Y-%m-%d")

                for task in self.tasks:
                    if not task.get('enabled'):
                        continue
                    
                    if task.get('schedule_time') == current_time_str:
                        if task.get('last_run_date') != today_str:
                            # It's time to run!
                            logger.info(f"Triggering scheduled monkey task: {task.get('name')}")
                            self._trigger_task(task)
                            task['last_run_date'] = today_str
                            self._save_tasks()

            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")

            # Sleep for a bit before checking again
            time.sleep(30)

    def _trigger_task(self, task):
        devices = task.get('devices', [])
        
        # We can just make an HTTP POST request to our own API using requests
        try:
            import requests
            port = os.environ.get('FLASK_RUN_PORT', 5000)
            
            # If devices is empty, we fetch all online devices
            if not devices:
                try:
                    dev_res = requests.get(f"http://127.0.0.1:{port}/monkey/api/devices", timeout=3)
                    if dev_res.status_code == 200:
                        data = dev_res.json()
                        if 'data' in data and 'devices' in data['data']:
                            devices = data['data']['devices']
                        elif 'devices' in data:
                            devices = data['devices']
                except Exception as e:
                    logger.warning(f"Scheduler failed to fetch online devices: {e}")
            
            if not devices:
                logger.warning(f"Task {task.get('id')} has no devices and no online devices found. Skipping.")
                return

            # We construct a request-like dict
            data = {
                'device_ids': devices,
                'package_name': task.get('package', 'com.thunder.ktv'),
                'events_count': task.get('events', 100000),
                'throttle': task.get('throttle', 300),
                'rounds': task.get('cycles', 1),
                'sample_interval_minutes': 5
            }
            
            url = f"http://127.0.0.1:{port}/monkey/api/batch/start"
            res = requests.post(url, json=data, timeout=5)
            logger.info(f"Successfully sent batch start request for task {task.get('id')}: {res.text}")
        except Exception as e:
            logger.error(f"Failed to trigger scheduled task {task.get('id')} via API: {e}")

scheduler_instance = MonkeyScheduler()
