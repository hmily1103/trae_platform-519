import subprocess
import time
import threading
import os
from datetime import datetime
from typing import Generator, Optional
from utils.logger import setup_logger

logger = setup_logger('screen_streamer')

class ScreenStreamer:
    """屏幕流管理器 (MJPEG)"""
    
    def __init__(self, adb_path: str = "adb"):
        self.adb_path = adb_path
        self._stop_event = threading.Event()

    def _run_adb_command(self, cmd: list, timeout: int = 5) -> bool:
        """执行 ADB 命令"""
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                startupinfo=startupinfo
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"ADB命令执行错误: {e}")
            return False

    def _get_frame_fallback(self, device_id: str, display_id: int) -> Optional[bytes]:
        """
        备用截图方案：保存到设备再拉取
        适用于 exec-out screencap 不工作的情况
        """
        try:
            temp_remote = f"/sdcard/stream_tmp_{display_id}.png"
            # 使用绝对路径，避免 CWD 问题
            import os
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            temp_captures_dir = os.path.join(base_dir, "temp_captures")
            temp_local = os.path.join(temp_captures_dir, f"stream_tmp_{display_id}.png")
            
            # 确保目录存在
            os.makedirs(temp_captures_dir, exist_ok=True)
            
            # 1. 截图到设备
            cmd_cap = [self.adb_path, "-s", device_id, "shell", "screencap", "-p", "-d", str(display_id), temp_remote]
            if not self._run_adb_command(cmd_cap, timeout=10): # 增加超时到10s
                self._log_debug(f"DEBUG: Fallback Screencap failed/timeout for {device_id}")
                return None
                
            # 2. 拉取到本地
            cmd_pull = [self.adb_path, "-s", device_id, "pull", temp_remote, temp_local]
            if not self._run_adb_command(cmd_pull, timeout=5):
                self._log_debug(f"DEBUG: Fallback Pull failed for {device_id}")
                return None
                
            # 3. 读取内容
            content = None
            if os.path.exists(temp_local):
                with open(temp_local, "rb") as f:
                    content = f.read()
                # 清理本地文件
                try:
                    os.remove(temp_local)
                except:
                    pass
            else:
                self._log_debug(f"DEBUG: Local file not found: {temp_local}")
        
            # 4. 清理设备文件 (异步或忽略错误以加快速度)
            # 这里的清理可以另起线程做，或者偶尔做，为了速度暂时同步但短超时
            cmd_rm = [self.adb_path, "-s", device_id, "shell", "rm", temp_remote]
            self._run_adb_command(cmd_rm, timeout=1)
            
            return content
            
        except Exception as e:
            logger.error(f"Fallback截图失败: {e}")
            self._log_debug(f"DEBUG: Exception in fallback: {e}")
            return None

    def _log_debug(self, msg):
        logger.debug("%s", msg)

    def _create_mjpeg_frame(self, data):
        return (
            b'--frame\r\n'
            b'Content-Type: image/png\r\n'
            b'Content-Length: ' + str(len(data)).encode() + b'\r\n'
            b'\r\n' + 
            data + 
            b'\r\n'
        )

    def stream_frames(self, device_id: str, display_id: int = 0) -> Generator[bytes, None, None]:
        """
        生成 MJPEG 帧流 (Continuous Mode)
        """
        self._log_debug(f"DEBUG: stream_frames continuous started for {device_id} display {display_id}")
        
        # 错误占位图 (1x1 Red Pixel)
        error_img = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9c6\x7f\xcc\xff\x03\x05\x00\x04\x85\x01\x80\x84\xa9\x8c!\x00\x00\x00\x00IEND\xaeB`\x82'
        
        sep_str = "|||TRAE_SEP|||"
        sep_bytes = sep_str.encode()
        
        # Windows startup info
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        # Continuous Command
        # 循环截图并输出分隔符
        cmd = [self.adb_path, "-s", device_id, "exec-out", f"while true; do screencap -p -d {display_id}; echo '{sep_str}'; done"]
        
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
            
            buffer = b""
            while not self._stop_event.is_set():
                # 读取数据块
                chunk = proc.stdout.read(8192)
                if not chunk:
                    self._log_debug("DEBUG: Continuous stream EOF")
                    break
                
                buffer += chunk
                
                while sep_bytes in buffer:
                    parts = buffer.split(sep_bytes, 1)
                    frame_raw = parts[0].strip()
                    buffer = parts[1]
                    
                    if len(frame_raw) > 100 and frame_raw.startswith(b'\x89PNG'):
                        yield self._create_mjpeg_frame(frame_raw)
                    
        except Exception as e:
            self._log_debug(f"DEBUG: Continuous stream exception: {e}")
            logger.error(f"Stream error: {e}")
        finally:
            if proc:
                proc.kill()
                self._log_debug("DEBUG: Killed continuous process")

        # Fallback Mode (Loop)
        self._log_debug("DEBUG: Switching to Fallback Mode")
        while not self._stop_event.is_set():
            frame = self._get_frame_fallback(device_id, display_id)
            if frame:
                yield self._create_mjpeg_frame(frame)
            else:
                yield self._create_mjpeg_frame(error_img)
            time.sleep(0.5) # Reduced sleep to improve FPS slightly in fallback mode

    def stop(self):
        self._stop_event.set()
