"""
ADB 命令缓存模块
缓存 ADB 命令结果，减少 subprocess 调用
"""
import time
import subprocess
from typing import Dict, Tuple, Optional, Any
from threading import Lock


class AdbCommandCache:
    """
    ADB 命令缓存
    缓存命令结果，减少进程创建开销
    """
    
    def __init__(self, default_ttl: int = 5):
        """
        初始化缓存
        
        :param default_ttl: 默认缓存时间（秒）
        """
        self.default_ttl = default_ttl
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.lock = Lock()
    
    def get(self, key: str, ttl: Optional[int] = None) -> Optional[Any]:
        """
        获取缓存值
        
        :param key: 缓存键
        :param ttl: 缓存时间（秒），None 使用默认值
        :return: 缓存值，如果过期或不存在返回 None
        """
        with self.lock:
            if key not in self.cache:
                return None
            
            value, timestamp = self.cache[key]
            cache_ttl = ttl or self.default_ttl
            
            if time.time() - timestamp > cache_ttl:
                # 缓存过期
                del self.cache[key]
                return None
            
            return value
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """
        设置缓存值
        
        :param key: 缓存键
        :param value: 缓存值
        :param ttl: 缓存时间（秒），None 使用默认值
        """
        with self.lock:
            self.cache[key] = (value, time.time())
    
    def execute_with_cache(self, key: str, command: list, 
                          ttl: Optional[int] = None,
                          timeout: int = 10) -> Tuple[Any, bool]:
        """
        执行命令并缓存结果
        
        :param key: 缓存键
        :param command: 命令列表
        :param ttl: 缓存时间（秒）
        :param timeout: 命令超时时间
        :return: (结果, 是否来自缓存)
        """
        # 先检查缓存
        cached_value = self.get(key, ttl)
        if cached_value is not None:
            return cached_value, True
        
        # 执行命令
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=timeout
            )
            self.set(key, result, ttl)
            return result, False
        except Exception as e:
            # 执行失败，返回 None
            return None, False
    
    def clear(self, key: Optional[str] = None):
        """
        清空缓存
        
        :param key: 如果指定，只清空该键的缓存；否则清空所有
        """
        with self.lock:
            if key:
                self.cache.pop(key, None)
            else:
                self.cache.clear()
    
    def cleanup_expired(self):
        """清理过期的缓存项"""
        current_time = time.time()
        with self.lock:
            expired_keys = [
                key for key, (_, timestamp) in self.cache.items()
                if current_time - timestamp > self.default_ttl
            ]
            for key in expired_keys:
                del self.cache[key]
    
    def get_stats(self) -> dict:
        """获取缓存统计信息"""
        with self.lock:
            return {
                'cache_size': len(self.cache),
                'keys': list(self.cache.keys())
            }


# 全局 ADB 缓存实例
_adb_cache: Optional[AdbCommandCache] = None


def get_adb_cache() -> AdbCommandCache:
    """获取全局 ADB 缓存实例"""
    global _adb_cache
    if _adb_cache is None:
        _adb_cache = AdbCommandCache(default_ttl=5)
    return _adb_cache
