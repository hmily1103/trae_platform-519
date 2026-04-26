"""
UI自动化数据模型
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any


@dataclass
class Project:
    """自动化测试项目"""
    id: str
    name: str
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }
    
    @classmethod
    def from_dict(cls, data: Dict):
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            description=data.get('description', ''),
            created_at=data.get('created_at', 0.0),
            updated_at=data.get('updated_at', 0.0)
        )


@dataclass
class TestSuite:
    """测试套件"""
    id: str
    name: str
    project_id: str
    case_ids: List[str] = field(default_factory=list)
    description: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict:
        return {
            'id': self.id,
            'name': self.name,
            'project_id': self.project_id,
            'case_ids': self.case_ids,
            'description': self.description,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }

    @classmethod
    def from_dict(cls, data: Dict):
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            project_id=data.get('project_id', ''),
            case_ids=data.get('case_ids', []),
            description=data.get('description', ''),
            created_at=data.get('created_at', 0.0),
            updated_at=data.get('updated_at', 0.0)
        )


@dataclass
class UISelector:
    """UI选择器（多策略）"""
    strategy: str  # resource_id, text, content_desc, xpath, coordinates
    value: str
    fallbacks: List[Dict] = field(default_factory=list)  # 备用策略
    bounds: Optional[Dict] = None  # 控件边界（用于坐标兜底）
    class_name: str = ""  # 控件类名
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'strategy': self.strategy,
            'value': self.value,
            'fallbacks': self.fallbacks,
            'bounds': self.bounds,
            'class_name': self.class_name
        }
    
    @classmethod
    def from_dict(cls, data: Dict):
        """从字典创建"""
        return cls(
            strategy=data.get('strategy', 'coordinates'),
            value=data.get('value', ''),
            fallbacks=data.get('fallbacks', []),
            bounds=data.get('bounds'),
            class_name=data.get('class_name', '')
        )


@dataclass
class UIAction:
    """UI操作记录"""
    step_num: int
    action_type: str  # click, swipe, input, long_press, wait, assertion
    selector: Optional[UISelector] = None
    value: Optional[str] = None  # 输入值或断言值
    coordinates: Optional[Dict] = None  # 坐标（兜底）
    screenshot: Optional[str] = None  # 操作前截图路径
    ui_tree: Optional[str] = None  # UI树XML路径
    timestamp: float = 0.0
    wait_after: int = 1000  # 操作后等待时间（ms）
    description: str = ""  # 操作描述
    display: str = ""  # 人类可读的步骤显示文本
    status: str = "completed"  # pending, completed, failed
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'step_num': self.step_num,
            'action_type': self.action_type,
            'selector': self.selector.to_dict() if self.selector else None,
            'value': self.value,
            'coordinates': self.coordinates,
            'screenshot': self.screenshot,
            'ui_tree': self.ui_tree,
            'timestamp': self.timestamp,
            'wait_after': self.wait_after,
            'description': self.description,
            'display': self.display,
            'status': self.status
        }
    
    @classmethod
    def from_dict(cls, data: Dict):
        """从字典创建"""
        selector = None
        if data.get('selector'):
            selector = UISelector.from_dict(data['selector'])
        
        return cls(
            step_num=data.get('step_num', 0),
            action_type=data.get('action_type', 'click'),
            selector=selector,
            value=data.get('value'),
            coordinates=data.get('coordinates'),
            screenshot=data.get('screenshot'),
            ui_tree=data.get('ui_tree'),
            timestamp=data.get('timestamp', 0.0),
            wait_after=data.get('wait_after', 1000),
            description=data.get('description', ''),
            display=data.get('display', ''),
            status=data.get('status', 'completed')
        )


@dataclass
class RecordingSession:
    """录制会话/测试用例"""
    id: str
    device_id: str
    package_name: str
    created_at: datetime
    actions: List[UIAction] = field(default_factory=list)
    description: str = ""
    project_id: str = ""  # 所属项目ID
    name: str = ""        # 用例名称
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'id': self.id,
            'device_id': self.device_id,
            'package_name': self.package_name,
            'created_at': self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            'actions': [action.to_dict() for action in self.actions],
            'description': self.description,
            'project_id': self.project_id,
            'name': self.name
        }
    
    @classmethod
    def from_dict(cls, data: Dict):
        """从字典创建"""
        actions = []
        if data.get('actions'):
            actions = [UIAction.from_dict(a) for a in data['actions']]
            
        created_at = data.get('created_at')
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError:
                pass
        
        return cls(
            id=data.get('id', ''),
            device_id=data.get('device_id', ''),
            package_name=data.get('package_name', ''),
            created_at=created_at,
            actions=actions,
            description=data.get('description', ''),
            project_id=data.get('project_id', ''),
            name=data.get('name', '')
        )


@dataclass
class ExecutionTrace:
    """
    单步执行Trace
    对应一次执行中的一个步骤
    """
    run_id: str           # 一次完整执行的ID
    device_id: str        # 设备ID
    step_num: int         # 第几步（对齐锚点）
    
    action_type: str      # 操作类型
    
    selector_strategy: str = ""  # 使用的策略 (resource_id, text, etc.)
    fallback_index: int = -1     # 使用的fallback索引 (-1表示主策略, 0表示第一个fallback)
    
    success: bool = False        # 是否成功
    duration_ms: int = 0         # 耗时(ms)
    
    bounds: Optional[Dict] = None      # 实际操作的元素bounds
    screenshot: Optional[str] = None   # 截图路径
    error: Optional[str] = None        # 错误信息
    
    timestamp: float = 0.0             # 发生时间
    
    def to_dict(self) -> Dict:
        return {
            'run_id': self.run_id,
            'device_id': self.device_id,
            'step_num': self.step_num,
            'action_type': self.action_type,
            'selector_strategy': self.selector_strategy,
            'fallback_index': self.fallback_index,
            'success': self.success,
            'duration_ms': self.duration_ms,
            'bounds': self.bounds,
            'screenshot': self.screenshot,
            'error': self.error,
            'timestamp': self.timestamp
        }
    
    @classmethod
    def from_dict(cls, data: Dict):
        return cls(
            run_id=data.get('run_id', ''),
            device_id=data.get('device_id', ''),
            step_num=data.get('step_num', 0),
            action_type=data.get('action_type', ''),
            selector_strategy=data.get('selector_strategy', ''),
            fallback_index=data.get('fallback_index', -1),
            success=data.get('success', False),
            duration_ms=data.get('duration_ms', 0),
            bounds=data.get('bounds'),
            screenshot=data.get('screenshot'),
            error=data.get('error'),
            timestamp=data.get('timestamp', 0.0)
        )
