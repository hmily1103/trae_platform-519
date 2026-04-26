"""
配置加载：从 JSON/YAML 文件或环境变量读取，支持运营修改。
统一入口：优先环境变量，其次 config/*.yaml, config/*.json
"""
import json
import os
from typing import Any, Dict, List

CONFIG_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
_platform_yaml_cache: Dict[str, Any] = {}


def load_yaml_config(path: str, default: Any = None) -> Any:
    """从 YAML 文件加载配置，失败时返回 default"""
    if not os.path.exists(path):
        return default
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except ImportError:
        return default
    except Exception:
        return default


def get_platform_yaml() -> Dict[str, Any]:
    """平台统一配置：config/platform.yaml"""
    global _platform_yaml_cache
    if _platform_yaml_cache:
        return _platform_yaml_cache
    path = os.path.join(CONFIG_ROOT, "platform.yaml")
    data = load_yaml_config(path) or {}
    _platform_yaml_cache = data
    return data


def get_config_section(section: str, key: str = None, default: Any = None) -> Any:
    """
    获取配置片段
    :param section: 如 'adb', 'player_stress', 'reboot'
    :param key: 可选，如 'timeout'
    :param default: 默认值
    """
    data = get_platform_yaml()
    val = data.get(section, default)
    if key is not None and isinstance(val, dict):
        return val.get(key, default)
    return val


def load_json_config(path: str, default: Any = None) -> Any:
    """从 JSON 文件加载配置，失败时返回 default"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def get_announcements() -> Dict[str, Any]:
    """系统公告：从 config/announcements.json 读取"""
    root = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(root, 'config', 'announcements.json')
    data = load_json_config(path)
    if not data:
        return {
            "title": "系统公告",
            "items": [
                "欢迎使用设备与服务器测试运维平台。",
                "当前平台已接入：Monkey 测试、UI 自动化、日志监控、性能监控、重启测试、播放器压测、用例管理等模块。",
            ],
        }
    return data


def get_platform_config() -> Dict[str, Any]:
    """平台级配置：config/platform.json 合并环境变量"""
    path = os.path.join(CONFIG_ROOT, "platform.json")
    data = load_json_config(path) or {}
    return {
        "log_dir": os.environ.get("LOG_DIR") or data.get("log_dir"),
        "runtime_data_dir": os.environ.get("RUNTIME_DATA_DIR") or data.get("runtime_data_dir"),
        "api_auth_enabled": os.environ.get("ENABLE_API_AUTH", "").lower() in ("1", "true", "yes"),
    }


def get_song_order_config() -> Dict[str, str]:
    """点歌配置：从环境变量读取，脱敏（不暴露给前端）"""
    return {
        "host": os.environ.get("SONG_ORDER_HOST", "192.168.16.210"),
        "search_port": os.environ.get("SONG_ORDER_SEARCH_PORT", "9000"),
        "vod_port": os.environ.get("SONG_ORDER_VOD_PORT", "8008"),
        "roominfo": os.environ.get("SONG_ORDER_ROOMINFO", "86f02338_192.168.1.114"),
        "userid": os.environ.get("SONG_ORDER_USERID", "123"),
        "appid": os.environ.get("SONG_ORDER_APPID", "32432424"),
    }
