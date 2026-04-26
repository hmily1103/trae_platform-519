"""
人眼感知卡顿分析器
整合多种数据源，提供更准确的卡顿检测
"""
import time
import math
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import deque


@dataclass
class FrameTimeData:
    """单帧时间数据"""
    frame_time_ms: float  # 帧渲染时间（毫秒）
    timestamp: float  # 时间戳
    is_jank: bool = False  # 是否为标准Jank（>16.67ms）


@dataclass
class PerceptualStallEvent:
    """人眼感知卡顿事件"""
    start_time: float  # 卡顿开始时间
    end_time: float  # 卡顿结束时间
    duration_ms: float  # 卡顿持续时间（毫秒）
    severity: str  # 严重程度：mild/moderate/severe
    stall_score: float  # 卡顿评分
    frame_count: int  # 涉及的帧数
    avg_frame_time_ms: float  # 平均帧时间
    max_frame_time_ms: float  # 最大帧时间
    description: str = ""  # 描述


@dataclass
class PerceptualStallMetrics:
    """人眼感知卡顿指标"""
    total_stall_events: int = 0  # 总卡顿事件数
    total_stall_duration_ms: float = 0.0  # 总卡顿时长
    stall_score: float = 0.0  # 累计卡顿评分
    mild_stalls: int = 0  # 轻微卡顿次数
    moderate_stalls: int = 0  # 中等卡顿次数
    severe_stalls: int = 0  # 严重卡顿次数
    current_stall_duration_ms: float = 0.0  # 当前卡顿持续时间（如果正在卡顿）
    is_stalling: bool = False  # 当前是否在卡顿
    avg_frame_time_ms: float = 0.0  # 平均帧时间
    frame_time_variance: float = 0.0  # 帧时间方差（波动性）


class PerceptualStallAnalyzer:
    """
    人眼感知卡顿分析器
    
    算法原理：
    1. 基于帧时间序列分析，检测帧时间突然增加
    2. 考虑连续多帧延迟（比单帧延迟更明显）
    3. 使用加权评分，严重卡顿权重更高
    4. 区分不同严重程度的卡顿
    """
    
    def __init__(self, target_fps: int = 60, window_size: int = 120):
        """
        初始化分析器
        
        :param target_fps: 目标帧率（默认60fps）
        :param window_size: 滑动窗口大小（保留最近N帧数据）
        """
        self.target_fps = target_fps
        self.expected_frame_time_ms = 1000.0 / target_fps  # 期望帧时间（60fps = 16.67ms）
        self.window_size = window_size
        
        # 帧时间序列（滑动窗口）
        self.frame_times: deque = deque(maxlen=window_size)
        
        # 卡顿事件历史
        self.stall_events: List[PerceptualStallEvent] = []
        
        # 当前状态
        self.current_stall_start: Optional[float] = None
        self.current_stall_frames: List[FrameTimeData] = []
        
        # 累计指标
        self.total_stall_score = 0.0
        self.total_stall_duration_ms = 0.0
        
        # 阈值配置
        self.mild_threshold_ms = self.expected_frame_time_ms * 1.5  # 25ms (1.5倍)
        self.moderate_threshold_ms = self.expected_frame_time_ms * 2.0  # 33.33ms (2倍)
        self.severe_threshold_ms = self.expected_frame_time_ms * 3.0  # 50ms (3倍)
        
        # 连续卡顿检测阈值
        self.consecutive_jank_threshold = 3  # 连续3帧卡顿视为感知卡顿
        
    def add_frame_time(self, frame_time_ms: float, timestamp: Optional[float] = None) -> Optional[PerceptualStallEvent]:
        """
        添加一帧的时间数据
        
        :param frame_time_ms: 帧渲染时间（毫秒）
        :param timestamp: 时间戳（可选，默认使用当前时间）
        :return: 如果检测到卡顿事件结束，返回事件对象；否则返回None
        """
        if timestamp is None:
            timestamp = time.time()
        
        # 创建帧数据
        frame_data = FrameTimeData(
            frame_time_ms=frame_time_ms,
            timestamp=timestamp,
            is_jank=frame_time_ms > self.expected_frame_time_ms
        )
        
        # 添加到滑动窗口
        self.frame_times.append(frame_data)
        
        # 检测卡顿
        return self._detect_stall(frame_data)
    
    def _detect_stall(self, frame_data: FrameTimeData) -> Optional[PerceptualStallEvent]:
        """
        检测卡顿
        
        :param frame_data: 当前帧数据
        :return: 如果卡顿结束，返回事件对象；否则返回None
        """
        frame_time = frame_data.frame_time_ms
        is_jank = frame_time > self.expected_frame_time_ms
        
        # 判断是否超过阈值
        is_stall = frame_time > self.mild_threshold_ms
        
        if is_stall:
            # 开始或继续卡顿
            if self.current_stall_start is None:
                # 开始新的卡顿事件
                self.current_stall_start = frame_data.timestamp
                self.current_stall_frames = [frame_data]
            else:
                # 继续当前卡顿
                self.current_stall_frames.append(frame_data)
        else:
            # 帧时间正常
            if self.current_stall_start is not None:
                # 卡顿结束，生成事件
                event = self._create_stall_event()
                self.current_stall_start = None
                self.current_stall_frames = []
                return event
        
        return None
    
    def _create_stall_event(self) -> PerceptualStallEvent:
        """
        创建卡顿事件
        
        :return: 卡顿事件对象
        """
        if not self.current_stall_frames:
            return None
        
        start_time = self.current_stall_start
        end_time = self.current_stall_frames[-1].timestamp
        duration_ms = (end_time - start_time) * 1000.0
        
        frame_count = len(self.current_stall_frames)
        frame_times = [f.frame_time_ms for f in self.current_stall_frames]
        avg_frame_time = sum(frame_times) / frame_count
        max_frame_time = max(frame_times)
        
        # 计算卡顿评分（使用加权算法）
        stall_score = self._calculate_stall_score(frame_times, duration_ms)
        
        # 判断严重程度
        severity = self._determine_severity(avg_frame_time, max_frame_time, duration_ms, frame_count)
        
        # 生成描述
        description = self._generate_description(severity, frame_count, avg_frame_time, duration_ms)
        
        event = PerceptualStallEvent(
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            severity=severity,
            stall_score=stall_score,
            frame_count=frame_count,
            avg_frame_time_ms=avg_frame_time,
            max_frame_time_ms=max_frame_time,
            description=description
        )
        
        # 更新累计指标
        self.stall_events.append(event)
        self.total_stall_score += stall_score
        self.total_stall_duration_ms += duration_ms
        
        return event
    
    def _calculate_stall_score(self, frame_times: List[float], duration_ms: float) -> float:
        """
        计算卡顿评分
        
        算法：
        - 基于帧时间超过期望值的程度
        - 考虑连续多帧的影响（连续卡顿权重更高）
        - 使用指数权重，严重卡顿权重更高
        
        :param frame_times: 帧时间列表
        :param duration_ms: 卡顿持续时间
        :return: 卡顿评分
        """
        score = 0.0
        
        for frame_time in frame_times:
            # 计算超出期望值的部分
            excess = frame_time - self.expected_frame_time_ms
            if excess > 0:
                # 使用指数权重：pow(excess, 1.2)
                # 严重卡顿（如50ms）的权重远高于轻微卡顿（如20ms）
                weight = math.pow(excess, 1.2)
                score += weight
        
        # 连续卡顿加成：如果连续多帧卡顿，额外加权
        if len(frame_times) >= self.consecutive_jank_threshold:
            score *= 1.3
        
        # 持续时间加成：长时间卡顿权重更高
        if duration_ms > 100:  # 超过100ms
            score *= 1.2
        elif duration_ms > 200:  # 超过200ms
            score *= 1.5
        
        return score
    
    def _determine_severity(self, avg_frame_time: float, max_frame_time: float, 
                           duration_ms: float, frame_count: int) -> str:
        """
        判断卡顿严重程度
        
        :param avg_frame_time: 平均帧时间
        :param max_frame_time: 最大帧时间
        :param duration_ms: 持续时间
        :param frame_count: 帧数
        :return: 严重程度（mild/moderate/severe）
        """
        # 基于最大帧时间判断
        if max_frame_time >= self.severe_threshold_ms:
            return "severe"
        elif max_frame_time >= self.moderate_threshold_ms:
            return "moderate"
        else:
            return "mild"
    
    def _generate_description(self, severity: str, frame_count: int, 
                             avg_frame_time: float, duration_ms: float) -> str:
        """
        生成卡顿描述
        
        :param severity: 严重程度
        :param frame_count: 帧数
        :param avg_frame_time: 平均帧时间
        :param duration_ms: 持续时间
        :return: 描述文本
        """
        severity_text = {
            "mild": "轻微",
            "moderate": "中等",
            "severe": "严重"
        }
        
        return f"{severity_text.get(severity, '未知')}卡顿: {frame_count}帧, " \
               f"平均{avg_frame_time:.1f}ms, 持续{duration_ms:.1f}ms"
    
    def get_current_metrics(self) -> PerceptualStallMetrics:
        """
        获取当前卡顿指标
        
        :return: 卡顿指标对象
        """
        # 统计不同严重程度的卡顿
        mild_count = sum(1 for e in self.stall_events if e.severity == "mild")
        moderate_count = sum(1 for e in self.stall_events if e.severity == "moderate")
        severe_count = sum(1 for e in self.stall_events if e.severity == "severe")
        
        # 计算平均帧时间和方差
        if self.frame_times:
            frame_times_list = [f.frame_time_ms for f in self.frame_times]
            avg_frame_time = sum(frame_times_list) / len(frame_times_list)
            
            # 计算方差
            variance = sum((ft - avg_frame_time) ** 2 for ft in frame_times_list) / len(frame_times_list)
        else:
            avg_frame_time = 0.0
            variance = 0.0
        
        # 当前卡顿状态
        is_stalling = self.current_stall_start is not None
        current_duration = 0.0
        if is_stalling:
            current_duration = (time.time() - self.current_stall_start) * 1000.0
        
        return PerceptualStallMetrics(
            total_stall_events=len(self.stall_events),
            total_stall_duration_ms=self.total_stall_duration_ms,
            stall_score=self.total_stall_score,
            mild_stalls=mild_count,
            moderate_stalls=moderate_count,
            severe_stalls=severe_count,
            current_stall_duration_ms=current_duration,
            is_stalling=is_stalling,
            avg_frame_time_ms=avg_frame_time,
            frame_time_variance=variance
        )
    
    def reset(self):
        """重置分析器状态"""
        self.frame_times.clear()
        self.stall_events.clear()
        self.current_stall_start = None
        self.current_stall_frames = []
        self.total_stall_score = 0.0
        self.total_stall_duration_ms = 0.0
    
    def get_recent_stall_events(self, count: int = 10) -> List[PerceptualStallEvent]:
        """
        获取最近的卡顿事件
        
        :param count: 返回数量
        :return: 卡顿事件列表
        """
        return self.stall_events[-count:] if len(self.stall_events) > count else self.stall_events
