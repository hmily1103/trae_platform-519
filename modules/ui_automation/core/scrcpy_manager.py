"""
scrcpy进程管理器
负责启动、停止和管理scrcpy进程
"""
import subprocess
import os
import threading
import time
from typing import Optional, Dict
from utils.logger import setup_logger

logger = setup_logger('scrcpy_manager')


class ScrcpyManager:
    """scrcpy进程管理器"""
    
    def __init__(self, scrcpy_path: str = None):
        """
        初始化scrcpy管理器
        
        :param scrcpy_path: scrcpy可执行文件路径
        """
        # 用户指定的默认路径
        user_default_path = r"C:\Users\58857\Desktop\scrcpy1.24\scrcpy.exe"
        
        if scrcpy_path is None:
            if os.path.exists(user_default_path):
                scrcpy_path = user_default_path
            else:
                scrcpy_path = "scrcpy"
                
        self.scrcpy_path = os.environ.get("SCRCPY_PATH", scrcpy_path)
        self.processes: Dict[str, subprocess.Popen] = {}  # {device_id: process}
        self.lock = threading.Lock()
    
    def start(self, device_id: str, max_size: int = 1024, 
              bit_rate: str = "8M", codec: str = "h264") -> bool:
        """
        启动scrcpy进程
        
        :param device_id: 设备ID
        :param max_size: 最大分辨率
        :param bit_rate: 码率
        :param codec: 编码格式
        :return: 是否成功
        """
        with self.lock:
            if device_id in self.processes:
                logger.warning(f"scrcpy进程已存在: {device_id}")
                return False
            
            try:
                # 构建scrcpy命令
                cmd = [
                    self.scrcpy_path,
                    "-s", device_id,
                    "--no-control",  # 不启用控制（我们通过adb控制）
                    "--max-size", str(max_size),
                    "--bit-rate", bit_rate,
                    "--codec", codec,
                    "--record", "/dev/null",  # 不保存文件（只输出流）
                    "--no-display",  # 不显示窗口
                    "--turn-screen-off",  # 关闭屏幕（可选）
                ]
                
                # 启动进程
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE
                )
                
                self.processes[device_id] = process
                logger.info(f"scrcpy进程已启动: {device_id}")
                
                # 等待一下，检查进程是否正常启动
                time.sleep(0.5)
                if process.poll() is not None:
                    # 进程已退出，启动失败
                    stderr = process.stderr.read().decode('utf-8', errors='ignore')
                    logger.error(f"scrcpy启动失败: {stderr}")
                    del self.processes[device_id]
                    return False
                
                return True
                
            except FileNotFoundError:
                logger.error(f"scrcpy未找到，请确保已安装scrcpy: {self.scrcpy_path}")
                return False
            except Exception as e:
                logger.error(f"启动scrcpy失败: {e}", exc_info=True)
                return False
    
    def start_window(self, device_id: str, max_size: int = 1280, bit_rate: str = "8M", codec: str = "h264", 
                     display_id: int = None, window_x: int = None, window_y: int = None) -> bool:
        with self.lock:
            # Generate a unique key for the process
            process_key = device_id
            if display_id is not None:
                process_key = f"{device_id}_{display_id}"

            # 如果进程已存在且仍在运行，则不重复启动
            if process_key in self.processes:
                proc = self.processes[process_key]
                if proc.poll() is None:
                    logger.info(f"Scrcpy window already running for {process_key}")
                    return True
                else:
                    # 进程已死，移除
                    del self.processes[process_key]

            try:
                exe = self.scrcpy_path
                cmd = [
                    exe, 
                    "-s", device_id,
                    "--max-size", str(max_size),
                    "--bit-rate", bit_rate,
                    "--codec", codec,
                    "--window-title", f"Trae Remote: {process_key}" 
                ]
                
                if display_id is not None:
                    cmd.extend(["--display", str(display_id)])
                
                if window_x is not None:
                    cmd.extend(["--window-x", str(window_x)])
                
                if window_y is not None:
                    cmd.extend(["--window-y", str(window_y)])
                
                logger.info(f"Starting scrcpy window: {' '.join(cmd)}")
                
                try:
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except FileNotFoundError:
                    logger.warning(f"Scrcpy executable not found at {exe}, trying 'scrcpy'")
                    exe = "scrcpy"
                    cmd[0] = exe
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                # 检查进程是否立即退出
                time.sleep(1)
                if process.poll() is not None:
                    stderr = process.stderr.read().decode('utf-8', errors='ignore')
                    logger.error(f"Scrcpy window failed to start immediately: {stderr}")
                    return False

                self.processes[process_key] = process
                return True
            except Exception as e:
                logger.error(f"Failed to start scrcpy window: {e}")
                return False
    
    def stop(self, device_id: str) -> bool:
        """
        停止scrcpy进程 (包括该设备的所有Display实例)
        
        :param device_id: 设备ID
        :return: 是否成功
        """
        with self.lock:
            stopped_any = False
            keys_to_remove = []
            
            # Find all keys belonging to this device
            for key in self.processes.keys():
                if key == device_id or key.startswith(f"{device_id}_"):
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                process = self.processes[key]
                try:
                    # 终止进程
                    process.terminate()
                    
                    # 等待进程结束（最多等待2秒）
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # 强制杀死
                        process.kill()
                        process.wait()
                    
                    del self.processes[key]
                    logger.info(f"scrcpy进程已停止: {key}")
                    stopped_any = True
                    
                except Exception as e:
                    logger.error(f"停止scrcpy失败: {e}", exc_info=True)
            
            return stopped_any
    
    def is_running(self, device_id: str) -> bool:
        """
        检查scrcpy进程是否运行
        
        :param device_id: 设备ID
        :return: 是否运行
        """
        with self.lock:
            if device_id not in self.processes:
                return False
            
            process = self.processes[device_id]
            return process.poll() is None
    
    def get_process(self, device_id: str) -> Optional[subprocess.Popen]:
        """
        获取scrcpy进程对象
        
        :param device_id: 设备ID
        :return: 进程对象
        """
        with self.lock:
            return self.processes.get(device_id)
    
    def stop_all(self):
        """停止所有scrcpy进程"""
        with self.lock:
            device_ids = list(self.processes.keys())
            for device_id in device_ids:
                self.stop(device_id)
