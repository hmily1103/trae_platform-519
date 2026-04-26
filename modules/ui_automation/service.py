"""
UI自动化服务
管理录制任务、视频流、设备控制等
"""
import threading
import time
import os
import logging
from datetime import datetime
from typing import Dict, Optional, Callable, List, Union
from .core.scrcpy_manager import ScrcpyManager
from .core.device_controller import DeviceController
from .core.video_stream import VideoStreamManager
from .recorder.action_recorder import ActionRecorder
from .storage import RecordingStorage
from .core.script_generator import ScriptGenerator
from .core.trace_analyzer import TraceAnalyzer
from .executor.script_executor import ScriptExecutor
from utils.logger import setup_logger
from .models import RecordingSession, UIAction, Project, TestSuite, ExecutionTrace
from core.runtime import get_runtime_manager, RuntimeStatus

logger = setup_logger('ui_automation_service')

try:
    from shared.unified.report_store import get_unified_report_store
except Exception:
    get_unified_report_store = None


class UIAutomationService:
    """UI自动化服务"""
    
    def __init__(self):
        """初始化服务"""
        self.scrcpy_manager = ScrcpyManager()
        self.video_stream_manager = VideoStreamManager()
        self.storage = RecordingStorage()
        self.script_generator = ScriptGenerator()
        self.script_executor = ScriptExecutor()
        self.analyzer = TraceAnalyzer()
        
        # 任务管理
        self.video_tasks: Dict[str, Dict] = {}  # {device_id: {status, ...}}
        self.recording_tasks: Dict[str, ActionRecorder] = {}  # {recording_id: recorder}
        self.suite_executions: Dict[str, Dict] = {}  # {job_id: {status, ...}}
        self.device_controllers: Dict[str, DeviceController] = {} # {device_id: controller}
        self.lock = threading.RLock()

    def save_execution_trace(self, trace_data: Dict) -> bool:
        """
        保存执行Trace
        
        :param trace_data: Trace字典数据
        :return: 是否成功
        """
        try:
            trace = ExecutionTrace.from_dict(trace_data)
            return self.storage.save_execution_trace(trace)
        except Exception as e:
            logger.error(f"保存Trace失败: {e}")
            return False

    def analyze_execution_stability(self, run_ids: List[str]) -> Dict:
        """
        分析执行稳定性
        
        :param run_ids: 执行ID列表
        :return: 分析报告
        """
        all_traces = []
        for run_id in run_ids:
            traces = self.storage.load_execution_traces(run_id)
            all_traces.extend(traces)
        
        stability_report = self.analyzer.analyze_stability(all_traces)
        drift_report = self.analyzer.detect_ui_drift(all_traces)
        device_diff_report = self.analyzer.analyze_device_diff(all_traces)
        suggestions = self.analyzer.generate_suggestions(all_traces)
        
        return {
            'stability': stability_report,
            'drift': drift_report,
            'device_diff': device_diff_report,
            'suggestions': suggestions
        }

    def get_device_controller(self, device_id: str) -> DeviceController:
        """
        获取设备控制器（单例模式）
        
        :param device_id: 设备ID
        :return: DeviceController实例
        """
        with self.lock:
            if device_id not in self.device_controllers:
                controller = DeviceController(device_id)
                # 开启后台UI监听
                controller.start_monitor()
                self.device_controllers[device_id] = controller
            return self.device_controllers[device_id]
    
    def start_video_stream(self, device_id: str, callback: Optional[Callable[[str], None]] = None) -> bool:
        """
        启动视频流
        
        :param device_id: 设备ID
        :param callback: 视频帧回调函数（可选）
        :return: 是否成功
        """
        with self.lock:
            if device_id in self.video_tasks:
                logger.warning(f"视频流已启动: {device_id}")
                if callback:
                    self.video_stream_manager.start_stream(device_id, callback)
                return True
            
            # 启动scrcpy（可选，当前使用截图方式）
            # self.scrcpy_manager.start(device_id)
            
            # 启动视频流管理器
            if callback:
                if self.video_stream_manager.start_stream(device_id, callback):
                    self.video_tasks[device_id] = {
                        'status': 'running',
                        'start_time': time.time()
                    }
                    return True
            else:
                # 没有回调，只标记状态
                self.video_tasks[device_id] = {
                    'status': 'running',
                    'start_time': time.time()
                }
                return True
            
            return False
    
    def stop_video_stream(self, device_id: str) -> bool:
        """
        停止视频流
        
        :param device_id: 设备ID
        :return: 是否成功
        """
        with self.lock:
            if device_id not in self.video_tasks:
                return False
            
            # 停止视频流管理器
            self.video_stream_manager.stop_stream(device_id)
            
            # 停止scrcpy（如果启动了）
            # self.scrcpy_manager.stop(device_id)
            
            del self.video_tasks[device_id]
            return True
    
    def is_video_streaming(self, device_id: str) -> bool:
        """检查视频流是否运行"""
        return self.scrcpy_manager.is_running(device_id)
    
    def start_recording(self, device_id: str, recording_id: str, 
                       package_name: str = "", description: str = "", 
                       project_id: str = "", name: str = "") -> bool:
        """
        开始录制
        
        :param device_id: 设备ID
        :param recording_id: 录制ID
        :param package_name: 应用包名
        :param description: 描述
        :param project_id: 所属项目ID
        :param name: 用例名称
        :return: 是否成功
        """
        with self.lock:
            if recording_id in self.recording_tasks:
                logger.warning(f"录制任务已存在: {recording_id}")
                return False
            
            # 获取共享的设备控制器
            controller = self.get_device_controller(device_id)
            
            recorder = ActionRecorder(
                device_id=device_id,
                recording_id=recording_id,
                storage=self.storage,
                controller=controller,
                auto_ui_tree=True
            )
            
            recorder.start(package_name=package_name, description=description, 
                          project_id=project_id, name=name)
            self.recording_tasks[recording_id] = recorder
            
            logger.info(f"开始录制: {recording_id}")
            return True
    
    def stop_recording(self, recording_id: str, save: bool = True) -> bool:
        """
        停止录制
        
        :param recording_id: 录制ID
        :param save: 是否保存
        :return: 是否成功
        """
        with self.lock:
            if recording_id not in self.recording_tasks:
                return False
            
            recorder = self.recording_tasks[recording_id]
            recorder.stop()
            
            if save:
                recorder.save()
            
            del self.recording_tasks[recording_id]
            
            logger.info(f"停止录制: {recording_id}")
            return True
    
    def get_recorder(self, recording_id: str) -> Optional[ActionRecorder]:
        """获取录制器"""
        with self.lock:
            return self.recording_tasks.get(recording_id)
            
    def get_latest_screenshot(self, device_id: str) -> Optional[str]:
        """获取最新截图 (Base64)"""
        # 1. 尝试从视频流获取
        frame = self.video_stream_manager.get_last_frame(device_id)
        if frame:
            return frame
            
        # 2. 如果没有视频流，尝试主动截图
        # 注意：这可能会有延迟，但对于非流式预览是必要的
        try:
            controller = self.get_device_controller(device_id)
            import tempfile
            import base64
            
            temp_dir = tempfile.gettempdir()
            safe_device_id = "".join(c if c.isalnum() else "_" for c in device_id)
            temp_path = os.path.join(temp_dir, f"ui_automation_snapshot_{safe_device_id}.png")
            
            if controller.screenshot(temp_path):
                 with open(temp_path, 'rb') as f:
                     return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logger.warning(f"主动截图失败: {e}")
            
        return None
    
    def record_click(self, recording_id: str, x: int, y: int, description: str = "") -> Optional[bool]:
        """录制点击"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return None
        return recorder.record_click(x, y, description)

    def record_step_async(self, recording_id: str, x: int, y: int, description: str = "", action_type: str = "click") -> bool:
        """异步录制步骤 (不执行操作)"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return False
        return recorder.record_step_async(x, y, description, action_type=action_type)
    
    def record_swipe(self, recording_id: str, x1: int, y1: int, x2: int, y2: int,
                    duration: int = 300, description: str = "") -> bool:
        """录制滑动"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return False
        return recorder.record_swipe(x1, y1, x2, y2, duration, description)
    
    def record_input(self, recording_id: str, x: int, y: int, text: str, description: str = "") -> bool:
        """录制输入"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return False
        return recorder.record_input(x, y, text, description)

    def record_assertion(self, recording_id: str, x: int, y: int, assertion_type: str, expected_value: str = "", description: str = "") -> bool:
        """录制断言"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return False
        return recorder.record_assertion(x, y, assertion_type, expected_value, description)

    def delete_recording_action(self, recording_id: str, step_index: int) -> bool:
        """删除录制操作"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return False
        return recorder.delete_action(step_index)

    def update_recording_action(self, recording_id: str, step_index: int, 
                              action_type: str = None, value: str = None, 
                              description: str = None) -> bool:
        """更新录制操作"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return False
        return recorder.update_action(step_index, action_type, value, description)
    
    def generate_script(self, recording_id: str, device_id: str = None) -> Optional[str]:
        """
        生成脚本
        
        :param recording_id: 录制ID
        :param device_id: 设备ID（可选）
        :return: 脚本内容
        """
        session = self.storage.load_recording(recording_id)
        if not session:
            return None
        
        script_content = self.script_generator.generate(session, device_id)
        
        # 保存脚本
        script_path = self.storage.get_script_path(recording_id)
        try:
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(script_content)
            return script_content
        except Exception as e:
            logger.error(f"保存脚本失败: {e}", exc_info=True)
            return script_content  # 即使保存失败也返回内容
    
    def execute_script(self, script_content: str, device_id: str, 
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
        return self.script_executor.execute(
            script_content=script_content,
            device_id=device_id,
            execution_id=execution_id,
            output_callback=output_callback
        )
    
    def stop_execution(self, execution_id: str) -> bool:
        """停止脚本执行"""
        return self.script_executor.stop(execution_id)
    
    def get_execution_status(self, execution_id: str) -> Optional[Dict]:
        """获取执行状态"""
        return self.script_executor.get_status(execution_id)

    # --- Project Methods ---

    def create_project(self, name: str, description: str = "") -> Optional[Project]:
        """创建项目"""
        import uuid
        project = Project(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            created_at=time.time(),
            updated_at=time.time()
        )
        if self.storage.save_project(project):
            return project
        return None

    def list_projects(self) -> List[Project]:
        """列出所有项目"""
        return self.storage.list_projects()

    def get_project(self, project_id: str) -> Optional[Project]:
        """获取项目"""
        return self.storage.load_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        """删除项目"""
        return self.storage.delete_project(project_id)
    
    def list_cases(self, project_id: Optional[str] = None) -> List[Dict]:
        """列出测试用例（录制）"""
        return self.storage.list_recordings(project_id)

    def delete_recording(self, recording_id: str) -> bool:
        """删除录制"""
        # 如果正在录制或执行，可能需要先停止
        if recording_id in self.recording_tasks:
            self.stop_recording(recording_id, save=False)
        
        return self.storage.delete_recording(recording_id)

    # --- Suite Methods ---

    def create_suite(self, name: str, project_id: str, case_ids: List[str], description: str = "") -> Optional[TestSuite]:
        """创建测试套件"""
        import uuid
        suite = TestSuite(
            id=str(uuid.uuid4()),
            name=name,
            project_id=project_id,
            case_ids=case_ids,
            description=description,
            created_at=time.time(),
            updated_at=time.time()
        )
        if self.storage.save_suite(suite):
            return suite
        return None

    def list_suites(self, project_id: Optional[str] = None) -> List[TestSuite]:
        """列出测试套件"""
        return self.storage.list_suites(project_id)

    def get_suite(self, suite_id: str) -> Optional[TestSuite]:
        """获取测试套件"""
        return self.storage.load_suite(suite_id)

    def delete_suite(self, suite_id: str) -> bool:
        """删除测试套件"""
        return self.storage.delete_suite(suite_id)

    def update_suite(self, suite_id: str, name: str = None, case_ids: List[str] = None, description: str = None) -> bool:
        """更新测试套件"""
        suite = self.get_suite(suite_id)
        if not suite:
            return False
        
        if name is not None:
            suite.name = name
        if case_ids is not None:
            suite.case_ids = case_ids
        if description is not None:
            suite.description = description
        
        suite.updated_at = time.time()
        return self.storage.save_suite(suite)

    def run_suite(self, suite_id: str, device_ids: Union[str, List[str]]) -> Optional[str]:
        """运行测试套件 (支持多设备并发)"""
        suite = self.get_suite(suite_id)
        if not suite:
            return None
        
        if isinstance(device_ids, str):
            device_ids = [device_ids]
            
        if not device_ids:
            return None
        
        import uuid
        job_id = f"job_{uuid.uuid4()}"
        
        # Create Runtime
        runtime = get_runtime_manager().create_runtime(
            name=f"UI Suite: {suite.name}",
            module="ui_automation",
            context={
                'suite_id': suite.id,
                'device_ids': device_ids,
                'job_id': job_id
            }
        )

        # Start execution thread
        thread = threading.Thread(
            target=self._run_suite_thread,
            args=(job_id, suite, device_ids, runtime.runtime_id)
        )
        thread.daemon = True
        thread.start()
        
        return job_id
    
    def get_suite_job_status(self, job_id: str) -> Optional[Dict]:
        """获取套件任务状态"""
        return self.suite_executions.get(job_id)

    def get_running_suite_jobs(self) -> List[Dict]:
        """获取运行中的套件任务列表，用于页面切换后恢复"""
        jobs = []
        with self.lock:
            for job_id, info in self.suite_executions.items():
                if info.get('status') == 'running':
                    suite = self.get_suite(info.get('suite_id'))
                    jobs.append({
                        'job_id': job_id,
                        'suite_id': info.get('suite_id'),
                        'suite_name': suite.name if suite else job_id,
                        'device_ids': info.get('device_ids', []),
                    })
        return jobs

    def stop_suite_job(self, job_id: str) -> bool:
        """停止套件任务"""
        if job_id in self.suite_executions:
            self.suite_executions[job_id]['status'] = 'stopped'
            return True
        return False

    def get_dashboard_stats(self) -> Dict:
        """获取仪表盘统计数据"""
        projects = self.list_projects()
        cases = self.list_cases()
        reports = self.storage.list_reports()
        
        total_runs = len(reports)
        
        total_cases_executed = 0
        total_cases_passed = 0
        
        for report in reports:
            results = report.get('results', [])
            for res in results:
                total_cases_executed += 1
                if res.get('status') == 'completed':
                    total_cases_passed += 1
        
        pass_rate = 0.0
        if total_cases_executed > 0:
            pass_rate = (total_cases_passed / total_cases_executed) * 100
            
        return {
            'project_count': len(projects),
            'case_count': len(cases),
            'total_runs': total_runs,
            'pass_rate': round(pass_rate, 1),
            'recent_reports': reports[:10]
        }

    def _run_suite_thread(self, job_id: str, suite: TestSuite, device_ids: List[str], runtime_id: str):
        """套件执行线程 (支持并发)"""
        try:
            # Update Runtime Status
            get_runtime_manager().update_status(runtime_id, RuntimeStatus.RUNNING)

            # Initialize job status
            self.suite_executions[job_id] = {
                'suite_id': suite.id,
                'device_ids': device_ids,
                'status': 'running',
                'current_case_index': 0,
                'total_cases': len(suite.case_ids),
                'results': [],
                'start_time': time.time()
            }
            
            logger.info(f"开始执行测试套件: {suite.name} ({job_id}), 设备: {device_ids}")
            
            # Prepare case queue
            case_queue = list(enumerate(suite.case_ids))
            queue_lock = threading.Lock()
            results_lock = threading.Lock()
            
            def worker(device_id: str):
                while True:
                    # Get next case
                    with queue_lock:
                        if not case_queue:
                            break
                        if self.suite_executions[job_id]['status'] == 'stopped':
                            break
                        index, case_id = case_queue.pop(0)
                        
                        # Update progress (approximate)
                        self.suite_executions[job_id]['current_case_index'] = index
                    
                    # Execute case
                    try:
                        case_info = self.storage.load_recording(case_id)
                        case_name = case_info.name if case_info else case_id
                        
                        script_content = self.generate_script(case_id, device_id)
                        
                        if not script_content:
                            with results_lock:
                                self.suite_executions[job_id]['results'].append({
                                    'case_id': case_id,
                                    'case_name': case_name,
                                    'status': 'failed',
                                    'error': 'Script generation failed',
                                    'device_id': device_id,
                                    'end_time': time.time()
                                })
                            continue
                            
                        exec_id = self.execute_script(script_content, device_id)
                        
                        # Wait for completion
                        while True:
                            status = self.get_execution_status(exec_id)
                            if not status or status['status'] in ['completed', 'failed', 'error', 'stopped']:
                                # Parse screenshot from output
                                screenshot_file = None
                                if status and status.get('output'):
                                    for line in reversed(status['output']):
                                        if 'ERROR_SCREENSHOT: ' in line:
                                            screenshot_file = line.split('ERROR_SCREENSHOT: ')[1].strip()
                                            break
                                
                                with results_lock:
                                    result_entry = {
                                        'case_id': case_id,
                                        'case_name': case_name,
                                        'status': status['status'] if status else 'unknown',
                                        'output': status.get('output', '') if status else '',
                                        'error': status.get('error', '') if status else '',
                                        'device_id': device_id,
                                        'end_time': time.time()
                                    }
                                    if screenshot_file:
                                        result_entry['screenshot'] = screenshot_file
                                    self.suite_executions[job_id]['results'].append(result_entry)
                                break
                            
                            if self.suite_executions[job_id]['status'] == 'stopped':
                                self.stop_execution(exec_id)
                                break
                                
                            time.sleep(1)
                            
                    except Exception as e:
                        logger.error(f"Execution failed for case {case_id} on {device_id}: {e}")
                        with results_lock:
                            self.suite_executions[job_id]['results'].append({
                                'case_id': case_id,
                                'case_name': case_id,
                                'status': 'error',
                                'error': str(e),
                                'device_id': device_id,
                                'end_time': time.time()
                            })
            
            # Start workers
            threads = []
            for dev_id in device_ids:
                t = threading.Thread(target=worker, args=(dev_id,))
                t.start()
                threads.append(t)
                
            # Wait for all workers
            for t in threads:
                t.join()
                
            # Finalize
            if self.suite_executions[job_id]['status'] != 'stopped':
                self.suite_executions[job_id]['status'] = 'completed'
                logger.info(f"测试套件执行完成: {suite.name} ({job_id})")
                
        except Exception as e:
            logger.error(f"测试套件执行异常: {e}", exc_info=True)
            self.suite_executions[job_id]['status'] = 'error'
            self.suite_executions[job_id]['error'] = str(e)
            
        finally:
            # Ensure end_time is set
            self.suite_executions[job_id]['end_time'] = time.time()
            
            # Calculate stats
            total = len(self.suite_executions[job_id].get('results', []))
            passed = sum(1 for r in self.suite_executions[job_id].get('results', []) if r['status'] == 'completed')
            self.suite_executions[job_id]['pass_rate'] = (passed / total * 100) if total > 0 else 0
            
            # Determine Final Runtime Status
            final_status = self.suite_executions[job_id]['status']
            runtime_status = RuntimeStatus.COMPLETED
            if final_status == 'stopped':
                runtime_status = RuntimeStatus.CANCELLED
            elif final_status == 'error':
                runtime_status = RuntimeStatus.FAILED
            
            # Update Runtime
            get_runtime_manager().update_status(
                runtime_id, 
                runtime_status,
                result={
                    'pass_rate': self.suite_executions[job_id]['pass_rate'],
                    'total': total,
                    'passed': passed,
                    'suite_status': final_status,
                    'error': self.suite_executions[job_id].get('error')
                }
            )

            # Save report to disk
            report_data = self.suite_executions[job_id].copy()
            report_data['job_id'] = job_id
            report_data['suite_name'] = suite.name
            proj = self.get_project(suite.project_id) if suite.project_id else None
            report_data['project_name'] = proj.name if proj else (suite.project_id or '')
            
            if self.storage.save_report(report_data):
                logger.info(f"测试套件执行报告已保存: {job_id}")
            else:
                logger.error(f"保存测试套件执行报告失败: {job_id}")

            # Write unified report (additive; does not affect existing endpoints)
            if get_unified_report_store:
                try:
                    unified_id = f"ui_automation_suite_{job_id}"
                    status = self.suite_executions[job_id].get("status", "unknown")
                    results = self.suite_executions[job_id].get("results", [])
                    total = len(results)
                    passed = sum(1 for r in results if r.get("status") == "completed")
                    summary = {
                        "suite_id": suite.id,
                        "suite_name": suite.name,
                        "status": status,
                        "total_cases_executed": total,
                        "passed": passed,
                        "pass_rate": self.suite_executions[job_id].get("pass_rate", 0),
                        "devices": list(device_ids),
                    }
                    details = {
                        "job_id": job_id,
                        "results": results,
                        "start_time": self.suite_executions[job_id].get("start_time"),
                        "end_time": self.suite_executions[job_id].get("end_time"),
                    }
                    get_unified_report_store().save_report(
                        unified_id=unified_id,
                        module="ui_automation",
                        kind="suite",
                        status=status,
                        summary=summary,
                        details=details,
                        device_id=",".join(device_ids) if device_ids else None,
                        legacy_id=job_id,
                        started_at=self.suite_executions[job_id].get("start_time"),
                        finished_at=self.suite_executions[job_id].get("end_time"),
                        raw={"ui_automation": report_data},
                    )
                except Exception as e:
                    logger.warning(f"Failed to write unified report for suite {job_id}: {e}")
