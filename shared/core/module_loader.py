"""
ModuleLoader：模块插件加载器，统一管理模块注册、状态查询、按任务类型分发。

- 各模块通过 register() 注册自身
- get_status(module_id) 获取模块状态
- get_modules_by_task_type(task_type) 按任务类型获取可用模块
- list_modules() 列出所有已注册模块
"""
import threading
from typing import Any, Callable, Dict, List, Optional

from .module_plugin import ModuleInfo, ModuleState


class ModuleLoader:
    _instance: Optional["ModuleLoader"] = None
    _lock = threading.RLock()

    def __init__(self):
        self._registry: Dict[str, ModuleInfo] = {}

    @classmethod
    def get_instance(cls) -> "ModuleLoader":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register(self, info: ModuleInfo) -> None:
        """注册模块"""
        self._registry[info.module_id] = info

    def register_simple(
        self,
        module_id: str,
        name: str,
        status_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        task_types: Optional[List[str]] = None,
        description: str = "",
    ) -> None:
        """简化注册：仅提供 id、名称、状态回调"""
        self.register(ModuleInfo(
            module_id=module_id,
            name=name,
            description=description,
            task_types=task_types or [],
            status_fn=status_fn,
        ))

    def unregister(self, module_id: str) -> None:
        """取消注册"""
        self._registry.pop(module_id, None)

    def get_status(self, module_id: str) -> Dict[str, Any]:
        """获取模块状态，调用其 status_fn"""
        info = self._registry.get(module_id)
        if not info:
            return {"state": ModuleState.UNKNOWN.value, "module_id": module_id}
        if not info.status_fn:
            return {"state": ModuleState.IDLE.value, "module_id": module_id}
        try:
            return info.status_fn()
        except Exception as e:
            return {
                "state": ModuleState.FAILED.value,
                "module_id": module_id,
                "error": str(e),
            }

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有已注册模块的状态"""
        return {mid: self.get_status(mid) for mid in self._registry}

    def list_modules(self) -> List[ModuleInfo]:
        """列出所有已注册模块"""
        return list(self._registry.values())

    def get_modules_by_task_type(self, task_type: str) -> List[ModuleInfo]:
        """按任务类型获取可用模块"""
        return [m for m in self._registry.values() if task_type in m.task_types]

    def get_module(self, module_id: str) -> Optional[ModuleInfo]:
        """获取模块信息"""
        return self._registry.get(module_id)


def get_module_loader() -> ModuleLoader:
    return ModuleLoader.get_instance()
