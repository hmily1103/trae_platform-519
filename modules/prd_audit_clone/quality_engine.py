# -*- coding: utf-8 -*-
from typing import Any, Dict, List


def _to_list(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _count_defects_by_level(defects: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"P0": 0, "P1": 0, "P2": 0}
    for d in defects or []:
        lv = str((d or {}).get("risk_level") or "").upper()
        if lv in out:
            out[lv] += 1
    return out


def _clamp(v: float) -> float:
    if v < 0:
        return 0.0
    if v > 100:
        return 100.0
    return round(v, 1)


def run_prd_quality_5d(
    stage1_output: Dict[str, Any],
    stage2_output: Dict[str, Any],
    outline_engine: Dict[str, Any],
    dependency_analysis: Dict[str, Any],
    test_matrix: Dict[str, Any],
) -> Dict[str, Any]:
    s1 = stage1_output if isinstance(stage1_output, dict) else {}
    s2 = stage2_output if isinstance(stage2_output, dict) else {}
    outline = outline_engine if isinstance(outline_engine, dict) else {}
    deps = dependency_analysis if isinstance(dependency_analysis, dict) else {}
    tm = test_matrix if isinstance(test_matrix, dict) else {}

    blocks = s1.get("blocks") if isinstance(s1.get("blocks"), list) else []
    flows = _to_list(s1.get("flows"))
    rules = _to_list(s1.get("business_rules"))
    exceptions = _to_list(s1.get("exceptions"))
    outline_nodes = outline.get("nodes") if isinstance(outline.get("nodes"), list) else []
    defects = s2.get("defects") if isinstance(s2.get("defects"), list) else []
    risk_links = deps.get("risk_links") if isinstance(deps.get("risk_links"), list) else []

    dimension = {}
    dimension["结构完整度"] = _clamp(45 + len(blocks) * 2 + min(25, len(outline_nodes) * 0.8))
    dimension["流程清晰度"] = _clamp(40 + len(flows) * 8 + min(20, len(risk_links) * 2))
    dimension["规则完备度"] = _clamp(35 + len(rules) * 7)
    dimension["异常覆盖度"] = _clamp(30 + len(exceptions) * 12)

    matrix_cases = 0
    for k in ["function_matrix", "boundary_matrix", "concurrent_matrix", "permission_matrix"]:
        rows = tm.get(k)
        if isinstance(rows, list):
            matrix_cases += len(rows)
    defect_count = len(defects)
    test_design = 35 + min(45, matrix_cases * 1.5) + (8 if defect_count > 0 else 0)
    dimension["测试可设计性"] = _clamp(test_design)

    risk_count = _count_defects_by_level(defects)
    risk_penalty = risk_count["P0"] * 6 + risk_count["P1"] * 3 + risk_count["P2"] * 1
    overall_raw = sum(dimension.values()) / max(1, len(dimension)) - risk_penalty
    overall = _clamp(overall_raw)

    grade = "A"
    if overall < 85:
        grade = "B"
    if overall < 70:
        grade = "C"
    if overall < 55:
        grade = "D"

    suggestions = []
    if dimension["结构完整度"] < 65:
        suggestions.append("补齐一级模块与子模块结构，确保目录可追踪。")
    if dimension["流程清晰度"] < 65:
        suggestions.append("补充端到端主流程与关键分支流程。")
    if dimension["规则完备度"] < 65:
        suggestions.append("补充优先级、权限、限制类规则，并给出冲突处理口径。")
    if dimension["异常覆盖度"] < 65:
        suggestions.append("补充失败、超时、弱网、回退策略等异常流程。")
    if dimension["测试可设计性"] < 65:
        suggestions.append("增加可验证的验收口径和边界条件，便于测试矩阵落地。")
    if not suggestions:
        suggestions.append("当前 PRD 质量较好，建议重点验证高风险链路。")

    return {
        "overall_score": overall,
        "grade": grade,
        "dimensions": dimension,
        "risk_counts": risk_count,
        "suggestions": suggestions,
    }

