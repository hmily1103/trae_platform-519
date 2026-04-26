#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志告警引擎
"""

import re
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field


@dataclass
class AlertRule:
    """告警规则"""
    id: str
    name: str
    type: str  # keyword/exception/anr/crash/level/frequency
    pattern: str  # 正则表达式或关键词
    severity: str  # high/medium/low
    enabled: bool = True
    description: str = ""
    action: str = ""  # screenshot/shell:.../none
    frequency_threshold: int = 1  # 频率告警：N次/时间窗口
    frequency_window_seconds: int = 60  # 频率告警时间窗口
    
    def matches(self, log_line: str) -> bool:
        """检查日志行是否匹配规则"""
        if not self.enabled:
            return False
        
        if self.type == 'keyword':
            # 关键词匹配（支持正则）
            try:
                return bool(re.search(self.pattern, log_line, re.IGNORECASE))
            except Exception:
                # 如果不是正则，作为普通字符串匹配
                return self.pattern.lower() in log_line.lower()
        
        elif self.type == 'exception':
            # 异常匹配（自动检测）
            exception_patterns = [
                r'FATAL\s+EXCEPTION',
                r'java\.lang\.(NullPointerException|OutOfMemoryError|IllegalStateException)',
                r'Exception.*at\s+',
            ]
            for pattern in exception_patterns:
                if re.search(pattern, log_line, re.IGNORECASE):
                    return True
            return False
        
        elif self.type == 'anr':
            # ANR匹配
            anr_patterns = [
                r'ANR\s+in',
                r'Application\s+Not\s+Responding',
                r'ActivityManager.*ANR',
            ]
            for pattern in anr_patterns:
                if re.search(pattern, log_line, re.IGNORECASE):
                    return True
            return False
        
        elif self.type == 'crash':
            # Crash匹配
            crash_patterns = [
                r'FATAL\s+EXCEPTION',
                r'SIGSEGV|SIGABRT',
                r'F/libc.*signal',
            ]
            for pattern in crash_patterns:
                if re.search(pattern, log_line, re.IGNORECASE):
                    return True
            return False
        
        elif self.type == 'level':
            # 日志级别匹配
            level_map = {
                'Error': r'^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+E\s+',
                'Fatal': r'^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+F\s+',
            }
            if self.pattern in level_map:
                return bool(re.search(level_map[self.pattern], log_line))
            return False
        
        return False


@dataclass
class AlertRecord:
    """告警记录"""
    id: str
    rule_id: str
    rule_name: str
    severity: str
    type: str
    message: str
    log_line: str
    timestamp: datetime
    device_id: str
    package_name: str = ""
    action_taken: str = ""  # 记录触发的动作
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: str = ""
    
    def to_dict(self):
        return {
            'id': self.id,
            'rule_id': self.rule_id,
            'rule_name': self.rule_name,
            'severity': self.severity,
            'type': self.type,
            'message': self.message,
            'log_line': self.log_line,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'device_id': self.device_id,
            'package_name': self.package_name,
            'action_taken': self.action_taken,
            'acknowledged': self.acknowledged,
            'acknowledged_at': self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            'acknowledged_by': self.acknowledged_by
        }


class AlertEngine:
    """告警引擎"""
    
    def __init__(self):
        self.rules: Dict[str, AlertRule] = {}
        self.alert_history: List[AlertRecord] = []
        self.frequency_counters: Dict[str, List[float]] = {}  # {rule_id: [timestamps]}
        
        # 初始化默认规则
        self._init_default_rules()
    
    def _init_default_rules(self):
        """初始化默认告警规则"""
        default_rules = [
            AlertRule(
                id='rule_fatal_exception',
                name='FATAL EXCEPTION',
                type='exception',
                pattern='FATAL EXCEPTION',
                severity='high',
                description='检测到致命异常'
            ),
            AlertRule(
                id='rule_anr',
                name='ANR检测',
                type='anr',
                pattern='ANR',
                severity='high',
                description='检测到ANR（应用无响应）'
            ),
            AlertRule(
                id='rule_crash',
                name='应用崩溃',
                type='crash',
                pattern='crash',
                severity='high',
                description='检测到应用崩溃'
            ),
            AlertRule(
                id='rule_oom',
                name='内存溢出',
                type='keyword',
                pattern='OutOfMemoryError',
                severity='high',
                description='检测到内存溢出'
            ),
            AlertRule(
                id='rule_npe',
                name='空指针异常',
                type='keyword',
                pattern='NullPointerException',
                severity='medium',
                description='检测到空指针异常'
            ),
        ]
        
        for rule in default_rules:
            self.rules[rule.id] = rule
    
    def add_rule(self, rule: AlertRule) -> bool:
        """添加告警规则"""
        if rule.id in self.rules:
            return False
        self.rules[rule.id] = rule
        return True
    
    def update_rule(self, rule: AlertRule) -> bool:
        """更新告警规则"""
        if rule.id not in self.rules:
            return False
        self.rules[rule.id] = rule
        return True
    
    def delete_rule(self, rule_id: str) -> bool:
        """删除告警规则"""
        if rule_id in self.rules:
            del self.rules[rule_id]
            if rule_id in self.frequency_counters:
                del self.frequency_counters[rule_id]
            return True
        return False
    
    def check_log(self, log_line: str, device_id: str, package_name: str = "") -> List[AlertRecord]:
        """检查日志行，返回匹配的告警"""
        alerts = []
        current_time = datetime.now()
        
        for rule_id, rule in self.rules.items():
            if rule.matches(log_line):
                # 频率告警检查
                if rule.type == 'frequency':
                    if rule_id not in self.frequency_counters:
                        self.frequency_counters[rule_id] = []
                    
                    # 清理过期的时间戳
                    window_start = current_time.timestamp() - rule.frequency_window_seconds
                    self.frequency_counters[rule_id] = [
                        ts for ts in self.frequency_counters[rule_id] if ts > window_start
                    ]
                    
                    # 添加当前时间戳
                    self.frequency_counters[rule_id].append(current_time.timestamp())
                    
                    # 检查是否超过阈值
                    if len(self.frequency_counters[rule_id]) < rule.frequency_threshold:
                        continue  # 未达到频率阈值，不触发告警
                
                # 创建告警记录
                alert_id = f"alert_{int(current_time.timestamp() * 1000)}_{len(self.alert_history)}"
                alert = AlertRecord(
                    id=alert_id,
                    rule_id=rule.id,
                    rule_name=rule.name,
                    severity=rule.severity,
                    type=rule.type,
                    message=f"{rule.name}: {log_line[:100]}",
                    log_line=log_line,
                    timestamp=current_time,
                    device_id=device_id,
                    package_name=package_name
                )
                
                alerts.append(alert)
                self.alert_history.append(alert)
                
                # 只保留最近10000条告警记录
                if len(self.alert_history) > 10000:
                    self.alert_history = self.alert_history[-10000:]
        
        return alerts
    
    def get_alerts(self, 
                   device_id: Optional[str] = None,
                   severity: Optional[str] = None,
                   acknowledged: Optional[bool] = None,
                   start_time: Optional[datetime] = None,
                   end_time: Optional[datetime] = None,
                   limit: int = 100) -> List[AlertRecord]:
        """获取告警记录"""
        alerts = list(self.alert_history)
        
        # 筛选
        if device_id:
            alerts = [a for a in alerts if a.device_id == device_id]
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        if acknowledged is not None:
            alerts = [a for a in alerts if a.acknowledged == acknowledged]
        if start_time:
            alerts = [a for a in alerts if a.timestamp and a.timestamp >= start_time]
        if end_time:
            alerts = [a for a in alerts if a.timestamp and a.timestamp <= end_time]
        
        # 按时间倒序
        alerts.sort(key=lambda x: x.timestamp or datetime.min, reverse=True)
        
        # 限制数量
        return alerts[:limit]
    
    def acknowledge_alert(self, alert_id: str, user: str = "system") -> bool:
        """确认告警"""
        for alert in self.alert_history:
            if alert.id == alert_id:
                alert.acknowledged = True
                alert.acknowledged_at = datetime.now()
                alert.acknowledged_by = user
                return True
        return False
    
    def get_statistics(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        """获取告警统计"""
        alerts = self.get_alerts(device_id=device_id, limit=10000)
        
        total = len(alerts)
        by_severity = {
            'high': len([a for a in alerts if a.severity == 'high']),
            'medium': len([a for a in alerts if a.severity == 'medium']),
            'low': len([a for a in alerts if a.severity == 'low'])
        }
        by_type = {}
        for alert in alerts:
            alert_type = alert.type
            by_type[alert_type] = by_type.get(alert_type, 0) + 1
        
        acknowledged_count = len([a for a in alerts if a.acknowledged])
        
        return {
            'total': total,
            'by_severity': by_severity,
            'by_type': by_type,
            'acknowledged': acknowledged_count,
            'unacknowledged': total - acknowledged_count
        }
