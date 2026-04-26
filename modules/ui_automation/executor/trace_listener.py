import requests
import time
import traceback
import json
from typing import Optional, Dict, Any

class TraceListener:
    """
    Trace监听器 (运行在脚本进程中)
    负责收集步骤执行数据并发送给服务端
    """
    
    def __init__(self, run_id: str, device_id: str, server_url: str = "http://127.0.0.1:5000"):
        self.run_id = run_id
        self.device_id = device_id
        self.server_url = server_url.rstrip('/')
        self.api_url = f"{self.server_url}/api/ui_automation/trace"
        
    def log_step(self, step_num: int, action_type: str, 
                 selector_strategy: str = "", fallback_index: int = -1,
                 duration_ms: int = 0, success: bool = True,
                 bounds: Optional[Dict] = None, screenshot: Optional[str] = None,
                 error: Optional[str] = None):
        """记录单步执行结果"""
        
        data = {
            'run_id': self.run_id,
            'device_id': self.device_id,
            'step_num': step_num,
            'action_type': action_type,
            'selector_strategy': selector_strategy,
            'fallback_index': fallback_index,
            'success': success,
            'duration_ms': duration_ms,
            'bounds': bounds,
            'screenshot': screenshot,
            'error': str(error) if error else None,
            'timestamp': time.time()
        }
        
        try:
            # 异步发送或简单发送 (为了不阻塞脚本执行太久，设置短超时)
            requests.post(self.api_url, json=data, timeout=1.0)
        except Exception as e:
            print(f"[TraceListener] Failed to send trace: {e}")

class StepContext:
    """步骤执行上下文管理器"""
    
    def __init__(self, listener: Optional[TraceListener], step_num: int, action_type: str):
        self.listener = listener
        self.step_num = step_num
        self.action_type = action_type
        self.start_time = 0
        self.strategy = ""
        self.fallback_index = -1
        self.bounds = None
        
    def __enter__(self):
        self.start_time = time.time()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.listener:
            return False
            
        duration_ms = int((time.time() - self.start_time) * 1000)
        success = exc_type is None
        error = str(exc_val) if exc_val else None
        
        # 如果是断言失败，提取具体信息
        if exc_type == AssertionError:
            error = f"Assertion failed: {exc_val}"
            
        self.listener.log_step(
            step_num=self.step_num,
            action_type=self.action_type,
            selector_strategy=self.strategy,
            fallback_index=self.fallback_index,
            duration_ms=duration_ms,
            success=success,
            bounds=self.bounds,
            error=error
        )
        # 不吞掉异常，让脚本继续抛出
        return False

    def set_strategy(self, strategy: str, index: int = -1):
        self.strategy = strategy
        self.fallback_index = index
        
    def set_bounds(self, bounds: Dict):
        self.bounds = bounds
