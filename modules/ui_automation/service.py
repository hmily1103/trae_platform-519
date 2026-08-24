"""
UI自动化服务
管理录制任务、视频流、设备控制等
"""
import threading
import time
import os
import logging
from datetime import datetime
from typing import Dict, Optional, Callable, List, Union, Any
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
        # AI 闭环探索异步任务 {job_id: {status, events, result, error, ...}}
        self.ai_explore_jobs: Dict[str, Dict] = {}
        self._load_explore_jobs_from_disk()

    def _load_explore_jobs_from_disk(self) -> None:
        """进程重启后恢复近期探索任务，便于前端续看。"""
        try:
            for job in self.storage.list_explore_jobs(limit=40):
                jid = job.get("job_id")
                if not jid:
                    continue
                # 运行中任务在重启后视为中断
                if job.get("status") == "running":
                    job["status"] = "failed"
                    job["error"] = job.get("error") or "服务重启，探索中断"
                    job["events"] = list(job.get("events") or [])
                    job["events"].append(
                        {
                            "phase": "error",
                            "message": "服务重启，探索中断",
                            "ts": time.time(),
                        }
                    )
                    self.storage.save_explore_job(job)
                self.ai_explore_jobs[jid] = job
        except Exception as e:
            logger.warning(f"加载探索任务快照失败: {e}")

    def _persist_explore_job(self, job_id: str) -> None:
        with self.lock:
            job = self.ai_explore_jobs.get(job_id)
            if not job:
                return
            snapshot = dict(job)
        try:
            self.storage.save_explore_job(snapshot)
        except Exception as e:
            logger.debug(f"persist explore job failed: {e}")

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
            
    def get_latest_screenshot(self, device_id: str, force: bool = True) -> Optional[str]:
        """获取最新截图 Base64（兼容旧调用）。默认强制刷新。"""
        payload = self.get_latest_screenshot_payload(device_id, force=force)
        return payload.get("image") if payload else None

    def invalidate_preview_cache(self, device_id: str) -> None:
        try:
            self.video_stream_manager.invalidate_frame_cache(device_id)
        except Exception:
            pass

    def get_latest_screenshot_payload(self, device_id: str, force: bool = True) -> Optional[Dict]:
        """
        获取预览截图载荷：image + 设备/预览分辨率（点击坐标映射必需）。
        默认 force=True 强制重截，避免点击后仍返回旧缓存画面。
        """
        from .core.preview_image import encode_preview_payload

        if not hasattr(self, "_screenshot_locks"):
            self._screenshot_locks: Dict[str, threading.Lock] = {}
        with self.lock:
            if device_id not in self._screenshot_locks:
                self._screenshot_locks[device_id] = threading.Lock()
            shot_lock = self._screenshot_locks[device_id]

        with shot_lock:
            # 仅在非强制时允许短缓存（首屏加速）
            if not force:
                cached = self.video_stream_manager.get_last_frame_payload(device_id)
                if cached and cached.get("image"):
                    return cached

            try:
                controller = self.get_device_controller(device_id)
                dw, dh = controller.get_display_size()
                png = controller.screenshot_png_bytes(timeout=25)
                if not png:
                    logger.warning(
                        f"主动截图失败: {device_id} → {controller.last_output or 'unknown'}"
                    )
                    # 强制失败时再尝试缓存兜底
                    cached = self.video_stream_manager.get_last_frame_payload(device_id)
                    if cached and cached.get("image"):
                        return cached
                    return None
                payload = encode_preview_payload(
                    png, device_width=dw, device_height=dh
                )
                if payload and payload.get("image"):
                    try:
                        self.video_stream_manager.set_last_frame_payload(device_id, payload)
                    except Exception:
                        pass
                    return payload
            except Exception as e:
                logger.warning(f"主动截图失败: {device_id}: {e}")
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
                              description: str = None,
                              wait_after: int = None) -> bool:
        """更新录制操作"""
        recorder = self.get_recorder(recording_id)
        if not recorder:
            return False
        return recorder.update_action(step_index, action_type, value, description, wait_after)
    
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
        """获取执行状态（含步骤进度）"""
        base = self.script_executor.get_status(execution_id)
        if not base:
            return None
        # 聚合 trace 数据获取步骤进度
        progress = self._aggregate_execution_progress(execution_id)
        base.update(progress)
        return base

    def _aggregate_execution_progress(self, execution_id: str) -> Dict:
        """从 trace 数据分析步骤级执行进度"""
        traces = self.storage.load_execution_traces(execution_id)
        progress = {
            'total_steps': 0,
            'current_step': 0,
            'completed_steps': 0,
            'failed_step': None,
            'failed_error': None,
            'failed_screenshot': None,
            'step_details': []
        }

        if not traces:
            return progress

        steps = {}
        for t in traces:
            sn = t.get('step_num', 0)
            if sn not in steps:
                steps[sn] = t
            else:
                # Keep the latest entry for each step
                steps[sn] = t

        progress['total_steps'] = max(steps.keys()) if steps else 0
        progress['current_step'] = max(steps.keys()) if steps else 0

        for sn in sorted(steps.keys()):
            t = steps[sn]
            step = {
                'step_num': sn,
                'action_type': t.get('action_type', ''),
                'success': t.get('success', False),
                'duration_ms': t.get('duration_ms', 0),
                'selector_strategy': t.get('selector_strategy', ''),
                'fallback_index': t.get('fallback_index', -1),
                'error': t.get('error', ''),
                'screenshot': t.get('screenshot', '')
            }
            if step['success']:
                progress['completed_steps'] += 1
            elif not progress['failed_step']:
                progress['failed_step'] = sn
                progress['failed_error'] = t.get('error', '')
                progress['failed_screenshot'] = t.get('screenshot', '')

            progress['step_details'].append(step)

        return progress

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

    def get_case_insights(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """获取用例质量体检 + 最近失败归因。"""
        session = self.storage.load_recording(recording_id)
        if not session:
            return None
        return {
            'quality': self.analyze_case_quality(session),
            'latest_diagnosis': self.get_latest_case_diagnosis(recording_id),
        }

    def analyze_case_quality(self, session: RecordingSession) -> Dict[str, Any]:
        """对用例做轻量质量治理，识别脆弱定位和回归风险。"""
        actions = list(session.actions or [])
        counts = {
            'total_steps': len(actions),
            'coordinate_steps': 0,
            'xpath_steps': 0,
            'text_steps': 0,
            'resource_id_steps': 0,
            'content_desc_steps': 0,
            'missing_selector_steps': 0,
            'assertion_steps': 0,
            'wait_steps': 0,
        }
        risks: List[str] = []
        suggestions: List[str] = []
        score = 100

        for action in actions:
            if action.action_type == 'assertion':
                counts['assertion_steps'] += 1
            if action.action_type == 'wait':
                counts['wait_steps'] += 1
                if (action.wait_after or 0) >= 3000:
                    score -= 2
                continue

            selector = action.selector
            if not selector:
                counts['missing_selector_steps'] += 1
                score -= 12
                continue

            strategy = (selector.strategy or '').strip().lower()
            if strategy == 'coordinates':
                counts['coordinate_steps'] += 1
                score -= 10
            elif strategy == 'xpath':
                counts['xpath_steps'] += 1
                score -= 7
            elif strategy == 'text':
                counts['text_steps'] += 1
                score -= 4
            elif strategy == 'content_desc':
                counts['content_desc_steps'] += 1
                score -= 3
            elif strategy == 'resource_id':
                counts['resource_id_steps'] += 1
                score -= 1
            else:
                score -= 5

            if not selector.fallbacks:
                score -= 1

        if counts['coordinate_steps'] > 0:
            risks.append(f"存在 {counts['coordinate_steps']} 个坐标定位步骤，界面稍有变动就可能失效")
            suggestions.append("优先把坐标点击替换成 resource_id / content_desc / text 等稳定定位")
        if counts['xpath_steps'] > 0:
            risks.append(f"存在 {counts['xpath_steps']} 个 XPath 步骤，层级变化时容易回归失败")
            suggestions.append("尽量减少 XPath，优先使用 resource_id 或语义更稳定的定位")
        if counts['missing_selector_steps'] > 0:
            risks.append(f"有 {counts['missing_selector_steps']} 步没有可靠选择器，只能依赖弱回放信息")
            suggestions.append("回到录制页补录这些步骤，确保每一步都有可解析的 UI 选择器")
        if counts['assertion_steps'] == 0:
            score -= 8
            risks.append("当前用例没有明确断言，容易出现“执行了但没真正校验”的情况")
            suggestions.append("至少补 1 个结果断言，确认页面到位或关键结果出现")
        if counts['wait_steps'] >= max(2, counts['total_steps'] // 3 or 1):
            score -= 5
            risks.append("等待步骤占比偏高，说明用例可能依赖固定时延")
            suggestions.append("减少硬等待，优先改成页面元素出现/状态变化再继续")
        if counts['resource_id_steps'] >= max(1, counts['total_steps'] // 2):
            score += 3

        score = max(0, min(100, score))
        if score >= 85:
            level = 'high'
            level_label = '高'
        elif score >= 65:
            level = 'medium'
            level_label = '中'
        else:
            level = 'low'
            level_label = '低'

        summary = f"可回归等级：{level_label}（{score}分）"
        if level == 'low':
            summary += "，建议先治理弱定位再纳入日常回归"
        elif level == 'medium':
            summary += "，可使用但仍有一定维护成本"
        else:
            summary += "，适合重复回归执行"

        return {
            'score': score,
            'level': level,
            'level_label': level_label,
            'summary': summary,
            'counts': counts,
            'risks': risks[:5],
            'suggestions': suggestions[:5],
        }

    def get_latest_case_diagnosis(self, case_id: str) -> Optional[Dict[str, Any]]:
        """结合最近一次执行结果做失败归因。"""
        try:
            reports = self.storage.list_reports()
        except Exception:
            return None

        for report in reports:
            for res in report.get('results', []) or []:
                if res.get('case_id') != case_id:
                    continue
                status = (res.get('status') or 'unknown').lower()
                if status == 'completed':
                    return {
                        'status': 'completed',
                        'category': 'passed',
                        'label': '最近执行通过',
                        'summary': '最近一次执行通过，当前没有新的失败归因。',
                        'job_id': report.get('job_id', ''),
                        'device_id': res.get('device_id', ''),
                        'failed_step': None,
                        'next_actions': [],
                    }
                return self._build_case_failure_diagnosis(report, res)
        return None

    def _build_case_failure_diagnosis(self, report: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        error_text = ' '.join([
            str(result.get('error') or ''),
            str(result.get('failed_error') or ''),
            ' '.join(str(x) for x in (result.get('output') or [])[-5:]) if isinstance(result.get('output'), list) else str(result.get('output') or ''),
        ]).lower()
        step_details = result.get('step_details') or []
        category = 'unknown'
        label = '未知失败'
        summary = '最近一次执行失败，但暂时无法准确归类。'
        next_actions: List[str] = [
            '查看失败截图和失败步骤，确认是否为页面变化或设备状态异常',
        ]

        if any(k in error_text for k in ['device not found', 'offline', 'unauthorized', 'no devices/emulators', 'adb']):
            category = 'device_connection'
            label = '设备连接异常'
            summary = '最近一次失败更像是设备断连、未授权或 ADB 异常，而不是业务页面问题。'
            next_actions = ['先确认设备在线和授权状态，再重新执行用例', '必要时重连 ADB 或重启测试设备']
        elif any(k in error_text for k in ['not found', 'no such element', 'uiautomator', 'objectnotfound', 'element']):
            category = 'element_not_found'
            label = '控件定位失败'
            summary = '最近一次失败更像是目标控件没有找到，通常与定位策略脆弱或页面结构变化有关。'
            next_actions = ['回看失败步骤的 selector，优先替换坐标/XPath 为稳定 resource_id', '确认失败时页面是否真的到达预期位置']
        elif any(k in error_text for k in ['assert', 'assertion', '预期', 'expected']):
            category = 'assertion_failure'
            label = '断言失败'
            summary = '脚本动作大概率执行到了，但结果校验没有通过，说明业务结果与预期不一致。'
            next_actions = ['确认断言条件是否过严或页面文案是否变更', '对照手工结果判断是真缺陷还是断言需要更新']
        elif any(k in error_text for k in ['timeout', 'timed out', 'wait', 'sleep']):
            category = 'page_not_ready'
            label = '页面未就绪'
            summary = '最近一次失败更像是页面加载慢、弹层未消失或等待条件不足。'
            next_actions = ['减少硬编码等待，改成等待关键控件出现', '确认网络、首屏加载和过渡动画是否影响执行']
        elif any(k in error_text for k in ['input', 'set_text', 'send_keys', 'ime']):
            category = 'input_failure'
            label = '输入失败'
            summary = '最近一次失败发生在输入链路，可能是输入框未聚焦、键盘干扰或控件不可编辑。'
            next_actions = ['确认输入前是否已点击聚焦目标输入框', '必要时在输入前增加页面状态检查']
        elif step_details and any((d.get('selector_strategy') or '') == 'coordinates' and not d.get('success', True) for d in step_details):
            category = 'weak_locator'
            label = '弱定位失效'
            summary = '失败步骤使用了坐标等弱定位，界面布局变化后很容易跑挂。'
            next_actions = ['优先治理失败步骤，把坐标操作改成稳定选择器', '将这条用例标记为待加固后再纳入高频回归']

        return {
            'status': result.get('status', 'failed'),
            'category': category,
            'label': label,
            'summary': summary,
            'job_id': report.get('job_id', ''),
            'device_id': result.get('device_id', ''),
            'failed_step': result.get('failed_step'),
            'error': result.get('error') or result.get('failed_error') or '',
            'next_actions': next_actions[:3],
        }

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

    def run_suite(self, suite_id: str, device_ids: Union[str, List[str]], precision_context: Optional[Dict] = None) -> Optional[str]:
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
        
        precision_context = {
            k: str(v or "").strip()
            for k, v in (precision_context or {}).items()
            if str(v or "").strip()
        }

        # Create Runtime
        runtime = get_runtime_manager().create_runtime(
            name=f"UI Suite: {suite.name}",
            module="ui_automation",
            context={
                'suite_id': suite.id,
                'device_ids': device_ids,
                'job_id': job_id,
                **precision_context,
            }
        )

        # Start execution thread
        thread = threading.Thread(
            target=self._run_suite_thread,
            args=(job_id, suite, device_ids, runtime.runtime_id, precision_context)
        )
        thread.daemon = True
        thread.start()
        
        return job_id

    def quick_run_case(self, case_id: str, device_ids: Union[str, List[str]], precision_context: Optional[Dict] = None) -> Optional[str]:
        """快速执行单个用例（自动创建临时套件）"""
        recording = self.storage.load_recording(case_id)
        if not recording:
            return None

        if isinstance(device_ids, str):
            device_ids = [device_ids]

        # 创建临时套件
        import uuid as _uuid
        temp_suite = TestSuite(
            id=f"quick_{_uuid.uuid4().hex[:8]}",
            name=f"快速执行: {recording.name or case_id}",
            project_id=getattr(recording, 'project_id', None) or '',
            case_ids=[case_id],
            description='quick-run temporary suite',
        )
        self.storage.save_suite(temp_suite)

        job_id = self.run_suite(temp_suite.id, device_ids, precision_context=precision_context)

        # 延迟清理临时套件
        def _cleanup():
            time.sleep(30)
            try:
                self.storage.delete_suite(temp_suite.id)
            except Exception:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()

        return job_id

    def get_cases_execution_status(self, case_ids: List[str]) -> Dict[str, Dict]:
        """批量查询用例最近一次执行状态"""
        status_map = {}
        try:
            reports = self.storage.list_reports()
            for report in reports:
                results = report.get('results', [])
                for res in results:
                    cid = res.get('case_id', '')
                    if cid in case_ids and cid not in status_map:
                        status_map[cid] = {
                            'status': res.get('status', 'unknown'),
                            'job_id': report.get('job_id', ''),
                            'start_time': report.get('start_time', 0),
                            'error': res.get('error', ''),
                            'failed_step': res.get('failed_step')
                        }
        except Exception:
            pass
        return status_map

    def get_suite_job_status(self, job_id: str) -> Optional[Dict]:
        """获取套件任务状态（含步骤级进度）"""
        status = self.suite_executions.get(job_id)
        if not status:
            return None
        # 为每个结果补充步骤级 trace 数据
        results = status.get('results', [])
        for res in results:
            case_id = res.get('case_id', '')
            if case_id:
                traces = self.storage.load_execution_traces(case_id)
                if traces:
                    res['step_count'] = len(set(t.get('step_num', 0) for t in traces))
                    failed_steps = [t for t in traces if not t.get('success', False)]
                    if failed_steps:
                        res['failed_step'] = failed_steps[0].get('step_num')
                        res['failed_error'] = failed_steps[0].get('error', '')
                        res['failed_screenshot'] = failed_steps[0].get('screenshot', '')
        return status

    def get_report_detail(self, job_id: str) -> Optional[Dict]:
        """获取执行报告详情（含完整 trace 数据）

        报告在套件执行结束后才落盘；执行过程中回退到内存中的
        suite_executions 快照，保证轮询期间进度/结果实时可见。
        """
        report = self.storage.load_report(job_id)
        if not report:
            # 执行中：磁盘尚无报告，回退内存快照（浅拷贝避免污染执行态）
            running = self.suite_executions.get(job_id)
            if not running:
                return None
            report = dict(running)
            report['job_id'] = job_id
            report['results'] = [dict(r) for r in running.get('results', [])]

        results = report.get('results', [])
        for res in results:
            case_id = res.get('case_id', '')
            execution_id = res.get('execution_id') or ''
            # 优先用执行 ID 的 trace；兼容旧报告仅有 case_id 的情况
            traces = []
            if execution_id:
                traces = self.storage.load_execution_traces(execution_id)
            if not traces and case_id:
                traces = self.storage.load_execution_traces(case_id)
            if traces:
                step_details = []
                for t in traces[:]:
                    td = t.to_dict() if hasattr(t, 'to_dict') else t
                    step_details.append(td)
                step_details.sort(key=lambda s: s.get('step_num', 0))
                if not res.get('step_details'):
                    res['step_details'] = step_details
                res['step_count'] = res.get('step_count') or len(step_details)
                if not res.get('failed_step'):
                    for s in step_details:
                        if not s.get('success'):
                            res['failed_step'] = s.get('step_num')
                            res['failed_error'] = s.get('error', '')
                            res['failed_screenshot'] = s.get('screenshot', '')
                            break

        return report

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
        """获取工作台数据"""
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

        # 最近失败的执行
        recent_failures = [r for r in reports[:20] if r.get('status') in ('failed', 'error', 'stopped')][:5]

        return {
            'project_count': len(projects),
            'case_count': len(cases),
            'total_runs': total_runs,
            'pass_rate': round(pass_rate, 1),
            'recent_cases': cases[:5],
            'recent_reports': reports[:8],
            'recent_failures': recent_failures
        }

    def _run_suite_thread(self, job_id: str, suite: TestSuite, device_ids: List[str], runtime_id: str, precision_context: Optional[Dict] = None):
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
                'start_time': time.time(),
                **(precision_context or {}),
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
                                        'end_time': time.time(),
                                        'execution_id': exec_id,
                                    }
                                    if status:
                                        if status.get('failed_step'):
                                            result_entry['failed_step'] = status.get('failed_step')
                                        if status.get('failed_error'):
                                            result_entry['error'] = status.get('failed_error') or result_entry.get('error')
                                        if status.get('failed_screenshot'):
                                            result_entry['failed_screenshot'] = status.get('failed_screenshot')
                                        if status.get('step_details'):
                                            result_entry['step_details'] = status.get('step_details')
                                            result_entry['step_count'] = len(status.get('step_details') or [])
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
            failed = sum(1 for r in self.suite_executions[job_id].get('results', []) if r.get('status') not in ('completed',))
            final_status = self.suite_executions[job_id]['status']
            self.suite_executions[job_id]['pass_rate'] = (passed / total * 100) if total > 0 else 0
            self.suite_executions[job_id]['passed'] = passed
            self.suite_executions[job_id]['failed'] = failed
            if final_status == 'completed' and total > 0 and failed == 0:
                self.suite_executions[job_id]['precision_status'] = 'PASS'
                self.suite_executions[job_id]['summary'] = f'UI自动化执行通过：{passed}/{total}'
            elif final_status in ('stopped', 'error') or failed > 0:
                self.suite_executions[job_id]['precision_status'] = 'FAIL'
                self.suite_executions[job_id]['summary'] = f'UI自动化执行存在失败：通过 {passed} / 失败 {failed} / 总数 {total}'
            else:
                self.suite_executions[job_id]['precision_status'] = 'PENDING'
                self.suite_executions[job_id]['summary'] = 'UI自动化未产生明确执行结果'
            
            # Determine Final Runtime Status
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
            report_data['report_url'] = f'/ui_automation/report/{job_id}'
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

    # --- AI 探索 / 失败归因（人工在环）---

    def ai_explore_case(
        self,
        device_id: str,
        case_text: str,
        *,
        name: str = "",
        package_name: str = "",
        project_id: str = "",
        description: str = "",
        execute_while_exploring: bool = True,
        progress_callback=None,
        closed_loop: bool = True,
        auto_diagnose: bool = True,
        cancel_check=None,
        timeout_sec: float = 600,
        attach_previews: bool = True,
        auto_validate: bool = True,
    ) -> Dict:
        """自然语言/分步用例 → AI 探索固化为可回归用例。

        closed_loop=True（默认）走 ExploreAgent：失败重试/滚动/重规划。
        """
        if closed_loop:
            from .agents.explore_agent import ExploreAgent

            agent = ExploreAgent(self.storage)
            return agent.run(
                device_id=device_id,
                case_text=case_text,
                name=name,
                package_name=package_name,
                project_id=project_id,
                description=description,
                execute_while_exploring=execute_while_exploring,
                progress_callback=progress_callback,
                auto_diagnose=auto_diagnose,
                cancel_check=cancel_check,
                timeout_sec=timeout_sec,
                attach_previews=attach_previews,
                auto_validate=auto_validate,
            )

        from .ai_explorer import AIExplorer

        explorer = AIExplorer(self.storage)
        return explorer.explore_and_create_case(
            device_id=device_id,
            case_text=case_text,
            name=name,
            package_name=package_name,
            project_id=project_id,
            description=description,
            execute_while_exploring=execute_while_exploring,
            progress_callback=progress_callback,
        )

    def start_ai_explore_job(
        self,
        device_id: str,
        case_text: str,
        *,
        name: str = "",
        package_name: str = "",
        project_id: str = "",
        description: str = "",
        execute_while_exploring: bool = True,
        closed_loop: bool = True,
        auto_diagnose: bool = True,
        timeout_sec: float = 600,
    ) -> str:
        """启动异步闭环探索，立即返回 job_id。"""
        import uuid

        job_id = f"explore_{uuid.uuid4().hex[:12]}"
        with self.lock:
            self.ai_explore_jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "events": [],
                "result": None,
                "error": None,
                "cancel_requested": False,
                "timeout_sec": float(timeout_sec or 600),
                "created_at": time.time(),
                "updated_at": time.time(),
            }

        def _progress(event: Dict):
            with self.lock:
                job = self.ai_explore_jobs.get(job_id)
                if not job:
                    return
                # 缩略图事件较大，只保留最近少量 preview
                ev = dict(event)
                if ev.get("preview_image") and len(str(ev.get("preview_image"))) > 200000:
                    ev.pop("preview_image", None)
                job["events"].append(ev)
                if len(job["events"]) > 200:
                    # 丢掉过旧 preview 以控内存
                    trimmed = []
                    for e in job["events"][-150:]:
                        if e.get("phase") == "preview" and len(trimmed) > 20:
                            continue
                        trimmed.append(e)
                    job["events"] = trimmed[-150:]
                job["updated_at"] = time.time()
                job["last_event"] = {k: v for k, v in ev.items() if k != "preview_image"}
            # 进度落盘（低频可接受）
            if (event or {}).get("phase") in (
                "planned",
                "step_done",
                "validated",
                "done",
                "cancelled",
                "error",
                "budget_exhausted",
            ):
                self._persist_explore_job(job_id)

        def _cancel_check() -> bool:
            with self.lock:
                job = self.ai_explore_jobs.get(job_id)
                if not job:
                    return True
                return bool(job.get("cancel_requested"))

        def _worker():
            try:
                result = self.ai_explore_case(
                    device_id=device_id,
                    case_text=case_text,
                    name=name,
                    package_name=package_name,
                    project_id=project_id,
                    description=description,
                    execute_while_exploring=execute_while_exploring,
                    progress_callback=_progress,
                    closed_loop=closed_loop,
                    auto_diagnose=auto_diagnose,
                    cancel_check=_cancel_check,
                    timeout_sec=float(timeout_sec or 600),
                    auto_validate=True,
                )
                with self.lock:
                    job = self.ai_explore_jobs.get(job_id)
                    if job:
                        if result.get("cancelled"):
                            job["status"] = "cancelled"
                            job["error"] = result.get("cancel_reason") or "已取消"
                        else:
                            job["status"] = "completed"
                        job["result"] = result
                        job["updated_at"] = time.time()
                self._persist_explore_job(job_id)
            except Exception as e:
                logger.error(f"AI explore job {job_id} failed: {e}", exc_info=True)
                with self.lock:
                    job = self.ai_explore_jobs.get(job_id)
                    if job:
                        job["status"] = "failed"
                        job["error"] = str(e)
                        job["updated_at"] = time.time()
                        job["events"].append(
                            {"phase": "error", "message": str(e), "ts": time.time()}
                        )
                self._persist_explore_job(job_id)

        threading.Thread(target=_worker, daemon=True, name=f"ui-explore-{job_id}").start()
        self._persist_explore_job(job_id)
        return job_id

    def cancel_ai_explore_job(self, job_id: str) -> bool:
        """请求取消正在运行的探索任务（协作式，下一检查点生效）。"""
        with self.lock:
            job = self.ai_explore_jobs.get(job_id)
            if not job:
                # 尝试从磁盘加载
                disk = self.storage.load_explore_job(job_id)
                if disk:
                    self.ai_explore_jobs[job_id] = disk
                    job = disk
            if not job:
                return False
            if job.get("status") != "running":
                return False
            job["cancel_requested"] = True
            job["updated_at"] = time.time()
            job["events"].append(
                {
                    "phase": "cancel_requested",
                    "message": "已请求取消，等待当前步骤结束…",
                    "ts": time.time(),
                }
            )
        self._persist_explore_job(job_id)
        return True

    def get_ai_explore_job(self, job_id: str, *, since: int = 0) -> Optional[Dict]:
        with self.lock:
            job = self.ai_explore_jobs.get(job_id)
            if not job:
                disk = self.storage.load_explore_job(job_id)
                if disk:
                    self.ai_explore_jobs[job_id] = disk
                    job = disk
            if not job:
                return None
            events = job.get("events") or []
            slice_events = events[since:] if since > 0 else events
            return {
                "job_id": job_id,
                "status": job.get("status"),
                "error": job.get("error"),
                "result": job.get("result"),
                "events": list(slice_events),
                "event_count": len(events),
                "last_event": job.get("last_event"),
                "cancel_requested": bool(job.get("cancel_requested")),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
            }

    def list_ai_explore_jobs(self, limit: int = 20) -> List[Dict]:
        with self.lock:
            items = list(self.ai_explore_jobs.values())
        items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
        out = []
        for job in items[: max(1, int(limit))]:
            out.append(
                {
                    "job_id": job.get("job_id"),
                    "status": job.get("status"),
                    "error": job.get("error"),
                    "created_at": job.get("created_at"),
                    "updated_at": job.get("updated_at"),
                    "recording_id": (job.get("result") or {}).get("recording_id"),
                    "regression_ready": (job.get("result") or {}).get("regression_ready"),
                    "message": ((job.get("result") or {}).get("message") or job.get("error") or ""),
                }
            )
        return out

    def ai_diagnose_failure(
        self,
        case_id: str,
        *,
        device_id: str = "",
        failed_step=None,
        error: str = "",
        step_details=None,
        dump_live_ui: bool = True,
    ) -> Dict:
        """执行失败 AI 归因；补丁仅建议，需人工确认。"""
        from .ai_diagnoser import AIDiagnoser

        diagnoser = AIDiagnoser(self.storage)
        return diagnoser.diagnose_failure(
            case_id,
            device_id=device_id,
            failed_step=failed_step,
            error=error,
            step_details=step_details,
            dump_live_ui=dump_live_ui,
        )

    def ai_apply_patch(
        self,
        case_id: str,
        suggested_patch: Dict,
        *,
        approved: bool = False,
    ) -> Dict:
        """人工确认后应用建议补丁到用例。"""
        from .ai_diagnoser import AIDiagnoser

        diagnoser = AIDiagnoser(self.storage)
        return diagnoser.apply_suggested_patch(
            case_id,
            suggested_patch,
            approved=approved,
        )
