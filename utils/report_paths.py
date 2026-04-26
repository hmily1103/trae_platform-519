"""
统一报告存储路径
所有模块的报告统一存储在 data/reports/<module_name>/
"""
import os


def get_report_base_dir() -> str:
    """获取报告根目录: trae_platform/data/reports"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, 'data', 'reports')
    os.makedirs(base, exist_ok=True)
    return base


def get_module_report_dir(module_name: str) -> str:
    """
    获取指定模块的报告目录
    :param module_name: 模块名，如 player_stress, reboot
    :return: 完整路径，如 data/reports/player_stress
    """
    base = get_report_base_dir()
    path = os.path.join(base, module_name)
    os.makedirs(path, exist_ok=True)
    return path


def get_screenshots_dir(module_name: str = 'player_stress') -> str:
    """获取截图子目录"""
    report_dir = get_module_report_dir(module_name)
    path = os.path.join(report_dir, 'screenshots')
    os.makedirs(path, exist_ok=True)
    return path
