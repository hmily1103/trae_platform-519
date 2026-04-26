"""
Pytest 配置与 fixtures
"""
import os
import sys

# 确保项目根目录在 path 中
_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _root not in sys.path:
    sys.path.insert(0, _root)


def pytest_configure(config):
    """测试环境配置"""
    os.environ.setdefault('FLASK_ENV', 'testing')
