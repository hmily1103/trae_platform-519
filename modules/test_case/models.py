#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试用例管理 - 数据模型
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any


@dataclass
class TestStep:
    """测试步骤"""
    step_num: int
    action: str  # 操作描述
    expected: str  # 预期结果
    actual: Optional[str] = None  # 实际结果（执行时填写）
    status: Optional[str] = None  # 执行状态：passed/failed/skipped
    
    def to_dict(self):
        return {
            'step_num': self.step_num,
            'action': self.action,
            'expected': self.expected,
            'actual': self.actual,
            'status': self.status
        }
    
    @classmethod
    def from_dict(cls, data):
        return cls(
            step_num=data.get('step_num', 0),
            action=data.get('action', ''),
            expected=data.get('expected', ''),
            actual=data.get('actual'),
            status=data.get('status')
        )


@dataclass
class TestCase:
    """测试用例"""
    id: str
    name: str
    description: str = ""
    category: str = "功能测试"  # 功能测试/性能测试/稳定性测试/兼容性测试
    tags: List[str] = field(default_factory=list)
    priority: str = "medium"  # high/medium/low
    status: str = "active"  # active/deprecated/archived
    steps: List[TestStep] = field(default_factory=list)
    preconditions: str = ""
    test_type: str = "manual"  # manual/automated/monkey
    related_package: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: str = "system"
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'category': self.category,
            'tags': self.tags,
            'priority': self.priority,
            'status': self.status,
            'steps': [step.to_dict() for step in self.steps],
            'preconditions': self.preconditions,
            'test_type': self.test_type,
            'related_package': self.related_package,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_by': self.created_by
        }
    
    @classmethod
    def from_dict(cls, data):
        steps = [TestStep.from_dict(s) for s in data.get('steps', [])]
        created_at = datetime.fromisoformat(data['created_at']) if data.get('created_at') else None
        updated_at = datetime.fromisoformat(data['updated_at']) if data.get('updated_at') else None
        
        return cls(
            id=data['id'],
            name=data['name'],
            description=data.get('description', ''),
            category=data.get('category', '功能测试'),
            tags=data.get('tags', []),
            priority=data.get('priority', 'medium'),
            status=data.get('status', 'active'),
            steps=steps,
            preconditions=data.get('preconditions', ''),
            test_type=data.get('test_type', 'manual'),
            related_package=data.get('related_package', ''),
            created_at=created_at,
            updated_at=updated_at,
            created_by=data.get('created_by', 'system')
        )


@dataclass
class PromptConfig:
    """提示词配置"""
    id: str
    name: str
    type: str  # outline_writing/case_writing
    content: str
    tags: List[str] = field(default_factory=list)
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: str = "admin"

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'content': self.content,
            'tags': self.tags,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_by': self.created_by
        }

    @classmethod
    def from_dict(cls, data):
        created_at = datetime.fromisoformat(data['created_at']) if data.get('created_at') else None
        updated_at = datetime.fromisoformat(data['updated_at']) if data.get('updated_at') else None
        
        return cls(
            id=data['id'],
            name=data['name'],
            type=data.get('type', 'case_writing'),
            content=data.get('content', ''),
            tags=data.get('tags', []),
            is_active=data.get('is_active', True),
            created_at=created_at,
            updated_at=updated_at,
            created_by=data.get('created_by', 'admin')
        )


@dataclass
class TestSuite:
    """测试套件"""
    id: str
    name: str
    description: str = ""
    test_case_ids: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'test_case_ids': self.test_case_ids,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    @classmethod
    def from_dict(cls, data):
        created_at = datetime.fromisoformat(data['created_at']) if data.get('created_at') else None
        updated_at = datetime.fromisoformat(data['updated_at']) if data.get('updated_at') else None
        
        return cls(
            id=data['id'],
            name=data['name'],
            description=data.get('description', ''),
            test_case_ids=data.get('test_case_ids', []),
            created_at=created_at,
            updated_at=updated_at
        )


@dataclass
class TestCaseExecution:
    """用例执行记录"""
    id: str
    test_case_id: str
    test_suite_id: Optional[str] = None
    device_id: str = ""
    package_name: str = ""
    status: str = "running"  # running/passed/failed/skipped
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: float = 0.0  # 秒
    executor: str = "system"  # system/manual
    step_results: List[Dict[str, Any]] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    related_monkey_test: Optional[str] = None
    related_performance_session: Optional[str] = None
    related_log_monitor_session: Optional[str] = None
    notes: str = ""
    
    def __post_init__(self):
        if self.start_time is None:
            self.start_time = datetime.now()
    
    def to_dict(self):
        return {
            'id': self.id,
            'test_case_id': self.test_case_id,
            'test_suite_id': self.test_suite_id,
            'device_id': self.device_id,
            'package_name': self.package_name,
            'status': self.status,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'duration': self.duration,
            'executor': self.executor,
            'step_results': self.step_results,
            'screenshots': self.screenshots,
            'logs': self.logs,
            'errors': self.errors,
            'related_monkey_test': self.related_monkey_test,
            'related_performance_session': self.related_performance_session,
            'related_log_monitor_session': self.related_log_monitor_session,
            'notes': self.notes
        }
    
    @classmethod
    def from_dict(cls, data):
        start_time = datetime.fromisoformat(data['start_time']) if data.get('start_time') else None
        end_time = datetime.fromisoformat(data['end_time']) if data.get('end_time') else None
        
        return cls(
            id=data['id'],
            test_case_id=data['test_case_id'],
            test_suite_id=data.get('test_suite_id'),
            device_id=data.get('device_id', ''),
            package_name=data.get('package_name', ''),
            status=data.get('status', 'running'),
            start_time=start_time,
            end_time=end_time,
            duration=data.get('duration', 0.0),
            executor=data.get('executor', 'system'),
            step_results=data.get('step_results', []),
            screenshots=data.get('screenshots', []),
            logs=data.get('logs', []),
            errors=data.get('errors', []),
            related_monkey_test=data.get('related_monkey_test'),
            related_performance_session=data.get('related_performance_session'),
            related_log_monitor_session=data.get('related_log_monitor_session'),
            notes=data.get('notes', '')
        )
