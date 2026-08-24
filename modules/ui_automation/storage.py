"""
UI自动化数据存储
"""
import json
import os
import threading
from datetime import datetime
from typing import List, Dict, Optional
from .models import RecordingSession, UIAction, Project, TestSuite, ExecutionTrace
from utils.logger import setup_logger

logger = setup_logger('ui_automation_storage')


class RecordingStorage:
    """录制数据存储管理器"""
    
    def __init__(self, storage_dir: str = "ui_automation_data"):
        """
        初始化存储管理器
        
        :param storage_dir: 存储目录
        """
        self.storage_dir = storage_dir
        self.recordings_dir = os.path.join(storage_dir, "recordings")
        self.projects_dir = os.path.join(storage_dir, "projects")
        self.screenshots_dir = os.path.join(storage_dir, "screenshots")
        self.ui_trees_dir = os.path.join(storage_dir, "ui_trees")
        self.scripts_dir = os.path.join(storage_dir, "scripts")
        self.suites_dir = os.path.join(storage_dir, "suites")
        self.reports_dir = os.path.join(storage_dir, "reports")
        self.traces_dir = os.path.join(storage_dir, "traces")
        self.explore_jobs_dir = os.path.join(storage_dir, "ai_explore_jobs")
        
        # 创建目录
        os.makedirs(self.recordings_dir, exist_ok=True)
        os.makedirs(self.projects_dir, exist_ok=True)
        os.makedirs(self.screenshots_dir, exist_ok=True)
        os.makedirs(self.ui_trees_dir, exist_ok=True)
        os.makedirs(self.scripts_dir, exist_ok=True)
        os.makedirs(self.suites_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)
        os.makedirs(self.traces_dir, exist_ok=True)
        os.makedirs(self.explore_jobs_dir, exist_ok=True)
        
        self.lock = threading.Lock()

    def _normalize_session_defaults(self, session: Optional[RecordingSession]) -> Optional[RecordingSession]:
        """兼容旧用例字段，统一补齐 Android 默认值。"""
        if not session:
            return session
        session.platform = (session.platform or 'android').strip() or 'android'
        if session.platform == 'android':
            session.target = (session.target or session.device_id or session.package_name or '').strip()
            session.entry_url = ''
        else:
            session.target = (session.target or '').strip()
            session.entry_url = (session.entry_url or '').strip()
        return session
    
    def save_execution_trace(self, trace: ExecutionTrace) -> bool:
        """
        保存执行Trace (追加模式)
        
        :param trace: Trace对象
        :return: 是否成功
        """
        try:
            file_path = os.path.join(self.traces_dir, f"{trace.run_id}.jsonl")
            with self.lock:
                with open(file_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(trace.to_dict(), ensure_ascii=False) + '\n')
            return True
        except Exception as e:
            logger.warning(f"保存Trace失败: {e}")
            return False

    def load_execution_traces(self, run_id: str) -> List[ExecutionTrace]:
        """
        加载指定运行的所有Trace
        
        :param run_id: 运行ID
        :return: Trace列表
        """
        traces = []
        try:
            file_path = os.path.join(self.traces_dir, f"{run_id}.jsonl")
            if not os.path.exists(file_path):
                return []
                
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            data = json.loads(line)
                            traces.append(ExecutionTrace.from_dict(data))
                        except:
                            pass
            return traces
        except Exception as e:
            logger.warning(f"加载Trace失败: {e}")
            return []

    
    def save_recording(self, session: RecordingSession) -> bool:
        """
        保存录制会话
        
        :param session: 录制会话
        :return: 是否成功
        """
        try:
            file_path = os.path.join(self.recordings_dir, f"{session.id}.json")
            with self.lock:
                existing_session: Optional[RecordingSession] = None
                if os.path.exists(file_path):
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            existing_data = json.load(f)
                        existing_session = RecordingSession.from_dict(existing_data)
                    except Exception:
                        existing_session = None

                session_to_save = self._normalize_session_defaults(session)
                if existing_session:
                    existing_session = self._normalize_session_defaults(existing_session)
                    existing_actions_by_key = {}
                    for action in existing_session.actions:
                        existing_actions_by_key[(action.step_num, action.timestamp)] = action

                    for action in session.actions:
                        existing_actions_by_key[(action.step_num, action.timestamp)] = action

                    merged_actions = [
                        existing_actions_by_key[k]
                        for k in sorted(existing_actions_by_key.keys(), key=lambda x: (x[0], x[1]))
                    ]

                    session_to_save = RecordingSession(
                        id=session.id or existing_session.id,
                        device_id=session.device_id or existing_session.device_id,
                        package_name=session.package_name or existing_session.package_name,
                        created_at=min(session.created_at, existing_session.created_at),
                        actions=merged_actions,
                        description=session.description or existing_session.description,
                        project_id=session.project_id or existing_session.project_id,
                        name=session.name or existing_session.name,
                        platform=session.platform or existing_session.platform or "android",
                        target=session.target or existing_session.target,
                        entry_url=session.entry_url or existing_session.entry_url,
                        meta=(existing_session.meta or {}) | (session.meta or {}),
                    )

                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(session_to_save.to_dict(), f, ensure_ascii=False, indent=2)
                return True
        except Exception as e:
            logger.warning(f"保存录制失败: {e}")
            return False
    
    def load_recording(self, recording_id: str) -> Optional[RecordingSession]:
        """
        加载录制会话
        
        :param recording_id: 录制ID
        :return: 录制会话
        """
        try:
            file_path = os.path.join(self.recordings_dir, f"{recording_id}.json")
            if not os.path.exists(file_path):
                return None
            
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return self._normalize_session_defaults(RecordingSession.from_dict(data))
        except Exception as e:
            logger.warning(f"加载录制失败: {e}")
            return None
    
    def list_recordings(self, project_id: Optional[str] = None) -> List[Dict]:
        """
        列出所有录制
        
        :param project_id: 按项目ID过滤（可选）
        :return: 录制列表
        """
        recordings = []
        
        try:
            for filename in os.listdir(self.recordings_dir):
                if not filename.endswith('.json'):
                    continue
                
                recording_id = filename[:-5]  # 去掉.json
                session = self.load_recording(recording_id)
                if session:
                    if project_id and session.project_id != project_id:
                        continue
                        
                    recordings.append({
                        'id': session.id,
                        'device_id': session.device_id,
                        'package_name': session.package_name,
                        'platform': session.platform or 'android',
                        'target': session.target or session.device_id,
                        'entry_url': session.entry_url or '',
                        'created_at': session.created_at.isoformat() if isinstance(session.created_at, datetime) else session.created_at,
                        'action_count': len(session.actions),
                        'artifact_step_count': self.get_artifact_step_count(session.id),
                        'description': session.description,
                        'project_id': session.project_id,
                        'name': session.name
                    })
        except Exception as e:
            logger.warning(f"列出录制失败: {e}")
        
        # 按创建时间倒序排列
        recordings.sort(key=lambda x: x['created_at'], reverse=True)
        return recordings

    # --- Project Methods ---

    def save_project(self, project: Project) -> bool:
        """保存项目"""
        try:
            file_path = os.path.join(self.projects_dir, f"{project.id}.json")
            with self.lock:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(project.to_dict(), f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"保存项目失败: {e}")
            return False

    def load_project(self, project_id: str) -> Optional[Project]:
        """加载项目"""
        try:
            file_path = os.path.join(self.projects_dir, f"{project_id}.json")
            if not os.path.exists(file_path):
                return None
            
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return Project.from_dict(data)
        except Exception as e:
            logger.warning(f"加载项目失败: {e}")
            return None

    def list_projects(self) -> List[Project]:
        """列出所有项目"""
        projects = []
        try:
            for filename in os.listdir(self.projects_dir):
                if not filename.endswith('.json'):
                    continue
                
                project_id = filename[:-5]
                project = self.load_project(project_id)
                if project:
                    projects.append(project)
        except Exception as e:
            logger.warning(f"列出项目失败: {e}")
        
        # 按更新时间倒序
        projects.sort(key=lambda x: x.updated_at, reverse=True)
        return projects

    def delete_project(self, project_id: str) -> bool:
        """删除项目"""
        try:
            file_path = os.path.join(self.projects_dir, f"{project_id}.json")
            if os.path.exists(file_path):
                os.remove(file_path)
            return True
        except Exception as e:
            logger.warning(f"删除项目失败: {e}")
            return False


    def get_artifact_step_count(self, recording_id: str) -> int:
        try:
            screenshot_dir = os.path.join(self.screenshots_dir, recording_id)
            ui_tree_dir = os.path.join(self.ui_trees_dir, recording_id)
            screenshot_count = 0
            ui_tree_count = 0
            if os.path.exists(screenshot_dir):
                screenshot_count = len([f for f in os.listdir(screenshot_dir) if f.lower().endswith('.png')])
            if os.path.exists(ui_tree_dir):
                ui_tree_count = len([f for f in os.listdir(ui_tree_dir) if f.lower().endswith('.xml')])
            return max(screenshot_count, ui_tree_count)
        except Exception:
            return 0
    
    def delete_recording(self, recording_id: str) -> bool:
        """
        删除录制
        
        :param recording_id: 录制ID
        :return: 是否成功
        """
        try:
            file_path = os.path.join(self.recordings_dir, f"{recording_id}.json")
            if os.path.exists(file_path):
                os.remove(file_path)
            
            # 删除相关的截图和UI树目录
            screenshot_dir = os.path.join(self.screenshots_dir, recording_id)
            if os.path.exists(screenshot_dir):
                import shutil
                shutil.rmtree(screenshot_dir)
            
            ui_tree_dir = os.path.join(self.ui_trees_dir, recording_id)
            if os.path.exists(ui_tree_dir):
                import shutil
                shutil.rmtree(ui_tree_dir)
            
            return True
        except Exception as e:
            logger.warning(f"删除录制失败: {e}")
            return False
    
    def get_screenshot_path(self, recording_id: str, step_num: int) -> str:
        """
        获取截图路径
        
        :param recording_id: 录制ID
        :param step_num: 步骤编号
        :return: 截图路径
        """
        screenshot_dir = os.path.join(self.screenshots_dir, recording_id)
        os.makedirs(screenshot_dir, exist_ok=True)
        return os.path.join(screenshot_dir, f"step_{step_num}.png")
    
    def get_ui_tree_path(self, recording_id: str, step_num: int) -> str:
        """
        获取UI树路径
        
        :param recording_id: 录制ID
        :param step_num: 步骤编号
        :return: UI树路径
        """
        ui_tree_dir = os.path.join(self.ui_trees_dir, recording_id)
        os.makedirs(ui_tree_dir, exist_ok=True)
        return os.path.join(ui_tree_dir, f"step_{step_num}.xml")
    
    def get_script_path(self, recording_id: str) -> str:
        """
        获取脚本路径
        
        :param recording_id: 录制ID
        :return: 脚本路径
        """
        return os.path.join(self.scripts_dir, f"{recording_id}.py")

    # --- Suite Methods ---

    def save_suite(self, suite: TestSuite) -> bool:
        """保存测试套件"""
        try:
            file_path = os.path.join(self.suites_dir, f"{suite.id}.json")
            with self.lock:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(suite.to_dict(), f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"保存测试套件失败: {e}")
            return False

    def load_suite(self, suite_id: str) -> Optional[TestSuite]:
        """加载测试套件"""
        try:
            file_path = os.path.join(self.suites_dir, f"{suite_id}.json")
            if not os.path.exists(file_path):
                return None
            
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return TestSuite.from_dict(data)
        except Exception as e:
            logger.warning(f"加载测试套件失败: {e}")
            return None

    def list_suites(self, project_id: Optional[str] = None) -> List[TestSuite]:
        """列出所有测试套件"""
        suites = []
        try:
            for filename in os.listdir(self.suites_dir):
                if not filename.endswith('.json'):
                    continue
                
                suite_id = filename[:-5]
                suite = self.load_suite(suite_id)
                if suite:
                    if project_id and suite.project_id != project_id:
                        continue
                    suites.append(suite)
        except Exception as e:
            logger.warning(f"列出测试套件失败: {e}")
        
        # 按更新时间倒序
        suites.sort(key=lambda x: x.updated_at, reverse=True)
        return suites

    def delete_suite(self, suite_id: str) -> bool:
        """删除测试套件"""
        try:
            file_path = os.path.join(self.suites_dir, f"{suite_id}.json")
            if os.path.exists(file_path):
                os.remove(file_path)
            return True
        except Exception as e:
            logger.warning(f"删除测试套件失败: {e}")
            return False

    # --- Report Methods ---

    def save_report(self, report_data: Dict) -> bool:
        """保存执行报告"""
        try:
            job_id = report_data.get('job_id')
            if not job_id:
                return False
            
            file_path = os.path.join(self.reports_dir, f"{job_id}.json")
            with self.lock:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(report_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"保存执行报告失败: {e}")
            return False

    def load_report(self, job_id: str) -> Optional[Dict]:
        """加载单个执行报告"""
        try:
            file_path = os.path.join(self.reports_dir, f"{job_id}.json")
            if not os.path.exists(file_path):
                return None
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载执行报告失败: {e}")
            return None

    def list_reports(self) -> List[Dict]:
        """列出所有执行报告"""
        reports = []
        try:
            for filename in os.listdir(self.reports_dir):
                if not filename.endswith('.json'):
                    continue
                
                try:
                    file_path = os.path.join(self.reports_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        reports.append(json.load(f))
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"列出执行报告失败: {e}")
            
        # 按开始时间倒序
        reports.sort(key=lambda x: x.get('start_time', 0), reverse=True)
        return reports

    # --- AI 探索任务落盘 ---

    def save_explore_job(self, job: Dict) -> bool:
        """持久化探索任务快照（去掉过大 preview 图）。"""
        try:
            job_id = (job or {}).get("job_id") or ""
            if not job_id:
                return False
            payload = dict(job)
            # 事件里去掉 base64 预览，避免磁盘膨胀
            events = []
            for ev in payload.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                e = {k: v for k, v in ev.items() if k != "preview_image"}
                events.append(e)
            payload["events"] = events[-200:]
            if payload.get("last_event") and isinstance(payload["last_event"], dict):
                payload["last_event"] = {
                    k: v for k, v in payload["last_event"].items() if k != "preview_image"
                }
            path = os.path.join(self.explore_jobs_dir, f"{job_id}.json")
            with self.lock:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"保存探索任务失败: {e}")
            return False

    def load_explore_job(self, job_id: str) -> Optional[Dict]:
        try:
            path = os.path.join(self.explore_jobs_dir, f"{job_id}.json")
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载探索任务失败: {e}")
            return None

    def list_explore_jobs(self, limit: int = 30) -> List[Dict]:
        jobs = []
        try:
            for name in os.listdir(self.explore_jobs_dir):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(self.explore_jobs_dir, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        jobs.append(json.load(f))
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"列出探索任务失败: {e}")
        jobs.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or 0, reverse=True)
        return jobs[: max(1, int(limit))]
