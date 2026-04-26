import re
from typing import Union, Match, Tuple
from queue import Queue
from datetime import datetime

from .models.analysis_models import StartupRecord


class LogAnalyzer:
    """Analyzes log lines to detect critical errors and patterns."""

    # --- Target Package for Performance Analysis ---
    # This will be set dynamically
    TARGET_PACKAGE = "com.thunder.ktv"

    # Pre-compile regex patterns for performance, as requested in the design.
    REGEX_RULES = {
        # --- Fatal Errors --- (Highest Priority)
        'JAVA_CRASH': re.compile(r'E\/AndroidRuntime: FATAL EXCEPTION: (.+?)'),
        'NATIVE_CRASH_SIGSEGV': re.compile(r'(F\/libc|E\/DEBUG): signal 11 \(SIGSEGV\)'),
        'NATIVE_CRASH_SIGABRT': re.compile(r'(F\/libc|E\/DEBUG): signal 6 \(SIGABRT\)'),
        'ANR': re.compile(r'W\/ActivityManager: ANR in ([\w\.]+)\/([\w\.]+)'),
        
        # --- Common Exceptions --- (High Priority - for highlighting)
        'NULL_POINTER_EXCEPTION': re.compile(r'(E|W)\/.*?: java\.lang\.NullPointerException'),
        'OUT_OF_MEMORY_ERROR': re.compile(r'E\/.*?: java\.lang\.OutOfMemoryError'),
        'ILLEGAL_STATE_EXCEPTION': re.compile(r'(E|W)\/.*?: java\.lang\.IllegalStateException'),
        'IO_EXCEPTION': re.compile(r'E\/.*?: java\.io\.IOException'),

        # --- Performance & App Lifecycle --- (For structured analysis)
        'APP_START': re.compile(r'I\/ActivityManager: START u\d+ \{act=android\.intent\.action\.MAIN cat=\[android\.intent\.category\.LAUNCHER\] flg=0x10200000 cmp=([\w\.\/]+)\}'),
        'ACTIVITY_DISPLAYED': re.compile(r'I\/ActivityManager: Displayed ([\w\.\/]+): \+([\dms\.]+)'),

        # --- System Services Lifecycle ---
        'MEDIASERVER_DEATH': re.compile(r'I\/ActivityManager: Process mediaserver \(pid \d+\) has died'),
        'MEDIA_SERVICE_RESTART': re.compile(r'I\/init: Starting service \'media\'.*'),

        # --- GC Events ---
        'GARBAGE_COLLECTION': re.compile(r'I\/art: (?i)gc\s+((concurrent|semispace)\s+)?(freed|reclaimed)\s+\d+\(\d+[kKmMbB]?\)\s+\d+%\s+free'),
    }

    def __init__(self):
        """Initializes the Log Analyzer."""
        self.target_package = self.TARGET_PACKAGE
        # A queue to hold structured analysis results (e.g., StartupRecord objects)
        self.analysis_results = Queue()
        # A state machine to track ongoing events, like app startups.
        self._pending_starts = {}
        # Counter for GC events per PID
        self.gc_events = {}

    def _parse_pid(self, log_line: str) -> int:
        """Parses the PID from a standard threadtime log line."""
        try:
            # Format: Date Time PID TID Level Tag: Message
            # Split by whitespace, PID should be at index 2
            parts = log_line.split()
            if len(parts) > 2 and parts[2].isdigit():
                return int(parts[2])
        except Exception:
            pass
        return 0

    def analyze_line(self, log_line: str) -> Union[Tuple[str, Match], None]:
        """
        Analyzes a single log line against the rule set.

        :param log_line: The log line string to analyze.
        :return: A tuple containing the rule name (e.g., 'JAVA_CRASH') and the match object 
                 if a match is found, otherwise None.
        """
        # 优化：快速检查是否是错误级别日志（E/F），如果不是则跳过大部分正则匹配
        if len(log_line) > 20:
            # 检查日志级别（格式：日期 时间 PID TID Level Tag: Message）
            parts = log_line.split()
            if len(parts) >= 5:
                level_char = parts[4]
                # 如果不是错误级别，只检查特定规则（如启动事件、GC等）
                if level_char not in ['E', 'F', 'W']:
                    # 只检查非错误相关的规则
                    for rule_name in ['APP_START', 'ACTIVITY_DISPLAYED', 'GARBAGE_COLLECTION']:
                        if rule_name in self.REGEX_RULES:
                            match = self.REGEX_RULES[rule_name].search(log_line)
                            if match:
                                # 处理这些特殊规则
                                if rule_name == 'APP_START':
                                    component = match.group(1)
                                    if self.target_package and not component.startswith(self.target_package):
                                        return None
                                    timestamp_str = log_line.split(' ', 2)[:2]
                                    timestamp = self._parse_log_timestamp(' '.join(timestamp_str))
                                    if timestamp:
                                        self._pending_starts[component] = timestamp
                                        if len(self._pending_starts) > 50:
                                            keys_to_remove = list(self._pending_starts.keys())[:10]
                                            for k in keys_to_remove:
                                                del self._pending_starts[k]
                                    return None
                                elif rule_name == 'ACTIVITY_DISPLAYED':
                                    component = match.group(1)
                                    if self.target_package and not component.startswith(self.target_package):
                                        return None
                                    if component in self._pending_starts:
                                        start_time = self._pending_starts.pop(component)
                                        timestamp_str = log_line.split(' ', 2)[:2]
                                        end_time = self._parse_log_timestamp(' '.join(timestamp_str))
                                        if end_time:
                                            package, activity = (component.split('/') + [None])[:2]
                                            record = StartupRecord(
                                                package_name=package,
                                                activity_name=activity,
                                                start_time=start_time,
                                                end_time=end_time
                                            )
                                            self.analysis_results.put(record)
                                    return None
                                elif rule_name == 'GARBAGE_COLLECTION':
                                    pid = self._parse_pid(log_line)
                                    if pid > 0:
                                        self.gc_events[pid] = self.gc_events.get(pid, 0) + 1
                                    return None
                    return None  # 非错误级别且不匹配特殊规则，直接返回
        for rule_name, pattern in self.REGEX_RULES.items():
            match = pattern.search(log_line)
            if match:
                # --- Handle TTD (Time to Display) Analysis ---
                if rule_name == 'APP_START':
                    component = match.group(1)
                    # Filter to only track the target package
                    if self.target_package and not component.startswith(self.target_package):
                        return None
                    # The timestamp is at the start of the log line, e.g., "12-05 12:00:01.123"
                    timestamp_str = log_line.split(' ', 2)[:2]
                    timestamp = self._parse_log_timestamp(' '.join(timestamp_str))
                    if timestamp:
                        self._pending_starts[component] = timestamp
                        # Cleanup old pending starts if too many accumulate (e.g., missed end events)
                        if len(self._pending_starts) > 50:
                            # Remove oldest 10 items
                            keys_to_remove = list(self._pending_starts.keys())[:10]
                            for k in keys_to_remove:
                                del self._pending_starts[k]
                    return None # This is not an "error" to be highlighted, so we continue

                elif rule_name == 'ACTIVITY_DISPLAYED':
                    component = match.group(1)
                    # Filter to only track the target package
                    if self.target_package and not component.startswith(self.target_package):
                        return None
                    if component in self._pending_starts:
                        start_time = self._pending_starts.pop(component)
                        # The timestamp for the end event is also at the start of the line
                        timestamp_str = log_line.split(' ', 2)[:2]
                        end_time = self._parse_log_timestamp(' '.join(timestamp_str))
                        if end_time:
                            package, activity = (component.split('/') + [None])[:2]
                            record = StartupRecord(
                                package_name=package,
                                activity_name=activity,
                                start_time=start_time,
                                end_time=end_time
                            )
                            self.analysis_results.put(record)
                    return None # Also not an error for direct highlighting

                elif rule_name == 'GARBAGE_COLLECTION':
                    pid = self._parse_pid(log_line)
                    if pid > 0:
                        self.gc_events[pid] = self.gc_events.get(pid, 0) + 1
                    return None # Not an error, just for counting
                # Return the name of the rule that matched and the match object itself
                return rule_name, match
        return None

    def set_package_name(self, package_name: str):
        """Sets the target package name to filter logs."""
        self.target_package = package_name

    def _parse_log_timestamp(self, timestamp_str: str) -> Union[datetime, None]:
        """Parses a logcat timestamp (e.g., '12-05 12:00:01.123') into a datetime object."""
        try:
            # Assuming the year is the current year. Logcat doesn't include the year.
            now = datetime.now()
            return datetime.strptime(f"{now.year}-{timestamp_str}", "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return None
