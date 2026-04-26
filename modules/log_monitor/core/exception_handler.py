"""
统一异常处理模块
提供装饰器和上下文管理器，统一处理异常
"""
import functools
import traceback
import logging
from typing import Callable, Optional, Any
from datetime import datetime


# 配置日志
logger = logging.getLogger(__name__)


class ExceptionHandler:
    """异常处理器"""
    
    @staticmethod
    def handle_exception(e: Exception, context: str = "", log_level: str = "error") -> str:
        """
        统一处理异常
        
        :param e: 异常对象
        :param context: 上下文信息
        :param log_level: 日志级别
        :return: 错误消息
        """
        error_msg = f"{context}: {str(e)}" if context else str(e)
        error_trace = traceback.format_exc()
        
        # 记录日志
        if log_level == "error":
            logger.error(f"{error_msg}\n{error_trace}")
        elif log_level == "warning":
            logger.warning(f"{error_msg}\n{error_trace}")
        else:
            logger.debug(f"{error_msg}\n{error_trace}")
        
        return error_msg
    
    @staticmethod
    def safe_execute(func: Callable, default_return: Any = None, context: str = "") -> Any:
        """
        安全执行函数，捕获异常并返回默认值
        
        :param func: 要执行的函数
        :param default_return: 异常时的默认返回值
        :param context: 上下文信息
        :return: 函数返回值或默认值
        """
        try:
            return func()
        except Exception as e:
            ExceptionHandler.handle_exception(e, context)
            return default_return


def handle_background_exception(func: Callable) -> Callable:
    """
    装饰器：处理后台线程异常，记录日志但不中断程序
    
    :param func: 被装饰的函数
    :return: 装饰后的函数
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            ExceptionHandler.handle_exception(e, f"后台任务失败: {func.__name__}")
            return None
    return wrapper


def handle_adb_exception(func: Callable) -> Callable:
    """
    装饰器：处理 ADB 相关异常，自动重试
    
    :param func: 被装饰的函数
    :return: 装饰后的函数
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = kwargs.pop('max_retries', 3)
        retry_delay = kwargs.pop('retry_delay', 1.0)
        
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt < max_retries - 1:
                    ExceptionHandler.handle_exception(e, f"ADB操作失败 (尝试 {attempt + 1}/{max_retries})", "warning")
                    import time
                    time.sleep(retry_delay)
                else:
                    ExceptionHandler.handle_exception(e, f"ADB操作最终失败: {func.__name__}")
                    raise
    return wrapper


class SafeContext:
    """安全上下文管理器"""
    
    def __init__(self, context_name: str = "", default_return: Any = None):
        self.context_name = context_name
        self.default_return = default_return
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            ExceptionHandler.handle_exception(exc_val, self.context_name)
            return True  # 抑制异常
        return False
