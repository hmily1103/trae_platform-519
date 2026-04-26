# -*- coding: utf-8 -*-
"""
测试用例与 XMind 互转
"""
import tempfile
from typing import List, Dict, Any

from .models import TestCase


def _title_of(node: Dict) -> str:
    return (node.get('title') or '').strip()


def _note_of(node: Dict) -> str:
    notes = node.get('notes') or {}
    plain = notes.get('plain') or {}
    return (plain.get('content') or '').strip()


def xmind_to_test_cases(xmind_path_or_bytes) -> List[Dict[str, Any]]:
    """
    将 XMind 文件解析为测试用例列表
    约定：每个 sheet 的根主题的子节点 = 测试用例；每个用例的子节点 = 步骤
    步骤格式：标题为 "操作描述" 或 "操作: xxx"，可含子节点表示预期
    """
    try:
        import xmindparser
    except ImportError:
        raise ImportError('请安装 xmindparser: pip install xmindparser')

    if isinstance(xmind_path_or_bytes, bytes):
        with tempfile.NamedTemporaryFile(suffix='.xmind', delete=False) as f:
            f.write(xmind_path_or_bytes)
            path = f.name
        try:
            sheets = xmindparser.xmind_to_dict(path)
        finally:
            import os
            try:
                os.unlink(path)
            except Exception:
                pass
    else:
        sheets = xmindparser.xmind_to_dict(xmind_path_or_bytes)

    cases = []
    for sheet in (sheets or []):
        topic = sheet.get('topic') or {}
        root_children = (topic.get('topics') or [])
        for tc_node in root_children:
            name = _title_of(tc_node) or '未命名用例'
            if not name or name == '未命名用例':
                continue
            desc = _note_of(tc_node)
            step_nodes = tc_node.get('topics') or []
            steps = []
            for i, s in enumerate(step_nodes, 1):
                if not isinstance(s, dict):
                    continue
                action = _title_of(s)
                expected = ''
                subs = s.get('topics') or []
                if subs:
                    expected = _title_of(subs[0]) or ''
                elif ':' in action and '→' not in action:
                    parts = action.split(':', 1)
                    if len(parts) >= 2:
                        action = parts[0].strip()
                        expected = parts[1].strip()
                if action:
                    steps.append({'step_num': i, 'action': action, 'expected': expected})
            cases.append({
                'name': name,
                'description': desc,
                'category': '功能测试',
                'priority': 'medium',
                'tags': [],
                'steps': steps,
                'status': 'active',
            })
    return cases


def test_cases_to_xmind(cases: List[TestCase]) -> bytes:
    """
    将测试用例列表导出为 XMind 文件
    """
    try:
        import xmind
    except ImportError:
        raise ImportError('请安装 XMind: pip install XMind')

    tmp_path = tempfile.mktemp(suffix='.xmind')

    try:
        workbook = xmind.load(tmp_path)
        sheet = workbook.getPrimarySheet()
        sheet.setTitle('测试用例')
        root = sheet.getRootTopic()
        root.setTitle('测试用例库')

        for case in cases:
            tc_topic = root.addSubTopic()
            tc_topic.setTitle(case.name or '未命名')
            if case.description:
                tc_topic.setPlainNotes(case.description)
            for step in (case.steps or []):
                step_topic = tc_topic.addSubTopic()
                action = step.action or ''
                expected = step.expected or ''
                if expected:
                    step_topic.setTitle(f"步骤{step.step_num}: {action} → 预期: {expected}")
                else:
                    step_topic.setTitle(f"步骤{step.step_num}: {action}")

        out_path = tmp_path + '.out.xmind'
        xmind.save(workbook, path=out_path)
        with open(out_path, 'rb') as f:
            data = f.read()
        import os
        try:
            os.unlink(out_path)
        except Exception:
            pass
        return data
    finally:
        import os
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
