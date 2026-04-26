"""
模块注册：在 app 启动时将所有功能模块注册到 ModuleLoader。

各模块通过 url_prefix 下的 /api/status 提供状态，ModuleLoader 通过
Flask test_client 内部请求获取，无需各模块额外实现。
"""
from typing import Callable, Dict, Any, Optional

from .module_loader import get_module_loader
from .module_plugin import ModuleState


def _make_status_fetcher(app, url_prefix: str, module_id: str) -> Callable[[], Dict[str, Any]]:
    """生成通过 test_client 请求 /api/status 的状态获取函数"""
    def _fetch():
        try:
            with app.test_client() as c:
                r = c.get(f"{url_prefix}/api/status")
                if r and r.status_code == 200:
                    d = r.get_json() or {}
                    # 统一为 {state, module_id, ...}
                    state = d.get("state") or d.get("status")
                    if state is None and d.get("running") is True:
                        state = ModuleState.RUNNING.value
                    if state is None:
                        state = ModuleState.IDLE.value
                    if isinstance(state, dict):
                        state = state.get("running") and ModuleState.RUNNING.value or ModuleState.IDLE.value
                    return {"state": str(state), "module_id": module_id, "data": d}
                return {"state": ModuleState.UNKNOWN.value, "module_id": module_id}
        except Exception as e:
            return {"state": ModuleState.FAILED.value, "module_id": module_id, "error": str(e)}
    return _fetch


# 模块元数据：module_id -> (name, url_prefix, task_types)
MODULE_META = {
    "monkey": ("Monkey 测试", "/monkey", ["monkey"]),
    "ui_automation": ("UI 自动化", "/ui_automation", ["ui_auto"]),
    "player_stress": ("播放器压测", "/player_stress", ["player_stress"]),
    "reboot": ("中控重启", "/reboot", ["reboot"]),
    "log_monitor": ("日志监控", "/log_monitor", ["log"]),
    "performance_monitor": ("性能监控", "/performance_monitor", ["perf"]),
    "server_stress": ("ARM 服务器压测", "/server_stress", []),
    "clean_ad": ("广告清理", "/clean_ad", []),
    "unified": ("一键任务", "/unified", ["monkey", "ui_auto", "reboot", "log", "perf"]),
    "combined_test": ("组合测试", "/combined_test", ["reboot", "player_stress", "log"]),
    "song_order": ("点歌与搜索", "/song_order", []),
    "sanfang": ("第三方规则配置", "/sanfang", []),
    "runtime_center": ("运行中心", "/runtime_center", []),
    "test_case": ("用例管理", "/test_case", []),
    "remote_control": ("远程控制", "/remote_control", []),
}


def register_all_modules(app) -> None:
    """将各模块注册到 ModuleLoader"""
    loader = get_module_loader()
    for module_id, (name, prefix, task_types) in MODULE_META.items():
        if module_id not in app.blueprints:
            continue
        status_fn = _make_status_fetcher(app, prefix, module_id)
        loader.register_simple(
            module_id=module_id,
            name=name,
            status_fn=status_fn,
            task_types=task_types,
            description=f"{name} 模块",
        )
