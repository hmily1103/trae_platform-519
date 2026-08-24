#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志告警引擎
"""

import os
import re
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field

# 去重合并用：严重度排序与类型优先级（越具体越靠前）
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
_TYPE_PRIORITY = {"exception": 0, "crash": 1, "anr": 2, "keyword": 3, "level": 3, "frequency": 3}
# 同设备崩溃事件聚类窗口（秒）：窗口内连续命中的崩溃告警合并为单条（可用环境变量覆盖）
DEDUP_WINDOW_SECONDS = float(os.environ.get("LOG_ALERT_DEDUP_WINDOW", "2.0"))

# ========== #24 去噪 ==========
# 噪音黑名单：高频无意义系统日志（tag/内容），命中且无强异常信号时不参与告警匹配。
_NOISE_PATTERNS = re.compile(
    r'\bchatty\b.*identical'
    r'|BufferQueue(?:Producer|Consumer)?'
    r'|\bgralloc\b|GraphicBuffer|OpenGLRenderer|EGL_emulation|libEGL'
    r'|ViewRootImpl|InputMethodManager|InputEventReceiver'
    r'|audio_hw_primary|AudioFlinger|AudioTrack|SurfaceFlinger'
    r'|WifiHAL|wpa_supplicant|\bnetd\b|ConnectivityService|NetworkMonitor'
    r'|dex2oat|Zygote|installd|JobScheduler|AlarmManager.*send',
    re.I,
)
# 强异常信号：即使 tag 在噪音名单里，含这些关键字的行也绝不过滤（宁可多报不可漏报）
_STRONG_SIGNAL_RE = re.compile(
    r'FATAL|EXCEPTION|\bANR\b|SIGSEGV|SIGABRT|OutOfMemory|NullPointer'
    r'|Application Not Responding|F/libc|\bcrash',
    re.I,
)
# 重复刷屏抑制：同一签名的告警在窗口内最多触发 N 次，之后仅计数不重复告警
REPEAT_SUPPRESS_THRESHOLD = int(os.environ.get("LOG_ALERT_REPEAT_THRESHOLD", "3"))
REPEAT_SUPPRESS_WINDOW = float(os.environ.get("LOG_ALERT_REPEAT_WINDOW", "60.0"))
# 日志签名归一化：去掉时间戳/PID/数字等易变部分，用于识别"同一条日志反复刷屏"
_SIG_TS_RE = re.compile(r'^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s*')
_SIG_HEX_RE = re.compile(r'0x[0-9a-fA-F]+')
_SIG_NUM_RE = re.compile(r'\d+')
# 单事件关联日志行上限（防止极端刷屏撑爆内存/落盘体积）
MAX_RELATED_LINES = 200

# ========== #28 regex 规则支持 ==========
# 正则编译缓存：避免每行日志都重新编译（{pattern: compiled or None}，None=非法正则）
_REGEX_CACHE: Dict[str, Optional[Any]] = {}
_REGEX_CACHE_MAX = 500


def _get_compiled_regex(pattern: str) -> Optional[Any]:
    """获取编译后的正则（带缓存）。非法正则返回 None（缓存住，避免反复报错）。"""
    if pattern in _REGEX_CACHE:
        return _REGEX_CACHE[pattern]
    if len(_REGEX_CACHE) >= _REGEX_CACHE_MAX:
        _REGEX_CACHE.clear()
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        compiled = None
    _REGEX_CACHE[pattern] = compiled
    return compiled


def validate_rule_pattern(rule_type: str, pattern: str) -> Optional[str]:
    """规则保存前的 pattern 预检（#28）。

    :return: 错误信息字符串；合法返回 None。
    - regex 类型：正则必须能编译，否则规则会静默永不触发（P0 漏报隐患）
    - keyword 类型：不强制（非法正则会自动降级为字符串包含匹配）
    """
    if (rule_type or "").lower() != "regex":
        return None
    if not pattern or not pattern.strip():
        return "正则表达式不能为空"
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"正则表达式非法: {e}"
    return None


@dataclass
class AlertRule:
    """告警规则"""
    id: str
    name: str
    type: str  # keyword/regex/exception/anr/crash/level/frequency
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

        elif self.type == 'regex':
            # ========== #28 正则规则匹配（此前缺失该分支，规则静默永不触发） ==========
            # 编译缓存 + 非法正则安全降级：不抛异常、不匹配、缓存住避免每行日志重复报错
            compiled = _get_compiled_regex(self.pattern)
            if compiled is None:
                return False
            try:
                return bool(compiled.search(log_line))
            except Exception:
                return False

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
    self_heal: Optional[Dict] = None  # 自愈 Agent 分析结果（根因/建议/证据），Step2 回写
    # ========== #24 同事件聚合字段 ==========
    occurrence_count: int = 1                 # 同事件被合并/抑制的累计次数
    related_lines: List[str] = field(default_factory=list)  # 同事件关联的其他原始日志行
    last_seen: Optional[datetime] = None      # 最近一次同事件命中的时间

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
            'acknowledged_by': self.acknowledged_by,
            'self_heal': self.self_heal,
            'occurrence_count': self.occurrence_count,
            'related_lines': self.related_lines[-MAX_RELATED_LINES:],
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
        }


class AlertEngine:
    """告警引擎"""
    
    def __init__(self):
        self.rules: Dict[str, AlertRule] = {}
        self.alert_history: List[AlertRecord] = []
        self.frequency_counters: Dict[str, List[float]] = {}  # {rule_id: [timestamps]}
        # ========== #24 重复刷屏抑制 ==========
        self._repeat_counters: Dict[str, List[float]] = {}  # {log签名: [timestamps]}
        self._sig_last_alert: Dict[str, str] = {}           # {log签名: 最近一次告警id}
        self.suppressed_count = 0                           # 被抑制的告警总数（统计用）

        # 初始化默认规则
        self._init_default_rules()

    # ========== #24 去噪与刷屏抑制 ==========

    @staticmethod
    def _is_noise(log_line: str) -> bool:
        """噪音判定：命中系统噪音黑名单且不含强异常信号的行，直接跳过规则匹配。

        设计原则：宁可多报不可漏报——只要含 FATAL/EXCEPTION/ANR 等强信号一律放行。
        """
        if _STRONG_SIGNAL_RE.search(log_line):
            return False
        return bool(_NOISE_PATTERNS.search(log_line))

    @staticmethod
    def _log_signature(log_line: str) -> str:
        """日志签名归一化：剥离时间戳/PID/十六进制/数字，识别"同一条日志反复刷屏"。"""
        sig = _SIG_TS_RE.sub('', log_line)
        sig = _SIG_HEX_RE.sub('0x*', sig)
        sig = _SIG_NUM_RE.sub('#', sig)
        return sig.strip()[:300]

    def _is_repeat_suppressed(self, log_line: str, now_ts: float) -> Optional[str]:
        """重复刷屏抑制：同签名告警在时间窗内超过阈值后不再新建告警。

        :return: 被抑制时返回既有告警id（用于累加计数），否则返回 None
        """
        sig = self._log_signature(log_line)
        window_start = now_ts - REPEAT_SUPPRESS_WINDOW
        ts_list = [t for t in self._repeat_counters.get(sig, []) if t > window_start]
        ts_list.append(now_ts)
        self._repeat_counters[sig] = ts_list
        # 防止签名字典无限膨胀
        if len(self._repeat_counters) > 2000:
            oldest = sorted(self._repeat_counters.items(), key=lambda kv: kv[1][-1])[:1000]
            for k, _ in oldest:
                self._repeat_counters.pop(k, None)
                self._sig_last_alert.pop(k, None)
        if len(ts_list) > REPEAT_SUPPRESS_THRESHOLD:
            return self._sig_last_alert.get(sig, '')
        return None

    def _remember_alert_sig(self, log_line: str, alert_id: str):
        self._sig_last_alert[self._log_signature(log_line)] = alert_id
    
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
        """检查日志行，返回匹配的告警。

        #24 改造：
        ① 去噪——系统噪音行（无强异常信号）直接跳过；
        ② 刷屏抑制——同签名告警窗口内超阈值后只累计不新建；
        ③ 聚类合并改为"原地合并"——保留原告警 id/self_heal/acknowledged，
           被合并的行进 related_lines，且返回 [] 避免下游重复分析/重复推送。
        """
        current_time = datetime.now()

        # ① 去噪：噪音行直接跳过（含强异常信号的行永不过滤）
        if self._is_noise(log_line):
            return []

        # ③-前置：若该行属于正在进行中的崩溃事件（如 FATAL 后的堆栈行），
        # 追加进上一条告警的关联行，不触发新告警
        if self._absorb_into_ongoing_event(log_line, device_id, current_time):
            return []

        alerts = []
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

        if not alerts:
            return []

        # ② 刷屏抑制：同签名（去时间戳/PID/数字）的告警在窗口内超过阈值后，
        # 只累计 occurrence_count，不再新建告警、不再触发下游分析
        suppressed_id = self._is_repeat_suppressed(log_line, current_time.timestamp())
        if suppressed_id is not None:
            self.suppressed_count += 1
            for rec in reversed(self.alert_history):
                if rec.id == suppressed_id:
                    rec.occurrence_count += 1
                    rec.last_seen = current_time
                    break
            return []

        # 同一条日志行命中多条规则 → 合并为单条
        consolidated = self._merge_alerts(alerts)

        # ③ 时间窗聚类：与最近一条同设备崩溃事件"原地合并"（保 id/self_heal/acknowledged），
        # 合并成功则返回 []，下游不再重复推送/重复分析
        if self._cluster_merge_in_place(consolidated, device_id):
            return []

        self.alert_history.append(consolidated)
        self._remember_alert_sig(log_line, consolidated.id)
        # 只保留最近10000条告警记录
        if len(self.alert_history) > 10000:
            self.alert_history = self.alert_history[-10000:]
        return [consolidated]

    def _is_crash_family(self, alert: AlertRecord) -> bool:
        """判断告警是否属于同一崩溃家族（用于跨日志行聚类合并）。"""
        sig = (alert.log_line + " " + alert.rule_name).upper()
        return any(k in sig for k in (
            "EXCEPTION", "NULLPOINTER", "FATAL", "CRASH", "ANR", "OOM", "OUTOFMEMORY"
        ))

    # 崩溃事件延续行：堆栈帧 / Caused by / "... N more" / AndroidRuntime 续行
    _EVENT_CONT_RE = re.compile(
        r'^\s*(at\s+[\w.$<>\[\]]+\(|Caused by:|\.\.\.\s*\d+\s+more)'
        r'|AndroidRuntime.*(?:\bat\s|Caused by|Process:)',
    )

    def _absorb_into_ongoing_event(self, log_line: str, device_id: str, now: datetime) -> bool:
        """崩溃事件延续行吸收：FATAL 后的堆栈行归入上一条告警的 related_lines。

        条件：上一条告警属崩溃家族 + 同设备 + 时间窗内 + 当前行是明确的堆栈延续行。
        :return: True 表示已吸收（调用方应跳过该行的告警匹配）
        """
        if not self.alert_history:
            return False
        last = self.alert_history[-1]
        if last.device_id != device_id or not self._is_crash_family(last):
            return False
        anchor = last.last_seen or last.timestamp
        if anchor is None or abs((now - anchor).total_seconds()) > DEDUP_WINDOW_SECONDS:
            return False
        if not self._EVENT_CONT_RE.search(log_line):
            return False
        if len(last.related_lines) < MAX_RELATED_LINES:
            last.related_lines.append(log_line)
        last.last_seen = now
        return True

    def _cluster_merge_in_place(self, alert: AlertRecord, device_id: str) -> bool:
        """时间窗聚类（#24 改为原地合并）：新告警并入最近一条同设备崩溃告警。

        原实现会新建 AlertRecord 替换 history[-1]，导致 self_heal/acknowledged 丢失、
        且调用方拿到"重复告警"再次推送/分析。现改为直接修改既有记录：
        - 升级严重度/类型（取更高/更具体）
        - 规则名并集展示
        - 新日志行进 related_lines，occurrence_count+1
        :return: True 表示已并入既有告警
        """
        if not self.alert_history:
            return False
        last = self.alert_history[-1]
        within = abs((alert.timestamp - last.timestamp).total_seconds()) <= DEDUP_WINDOW_SECONDS
        if not (last.device_id == device_id and within
                and self._is_crash_family(alert) and self._is_crash_family(last)):
            return False
        # 严重度/类型：取更高严重度、更具体类型
        if _SEVERITY_RANK.get(alert.severity, 0) > _SEVERITY_RANK.get(last.severity, 0):
            last.severity = alert.severity
        if _TYPE_PRIORITY.get(alert.type, 9) < _TYPE_PRIORITY.get(last.type, 9):
            last.type = alert.type
        # 规则名/ID 并集
        names = {n.strip() for n in last.rule_name.split('/')} | {alert.rule_name}
        ids = {i.strip() for i in last.rule_id.split('/')} | {alert.rule_id}
        last.rule_name = " / ".join(sorted(names))
        last.rule_id = " / ".join(sorted(ids))
        last.message = f"{last.rule_name}: {last.log_line[:100]}"
        # 关联行与计数
        if alert.log_line and alert.log_line != last.log_line \
                and len(last.related_lines) < MAX_RELATED_LINES:
            last.related_lines.append(alert.log_line)
        last.occurrence_count += 1
        last.last_seen = alert.timestamp
        self._remember_alert_sig(alert.log_line, last.id)
        return True

    def _merge_alerts(self, alerts: List[AlertRecord]) -> AlertRecord:
        """将同一条日志行命中的多条规则告警合并为单条。

        严重度取最高，类型取最具体（exception>crash>anr>keyword），
        规则名合并展示（如 "FATAL EXCEPTION / 空指针异常"）。
        """
        if len(alerts) == 1:
            return alerts[0]
        primary = max(
            alerts,
            key=lambda a: (_SEVERITY_RANK.get(a.severity, 0), -_TYPE_PRIORITY.get(a.type, 9)),
        )
        rule_names = " / ".join(sorted({a.rule_name for a in alerts}))
        rule_ids = " / ".join(sorted({a.rule_id for a in alerts}))
        return AlertRecord(
            id=primary.id,
            rule_id=rule_ids,
            rule_name=rule_names,
            severity=primary.severity,
            type=primary.type,
            message=f"{rule_names}: {primary.log_line[:100]}",
            log_line=primary.log_line,
            timestamp=primary.timestamp,
            device_id=primary.device_id,
            package_name=primary.package_name,
        )
    
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
