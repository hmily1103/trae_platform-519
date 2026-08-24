"""
Device controller for Android automation.

Uses uiautomator2 when it is responsive, and falls back to plain ADB when
uiautomator2 initialization or calls are unavailable.
"""
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional, Tuple, Union

from utils.logger import setup_logger

logger = setup_logger("device_controller")

try:
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
    """Controls an Android device through uiautomator2 or ADB."""

    def __init__(self, device_id: str, adb_path: str = "adb"):
        self.device_id = device_id
        self.adb_path = adb_path
        self.last_command: Optional[list] = None
        self.last_output: str = ""
        self.last_success: bool = False
        self.d = None

        self._hierarchy_cache: Optional[Union[str, Tuple[str, float]]] = None
        self._cache_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()
        self._display_size_cache: Optional[Tuple[int, int]] = None

        if HAS_U2:
            self.d = self._init_uiautomator2_with_timeout(timeout=3)

    def _init_uiautomator2_with_timeout(self, timeout: int = 3):
        """Try uiautomator2 briefly and fall back to ADB if it blocks."""

        def _connect_and_probe():
            device = u2.connect(self.device_id) if self.device_id else u2.connect()
            # 预览操控要跟手，去掉默认前后延迟
            device.settings["operation_delay"] = (0, 0)
            device.settings["operation_delay_methods"] = ["click", "swipe", "drag", "press"]
            _ = device.info
            return device

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_connect_and_probe)
                device = future.result(timeout=timeout)
            logger.info(f"Connected to device via uiautomator2: {self.device_id}")
            return device
        except FutureTimeoutError:
            logger.warning(
                f"uiautomator2 init timed out after {timeout}s for {self.device_id}, falling back to ADB"
            )
        except Exception as e:
            logger.warning(
                f"uiautomator2 init failed for {self.device_id}, falling back to ADB: {e}"
            )
        return None

    def _run_adb_command(self, command: list, timeout: int = 5) -> Tuple[bool, str]:
        """Run an ADB command, using adb_pool when available."""
        try:
            self.last_command = (
                [self.adb_path, "-s", self.device_id] + command
                if self.device_id
                else [self.adb_path] + command
            )
            if _pool_run_command and self.device_id:
                rc, stdout, stderr = _pool_run_command(self.device_id, command, timeout=timeout)
                if rc == 0:
                    self.last_success = True
                    self.last_output = stdout
                    return True, stdout
                self.last_success = False
                self.last_output = stderr or stdout
                return False, self.last_output

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
            self.last_output = "command timed out"
            return False, self.last_output
        except Exception as e:
            logger.error(f"Failed to run ADB command: {e}", exc_info=True)
            self.last_success = False
            self.last_output = str(e)
            return False, str(e)

    def click(self, x: int, y: int) -> bool:
        if self.d:
            try:
                self.d.click(x, y)
                logger.debug(f"Click via U2: ({x}, {y})")
                return True
            except Exception as e:
                logger.warning(f"U2 click failed, falling back to ADB: {e}")

        success, _ = self._run_adb_command(["shell", "input", "tap", str(x), str(y)])
        if success:
            logger.debug(f"Click via ADB: ({x}, {y})")
        return success

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> bool:
        if self.d:
            try:
                self.d.swipe(x1, y1, x2, y2, duration=duration / 1000)
                logger.debug(f"Swipe via U2: ({x1}, {y1}) -> ({x2}, {y2}), {duration}ms")
                return True
            except Exception as e:
                logger.warning(f"U2 swipe failed, falling back to ADB: {e}")

        success, _ = self._run_adb_command(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]
        )
        if success:
            logger.debug(f"Swipe via ADB: ({x1}, {y1}) -> ({x2}, {y2}), {duration}ms")
        return success

    def input_text(self, text: str) -> bool:
        if self.d:
            try:
                self.d.send_keys(text)
                logger.debug(f"Input via U2: {text}")
                return True
            except Exception as e:
                logger.warning(f"U2 input failed, falling back to ADB: {e}")

        escaped_text = text.replace(" ", "%s").replace("&", "\\&").replace("<", "\\<").replace(">", "\\>")
        success, _ = self._run_adb_command(["shell", "input", "text", escaped_text])
        if success:
            logger.debug(f"Input via ADB: {text}")
        return success

    def press_key(self, key_code: Union[int, str]) -> bool:
        if self.d:
            try:
                self.d.press(key_code)
                logger.debug(f"Key via U2: {key_code}")
                return True
            except Exception as e:
                logger.warning(f"U2 key press failed, falling back to ADB: {e}")

        success, _ = self._run_adb_command(["shell", "input", "keyevent", str(key_code)])
        if success:
            logger.debug(f"Key via ADB: {key_code}")
        return success

    def get_display_size(self) -> Tuple[int, int]:
        """返回设备逻辑分辨率 (width, height)，失败则 (0, 0)。结果会缓存。"""
        if self._display_size_cache and self._display_size_cache[0] > 0:
            return self._display_size_cache

        if self.d:
            try:
                info = self.d.info or {}
                w = int(info.get("displayWidth") or 0)
                h = int(info.get("displayHeight") or 0)
                if w > 0 and h > 0:
                    self._display_size_cache = (w, h)
                    return self._display_size_cache
            except Exception:
                pass

        success, output = self._run_adb_command(["shell", "wm", "size"], timeout=5)
        if success and output:
            import re
            matches = re.findall(r"(\d+)\s*x\s*(\d+)", output, flags=re.IGNORECASE)
            if matches:
                w, h = matches[-1]
                self._display_size_cache = (int(w), int(h))
                return self._display_size_cache
        return 0, 0

    def screenshot_png_bytes(self, timeout: int = 20) -> Optional[bytes]:
        """
        取 PNG 字节。优先 uiautomator2（本机实测更快），失败再 adb exec-out。
        """
        # 1) U2 内存截图（通常比网络 adb screencap 更快）
        if self.d:
            try:
                import io
                pil_img = self.d.screenshot()
                # u2 可能返回 PIL.Image，或在旧版本行为不同
                if hasattr(pil_img, "save"):
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG", optimize=False)
                    data = buf.getvalue()
                    if data and len(data) > 100:
                        self.last_success = True
                        self.last_output = f"u2 screenshot {len(data)} bytes"
                        return data
            except Exception as e:
                logger.debug(f"U2 screenshot bytes failed, fallback ADB: {e}")

        raw = self._adb_exec_out_screencap(timeout=timeout)
        if raw and len(raw) > 100:
            idx = raw.find(b"\x89PNG\r\n\x1a\n")
            if idx < 0:
                idx = raw.find(b"\x89PNG")
            if idx >= 0:
                raw = raw[idx:]
            if raw[:4] == b"\x89PNG":
                return raw

        # 回退：screencap 到 sdcard 再 pull
        import tempfile
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="ui_shot_", suffix=".png", delete=False) as f:
                temp_path = f.name
            if self.screenshot(temp_path) and os.path.exists(temp_path):
                with open(temp_path, "rb") as f:
                    data = f.read()
                if data and len(data) > 100:
                    return data
        except Exception as e:
            logger.warning(f"screenshot_png_bytes fallback failed: {e}")
            self.last_output = str(e)
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        return None

    def _adb_exec_out_screencap(self, timeout: int = 20) -> Optional[bytes]:
        """adb exec-out screencap -p → raw PNG bytes。"""
        try:
            cmd = [self.adb_path]
            if self.device_id:
                cmd.extend(["-s", self.device_id])
            cmd.extend(["exec-out", "screencap", "-p"])
            self.last_command = cmd
            result = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if result.returncode != 0:
                err = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
                self.last_success = False
                self.last_output = err or f"exec-out screencap rc={result.returncode}"
                return None
            data = result.stdout or b""
            # 某些机型会在 PNG 前混入无关字节，尝试定位 PNG 头
            idx = data.find(b"\x89PNG\r\n\x1a\n")
            if idx > 0:
                data = data[idx:]
            elif idx < 0:
                # Windows adb 偶发把 \n 变成 \r\n 破坏 PNG
                repaired = data.replace(b"\r\n", b"\n")
                idx2 = repaired.find(b"\x89PNG\r\n\x1a\n")
                if idx2 < 0:
                    idx2 = repaired.find(b"\x89PNG")
                if idx2 >= 0:
                    data = repaired[idx2:]
            if not data or len(data) < 100:
                self.last_success = False
                self.last_output = "exec-out screencap empty"
                return None
            self.last_success = True
            self.last_output = f"exec-out screencap {len(data)} bytes"
            return data
        except subprocess.TimeoutExpired:
            self.last_success = False
            self.last_output = "exec-out screencap timed out"
            return None
        except Exception as e:
            self.last_success = False
            self.last_output = str(e)
            logger.warning(f"exec-out screencap failed: {e}")
            return None

    def screenshot(self, save_path: str) -> bool:
        # 优先 ADB，减少与预览流 / UI dump 的 uiautomator2 锁竞争
        temp_path = "/sdcard/screenshot_ui_auto.png"
        success, _ = self._run_adb_command(["shell", "screencap", "-p", temp_path], timeout=15)
        if success:
            success, _ = self._run_adb_command(["pull", temp_path, save_path], timeout=20)
            self._run_adb_command(["shell", "rm", temp_path], timeout=5)
            if success:
                return True

        if self.d:
            try:
                self.d.screenshot(save_path)
                logger.debug(f"Screenshot via U2: {save_path}")
                return True
            except Exception as e:
                logger.warning(f"U2 screenshot failed: {e}")
                self.last_output = str(e)
        return False

    def get_ui_tree(self, save_path: Optional[str] = None) -> Optional[str]:
        if self.d:
            try:
                xml_content = self.d.dump_hierarchy(compressed=False, pretty=False)
                if save_path:
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, "w", encoding="utf-8") as f:
                        f.write(xml_content)
                logger.debug("Got UI tree via U2")
                return xml_content
            except Exception as e:
                logger.warning(f"U2 UI tree failed, falling back to ADB: {e}")

        temp_path = "/sdcard/ui_dump.xml"
        dump_cmd = ["shell", "uiautomator", "dump", temp_path]

        success = False
        output = ""
        for attempt in range(2):
            self._run_adb_command(["shell", "rm", temp_path], timeout=5)
            success, output = self._run_adb_command(dump_cmd, timeout=20)
            if success:
                break
            logger.warning(f"UI tree dump failed (attempt {attempt + 1}/2): {output}")
            time.sleep(1)

        if not success:
            logger.error("UI tree dump failed via adb uiautomator dump")
            return None

        if save_path:
            pull_success, _ = self._run_adb_command(["pull", temp_path, save_path], timeout=15)
            if not pull_success:
                logger.error("UI tree pull failed")
                return None

        try:
            if save_path:
                if not os.path.exists(save_path):
                    return None
                with open(save_path, "r", encoding="utf-8") as f:
                    content = f.read()
            else:
                pull_success, content = self._run_adb_command(["shell", "cat", temp_path], timeout=15)
                if not pull_success:
                    logger.error("UI tree read failed")
                    return None

            self._run_adb_command(["shell", "rm", temp_path])

            if not content or "<hierarchy" not in content:
                logger.warning("UI tree content invalid or empty")
                return None
            return content
        except Exception as e:
            logger.error(f"Failed to read UI tree: {e}", exc_info=True)
            return None

    def get_screen_size(self) -> Optional[Tuple[int, int]]:
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
            parts = output.split()[-1].split("x")
            width = int(parts[0])
            height = int(parts[1])
            return (width, height)
        except Exception:
            return None

    def start_monitor(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return

        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(target=self._auto_sync_hierarchy, daemon=True)
        self._monitor_thread.start()
        logger.info(f"Started UI monitor for device {self.device_id}")

    def stop_monitor(self):
        if self._monitor_thread:
            self._stop_monitor.set()
            self._monitor_thread.join(timeout=2)
            self._monitor_thread = None
            logger.info(f"Stopped UI monitor for device {self.device_id}")

    def _auto_sync_hierarchy(self):
        while not self._stop_monitor.is_set():
            try:
                start_time = time.time()
                content = None
                if self.d:
                    try:
                        content = self.d.dump_hierarchy(compressed=False, pretty=False)
                    except Exception as e:
                        logger.warning(f"Background U2 dump failed: {e}")

                if not content:
                    content = self.get_ui_tree()

                if content:
                    with self._cache_lock:
                        self._hierarchy_cache = (content, time.time())

                elapsed = time.time() - start_time
                time.sleep(max(0.1, 1.5 - elapsed))
            except Exception as e:
                logger.error(f"Error in UI monitor loop: {e}")
                time.sleep(1.5)

    def get_cached_ui_tree(self) -> Optional[str]:
        with self._cache_lock:
            if self._hierarchy_cache:
                if isinstance(self._hierarchy_cache, tuple):
                    return self._hierarchy_cache[0]
                return self._hierarchy_cache

        logger.info("Cache miss, fetching UI tree immediately")
        content = self.get_ui_tree()
        if content:
            with self._cache_lock:
                self._hierarchy_cache = (content, time.time())
        return content
