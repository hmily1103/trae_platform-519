"""
脚本执行器
执行生成的Python脚本（uiautomator2）
"""
import subprocess
import os
import tempfile
import threading
from typing import Optional, Dict, Callable, List
from datetime import datetime
from utils.logger import setup_logger

logger = setup_logger('script_executor')


class ScriptExecutor:
    """脚本执行器"""
    
    def __init__(self):
        """初始化执行器"""
        self.executions: Dict[str, Dict] = {}  # {execution_id: {process, status, ...}}
        self.lock = threading.Lock()
    
    def execute(self, script_content: str, device_id: str, 
                execution_id: str = None,
                output_callback: Optional[Callable[[str], None]] = None) -> str:
        """
        执行脚本
        
        :param script_content: 脚本内容
        :param device_id: 设备ID
        :param execution_id: 执行ID（可选）
        :param output_callback: 输出回调函数
        :return: 执行ID
        """
        import time
        
        if not execution_id:
            execution_id = f"execution_{int(time.time())}"
        
        # 创建临时脚本文件
        temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.py', encoding='utf-8')
        temp_file.write(script_content)
        temp_file.close()
        script_path = temp_file.name
        
        try:
            # 启动Python进程执行脚本
            cmd = ['python', script_path, execution_id]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            # 保存执行信息
            with self.lock:
                self.executions[execution_id] = {
                    'process': process,
                    'device_id': device_id,
                    'script_path': script_path,
                    'status': 'running',
                    'start_time': datetime.now(),
                    'output': [],
                    'output_callback': output_callback
                }
            
            # 启动输出读取线程
            thread = threading.Thread(
                target=self._read_output,
                args=(execution_id,),
                daemon=True
            )
            thread.start()
            
            logger.info(f"脚本执行已启动: {execution_id}")
            return execution_id
            
        except Exception as e:
            logger.error(f"启动脚本执行失败: {e}", exc_info=True)
            # 清理临时文件
            try:
                os.remove(script_path)
            except:
                pass
            raise
    
    def _read_output(self, execution_id: str):
        """读取脚本输出"""
        with self.lock:
            if execution_id not in self.executions:
                return
            
            execution = self.executions[execution_id]
            process = execution['process']
            output_callback = execution.get('output_callback')
        
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                
                line = line.strip()
                if line:
                    with self.lock:
                        if execution_id in self.executions:
                            self.executions[execution_id]['output'].append(line)
                    
                    # 调用回调
                    if output_callback:
                        try:
                            output_callback(line)
                        except Exception as e:
                            logger.error(f"输出回调失败: {e}")
            
            # 等待进程结束
            process.wait()
            
            # 更新状态
            with self.lock:
                if execution_id in self.executions:
                    self.executions[execution_id]['status'] = 'completed' if process.returncode == 0 else 'failed'
                    self.executions[execution_id]['end_time'] = datetime.now()
                    self.executions[execution_id]['return_code'] = process.returncode
            
            logger.info(f"脚本执行完成: {execution_id}, 返回码: {process.returncode}")
            
        except Exception as e:
            logger.error(f"读取输出失败: {e}", exc_info=True)
            with self.lock:
                if execution_id in self.executions:
                    self.executions[execution_id]['status'] = 'error'
                    self.executions[execution_id]['error'] = str(e)
        finally:
            # 清理临时文件
            with self.lock:
                if execution_id in self.executions:
                    script_path = self.executions[execution_id].get('script_path')
                    if script_path and os.path.exists(script_path):
                        try:
                            os.remove(script_path)
                        except:
                            pass
    
    def stop(self, execution_id: str) -> bool:
        """
        停止脚本执行
        
        :param execution_id: 执行ID
        :return: 是否成功
        """
        with self.lock:
            if execution_id not in self.executions:
                return False
            
            execution = self.executions[execution_id]
            process = execution['process']
            
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            except Exception as e:
                logger.error(f"停止脚本执行失败: {e}")
            
            execution['status'] = 'stopped'
            execution['end_time'] = datetime.now()
            
            return True
    
    def get_status(self, execution_id: str) -> Optional[Dict]:
        """
        获取执行状态
        
        :param execution_id: 执行ID
        :return: 状态信息
        """
        with self.lock:
            if execution_id not in self.executions:
                return None
            
            execution = self.executions[execution_id]
            
            return {
                'execution_id': execution_id,
                'device_id': execution['device_id'],
                'status': execution['status'],
                'start_time': execution['start_time'].isoformat(),
                'end_time': execution.get('end_time').isoformat() if execution.get('end_time') else None,
                'return_code': execution.get('return_code'),
                'output': execution['output'],
                'output_count': len(execution['output'])
            }
    
    def list_executions(self) -> List[Dict]:
        """列出所有执行"""
        with self.lock:
            executions = []
            for execution_id, execution in self.executions.items():
                executions.append({
                    'execution_id': execution_id,
                    'device_id': execution['device_id'],
                    'status': execution['status'],
                    'start_time': execution['start_time'].isoformat(),
                    'end_time': execution.get('end_time').isoformat() if execution.get('end_time') else None
                })
            return executions
