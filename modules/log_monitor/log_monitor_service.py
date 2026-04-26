#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志监控服务
管理多个设备的日志监控任务
"""

import os
import threading
import subprocess
import time
import re
from datetime import datetime
from typing import Dict, Optional, List, Callable
from collections import deque
from .alert_engine import AlertEngine, AlertRecord
from utils.logger import setup_logger

logger = setup_logger('log_monitor_service')


class LogMonitorSession:
    """日志监控会话"""
    
    def __init__(self, session_id: str, device_id: str, package_name: str = ""):
        self.session_id = session_id
        self.device_id = device_id
        self.package_name = package_name
        self.start_time = datetime.now()
        self.status = "running"  # running/stopped
        self.logcat_process = None
        self.monitoring_thread = None
        self.log_buffer = deque(maxlen=10000)  # 最近10000条日志
        self.alert_engine = AlertEngine()
        self.filters = {
            'log_level': 'Verbose',
            'tags': [],
            'keywords': []
        }
        self.on_log_callback: Optional[Callable] = None
        self.on_alert_callback: Optional[Callable] = None
    
    def add_log(self, log_line: str):
        """添加日志"""
        self.log_buffer.append({
            'log': log_line,
            'timestamp': datetime.now(),
            'session_id': self.session_id
        })
        
        # 检查告警
        alerts = self.alert_engine.check_log(log_line, self.device_id, self.package_name)
        
        # 执行自动化动作
        for alert in alerts:
            rule = self.alert_engine.rules.get(alert.rule_id)
            if rule and rule.action:
                action_result = self._execute_action(rule.action)
                alert.action_taken = action_result
                
        if alerts and self.on_alert_callback:
            for alert in alerts:
                self.on_alert_callback(alert)
        
        # 回调
        if self.on_log_callback:
            self.on_log_callback(log_line)
    
    def _execute_action(self, action: str) -> str:
        """执行自动化动作"""
        try:
            if action == 'screenshot':
                timestamp = int(datetime.now().timestamp())
                filename = f"alert_{self.session_id}_{timestamp}.png"
                # 确保保存目录存在: trae_platform/static/captures
                # __file__ is modules/log_monitor/log_monitor_service.py
                # base_dir is trae_platform
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                save_dir = os.path.join(base_dir, 'static', 'captures')
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, filename)
                
                # 执行截图
                with open(save_path, "wb") as f:
                    subprocess.run(
                        ["adb", "-s", self.device_id, "exec-out", "screencap", "-p"],
                        stdout=f,
                        timeout=10
                    )
                return f"Screenshot saved: {filename}"
                
            elif action.startswith('shell:'):
                cmd = action.split(':', 1)[1]
                subprocess.run(
                    ["adb", "-s", self.device_id, "shell", cmd],
                    timeout=5
                )
                return f"Shell executed: {cmd}"
                
            return f"Unknown action: {action}"
        except Exception as e:
            logger.error(f"执行动作失败: {action}, {e}")
            return f"Action failed: {str(e)}"

    
    def get_recent_logs(self, limit: int = 100) -> List[Dict]:
        """获取最近的日志"""
        return list(self.log_buffer)[-limit:]
    
    def stop(self):
        """停止监控"""
        self.status = "stopped"
        if self.logcat_process:
            try:
                self.logcat_process.terminate()
                self.logcat_process.wait(timeout=2)
            except Exception:
                try:
                    self.logcat_process.kill()
                except Exception:
                    pass
            self.logcat_process = None


class LogMonitorService:
    """日志监控服务（单例）"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self.sessions: Dict[str, LogMonitorSession] = {}
        self.sessions_lock = threading.Lock()
        self._initialized = True
    
    def start_monitoring(self, 
                        device_id: str,
                        package_name: str = "",
                        filters: Optional[Dict] = None,
                        alert_rules: Optional[List] = None,
                        on_log: Optional[Callable] = None,
                        on_alert: Optional[Callable] = None) -> str:
        """启动日志监控"""
        session_id = f"log_monitor_{int(time.time() * 1000)}"
        
        session = LogMonitorSession(session_id, device_id, package_name)
        session.filters = filters or session.filters
        session.on_log_callback = on_log
        session.on_alert_callback = on_alert
        
        # 添加自定义告警规则
        if alert_rules:
            from .alert_engine import AlertRule
            for rule_data in alert_rules:
                rule = AlertRule(
                    id=rule_data.get('id', f"rule_{int(time.time())}"),
                    name=rule_data.get('name', ''),
                    type=rule_data.get('type', 'keyword'),
                    pattern=rule_data.get('pattern', ''),
                    severity=rule_data.get('severity', 'medium'),
                    enabled=rule_data.get('enabled', True),
                    action=rule_data.get('action', '')
                )
                session.alert_engine.add_rule(rule)
        
        # 启动logcat进程
        self._start_logcat(session)
        
        with self.sessions_lock:
            self.sessions[session_id] = session
        
        logger.info(f"日志监控已启动: {session_id}, 设备: {device_id}")
        return session_id
    
    def _start_logcat(self, session: LogMonitorSession):
        """启动logcat进程"""
        try:
            # 构建logcat命令
            cmd = ["adb", "-s", session.device_id, "logcat", "-v", "time"]
            
            # 日志级别过滤
            level_map = {
                'Verbose': 'V',
                'Debug': 'D',
                'Info': 'I',
                'Warning': 'W',
                'Error': 'E',
                'Fatal': 'F'
            }
            min_level = session.filters.get('log_level', 'Verbose')
            if min_level in level_map:
                cmd.extend([f"*:{level_map[min_level]}"])
            
            # 启动进程
            session.logcat_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding='utf-8',
                errors='ignore'
            )
            
            # 启动读取线程
            def read_logs():
                try:
                    for line in iter(session.logcat_process.stdout.readline, ''):
                        if not line or session.status != "running":
                            break
                        line = line.rstrip()
                        if line:
                            session.add_log(line)
                except Exception as e:
                    logger.error(f"读取日志失败: {e}")
                finally:
                    session.stop()
            
            session.monitoring_thread = threading.Thread(target=read_logs, daemon=True)
            session.monitoring_thread.start()
            
        except Exception as e:
            logger.error(f"启动logcat失败: {e}", exc_info=True)
            session.stop()
            raise
    
    def stop_monitoring(self, session_id: str) -> bool:
        """停止日志监控"""
        with self.sessions_lock:
            session = self.sessions.get(session_id)
            if not session:
                return False
            
            session.stop()
            del self.sessions[session_id]
            logger.info(f"日志监控已停止: {session_id}")
            return True
    
    def get_session(self, session_id: str) -> Optional[LogMonitorSession]:
        """获取监控会话"""
        return self.sessions.get(session_id)
    
    def list_sessions(self) -> List[Dict]:
        """列出所有会话"""
        with self.sessions_lock:
            return [
                {
                    'session_id': s.session_id,
                    'device_id': s.device_id,
                    'package_name': s.package_name,
                    'start_time': s.start_time.isoformat(),
                    'status': s.status,
                    'log_count': len(s.log_buffer)
                }
                for s in self.sessions.values()
            ]
    
    def get_recent_logs(self, session_id: str, limit: int = 100) -> List[Dict]:
        """获取最近的日志"""
        session = self.get_session(session_id)
        if not session:
            return []
        return session.get_recent_logs(limit)
    
    def get_alerts(self, session_id: str, limit: int = 100) -> List[Dict]:
        """获取告警记录"""
        session = self.get_session(session_id)
        if not session:
            return []
        
        alerts = session.alert_engine.get_alerts(
            device_id=session.device_id,
            limit=limit
        )
        return [alert.to_dict() for alert in alerts]
    
    def add_alert_rule(self, session_id: str, rule_data: Dict) -> bool:
        """添加告警规则"""
        session = self.get_session(session_id)
        if not session:
            return False
        
        from .alert_engine import AlertRule
        rule = AlertRule(
            id=rule_data.get('id', f"rule_{int(time.time())}"),
            name=rule_data.get('name', ''),
            type=rule_data.get('type', 'keyword'),
            pattern=rule_data.get('pattern', ''),
            severity=rule_data.get('severity', 'medium'),
            enabled=rule_data.get('enabled', True),
            action=rule_data.get('action', '')
        )
        return session.alert_engine.add_rule(rule)
    
    def acknowledge_alert(self, session_id: str, alert_id: str, user: str = "system") -> bool:
        """确认告警"""
        session = self.get_session(session_id)
        if not session:
            return False
        return session.alert_engine.acknowledge_alert(alert_id, user)
