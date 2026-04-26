from typing import Dict, Any, List, Optional
from .collectors.base_collector import BaseCollector
from .collectors.rk_collector import RkCollector
from .collectors.fps_collector import FpsCollector
from .collectors.cpu_collector import CpuCollector

class CollectorManager:
    """
    采集器管理器
    负责协调多个采集器的工作
    """
    def __init__(self, adb_controller: Any, package_name: str, preferred_display_id: Optional[int] = None):
        self.adb = adb_controller
        self.package_name = package_name
        self.collectors: List[BaseCollector] = []
        self.preferred_display_id = preferred_display_id
        
        # 初始化采集器
        self.rk_collector = RkCollector(adb_controller)
        self.fps_collector = FpsCollector(adb_controller, package_name)
        if preferred_display_id is not None:
            self.fps_collector.preferred_display_id = preferred_display_id
        self.cpu_collector = CpuCollector(adb_controller, package_name)
        
        self.collectors.append(self.rk_collector)
        self.collectors.append(self.fps_collector)
        self.collectors.append(self.cpu_collector)
        
        # 检查 MPP 支持
        self.rk_collector.check_support()

    def collect_all(self) -> Dict[str, Any]:
        """
        采集所有数据
        """
        result = {}
        
        # 1. 采集 MPP 数据
        mpp_data = self.rk_collector.collect()
        result.update(mpp_data)
        
        # 2. 采集 FPS 数据
        video_fps = self.fps_collector.measure_video_fps(
            display_id=self.preferred_display_id,
            mpp_stats=mpp_data
        )
        result['video_fps'] = video_fps
        
        # 3. 采集 CPU 数据
        cpu_data = self.cpu_collector.collect()
        result.update(cpu_data)
        
        return result
    
    def reset_all(self):
        for collector in self.collectors:
            collector.reset()
