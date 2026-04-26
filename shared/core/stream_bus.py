"""
Stream Bus：统一数据流总线，各模块通过 publish 推送，Dashboard/Reports 通过 get_recent 拉取。

- publish(module, stream_type, payload) 推送事件
- get_recent(limit, stream_types) 获取最近事件
- 各模块可在任务完成/状态变更时调用 publish，实现统一数据流
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class StreamEvent:
    """流事件"""
    module: str
    stream_type: str  # metrics | logs | report | status
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


class StreamBus:
    _instance: Optional["StreamBus"] = None
    _lock = threading.RLock()

    def __init__(self, max_events: int = 1000):
        self._events: deque = deque(maxlen=max_events)
        self._lock_events = threading.RLock()

    @classmethod
    def get_instance(cls, max_events: int = 1000) -> "StreamBus":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(max_events=max_events)
        return cls._instance

    def publish(self, module: str, stream_type: str, payload: Dict[str, Any]) -> None:
        """推送事件到总线"""
        ev = StreamEvent(module=module, stream_type=stream_type, payload=payload)
        with self._lock_events:
            self._events.append(ev)

    def get_recent(self, limit: int = 50, stream_types: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        """获取最近 N 条事件（用于 API 拉取）"""
        with self._lock_events:
            events = list(self._events)[-limit:]
        result = []
        for ev in events:
            if stream_types and ev.stream_type not in stream_types:
                continue
            result.append({
                "module": ev.module,
                "stream_type": ev.stream_type,
                "payload": ev.payload,
                "timestamp": ev.timestamp,
            })
        return result


def get_stream_bus() -> StreamBus:
    return StreamBus.get_instance()
