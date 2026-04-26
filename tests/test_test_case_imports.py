"""
测试用例模块导入与关键路径检查。
用于防止遗漏 logging / typing / re 等导入导致运行时 NameError。
"""
import pytest


def test_prd_agents_imports_and_runtime_paths():
    """PRD 多角色评审：导入及使用 logging, re, typing 的路径可执行"""
    from modules.test_case.prd_agents import (
        _load_rules,
        _extract_sections_by_regex,
        extract_report_sections,
        run_prd_multi_agent,
    )
    r = _load_rules()
    assert isinstance(r, dict)
    s = _extract_sections_by_regex("## 一、总体结论\nfoo\n## 二、风险\nbar")
    assert "1" in s and "2" in s
    s2 = extract_report_sections("## 一、总体结论\nx\n## 二、风险\ny")
    assert isinstance(s2, dict)


def test_prd_rule_engine_imports():
    """PRD 规则引擎：导入及 re 使用正常"""
    from modules.test_case.prd_rule_engine import PRDRuleEngine
    from modules.test_case.system_model import SystemModel
    engine = PRDRuleEngine(SystemModel())
    result = engine.analyze()
    assert "issues" in result
    assert "quality_score" in result


def test_system_model_imports():
    """SystemModel 抽取模块可导入"""
    from modules.test_case.system_model import SystemModel, Transition, extract_system_model
    m = SystemModel.from_dict({"states": ["A"], "events": ["e1"], "transitions": []})
    assert m.states == ["A"]


def test_feishu_client_imports():
    """飞书客户端可导入；非 URL 时返回 (False, 原串)"""
    from modules.test_case.feishu_client import is_feishu_doc_url, fetch_feishu_doc_content
    assert not is_feishu_doc_url("not a url")
    ok, text = fetch_feishu_doc_content("hello")
    assert isinstance(ok, bool)
    assert isinstance(text, str)


def test_views_blueprint_registers():
    """test_case 蓝图可注册且含 PRD 相关路由"""
    from flask import Flask
    from modules.test_case.views import test_case_bp
    app = Flask(__name__)
    app.register_blueprint(test_case_bp)
    rules = [r.rule for r in app.url_map.iter_rules() if "test_case" in r.rule]
    assert any("/test_case/knowledge" in r for r in rules)
    assert any("/test_case/api/generate" in r for r in rules)
    assert any("/test_case/api/analyze_prd" in r for r in rules)
