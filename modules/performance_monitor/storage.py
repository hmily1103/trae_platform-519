"""
性能数据存储模块
支持性能数据的持久化存储和查询
"""
import json
import os
import re
import threading
from datetime import datetime
from typing import List, Dict, Optional, Any
from .models import PerformanceSnapshot


class PerformanceStorage:
    """性能数据存储管理器"""
    
    def __init__(self, storage_dir: str = "performance_data"):
        """
        初始化存储管理器
        
        :param storage_dir: 存储目录
        """
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.sessions: Dict[str, Dict] = {}  # {session_id: {snapshots: [], metadata: {}}}
        self.load_sessions()

    def _build_full_log_part_path(self, session_id: str, part_index: int) -> str:
        return os.path.join(self.storage_dir, f"{session_id}_full_log_part_{part_index:03d}.log")

    def _classify_log_level(self, raw_line: str, explicit_level: Any = None) -> str:
        level = str(explicit_level or '').strip().lower()
        if level in ('error', 'err', 'e', 'fatal', 'critical'):
            return 'error'
        if level in ('warning', 'warn', 'w'):
            return 'warning'
        text = str(raw_line or '')
        if re.search(r'(^|\s)(E|F)/', text) or re.search(r'\b(error|fatal|exception|crash)\b', text, re.I):
            return 'error'
        if re.search(r'(^|\s)W/', text) or re.search(r'\bwarn(ing)?\b', text, re.I):
            return 'warning'
        return ''

    def _ensure_full_log_part(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.sessions.get(session_id)
        if not session:
            return None
        metadata = session.setdefault('metadata', {})
        rotate_size_mb = metadata.get('full_log_rotate_size_mb', 20)
        try:
            rotate_size_mb = max(0.1, float(rotate_size_mb))
        except (TypeError, ValueError):
            rotate_size_mb = 20.0
        rotate_size_bytes = max(1, int(rotate_size_mb * 1024 * 1024))
        metadata['full_log_rotate_size_mb'] = rotate_size_mb
        parts = metadata.setdefault('full_log_parts', [])
        active_part = parts[-1] if parts else None
        if (not active_part) or int(active_part.get('size_bytes', 0) or 0) >= rotate_size_bytes:
            part_index = len(parts) + 1
            file_path = self._build_full_log_part_path(session_id, part_index)
            active_part = {
                'part_index': part_index,
                'file_name': os.path.basename(file_path),
                'file_path': file_path,
                'size_bytes': 0,
                'line_count': 0,
                'error_count': 0,
                'warning_count': 0,
                'start_time': datetime.now().isoformat(),
                'end_time': datetime.now().isoformat(),
            }
            parts.append(active_part)
            metadata['full_log_part_count'] = len(parts)
        return active_part
    
    def create_session(self, session_id: str, device_id: str, package_name: str, 
                      description: str = "", monitor_type: str = "video",
                      display_id: int = 1,
                      matched_log_saving_enabled: bool = False,
                      matched_log_keywords: Optional[List[str]] = None,
                      full_log_saving_enabled: bool = False,
                      full_log_rotate_size_mb: float = 20.0) -> bool:
        """
        创建新的监控会话
        
        :param session_id: 会话ID
        :param device_id: 设备ID
        :param package_name: 包名
        :param description: 描述
        :param monitor_type: 监控类型 ("video" 视频播放卡顿 / "ui" UI界面卡顿)
        :return: 是否成功
        """
        with self.lock:
            if session_id in self.sessions:
                return False
            
            matched_log_keywords = [str(x).strip() for x in (matched_log_keywords or []) if str(x).strip()]
            matched_log_file = os.path.join(self.storage_dir, f"{session_id}_matched_logs.log")
            self.sessions[session_id] = {
                'snapshots': [],
                'matched_logs': [],
                'alert_history': [],
                'metadata': {
                    'session_id': session_id,
                    'device_id': device_id,
                    'package_name': package_name,
                    'description': description,
                    'monitor_type': monitor_type,  # "video" 或 "ui"
                    'display_id': display_id,
                    'start_time': datetime.now().isoformat(),
                    'end_time': None,
                    'snapshot_count': 0,
                    'matched_log_saving_enabled': bool(matched_log_saving_enabled),
                    'matched_log_keywords': matched_log_keywords,
                    'matched_log_count': 0,
                    'matched_log_file': matched_log_file if matched_log_saving_enabled and matched_log_keywords else '',
                    'alert_history_count': 0,
                    'full_log_saving_enabled': bool(full_log_saving_enabled),
                    'full_log_rotate_size_mb': float(full_log_rotate_size_mb or 20.0),
                    'full_log_total_lines': 0,
                    'full_log_total_bytes': 0,
                    'full_log_error_count': 0,
                    'full_log_warning_count': 0,
                    'full_log_part_count': 0,
                    'full_log_parts': [],
                }
            }
            self.save_session(session_id)
            return True
    
    def add_snapshot(self, session_id: str, snapshot: PerformanceSnapshot) -> bool:
        """
        添加性能快照
        
        :param session_id: 会话ID
        :param snapshot: 性能快照
        :return: 是否成功
        """
        with self.lock:
            if session_id not in self.sessions:
                return False
            
            # 转换为字典格式
            snapshot_dict = {
                'timestamp': snapshot.timestamp.isoformat(),
                'total_pss': snapshot.total_pss,
                'gc_count': snapshot.gc_count,
                'cpu_usage': snapshot.cpu_usage,
                'fps': snapshot.fps,
                'jank_count': snapshot.jank_count,
                'network_rx_kb': snapshot.network_rx_kb,
                'network_tx_kb': snapshot.network_tx_kb,
                'device_info': snapshot.device_info,
                # 人眼感知卡顿数据
                'perceptual_stall_score': getattr(snapshot, 'perceptual_stall_score', 0),
                'perceptual_stall_events': getattr(snapshot, 'perceptual_stall_events', 0),
                'perceptual_stall_duration_ms': getattr(snapshot, 'perceptual_stall_duration_ms', 0),
                'is_perceptual_stalling': getattr(snapshot, 'is_perceptual_stalling', False),
                'perceptual_stall_severity': getattr(snapshot, 'perceptual_stall_severity', ''),
                'frame_time_variance': getattr(snapshot, 'frame_time_variance', 0),
                'mpp_active_instances': getattr(snapshot, 'mpp_active_instances', 0),
                'mpp_total_work_count': getattr(snapshot, 'mpp_total_work_count', 0),
                'mpp_work_count_delta': getattr(snapshot, 'mpp_work_count_delta', 0),
                'mpp_work_count_delta_time_sec': getattr(snapshot, 'mpp_work_count_delta_time_sec', 0),
                'decoder_stuck': getattr(snapshot, 'decoder_stuck', False),
                'decoder_stuck_duration_sec': getattr(snapshot, 'decoder_stuck_duration_sec', 0),
                'decode_fps_estimate': getattr(snapshot, 'decode_fps_estimate', 0),
                'decode_drop_estimate': getattr(snapshot, 'decode_drop_estimate', 0),
                'decode_drop_ratio': getattr(snapshot, 'decode_drop_ratio', 0),
                'processes': [
                    {
                        'pid': p.pid,
                        'process_name': p.process_name,
                        'cpu_usage': p.cpu_usage,
                        'rss_kb': p.rss_kb,
                        'pss_kb': p.pss_kb,
                        'gc_count': p.gc_count
                    }
                    for p in (snapshot.processes or [])
                ],
                'system_top10': [
                    {
                        'pid': p.pid,
                        'process_name': p.process_name,
                        'cpu_usage': p.cpu_usage,
                        'rss_kb': p.rss_kb,
                        'pss_kb': getattr(p, 'pss_kb', 0),
                        'gc_count': getattr(p, 'gc_count', 0)
                    }
                    for p in (getattr(snapshot, 'system_top_processes', None) or [])
                ]
            }
            
            self.sessions[session_id]['snapshots'].append(snapshot_dict)
            self.sessions[session_id]['metadata']['snapshot_count'] = len(self.sessions[session_id]['snapshots'])
            
            # 定期保存（每100个快照保存一次）
            if len(self.sessions[session_id]['snapshots']) % 100 == 0:
                self.save_session(session_id)
            
            return True
    
    def end_session(self, session_id: str) -> bool:
        """
        结束监控会话
        
        :param session_id: 会话ID
        :return: 是否成功
        """
        with self.lock:
            if session_id not in self.sessions:
                return False
            
            self.sessions[session_id]['metadata']['end_time'] = datetime.now().isoformat()
            self.save_session(session_id)
            return True

    def add_matched_log(self, session_id: str, item: Dict[str, Any]) -> bool:
        with self.lock:
            if session_id not in self.sessions or not isinstance(item, dict):
                return False
            session = self.sessions[session_id]
            matched_logs = session.setdefault('matched_logs', [])
            matched_logs.append(item)
            if len(matched_logs) > 5000:
                del matched_logs[:-5000]
            metadata = session.setdefault('metadata', {})
            metadata['matched_log_count'] = len(matched_logs)
            log_file = metadata.get('matched_log_file')
            if log_file:
                try:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        ts = item.get('timestamp') or datetime.now().isoformat()
                        keywords = item.get('matched_keywords') or []
                        prefix = f"[{ts}]"
                        if keywords:
                            prefix += f"[{','.join(str(x) for x in keywords)}]"
                        f.write(prefix + " " + str(item.get('log_line') or '') + "\n")
                except Exception as e:
                    print(f"写入命中日志文件失败: {e}")
            self.save_session(session_id)
            return True

    def get_matched_logs(self, session_id: str, limit: Optional[int] = None) -> List[Dict]:
        with self.lock:
            if session_id not in self.sessions:
                return []
            matched_logs = self.sessions[session_id].get('matched_logs', [])
            if not isinstance(matched_logs, list):
                return []
            return matched_logs[-limit:] if limit else matched_logs

    def add_alert_history(self, session_id: str, alert_item: Dict[str, Any]) -> bool:
        with self.lock:
            if session_id not in self.sessions or not isinstance(alert_item, dict):
                return False
            session = self.sessions[session_id]
            alert_history = session.setdefault('alert_history', [])
            alert_history.append(alert_item)
            if len(alert_history) > 2000:
                del alert_history[:-2000]
            metadata = session.setdefault('metadata', {})
            metadata['alert_history_count'] = len(alert_history)
            self.save_session(session_id)
            return True

    def add_full_log_line(self, session_id: str, item: Dict[str, Any]) -> bool:
        with self.lock:
            if session_id not in self.sessions or not isinstance(item, dict):
                return False
            session = self.sessions[session_id]
            metadata = session.setdefault('metadata', {})
            if not metadata.get('full_log_saving_enabled'):
                return False
            raw_line = str(item.get('log_line') or '')
            if not raw_line:
                return False
            level_kind = self._classify_log_level(raw_line, item.get('level'))
            part = self._ensure_full_log_part(session_id)
            if not part:
                return False
            line_text = raw_line.rstrip('\n') + '\n'
            encoded = line_text.encode('utf-8', errors='ignore')
            file_path = part.get('file_path') or self._build_full_log_part_path(session_id, int(part.get('part_index', 1) or 1))
            try:
                with open(file_path, 'ab') as f:
                    f.write(encoded)
            except Exception as e:
                print(f"写入全量日志文件失败: {e}")
                return False
            part['file_path'] = file_path
            part['size_bytes'] = int(part.get('size_bytes', 0) or 0) + len(encoded)
            part['line_count'] = int(part.get('line_count', 0) or 0) + 1
            if level_kind == 'error':
                part['error_count'] = int(part.get('error_count', 0) or 0) + 1
                metadata['full_log_error_count'] = int(metadata.get('full_log_error_count', 0) or 0) + 1
            elif level_kind == 'warning':
                part['warning_count'] = int(part.get('warning_count', 0) or 0) + 1
                metadata['full_log_warning_count'] = int(metadata.get('full_log_warning_count', 0) or 0) + 1
            part['end_time'] = item.get('timestamp') or datetime.now().isoformat()
            preview = session.setdefault('full_log_preview', [])
            preview.append({
                'timestamp': item.get('timestamp') or datetime.now().isoformat(),
                'log_line': raw_line,
                'level': level_kind,
            })
            if len(preview) > 200:
                del preview[:-200]
            metadata['full_log_total_lines'] = int(metadata.get('full_log_total_lines', 0) or 0) + 1
            metadata['full_log_total_bytes'] = int(metadata.get('full_log_total_bytes', 0) or 0) + len(encoded)
            metadata['full_log_part_count'] = len(metadata.get('full_log_parts') or [])
            if metadata['full_log_total_lines'] % 50 == 0:
                self.save_session(session_id)
            return True

    def list_full_log_parts(self, session_id: str) -> List[Dict[str, Any]]:
        with self.lock:
            if session_id not in self.sessions:
                return []
            metadata = self.sessions[session_id].get('metadata', {})
            parts = metadata.get('full_log_parts', [])
            return parts if isinstance(parts, list) else []

    def get_full_log_part_path(self, session_id: str, part_index: int) -> Optional[str]:
        with self.lock:
            if session_id not in self.sessions:
                return None
            parts = self.sessions[session_id].get('metadata', {}).get('full_log_parts', [])
            for part in parts if isinstance(parts, list) else []:
                if int(part.get('part_index', 0) or 0) == int(part_index):
                    file_path = part.get('file_path')
                    if file_path and os.path.exists(file_path):
                        return file_path
            return None

    def get_alert_history(self, session_id: str, limit: Optional[int] = None) -> List[Dict]:
        with self.lock:
            if session_id not in self.sessions:
                return []
            alert_history = self.sessions[session_id].get('alert_history', [])
            if not isinstance(alert_history, list):
                return []
            return alert_history[-limit:] if limit else alert_history
    
    def get_session(self, session_id: str) -> Optional[Dict]:
        """
        获取会话数据
        
        :param session_id: 会话ID
        :return: 会话数据
        """
        with self.lock:
            return self.sessions.get(session_id)
    
    def get_snapshots(self, session_id: str, limit: Optional[int] = None, 
                     start_time: Optional[datetime] = None,
                     end_time: Optional[datetime] = None) -> List[Dict]:
        """
        获取性能快照列表
        
        :param session_id: 会话ID
        :param limit: 限制数量
        :param start_time: 开始时间
        :param end_time: 结束时间
        :return: 快照列表
        """
        with self.lock:
            if session_id not in self.sessions:
                return []
            
            snapshots = self.sessions[session_id]['snapshots']
            
            # 时间过滤
            if start_time or end_time:
                filtered = []
                for snap in snapshots:
                    snap_time = datetime.fromisoformat(snap['timestamp'])
                    if start_time and snap_time < start_time:
                        continue
                    if end_time and snap_time > end_time:
                        continue
                    filtered.append(snap)
                snapshots = filtered
            
            # 限制数量
            if limit:
                snapshots = snapshots[-limit:]
            
            return snapshots
    
    def get_statistics(self, session_id: str) -> Dict[str, Any]:
        """
        获取会话统计信息
        
        :param session_id: 会话ID
        :return: 统计信息
        """
        with self.lock:
            if session_id not in self.sessions:
                return {}
            
            snapshots = self.sessions[session_id]['snapshots']
            if not snapshots:
                return {}
            
            # 计算统计值
            cpu_values = [s['cpu_usage'] for s in snapshots if s.get('cpu_usage')]
            pss_values = [s['total_pss'] for s in snapshots if s.get('total_pss')]
            fps_values = [s['fps'] for s in snapshots if s.get('fps')]
            jank_values = [s['jank_count'] for s in snapshots if s.get('jank_count')]
            
            # 人眼感知卡顿统计
            stall_scores = [s.get('perceptual_stall_score', 0) for s in snapshots]
            stall_events_list = [s.get('perceptual_stall_events', 0) for s in snapshots]
            stall_durations = [s.get('perceptual_stall_duration_ms', 0) for s in snapshots]
            stall_severities = [s.get('perceptual_stall_severity', '') for s in snapshots]
            
            # 统计不同严重程度的卡顿次数
            mild_count = sum(1 for s in snapshots if s.get('perceptual_stall_severity') == 'mild')
            moderate_count = sum(1 for s in snapshots if s.get('perceptual_stall_severity') == 'moderate')
            severe_count = sum(1 for s in snapshots if s.get('perceptual_stall_severity') == 'severe')
            
            # 获取最后一个快照的事件数（累计值）
            total_stall_events = stall_events_list[-1] if stall_events_list else 0
            total_stall_duration = stall_durations[-1] if stall_durations else 0
            total_stall_score = stall_scores[-1] if stall_scores else 0
            
            return {
                'snapshot_count': len(snapshots),
                'cpu': {
                    'avg': sum(cpu_values) / len(cpu_values) if cpu_values else 0,
                    'max': max(cpu_values) if cpu_values else 0,
                    'min': min(cpu_values) if cpu_values else 0
                },
                'memory': {
                    'avg_mb': (sum(pss_values) / len(pss_values) / 1024) if pss_values else 0,
                    'max_mb': (max(pss_values) / 1024) if pss_values else 0,
                    'min_mb': (min(pss_values) / 1024) if pss_values else 0
                },
                'fps': {
                    'avg': sum(fps_values) / len(fps_values) if fps_values else 0,
                    'max': max(fps_values) if fps_values else 0,
                    'min': min(fps_values) if fps_values else 0
                },
                'jank': {
                    'total': sum(jank_values) if jank_values else 0,
                    'max': max(jank_values) if jank_values else 0
                },
                'perceptual_stall': {
                    'total_score': total_stall_score,
                    'events': total_stall_events,
                    'total_duration_ms': total_stall_duration,
                    'mild_count': mild_count,
                    'moderate_count': moderate_count,
                    'severe_count': severe_count,
                    'avg_score': sum(stall_scores) / len(stall_scores) if stall_scores else 0,
                    'max_score': max(stall_scores) if stall_scores else 0
                }
            }
    
    def list_sessions(self, package_name: Optional[str] = None,
                     device_id: Optional[str] = None) -> List[Dict]:
        """
        列出所有会话
        
        :param package_name: 包名过滤
        :param device_id: 设备ID过滤
        :return: 会话列表
        """
        with self.lock:
            sessions = []
            for session_id, session_data in self.sessions.items():
                metadata = session_data['metadata']
                
                if package_name and metadata.get('package_name') != package_name:
                    continue
                if device_id and metadata.get('device_id') != device_id:
                    continue
                
                sessions.append({
                    'session_id': session_id,
                    'device_id': metadata.get('device_id'),
                    'package_name': metadata.get('package_name'),
                    'description': metadata.get('description'),
                    'start_time': metadata.get('start_time'),
                    'end_time': metadata.get('end_time'),
                    'snapshot_count': metadata.get('snapshot_count', 0)
                })
            
            # 按开始时间倒序排列
            sessions.sort(key=lambda x: x.get('start_time', ''), reverse=True)
            return sessions
    
    def delete_session(self, session_id: str) -> bool:
        """
        删除会话
        
        :param session_id: 会话ID
        :return: 是否成功
        """
        with self.lock:
            if session_id not in self.sessions:
                return False
            
            del self.sessions[session_id]
            
            # 删除文件
            session_file = os.path.join(self.storage_dir, f"{session_id}.json")
            if os.path.exists(session_file):
                os.remove(session_file)
            matched_log_file = os.path.join(self.storage_dir, f"{session_id}_matched_logs.log")
            if os.path.exists(matched_log_file):
                os.remove(matched_log_file)
            full_log_glob_prefix = f"{session_id}_full_log_part_"
            for filename in os.listdir(self.storage_dir):
                if filename.startswith(full_log_glob_prefix) and filename.endswith('.log'):
                    try:
                        os.remove(os.path.join(self.storage_dir, filename))
                    except Exception:
                        pass
            
            return True
    
    def save_session(self, session_id: str):
        """保存会话到文件"""
        if session_id not in self.sessions:
            return
        
        session_file = os.path.join(self.storage_dir, f"{session_id}.json")
        try:
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(self.sessions[session_id], f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存会话失败: {e}")
    
    def load_sessions(self):
        """从文件加载所有会话"""
        if not os.path.exists(self.storage_dir):
            return
        
        for filename in os.listdir(self.storage_dir):
            if not filename.endswith('.json'):
                continue
            
            session_id = filename[:-5]  # 去掉 .json
            session_file = os.path.join(self.storage_dir, filename)
            
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    self.sessions[session_id] = json.load(f)
                session = self.sessions[session_id]
                if not isinstance(session.get('full_log_preview'), list):
                    session['full_log_preview'] = []
                metadata = session.setdefault('metadata', {})
                if not isinstance(metadata.get('full_log_parts'), list):
                    metadata['full_log_parts'] = []
            except Exception as e:
                print(f"加载会话失败 {session_id}: {e}")
