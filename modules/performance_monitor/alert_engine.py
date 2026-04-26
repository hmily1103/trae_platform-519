"""
性能告警引擎
支持基于阈值的性能告警
"""
import time
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class PerformanceAlertRule:
    """性能告警规则"""
    id: str
    name: str
    metric: str  # cpu, memory, fps, jank, network_rx, network_tx
    operator: str  # >, <, >=, <=, ==
    threshold: float
    severity: str = 'medium'  # high, medium, low
    enabled: bool = True
    description: str = ''
    duration: int = 0  # 持续时间（秒），0表示立即告警


@dataclass
class PerformanceAlert:
    """性能告警记录"""
    id: str
    rule_id: str
    rule_name: str
    metric: str
    current_value: float
    threshold: float
    severity: str
    timestamp: datetime
    device_id: str = ''
    package_name: str = ''
    session_id: str = ''
    acknowledged: bool = False
    acknowledged_by: str = ''
    acknowledged_at: Optional[datetime] = None
    message: str = ''
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'rule_id': self.rule_id,
            'rule_name': self.rule_name,
            'metric': self.metric,
            'current_value': self.current_value,
            'threshold': self.threshold,
            'severity': self.severity,
            'timestamp': self.timestamp.isoformat(),
            'device_id': self.device_id,
            'package_name': self.package_name,
            'session_id': self.session_id,
            'acknowledged': self.acknowledged,
            'acknowledged_by': self.acknowledged_by,
            'acknowledged_at': self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            'message': self.message
        }


class PerformanceAlertEngine:
    """性能告警引擎"""
    
    def __init__(self):
        """初始化告警引擎"""
        self.rules: Dict[str, PerformanceAlertRule] = {}
        self.alerts: List[PerformanceAlert] = []
        self.alert_history: List[PerformanceAlert] = []  # 历史告警
        self.max_alerts = 1000  # 最大告警数量
        self.metric_values: Dict[str, List[Dict]] = {}  # 用于持续时间判断 {rule_id: [{timestamp, value}]}
    
    def add_rule(self, rule: PerformanceAlertRule) -> bool:
        """
        添加告警规则
        
        :param rule: 告警规则
        :return: 是否成功
        """
        if rule.id in self.rules:
            return False
        
        self.rules[rule.id] = rule
        self.metric_values[rule.id] = []
        return True
    
    def delete_rule(self, rule_id: str) -> bool:
        """
        删除告警规则
        
        :param rule_id: 规则ID
        :return: 是否成功
        """
        if rule_id in self.rules:
            del self.rules[rule_id]
            if rule_id in self.metric_values:
                del self.metric_values[rule_id]
            return True
        return False
    
    def update_rule(self, rule: PerformanceAlertRule) -> bool:
        """
        更新告警规则
        
        :param rule: 告警规则
        :return: 是否成功
        """
        if rule.id not in self.rules:
            return False
        
        self.rules[rule.id] = rule
        if rule.id not in self.metric_values:
            self.metric_values[rule.id] = []
        return True
    
    def check_performance(self, snapshot: Dict, device_id: str = '', 
                         package_name: str = '', session_id: str = '') -> List[PerformanceAlert]:
        """
        检查性能数据并生成告警
        
        :param snapshot: 性能快照字典
        :param device_id: 设备ID
        :param package_name: 包名
        :param session_id: 会话ID
        :return: 新生成的告警列表
        """
        new_alerts = []
        current_time = time.time()
        
        for rule_id, rule in self.rules.items():
            if not rule.enabled:
                continue
            
            # 获取指标值
            metric_value = self._get_metric_value(snapshot, rule.metric)
            if metric_value is None:
                continue
            
            # 检查是否触发告警
            triggered = self._check_threshold(metric_value, rule.operator, rule.threshold)
            
            if triggered:
                # 记录指标值用于持续时间判断
                self.metric_values[rule_id].append({
                    'timestamp': current_time,
                    'value': metric_value
                })
                
                # 清理过期数据（只保留最近1分钟的数据）
                self.metric_values[rule_id] = [
                    v for v in self.metric_values[rule_id]
                    if current_time - v['timestamp'] < 60
                ]
                
                # 检查持续时间
                if rule.duration > 0:
                    # 需要持续一段时间才告警
                    if not self._check_duration(rule_id, rule.duration):
                        continue
                
                # 检查是否已有未确认的相同告警
                if self._has_active_alert(rule_id):
                    continue
                
                # 创建告警
                alert = PerformanceAlert(
                    id=f"alert_{int(current_time * 1000)}_{rule_id}",
                    rule_id=rule_id,
                    rule_name=rule.name,
                    metric=rule.metric,
                    current_value=metric_value,
                    threshold=rule.threshold,
                    severity=rule.severity,
                    timestamp=datetime.now(),
                    device_id=device_id,
                    package_name=package_name,
                    session_id=session_id,
                    message=self._generate_message(rule, metric_value)
                )
                
                self.alerts.append(alert)
                self.alert_history.append(alert)
                new_alerts.append(alert)
                
                # 限制告警数量
                if len(self.alerts) > self.max_alerts:
                    self.alerts.pop(0)
                if len(self.alert_history) > self.max_alerts * 2:
                    self.alert_history.pop(0)
        
        return new_alerts
    
    def _get_metric_value(self, snapshot: Dict, metric: str) -> Optional[float]:
        """获取指标值"""
        metric_map = {
            'cpu': 'cpu_usage',
            'memory': 'total_pss',  # 返回 KB，需要转换为 MB
            'fps': 'fps',
            'jank': 'jank_count',
            'network_rx': 'network_rx_kb',
            'network_tx': 'network_tx_kb'
        }
        
        key = metric_map.get(metric)
        if not key:
            return None
        
        value = snapshot.get(key)
        if value is None:
            return None
        
        # 内存需要转换为 MB
        if metric == 'memory':
            return value / 1024.0
        
        return float(value)
    
    def _check_threshold(self, value: float, operator: str, threshold: float) -> bool:
        """检查阈值"""
        if operator == '>':
            return value > threshold
        elif operator == '<':
            return value < threshold
        elif operator == '>=':
            return value >= threshold
        elif operator == '<=':
            return value <= threshold
        elif operator == '==':
            return abs(value - threshold) < 0.01  # 浮点数比较
        else:
            return False
    
    def _check_duration(self, rule_id: str, duration: int) -> bool:
        """检查持续时间"""
        if rule_id not in self.metric_values:
            return False
        
        values = self.metric_values[rule_id]
        if len(values) < 2:
            return False
        
        current_time = time.time()
        # 检查最近 duration 秒内是否一直满足条件
        recent_values = [v for v in values if current_time - v['timestamp'] <= duration]
        
        if len(recent_values) < 2:
            return False
        
        # 检查是否持续满足条件（简单判断：最近的值都在阈值范围内）
        rule = self.rules[rule_id]
        all_triggered = all(
            self._check_threshold(v['value'], rule.operator, rule.threshold)
            for v in recent_values
        )
        
        return all_triggered
    
    def _has_active_alert(self, rule_id: str) -> bool:
        """检查是否有未确认的相同告警"""
        for alert in self.alerts:
            if alert.rule_id == rule_id and not alert.acknowledged:
                return True
        return False
    
    def _generate_message(self, rule: PerformanceAlertRule, value: float) -> str:
        """生成告警消息"""
        metric_names = {
            'cpu': 'CPU使用率',
            'memory': '内存使用',
            'fps': 'FPS',
            'jank': 'Jank次数',
            'network_rx': '网络接收速度',
            'network_tx': '网络发送速度'
        }
        
        metric_name = metric_names.get(rule.metric, rule.metric)
        unit = 'MB' if rule.metric == 'memory' else ('%' if rule.metric == 'cpu' else ('KB/s' if 'network' in rule.metric else ''))
        
        return f"{metric_name} {value:.2f}{unit} {rule.operator} {rule.threshold}{unit}"
    
    def get_alerts(self, severity: Optional[str] = None, 
                   acknowledged: Optional[bool] = None,
                   limit: int = 100) -> List[PerformanceAlert]:
        """
        获取告警列表
        
        :param severity: 严重程度过滤
        :param acknowledged: 是否已确认
        :param limit: 限制数量
        :return: 告警列表
        """
        alerts = self.alerts.copy()
        
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        
        if acknowledged is not None:
            alerts = [a for a in alerts if a.acknowledged == acknowledged]
        
        # 按时间倒序排列
        alerts.sort(key=lambda x: x.timestamp, reverse=True)
        
        return alerts[:limit]
    
    def acknowledge_alert(self, alert_id: str, user: str = 'system') -> bool:
        """
        确认告警
        
        :param alert_id: 告警ID
        :param user: 确认用户
        :return: 是否成功
        """
        for alert in self.alerts:
            if alert.id == alert_id:
                alert.acknowledged = True
                alert.acknowledged_by = user
                alert.acknowledged_at = datetime.now()
                return True
        return False
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取告警统计"""
        total = len(self.alerts)
        by_severity = {'high': 0, 'medium': 0, 'low': 0}
        by_metric = {}
        acknowledged = 0
        unacknowledged = 0
        
        for alert in self.alerts:
            by_severity[alert.severity] = by_severity.get(alert.severity, 0) + 1
            by_metric[alert.metric] = by_metric.get(alert.metric, 0) + 1
            
            if alert.acknowledged:
                acknowledged += 1
            else:
                unacknowledged += 1
        
        return {
            'total': total,
            'by_severity': by_severity,
            'by_metric': by_metric,
            'acknowledged': acknowledged,
            'unacknowledged': unacknowledged
        }
