"""
性能监控服务
管理性能监控任务，协调 AdbController、Storage 和 Baseline
"""
import threading
import time
import statistics
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List
from utils.logger import setup_logger
from modules.log_monitor.core.adb_controller import AdbController
from modules.log_monitor.core.models.analysis_models import PerformanceSnapshot
from .storage import PerformanceStorage
from .baseline import PerformanceBaseline
from .alert_engine import PerformanceAlertEngine
from .perceptual_stall_analyzer import PerceptualStallAnalyzer
from core.runtime.manager import get_runtime_manager
from core.runtime.model import RuntimeStatus

logger = setup_logger('performance_monitor_service')


class PerformanceMonitorService:
    """性能监控服务"""
    
    def __init__(self, storage_dir: str = "performance_data", 
                 baseline_file: str = "performance_baseline.json"):
        """
        初始化性能监控服务
        
        :param storage_dir: 存储目录
        :param baseline_file: 基线文件路径
        """
        self.storage = PerformanceStorage(storage_dir)
        self.baseline = PerformanceBaseline(baseline_file)
        self.alert_engine = PerformanceAlertEngine()
        self.tasks: Dict[str, Dict] = {}  # {task_id: {controller, device_id, session_id, alert_engine, ...}}
        self.tasks_lock = threading.Lock()

    @staticmethod
    def _normalize_log_keywords(keywords: Any) -> List[str]:
        if isinstance(keywords, str):
            raw = keywords.replace('\n', ',').split(',')
        elif isinstance(keywords, list):
            raw = keywords
        else:
            raw = []
        out: List[str] = []
        seen = set()
        for item in raw:
            key = str(item or '').strip()
            low = key.lower()
            if not key or low in seen:
                continue
            seen.add(low)
            out.append(key)
        return out
    
    def start_monitoring(self, task_id: str, device_id: str, package_name: str,
                        description: str = "", polling_interval: float = 3.0,
                        monitor_type: str = "video",
                        duration_seconds: int = 0,
                        display_id: int = 1,
                        save_matched_logs: bool = False,
                        log_keywords: Any = None,
                        save_full_logs: bool = False,
                        full_log_rotate_size_mb: float = 20.0) -> bool:
        """
        开始性能监控
        
        :param task_id: 任务ID
        :param device_id: 设备ID
        :param package_name: 包名
        :param description: 描述
        :param polling_interval: 轮询间隔（秒）
        :param monitor_type: 监控类型 ("video" 视频播放卡顿 / "ui" UI界面卡顿)
        :return: 是否成功
        """
        with self.tasks_lock:
            if task_id in self.tasks:
                logger.warning(f"任务 {task_id} 已存在")
                return False
            
            # 创建会话
            session_id = f"perf_{int(time.time())}"
            normalized_keywords = self._normalize_log_keywords(log_keywords)
            if not self.storage.create_session(
                session_id,
                device_id,
                package_name,
                description,
                monitor_type=monitor_type,
                display_id=display_id,
                matched_log_saving_enabled=save_matched_logs,
                matched_log_keywords=normalized_keywords,
                full_log_saving_enabled=save_full_logs,
                full_log_rotate_size_mb=full_log_rotate_size_mb,
            ):
                logger.error(f"创建会话失败: {session_id}")
                return False
            
            # 创建 ADB 控制器
            controller = AdbController()
            controller.target_package = package_name
            controller.polling_interval = polling_interval
            # 优先使用用户配置的 Display ID（用于 FPS 采集）
            try:
                controller.preferred_display_id = int(display_id)
            except Exception:
                controller.preferred_display_id = 1
            
            # 创建任务专用的告警引擎
            task_alert_engine = PerformanceAlertEngine()
            
            # 创建人眼感知卡顿分析器（默认60fps）
            stall_analyzer = PerceptualStallAnalyzer(target_fps=60, window_size=120)
            
            # Runtime Initialization
            runtime_id = None
            try:
                runtime = get_runtime_manager().create_runtime(
                    name=f"Performance Monitor: {package_name}",
                    module="performance_monitor",
                    context={
                        'device_id': device_id,
                        'package_name': package_name,
                        'task_id': task_id,
                        'session_id': session_id,
                        'description': description
                    }
                )
                runtime_id = runtime.runtime_id
                get_runtime_manager().update_status(runtime_id, RuntimeStatus.RUNNING)
                logger.info(f"Runtime created for Performance Monitor: {runtime_id}")
            except Exception as e:
                logger.warning(f"Failed to create Runtime for Performance Monitor: {e}")
            
            # 先创建任务信息（用于回调中访问）
            task_info = {
                'controller': None,  # 稍后设置
                'device_id': device_id,
                'package_name': package_name,
                'session_id': session_id,
                'start_time': time.time(),
                'description': description,
                'alert_engine': task_alert_engine,
                'stall_analyzer': stall_analyzer,  # 人眼感知卡顿分析器
                'alert_callback': None,  # 由 views 设置
                'runtime_id': runtime_id,
                'save_matched_logs': bool(save_matched_logs),
                'log_keywords': normalized_keywords,
                'save_full_logs': bool(save_full_logs),
                'full_log_rotate_size_mb': float(full_log_rotate_size_mb or 20.0),
            }
            self.tasks[task_id] = task_info
            
            # 性能数据回调
            def performance_callback(snapshot: PerformanceSnapshot):
                """性能数据回调"""
                # 处理人眼感知卡顿分析
                # 从 controller 获取帧时间序列（如果可用）
                frame_times = getattr(controller, '_cached_frame_times', [])
                
                if frame_times:
                    try:
                        stall_analyzer.update_expected_frame_time(statistics.median(frame_times))
                    except Exception:
                        pass
                    # 逐帧添加到分析器
                    current_time = time.time()
                    for frame_time_ms in frame_times:
                        stall_event = stall_analyzer.add_frame_time(frame_time_ms, current_time)
                        if stall_event:
                            # 打印卡顿日志
                            log_msg = f"【人眼感知卡顿】{stall_event.description} (评分: {stall_event.stall_score:.1f})"
                            
                            # 根据严重程度选择日志级别
                            if stall_event.severity == 'severe':
                                logger.error(log_msg)
                            elif stall_event.severity == 'moderate':
                                logger.warning(log_msg)
                            else:
                                logger.info(log_msg)
                                
                            # 记录到告警引擎（如果配置了规则）
                            if task_alert_engine:
                                # TODO: 适配告警规则
                                pass
                                
                        current_time += frame_time_ms / 1000.0  # 模拟时间戳
                    
                    # 获取当前卡顿指标
                    metrics = stall_analyzer.get_current_metrics()
                    
                    # 更新快照中的人眼感知卡顿数据
                    snapshot.perceptual_stall_score = metrics.stall_score
                    snapshot.perceptual_stall_events = metrics.total_stall_events
                    snapshot.perceptual_stall_duration_ms = metrics.total_stall_duration_ms
                    snapshot.is_perceptual_stalling = metrics.is_stalling
                    snapshot.frame_time_variance = metrics.frame_time_variance
                    
                    # 设置当前卡顿严重程度
                    if metrics.is_stalling:
                        # 根据当前卡顿持续时间判断严重程度
                        if metrics.current_stall_duration_ms > 200:
                            snapshot.perceptual_stall_severity = "severe"
                        elif metrics.current_stall_duration_ms > 100:
                            snapshot.perceptual_stall_severity = "moderate"
                        else:
                            snapshot.perceptual_stall_severity = "mild"
                    else:
                        snapshot.perceptual_stall_severity = ""
                    
                    # 清空已处理的帧时间（避免重复处理）
                    controller._cached_frame_times = []
                
                # 存储快照
                self.storage.add_snapshot(session_id, snapshot)
                
                # 检查告警
                snapshot_dict = {
                    'cpu_usage': snapshot.cpu_usage,
                    'total_pss': snapshot.total_pss,
                    'fps': snapshot.fps,
                    'jank_count': snapshot.jank_count,
                    'network_rx_kb': snapshot.network_rx_kb,
                    'network_tx_kb': snapshot.network_tx_kb,
                    'gc_count': snapshot.gc_count,
                    'perceptual_stall_score': snapshot.perceptual_stall_score,
                    'perceptual_stall_events': snapshot.perceptual_stall_events,
                    'is_perceptual_stalling': snapshot.is_perceptual_stalling
                }
                
                alerts = task_alert_engine.check_performance(
                    snapshot_dict,
                    device_id=device_id,
                    package_name=package_name,
                    session_id=session_id
                )
                
                # 如果有新告警，触发回调
                if alerts:
                    alert_callback = task_info.get('alert_callback')
                    for alert in alerts:
                        self.storage.add_alert_history(session_id, alert.to_dict())
                        if alert_callback:
                            alert_callback(alert)
            
            def log_callback(log_line, analysis_result):
                raw_line = str(log_line or '')
                timestamp = datetime.now().isoformat()
                if task_info.get('save_full_logs'):
                    self.storage.add_full_log_line(session_id, {
                        'timestamp': timestamp,
                        'log_line': raw_line,
                        'level': (analysis_result.get('level') if isinstance(analysis_result, dict) else ''),
                    })
                keywords = task_info.get('log_keywords') or []
                if not task_info.get('save_matched_logs') or not keywords:
                    return
                lower_line = raw_line.lower()
                matched_keywords = [kw for kw in keywords if kw.lower() in lower_line]
                if not matched_keywords:
                    return
                log_item = {
                    'timestamp': timestamp,
                    'matched_keywords': matched_keywords,
                    'log_line': raw_line,
                }
                if isinstance(analysis_result, dict):
                    if analysis_result.get('level'):
                        log_item['level'] = analysis_result.get('level')
                    if analysis_result.get('tag'):
                        log_item['tag'] = analysis_result.get('tag')
                    if analysis_result.get('message'):
                        log_item['message'] = analysis_result.get('message')
                self.storage.add_matched_log(session_id, log_item)
            
            # 开始监控
            controller.start_monitoring(
                device_id=device_id,
                log_callback=log_callback,
                min_log_level="Verbose",
                performance_callback=performance_callback,
                target_package=package_name
            )
            
            # 更新任务信息（设置controller）
            task_info['controller'] = controller
            
            logger.info(f"性能监控已启动: {task_id}, 会话: {session_id}")

            # 如配置了监控时长，则在后台线程中到时自动停止
            if duration_seconds and duration_seconds > 0:
                def auto_stop():
                    try:
                        logger.info(f"任务 {task_id} 配置了自动停止，时长 {duration_seconds} 秒")
                        time.sleep(duration_seconds)
                        # 若任务仍在运行，则请求停止
                        self.stop_monitoring(task_id)
                    except Exception as e:
                        logger.warning(f"自动停止任务 {task_id} 失败: {e}", exc_info=True)

                t = threading.Thread(target=auto_stop, daemon=True)
                t.start()

            return True
    
    def stop_monitoring(self, task_id: str) -> bool:
        """
        停止性能监控
        
        :param task_id: 任务ID
        :return: 是否成功
        """
        with self.tasks_lock:
            if task_id not in self.tasks:
                logger.warning(f"任务 {task_id} 不存在")
                return False
            
            task_info = self.tasks[task_id]
            controller = task_info['controller']
            session_id = task_info['session_id']
            
            # 停止监控
            controller.stop_monitoring()
            
            # 结束会话
            self.storage.end_session(session_id)
            
            # Update Runtime Status
            runtime_id = task_info.get('runtime_id')
            if runtime_id:
                try:
                    get_runtime_manager().update_status(
                        runtime_id, 
                        RuntimeStatus.COMPLETED,
                        result={"session_id": session_id}
                    )
                    logger.info(f"Runtime finished for Performance Monitor: {runtime_id}")
                except Exception as e:
                    logger.warning(f"Failed to update Runtime status for Performance Monitor: {e}")

            # 清理任务
            del self.tasks[task_id]
            
            logger.info(f"性能监控已停止: {task_id}")
            return True
    
    def get_task_info(self, task_id: str) -> Optional[Dict]:
        """
        获取任务信息
        
        :param task_id: 任务ID
        :return: 任务信息
        """
        with self.tasks_lock:
            return self.tasks.get(task_id)
    
    def list_tasks(self) -> list:
        """
        列出所有任务
        
        :return: 任务列表
        """
        with self.tasks_lock:
            return [
                {
                    'task_id': tid,
                    'device_id': info['device_id'],
                    'package_name': info['package_name'],
                    'session_id': info['session_id'],
                    'start_time': info['start_time'],
                    'running_time': int(time.time() - info['start_time']),
                    'description': info.get('description', '')
                }
                for tid, info in self.tasks.items()
            ]
