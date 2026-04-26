"""
ModulePlugin 抽象：定义模块插件接口，供 ModuleLoader 统一管理。

各功能模块可选择性实现此接口，实现 start/stop/status 等能力，
由 ModuleLoader 按需加载、调度、健康检查。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ModuleState(str, Enum):
    """模块运行状态"""
    IDLE = "idle"           # 空闲
    RUNNING = "running"     # 运行中
    STOPPED = "stopped"     # 已停止
    FAILED = "failed"       # 异常
    UNKNOWN = "unknown"     # 未知（未注册或未实现）


@dataclass
class ModuleInfo:
    """模块元信息"""
    module_id: str
    name: str
    description: str = ""
    task_types: List[str] = field(default_factory=list)  # 支持的任务类型，如 monkey, ui_auto
    status_fn: Optional[Callable[[], Dict[str, Any]]] = None  # 获取状态的回调
    blueprint_name: Optional[str] = None


class ModulePlugin(ABC):
    """
    模块插件抽象基类。
    各模块可继承此类并实现接口，由 ModuleLoader 统一管理。
    """

    @property
    @abstractmethod
    def module_id(self) -> str:
        """模块唯一标识"""
        pass

    @property
    def name(self) -> str:
        """模块显示名称"""
        return self.module_id

    @property
    def description(self) -> str:
        """模块描述"""
        return ""

    @property
    def task_types(self) -> List[str]:
        """支持的任务类型"""
        return []

    def get_status(self) -> Dict[str, Any]:
        """
        获取模块当前状态。
        子类可重写，默认返回 idle。
        """
        return {"state": ModuleState.IDLE.value, "module_id": self.module_id}

    def to_info(self) -> ModuleInfo:
        """转为 ModuleInfo 供 ModuleLoader 注册"""
        return ModuleInfo(
            module_id=self.module_id,
            name=self.name,
            description=self.description,
            task_types=self.task_types,
            status_fn=self.get_status,
        )
