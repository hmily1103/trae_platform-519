from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class BaseCollector(ABC):
    """
    性能数据采集器基类
    """
    def __init__(self, adb_controller: Any):
        self.adb = adb_controller

    @abstractmethod
    def collect(self) -> Dict[str, Any]:
        """
        采集数据
        :return: 包含采集数据的字典
        """
        pass
    
    @abstractmethod
    def reset(self):
        """
        重置状态（可选）
        """
        pass
