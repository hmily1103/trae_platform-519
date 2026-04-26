import pytest


def test_stage3_report_compliance_guard():
    """防倒退：缺三表则判不合格"""
    from modules.prd_audit import pipeline

    bad = "## 一、总体结论\n- 质量评分：0.0/10\n\n## 二、核心问题\n- xxx\n"
    assert pipeline._is_stage3_report_compliant(bad) is False

    good = "\n".join([
        "# 报告",
        "| 维度 | 评分 | 说明 |",
        "| :--- | :--- | :--- |",
        "| 需求完整度 | 3/10 | 缺功能 |",
        "",
        "| 风险等级 | 核心问题 | 涉及锚点 | 问题描述 | 风险分析 | 审计建议 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        "| P0 | 状态机缺失 | states | 例如：... | ... | ... |",
        "",
        "| 优先级 | 待确认项 | 涉及模块 | 具体问题 | 影响 |",
        "| :--- | :--- | :--- | :--- | :--- |",
        "| P0 | xx | yy | zz | 阻断 |",
    ])
    assert pipeline._is_stage3_report_compliant(good) is True


def test_python_fallback_dimension_scoring_not_all_same():
    """兜底七维：应为逐维评分，而非所有维度同分重复"""
    from modules.prd_audit import pipeline

    stage1 = {
        "goal": "【PRD未说明】",
        "flows": ["【PRD未说明】"],
        "states": ["【PRD未说明】"],
    }
    defects = [
        {"type": "异常流程缺失", "description": "缺少超时/重试", "risk_level": "P0"},
        {"type": "状态回滚缺失", "description": "支付失败未回滚", "risk_level": "P0"},
        {"type": "接口幂等性缺失", "description": "重复提交", "risk_level": "P1"},
    ]
    dims = pipeline._score_dimensions(stage1, defects)
    scores = [v["score"] for v in dims.values()]
    assert len(set(scores)) > 1


def test_rule_library_exception_coverage_insufficient_triggers_rule():
    """exceptions 有内容但覆盖不足时，也应命中“异常流程缺失”类规则"""
    from modules.prd_audit.prd_rule_engine import _match_rule

    rule = {"name": "异常流程缺失"}
    stage1 = {
        "exceptions": ["发生异常时提示错误"],  # 过于泛
        "edge_cases": ["【PRD未说明】"],
        "modules": ["下单"],
        "flows": ["下单->支付"],
        "states": ["待支付", "已支付"],
        "business_rules": [],
        "data_structures": [],
        "permissions": [],
        "dependencies": [],
        "non_functional_requirements": [],
        "goal": "完成支付",
    }
    assert _match_rule(rule, stage1) is True


def test_find_anchor_prefers_source_map():
    """锚点：若 Stage1 给了 source_map，应优先返回对应 Lxx-Lyy，而不是全文关键词扫描。"""
    from modules.prd_audit.prd_rule_engine import _find_anchor

    prd = "\n".join([
        "下单模块：用户创建订单",
        "支付模块：用户发起支付",
        "异常处理：失败提示",
        "更多说明：重复关键词 支付模块 支付模块",
    ])
    stage1 = {
        "modules": ["支付模块", "下单模块"],
        "flows": ["下单->支付"],
        "states": ["待支付", "已支付"],
        "source_map": {
            "modules": ["L0002-L0002", "L0001-L0001"],
            "flows": ["L0001-L0002"],
            "states": ["L0002-L0002", "L0002-L0002"],
            "business_rules": ["【PRD未说明】"],
            "data_structures": ["【PRD未说明】"],
            "permissions": ["【PRD未说明】"],
            "exceptions": ["L0003-L0003"],
            "edge_cases": ["【PRD未说明】"],
            "dependencies": ["【PRD未说明】"],
            "non_functional_requirements": ["【PRD未说明】"],
            "user_roles": ["【PRD未说明】"],
        },
    }
    anchor = _find_anchor(prd, stage1, module="支付模块", defect_type="异常流程缺失", description="缺超时重试")
    assert anchor.startswith("L0002")


def test_stage2_output_contains_coverage_matrix():
    """Stage2 输出应包含 coverage 矩阵，便于解释“exceptions写了但不全”。"""
    from modules.prd_audit.prd_rule_engine import run_stage2_defect_scan

    stage1 = {
        "modules": ["下单"],
        "user_roles": ["用户"],
        "flows": ["下单->支付"],
        "states": ["待支付", "已支付"],
        "business_rules": [],
        "data_structures": [],
        "permissions": [],
        "exceptions": ["发生异常时提示错误"],  # 泛化
        "edge_cases": ["【PRD未说明】"],
        "dependencies": [],
        "non_functional_requirements": [],
        "goal": "完成支付",
    }
    # 不调用 LLM：给一个假的 llm_config_path，但 run_stage2_defect_scan 内会尝试调用 LLM；
    # 因此这里只验证 coverage 构建逻辑：直接调用内部 build 函数更稳。
    from modules.prd_audit.prd_rule_engine import _build_coverage_matrix
    cov = _build_coverage_matrix(stage1)
    assert "exception_coverage" in cov and isinstance(cov["exception_coverage"], list)
    assert "boundary_coverage" in cov and isinstance(cov["boundary_coverage"], list)

