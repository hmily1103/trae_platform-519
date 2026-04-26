"""
优先级队列模块 - 优化版
实现关键日志优先保留机制
"""
import queue
import threading
from collections import deque
from typing import Tuple, Optional, Any


class PriorityLogQueue:
    """
    优先级日志队列
    关键日志优先保留，普通日志在队列满时丢弃
    """
    
    # 优先级定义
    PRIORITY_CRITICAL = 0  # Crash, ANR, OOM 等致命错误
    PRIORITY_HIGH = 1      # NullPointer, IllegalState 等严重错误
    PRIORITY_NORMAL = 2    # 普通日志
    
    def __init__(self, maxsize: int = 5000, critical_reserve: int = 1000):
        """
        初始化优先级队列
        
        :param maxsize: 队列最大容量
        :param critical_reserve: 为关键日志保留的空间
        """
        self.maxsize = maxsize
        self.critical_reserve = critical_reserve
        self.normal_maxsize = maxsize - critical_reserve
        
        # 使用两个队列分离关键日志和普通日志
        self.critical_queue = deque(maxlen=critical_reserve)
        self.normal_queue = deque(maxlen=self.normal_maxsize)
        
        # 线程锁
        self.lock = threading.Lock()
        
        # 统计信息
        self.total_put = 0
        self.total_dropped = 0
        self.critical_put = 0
        self.normal_put = 0
    
    def put(self, item: Tuple[str, Any], priority: int = PRIORITY_NORMAL, block: bool = False, timeout: Optional[float] = None):
        """
        添加日志到队列
        
        :param item: (log_line, analysis_result) 元组
        :param priority: 优先级（PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_NORMAL）
        :param block: 是否阻塞（暂不支持）
        :param timeout: 超时时间（暂不支持）
        """
        with self.lock:
            self.total_put += 1
            
            if priority <= self.PRIORITY_HIGH:
                # 关键日志：优先保留
                self.critical_queue.append(item)
                self.critical_put += 1
            else:
                # 普通日志：检查空间
                if len(self.normal_queue) >= self.normal_maxsize:
                    # 队列满，丢弃最旧的普通日志
                    self.total_dropped += 1
                    if len(self.normal_queue) > 0:
                        self.normal_queue.popleft()
                self.normal_queue.append(item)
                self.normal_put += 1
    
    def get(self, block: bool = False, timeout: Optional[float] = None) -> Tuple[str, Any]:
        """
        从队列获取日志（优先返回关键日志）
        
        :param block: 是否阻塞
        :param timeout: 超时时间
        :return: (log_line, analysis_result) 元组
        """
        with self.lock:
            # 优先返回关键日志
            if len(self.critical_queue) > 0:
                return self.critical_queue.popleft()
            elif len(self.normal_queue) > 0:
                return self.normal_queue.popleft()
            else:
                raise queue.Empty("Queue is empty")
    
    def get_nowait(self) -> Tuple[str, Any]:
        """非阻塞获取日志"""
        return self.get(block=False)
    
    def empty(self) -> bool:
        """检查队列是否为空"""
        with self.lock:
            return len(self.critical_queue) == 0 and len(self.normal_queue) == 0
    
    def qsize(self) -> int:
        """获取队列总大小"""
        with self.lock:
            return len(self.critical_queue) + len(self.normal_queue)
    
    def critical_size(self) -> int:
        """获取关键日志队列大小"""
        with self.lock:
            return len(self.critical_queue)
    
    def normal_size(self) -> int:
        """获取普通日志队列大小"""
        with self.lock:
            return len(self.normal_queue)
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        with self.lock:
            return {
                'total_put': self.total_put,
                'total_dropped': self.total_dropped,
                'critical_put': self.critical_put,
                'normal_put': self.normal_put,
                'current_size': self.qsize(),
                'critical_size': self.critical_size(),
                'normal_size': self.normal_size(),
                'drop_rate': self.total_dropped / max(self.total_put, 1) * 100
            }
    
    def clear(self):
        """清空队列"""
        with self.lock:
            self.critical_queue.clear()
            self.normal_queue.clear()


def get_log_priority(analysis_result: Optional[Tuple[str, Any]]) -> int:
    """
    根据分析结果确定日志优先级
    
    :param analysis_result: log_analyzer.analyze_line() 的返回值
    :return: 优先级值
    """
    if analysis_result is None:
        return PriorityLogQueue.PRIORITY_NORMAL
    
    rule_name, _ = analysis_result
    
    # 致命错误：最高优先级
    if rule_name in ['JAVA_CRASH', 'NATIVE_CRASH_SIGSEGV', 'NATIVE_CRASH_SIGABRT', 'ANR', 'OUT_OF_MEMORY_ERROR']:
        return PriorityLogQueue.PRIORITY_CRITICAL
    
    # 严重错误：高优先级
    if rule_name in ['NULL_POINTER_EXCEPTION', 'ILLEGAL_STATE_EXCEPTION', 'IO_EXCEPTION']:
        return PriorityLogQueue.PRIORITY_HIGH
    
    # 普通日志
    return PriorityLogQueue.PRIORITY_NORMAL
