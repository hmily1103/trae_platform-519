from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

@dataclass
class StartupRecord:
    """Represents a single app startup time record."""
    package_name: str
    activity_name: str
    start_time: datetime
    end_time: datetime

    @property
    def duration(self) -> float:
        """Calculates the startup duration in milliseconds."""
        return (self.end_time - self.start_time).total_seconds() * 1000

@dataclass
class ProcessSnapshot:
    """Represents performance data for a single process."""
    pid: int
    process_name: str
    cpu_usage: float
    rss_kb: int # Resident Set Size (from top)
    pss_kb: int # Proportional Set Size (from dumpsys)
    gc_count: int

@dataclass
class PerformanceSnapshot:
    """Represents a single performance snapshot (Memory, CPU, Network, FPS)."""
    timestamp: datetime
    total_pss: int  # in KB
    gc_count: int
    cpu_usage: float = 0.0 # Percentage (0-100 or more for multi-core)
    processes: List[ProcessSnapshot] = None
    system_top_processes: Optional[List[ProcessSnapshot]] = None  # 系统级 Top N 进程（按 CPU）
    device_info: str = ""
    # New metrics
    fps: int = 0
    jank_count: int = 0
    network_rx_kb: float = 0.0 # Downlink speed in KB/s (calculated from delta)
    network_tx_kb: float = 0.0 # Uplink speed in KB/s
    # 人眼感知卡顿指标
    perceptual_stall_score: float = 0.0  # 累计卡顿评分
    perceptual_stall_events: int = 0  # 卡顿事件数
    perceptual_stall_duration_ms: float = 0.0  # 总卡顿时长
    is_perceptual_stalling: bool = False  # 当前是否在卡顿
    perceptual_stall_severity: str = ""  # 当前卡顿严重程度：mild/moderate/severe
    frame_time_variance: float = 0.0  # 帧时间方差（波动性）
    mpp_active_instances: int = 0
    mpp_total_work_count: int = 0
    mpp_work_count_delta: int = 0
    mpp_work_count_delta_time_sec: float = 0.0
    decoder_stuck: bool = False
    decoder_stuck_duration_sec: float = 0.0
    decode_fps_estimate: float = 0.0
    decode_drop_estimate: int = 0
    decode_drop_ratio: float = 0.0
