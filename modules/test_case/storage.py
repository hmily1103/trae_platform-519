#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试用例管理 - 数据存储
"""

import os
import json
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)
from typing import List, Dict, Optional, Any
from .models import TestCase, TestSuite, TestCaseExecution, PromptConfig


class TestCaseStorage:
    """测试用例数据存储管理"""
    
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.test_cases_file = os.path.join(base_dir, 'test_cases.json')
        self.test_suites_file = os.path.join(base_dir, 'test_suites.json')
        self.test_executions_file = os.path.join(base_dir, 'test_executions.json')
        self.prompts_file = os.path.join(base_dir, 'prompts.json')
        
        self.lock = threading.Lock()
        
        # 内存索引（加速查询）
        self.test_cases: Dict[str, TestCase] = {}
        self.test_suites: Dict[str, TestSuite] = {}
        self.test_executions: List[TestCaseExecution] = []
        self.prompts: Dict[str, PromptConfig] = {}
        
        # 索引：按分类、标签、状态
        self.index_by_category: Dict[str, List[str]] = {}  # {category: [case_ids]}
        self.index_by_tag: Dict[str, List[str]] = {}  # {tag: [case_ids]}
        self.index_by_status: Dict[str, List[str]] = {}  # {status: [case_ids]}
        
        # 加载数据
        self.load_all()
    
    def load_all(self):
        """加载所有数据"""
        self.load_test_cases()
        self.load_test_suites()
        self.load_test_executions()
        self.load_prompts()
        self._rebuild_indexes()
    
    def load_prompts(self):
        """加载提示词配置"""
        self.prompts = {}
        if os.path.exists(self.prompts_file):
            try:
                with open(self.prompts_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        prompt = PromptConfig.from_dict(item)
                        self.prompts[prompt.id] = prompt
            except Exception as e:
                logger.warning("加载提示词配置失败: %s", e)

    def save_prompts(self):
        """保存提示词配置"""
        try:
            with open(self.prompts_file, 'w', encoding='utf-8') as f:
                data = [prompt.to_dict() for prompt in self.prompts.values()]
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存提示词配置失败: %s", e)

    def load_test_cases(self):
        """加载测试用例"""
        self.test_cases = {}
        if os.path.exists(self.test_cases_file):
            try:
                with open(self.test_cases_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        case = TestCase.from_dict(item)
                        self.test_cases[case.id] = case
            except Exception as e:
                logger.warning("加载测试用例失败: %s", e)
    
    def load_test_suites(self):
        """加载测试套件"""
        self.test_suites = {}
        if os.path.exists(self.test_suites_file):
            try:
                with open(self.test_suites_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        suite = TestSuite.from_dict(item)
                        self.test_suites[suite.id] = suite
            except Exception as e:
                logger.warning("加载测试套件失败: %s", e)
    
    def load_test_executions(self):
        """加载执行记录"""
        self.test_executions = []
        if os.path.exists(self.test_executions_file):
            try:
                with open(self.test_executions_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        execution = TestCaseExecution.from_dict(item)
                        self.test_executions.append(execution)
            except Exception as e:
                logger.warning("加载执行记录失败: %s", e)
    
    def save_test_cases(self):
        """保存测试用例"""
        try:
            with open(self.test_cases_file, 'w', encoding='utf-8') as f:
                data = [case.to_dict() for case in self.test_cases.values()]
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存测试用例失败: %s", e)
    
    def save_test_suites(self):
        """保存测试套件"""
        try:
            with open(self.test_suites_file, 'w', encoding='utf-8') as f:
                data = [suite.to_dict() for suite in self.test_suites.values()]
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存测试套件失败: %s", e)
    
    def save_test_executions(self):
        """保存执行记录"""
        try:
            with open(self.test_executions_file, 'w', encoding='utf-8') as f:
                data = [execution.to_dict() for execution in self.test_executions]
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("保存执行记录失败: %s", e)
    
    def _rebuild_indexes(self):
        """重建索引"""
        self.index_by_category = {}
        self.index_by_tag = {}
        self.index_by_status = {}
        
        for case_id, case in self.test_cases.items():
            # 按分类索引
            category = case.category
            if category not in self.index_by_category:
                self.index_by_category[category] = []
            self.index_by_category[category].append(case_id)
            
            # 按标签索引
            for tag in case.tags:
                if tag not in self.index_by_tag:
                    self.index_by_tag[tag] = []
                self.index_by_tag[tag].append(case_id)
            
            # 按状态索引
            status = case.status
            if status not in self.index_by_status:
                self.index_by_status[status] = []
            self.index_by_status[status].append(case_id)
    
    # ========== 测试用例操作 ==========
    
    def add_test_case(self, test_case: TestCase) -> bool:
        """添加测试用例"""
        with self.lock:
            if test_case.id in self.test_cases:
                return False
            test_case.updated_at = datetime.now()
            self.test_cases[test_case.id] = test_case
            self._rebuild_indexes()
            self.save_test_cases()
            return True
    
    def update_test_case(self, test_case: TestCase) -> bool:
        """更新测试用例"""
        with self.lock:
            if test_case.id not in self.test_cases:
                return False
            test_case.updated_at = datetime.now()
            self.test_cases[test_case.id] = test_case
            self._rebuild_indexes()
            self.save_test_cases()
            return True
    
    def get_test_case(self, case_id: str) -> Optional[TestCase]:
        """获取测试用例"""
        return self.test_cases.get(case_id)
    
    def delete_test_case(self, case_id: str) -> bool:
        """删除测试用例"""
        with self.lock:
            if case_id not in self.test_cases:
                return False
            del self.test_cases[case_id]
            self._rebuild_indexes()
            self.save_test_cases()
            return True
    
    def list_test_cases(self, 
                       category: Optional[str] = None,
                       tag: Optional[str] = None,
                       status: Optional[str] = None,
                       search: Optional[str] = None) -> List[TestCase]:
        """列出测试用例（支持筛选）"""
        cases = list(self.test_cases.values())
        
        # 筛选
        if category:
            cases = [c for c in cases if c.category == category]
        if tag:
            cases = [c for c in cases if tag in c.tags]
        if status:
            cases = [c for c in cases if c.status == status]
        if search:
            search_lower = search.lower()
            cases = [c for c in cases if 
                    search_lower in c.name.lower() or 
                    search_lower in c.description.lower() or
                    search_lower in c.id.lower()]
        
        return cases
    
    # ========== 测试套件操作 ==========
    
    def add_test_suite(self, test_suite: TestSuite) -> bool:
        """添加测试套件"""
        with self.lock:
            if test_suite.id in self.test_suites:
                return False
            test_suite.updated_at = datetime.now()
            self.test_suites[test_suite.id] = test_suite
            self.save_test_suites()
            return True
    
    def update_test_suite(self, test_suite: TestSuite) -> bool:
        """更新测试套件"""
        with self.lock:
            if test_suite.id not in self.test_suites:
                return False
            test_suite.updated_at = datetime.now()
            self.test_suites[test_suite.id] = test_suite
            self.save_test_suites()
            return True
    
    def get_test_suite(self, suite_id: str) -> Optional[TestSuite]:
        """获取测试套件"""
        return self.test_suites.get(suite_id)
    
    def delete_test_suite(self, suite_id: str) -> bool:
        """删除测试套件"""
        with self.lock:
            if suite_id not in self.test_suites:
                return False
            del self.test_suites[suite_id]
            self.save_test_suites()
            return True
    
    def list_test_suites(self) -> List[TestSuite]:
        """列出所有测试套件"""
        return list(self.test_suites.values())
    
    # ========== 执行记录操作 ==========
    
    def add_execution(self, execution: TestCaseExecution) -> bool:
        """添加执行记录"""
        with self.lock:
            self.test_executions.append(execution)
            # 只保留最近10000条记录
            if len(self.test_executions) > 10000:
                self.test_executions = self.test_executions[-10000:]
            self.save_test_executions()
            return True
    
    def update_execution(self, execution: TestCaseExecution) -> bool:
        """更新执行记录"""
        with self.lock:
            for i, e in enumerate(self.test_executions):
                if e.id == execution.id:
                    self.test_executions[i] = execution
                    self.save_test_executions()
                    return True
            return False
    
    def get_execution(self, exec_id: str) -> Optional[TestCaseExecution]:
        """获取执行记录"""
        for execution in self.test_executions:
            if execution.id == exec_id:
                return execution
        return None
    
    def list_executions(self,
                       test_case_id: Optional[str] = None,
                       test_suite_id: Optional[str] = None,
                       device_id: Optional[str] = None,
                       status: Optional[str] = None,
                       start_date: Optional[datetime] = None,
                       end_date: Optional[datetime] = None) -> List[TestCaseExecution]:
        """列出执行记录（支持筛选）"""
        executions = list(self.test_executions)
        
        # 筛选
        if test_case_id:
            executions = [e for e in executions if e.test_case_id == test_case_id]
        if test_suite_id:
            executions = [e for e in executions if e.test_suite_id == test_suite_id]
        if device_id:
            executions = [e for e in executions if e.device_id == device_id]
        if status:
            executions = [e for e in executions if e.status == status]
        if start_date:
            executions = [e for e in executions if e.start_time and e.start_time >= start_date]
        if end_date:
            executions = [e for e in executions if e.start_time and e.start_time <= end_date]
        
        # 按时间倒序
        executions.sort(key=lambda x: x.start_time or datetime.min, reverse=True)
        
        return executions
    
    def get_execution_statistics(self, 
                                test_case_id: Optional[str] = None,
                                test_suite_id: Optional[str] = None) -> Dict[str, Any]:
        """获取执行统计"""
        executions = self.list_executions(test_case_id=test_case_id, test_suite_id=test_suite_id)
        
        total = len(executions)
        passed = len([e for e in executions if e.status == 'passed'])
        failed = len([e for e in executions if e.status == 'failed'])
        skipped = len([e for e in executions if e.status == 'skipped'])
        running = len([e for e in executions if e.status == 'running'])
        
        pass_rate = (passed / total * 100) if total > 0 else 0
        
        return {
            'total': total,
            'passed': passed,
            'failed': failed,
            'skipped': skipped,
            'running': running,
            'pass_rate': round(pass_rate, 2)
        }
    
    def generate_case_id(self) -> str:
        """生成用例ID"""
        import time
        timestamp = int(time.time() * 1000)
        return f"TC_{timestamp}"
    
    def generate_suite_id(self) -> str:
        """生成套件ID"""
        import time
        timestamp = int(time.time() * 1000)
        return f"SUITE_{timestamp}"
    
    def generate_execution_id(self) -> str:
        """生成执行记录ID"""
        import time
        timestamp = int(time.time() * 1000)
        return f"EXEC_{timestamp}"
