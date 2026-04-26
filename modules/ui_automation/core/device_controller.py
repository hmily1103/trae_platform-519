"""
设备控制器
负责通过ADB控制Android设备（点击、滑动、输入等）
使用 uiautomator2 提升性能，并保留 ADB 作为底层通信通道
"""
import subprocess
import time
import os
import threading
from typing import Optional, Tuple, Union
from utils.logger import setup_logger

logger = setup_logger('device_controller')

try:
    # Fix for "Invalid version: ''" error in packaging.version which is used by uiautomator2
    import packaging.version
    original_parse = packaging.version.parse
    def safe_parse(version):
        if not version or version == "":
            return original_parse("0.0.0")
        return original_parse(version)
    packaging.version.parse = safe_parse
except ImportError:
    pass

try:
    import uiautomator2 as u2
    HAS_U2 = True
except ImportError:
    HAS_U2 = False
    logger.warning("uiautomator2 not installed, falling back to pure ADB")

try:
    from core.adb_pool import run_command as _pool_run_command
except ImportError:
    _pool_run_command = None


class DeviceController:
    """设备控制器"""
    
    def __init__(self, device_id: str, adb_path: str = "adb"):
        self.device_id = device_id
        self.adb_path = adb_path
        self.last_command: Optional[list] = None
        self.last_output: str = ""
        self.last_success: bool = False
        
        # uiautomator2 device instance
        self.d = None
        
        # UI Tree Caching
        self._hierarchy_cache: Optional[str] = None
        self._cache_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()
        
        if HAS_U2:
            try:
                if self.device_id:
                    self.d = u2.connect(self.device_id)
                else:
                    self.d = u2.connect()
                
                # 配置点击操作后的延迟（默认0.1s，比ADB快很多）
                self.d.settings['operation_delay'] = 0.1
                # 尝试获取一下设备信息，如果失败则说明连接有问题，直接回退到ADB
                try:
                    _ = self.d.info
                except Exception as e:
                    logger.warning(f"uiautomator2连接检查失败: {e}，将回退到ADB模式")
                    self.d = None
                else:
                    logger.info(f"Connected to device via uiautomator2: {self.device_id}")
            except Exception as e:
                logger.error(f"Failed to connect via uiautomator2: {e}")
                self.d = None

    def _run_adb_command(self, command: list, timeout: int = 5) -> Tuple[bool, str]:
        """使用 adb_pool 执行，避免多模块并发冲突"""
        try:
            self.last_command = [self.adb_path, "-s", self.device_id] + command if self.device_id else [self.adb_path] + command
            if _pool_run_command and self.device_id:
                rc, stdout, stderr = _pool_run_command(self.device_id, command, timeout=timeout)
                if rc == 0:
                    self.last_success = True
                    self.last_output = stdout
                    return True, stdout
                self.last_success = False
                self.last_output = stderr or stdout
                return False, self.last_output
            
            # Fallback
            cmd = [self.adb_path]
            if self.device_id:
                cmd.extend(["-s", self.device_id])
            cmd.extend(command)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                self.last_success = True
                self.last_output = result.stdout.strip()
                return True, result.stdout.strip()
            self.last_success = False
            self.last_output = result.stderr.strip() or result.stdout.strip()
            return False, self.last_output
        except subprocess.TimeoutExpired:
            self.last_success = False
            self.last_output = "命令执行超时"
            return False, "命令执行超时"
        except Exception as e:
            logger.error(f"执行ADB命令失败: {e}", exc_info=True)
            self.last_success = False
            self.last_output = str(e)
            return False, str(e)
    
    def click(self, x: int, y: int) -> bool:
        """
        点击坐标
        
        :param x: X坐标
        :param y: Y坐标
        :return: 是否成功
        """
        if self.d:
            try:
                self.d.click(x, y)
                logger.debug(f"点击坐标(U2): ({x}, {y})")
                return True
            except Exception as e:
                logger.warning(f"U2点击失败，回退到ADB: {e}")
        
        success, _ = self._run_adb_command(["shell", "input", "tap", str(x), str(y)])
        if success:
            logger.debug(f"点击坐标(ADB): ({x}, {y})")
        return success
    
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> bool:
        """
        滑动
        
        :param x1: 起始X坐标
        :param y1: 起始Y坐标
        :param x2: 结束X坐标
        :param y2: 结束Y坐标
        :param duration: 滑动时长（毫秒）
        :return: 是否成功
        """
        if self.d:
            try:
                # duration in seconds
                self.d.swipe(x1, y1, x2, y2, duration=duration/1000)
                logger.debug(f"滑动(U2): ({x1}, {y1}) -> ({x2}, {y2}), 时长: {duration}ms")
                return True
            except Exception as e:
                logger.warning(f"U2滑动失败，回退到ADB: {e}")

        success, _ = self._run_adb_command([
            "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2),
            str(duration)
        ])
        if success:
            logger.debug(f"滑动(ADB): ({x1}, {y1}) -> ({x2}, {y2}), 时长: {duration}ms")
        return success
    
    def input_text(self, text: str) -> bool:
        """
        输入文本
        
        :param text: 文本内容
        :return: 是否成功
        """
        if self.d:
            try:
                self.d.send_keys(text)
                logger.debug(f"输入文本(U2): {text}")
                return True
            except Exception as e:
                logger.warning(f"U2输入失败，回退到ADB: {e}")

        # 转义特殊字符
        escaped_text = text.replace(" ", "%s").replace("&", "\&").replace("<", "\<").replace(">", "\>")
        success, _ = self._run_adb_command(["shell", "input", "text", escaped_text])
        if success:
            logger.debug(f"输入文本(ADB): {text}")
        return success
        
    def press_key(self, key_code: Union[int, str]) -> bool:
        """
        按键
        
        :param key_code: 键值 (如 4 或 KEYCODE_BACK)
        :return: 是否成功
        """
        if self.d:
            try:
                self.d.press(key_code)
                logger.debug(f"按键(U2): {key_code}")
                return True
            except Exception as e:
                logger.warning(f"U2按键失败，回退到ADB: {e}")
                
        success, _ = self._run_adb_command(["shell", "input", "keyevent", str(key_code)])
        if success:
            logger.debug(f"按键(ADB): {key_code}")
        return success

    def screenshot(self, save_path: str) -> bool:
        """
        截图
        
        :param save_path: 保存路径
        :return: 是否成功
        """
        # U2 screenshot is faster
        if self.d:
            try:
                self.d.screenshot(save_path)
                logger.debug(f"截图成功(U2): {save_path}")
                return True
            except Exception as e:
                logger.warning(f"U2截图失败，回退到ADB: {e}")

        temp_path = "/sdcard/screenshot.png"
        success, _ = self._run_adb_command(["shell", "screencap", "-p", temp_path], timeout=10)
        if not success:
            return False
            
        success, _ = self._run_adb_command(["pull", temp_path, save_path], timeout=15)
        self._run_adb_command(["shell", "rm", temp_path])
        return success
    
    def get_ui_tree(self, save_path: Optional[str] = None) -> Optional[str]:
        """
        获取UI树（XML格式）
        
        :param save_path: 保存路径（可选）
        :return: UI树XML内容
        """
        # 优先使用 uiautomator2
        if self.d:
            try:
                # compressed=False is safer, similar to adb dump
                # pretty=False to reduce size
                xml_content = self.d.dump_hierarchy(compressed=False, pretty=False)
                
                if save_path:
                    # Create directory if not exists
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, "w", encoding="utf-8") as f:
                        f.write(xml_content)
                
                logger.debug("获取UI树成功(U2)")
                return xml_content
            except Exception as e:
                logger.warning(f"U2获取UI树失败，回退到ADB: {e}")
        
        # Fallback to ADB dump
        temp_path = "/sdcard/ui_dump.xml"
        
        # 暂时移除 --compressed 参数，因为部分设备（如模拟器或旧手机）不支持，会导致报错
        # 且失败后可能影响后续的 dump 操作
        dump_cmd = ["shell", "uiautomator", "dump", temp_path]
        
        # 重试机制
        for attempt in range(2):
            # 先清理旧文件
            self._run_adb_command(["shell", "rm", temp_path], timeout=5)
            
            success, output = self._run_adb_command(dump_cmd, timeout=20)
            if success:
                break
            
            # 记录失败原因
            logger.warning(f"获取UI树失败 (尝试 {attempt+1}/2): {output}")
            
            # 如果是 Idle 状态获取失败，尝试模拟点击一下空处或者按一下菜单键唤醒？不，这太危险。
            # 只能等待
            import time
            time.sleep(1)
        
        if not success:
            logger.error("获取UI树失败: uiautomator dump 命令执行失败")
            return None
        
        # 拉取到本地
        if save_path:
            pull_success, _ = self._run_adb_command(["pull", temp_path, save_path], timeout=15)
            if not pull_success:
                logger.error("获取UI树失败: pull 命令执行失败")
                return None
        
        # 读取内容
        try:
            content = None
            if save_path:
                if os.path.exists(save_path):
                    with open(save_path, 'r', encoding='utf-8') as f:
                        content = f.read()
            else:
                # 直接读取
                pull_success, content = self._run_adb_command(["shell", "cat", temp_path], timeout=15)
                if not pull_success:
                    logger.error("获取UI树失败: cat 命令执行失败")
                    return None
            
            # 删除设备上的临时文件
            self._run_adb_command(["shell", "rm", temp_path])
            
            # 简单的有效性检查
            if not content or '<hierarchy' not in content:
                logger.warning("获取UI树失败: 内容无效或为空")
                return None
                
            return content
            
        except Exception as e:
            logger.error(f"读取UI树失败: {e}", exc_info=True)
            return None
    
    def get_screen_size(self) -> Optional[Tuple[int, int]]:
        """
        获取屏幕尺寸
        
        :return: (宽度, 高度)
        """
        if self.d:
            try:
                info = self.d.window_size()
                return (info[0], info[1])
            except Exception:
                pass

        success, output = self._run_adb_command(["shell", "wm", "size"])
        if not success:
            return None
        
        try:
            # 输出格式: Physical size: 1920x1080
            parts = output.split()[-1].split('x')
            width = int(parts[0])
            height = int(parts[1])
            return (width, height)
        except Exception:
            return None

    def start_monitor(self):
        """开启后台 UI 树监听"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        
        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(target=self._auto_sync_hierarchy, daemon=True)
        self._monitor_thread.start()
        logger.info(f"Started UI monitor for device {self.device_id}")

    def stop_monitor(self):
        """停止后台 UI 树监听"""
        if self._monitor_thread:
            self._stop_monitor.set()
            self._monitor_thread.join(timeout=2)
            self._monitor_thread = None
            logger.info(f"Stopped UI monitor for device {self.device_id}")

    def _auto_sync_hierarchy(self):
        """后台线程：定期更新 UI 树缓存"""
        while not self._stop_monitor.is_set():
            try:
                start_time = time.time()
                # 使用 u2 的 dump_hierarchy，因为它更快且稳定
                # 如果没有 u2，回退到 get_ui_tree (可能会慢)
                content = None
                if self.d:
                    try:
                        content = self.d.dump_hierarchy(compressed=False, pretty=False)
                    except Exception as e:
                        logger.warning(f"Background dump failed (U2): {e}")
                
                if not content:
                    # 尝试用 ADB 方式（注意这里会调用 get_ui_tree，它内部有重试机制）
                    content = self.get_ui_tree()

                if content:
                    with self._cache_lock:
                        # 存储 (内容, 时间戳)
                        self._hierarchy_cache = (content, time.time())
                
                # 计算耗时，动态调整休眠时间，保证约 1.5s 更新一次
                elapsed = time.time() - start_time
                sleep_time = max(0.1, 1.5 - elapsed)
                time.sleep(sleep_time)
                
            except Exception as e:
                logger.error(f"Error in UI monitor loop: {e}")
                time.sleep(1.5)

    def get_cached_ui_tree(self) -> Optional[str]:
        """获取缓存的 UI 树，如果为空则立即获取"""
        with self._cache_lock:
            if self._hierarchy_cache:
                # 兼容旧逻辑（如果只存了字符串）和新逻辑（元组）
                if isinstance(self._hierarchy_cache, tuple):
                    return self._hierarchy_cache[0]
                return self._hierarchy_cache
        
        # 如果缓存为空，则立即获取一次
        logger.info("Cache miss, fetching UI tree immediately")
        content = self.get_ui_tree()
        if content:
            with self._cache_lock:
                self._hierarchy_cache = (content, time.time())
        return content
