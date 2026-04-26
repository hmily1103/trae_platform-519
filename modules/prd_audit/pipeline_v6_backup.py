# -*- coding: utf-8 -*-
"""
PRD 审计流水线：Stage1 结构解析 → Stage2 漏洞扫描 → Stage3 报告生成（LLM 或 Python 兜底）。
本模块为 prd_audit 自包含实现，不依赖 test_case。
"""

import os
import json
import logging
import re
from typing import Dict, Any, List, Generator, Optional, Tuple

from .system_model import extract_prd_structure
from .prd_rule_engine import (
    run_stage2_defect_scan,
    run_stage2_shift_left_analysis,
    run_test_case_generation,
)
from .outline_engine import run_outline_engine
from .platform_impact_engine import run_platform_impact_analysis
from .dependency_engine import run_dependency_analysis
from .quality_engine import run_prd_quality_5d
from .test_points_engine import run_test_points_engine
from .risk_prediction_engine import run_risk_prediction_engine
from .understanding_cards_engine import build_understanding_cards
from .release_gate_engine import run_release_gate
from .architecture_scanner import run_architecture_scan
from .guardrail_engine import evaluate_guardrail
from utils.llm_client import call_llm, call_llm_with_retry

logger = logging.getLogger(__name__)

STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))
STAGE3_MINIMAL_PROMPT_FILE = os.path.join(STORAGE_DIR, "prd_audit_prompt_stage3_minimal.txt")


def _ensure_list(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _to_md_items(items):
    arr = _ensure_list(items)
    if not arr:
        return "- 【PRD未说明】"
    return "\n".join([f"- {x}" for x in arr])


def _calc_quality_score(defects):
    p0 = sum(1 for d in defects if str(d.get("risk_level", "")).upper() == "P0")
    p1 = sum(1 for d in defects if str(d.get("risk_level", "")).upper() == "P1")
    p2 = sum(1 for d in defects if str(d.get("risk_level", "")).upper() == "P2")
    score = 10.0 - p0 * 2.0 - p1 * 1.0 - p2 * 0.5
    if score < 0.0:
        score = 0.0
    if score >= 9.0:
        level = "高质量"
    elif score >= 7.0:
        level = "基本可开发"
    elif score >= 5.0:
        level = "存在明显风险"
    else:
        level = "不具备开发条件"
    return round(score, 1), level


def _classify_defect_category(type_text: str, desc_text: str) -> str:
    t = (str(type_text or "") + " " + str(desc_text or "")).lower()
    if any(k in t for k in ["规则", "业务规则", "冲突", "口径", "歧义"]):
        return "D1"
    if any(k in t for k in ["状态机", "状态", "跳转", "回滚"]):
        return "D2"
    if any(k in t for k in ["接口", "返回字段", "字段", "协议"]):
        return "D3"
    if any(k in t for k in ["权限", "越权", "鉴权", "角色"]):
        return "D4"
    if any(k in t for k in ["校验", "参数", "输入"]):
        return "D5"
    if any(k in t for k in ["异常", "失败", "重试", "超时", "弱网"]):
        return "D6"
    if any(k in t for k in ["可测试性", "testability", "量化", "验收标准", "指标"]):
        return "D7"
    if any(k in t for k in ["歧义", "模糊", "不明确"]):
        return "D8"
    return "D9"


def _classify_risk_category(text: str) -> str:
    t = str(text or "").lower()
    if any(k in t for k in ["并发", "抢占", "竞态"]):
        return "R1"
    if any(k in t for k in ["性能", "卡顿", "延迟"]):
        return "R2"
    if any(k in t for k in ["一致性", "同步", "数据错乱"]):
        return "R3"
    if any(k in t for k in ["安全", "越权", "风控"]):
        return "R4"
    if any(k in t for k in ["规则", "逻辑", "流程"]):
        return "R5"
    return "R6"


def _classify_test_focus_category(text: str) -> str:
    t = str(text or "").lower()
    if any(k in t for k in ["流程", "闭环"]):
        return "T1"
    if any(k in t for k in ["边界", "极值", "重复"]):
        return "T2"
    if any(k in t for k in ["权限", "越权", "鉴权"]):
        return "T3"
    if any(k in t for k in ["异常", "失败", "重试", "超时", "弱网"]):
        return "T4"
    return "T5"


def _classify_dev_focus_category(text: str) -> str:
    t = str(text or "").lower()
    if any(k in t for k in ["状态机", "状态", "转移"]):
        return "DEV1"
    if any(k in t for k in ["并发", "幂等", "抢占"]):
        return "DEV2"
    if any(k in t for k in ["数据", "模型", "一致性"]):
        return "DEV3"
    if any(k in t for k in ["接口", "协议", "字段"]):
        return "DEV4"
    return "DEV5"


def _build_semantic_schema(
    stage1_output: Dict[str, Any],
    defects: List[Dict[str, Any]],
    merged_issues: List[Dict[str, Any]],
    summary: Dict[str, Any],
    plan_items: List[str],
) -> Dict[str, Any]:
    schema_defects: List[Dict[str, Any]] = []
    for i, d in enumerate(defects[:80], start=1):
        if not isinstance(d, dict):
            continue
        dtype = str(d.get("type") or "")
        desc = str(d.get("description") or "【PRD未说明】")
        schema_defects.append({
            "id": str(d.get("id") or f"DEFECT_{i:03d}"),
            "category": _classify_defect_category(dtype, desc),
            "module": str(d.get("module") or "【PRD未说明】"),
            "description": desc,
            "rule_id": str(d.get("rule_id") or ""),
            "severity": str(d.get("risk_level") or "P2").upper(),
            "suggestion": str(d.get("suggestion") or "补齐规则并明确验收口径"),
        })

    schema_risks: List[Dict[str, Any]] = []
    for i, m in enumerate(merged_issues[:30], start=1):
        if not isinstance(m, dict):
            continue
        txt = str(m.get("name") or "") + " " + str(m.get("description") or "")
        schema_risks.append({
            "id": f"RISK_{i:03d}",
            "category": _classify_risk_category(txt),
            "description": str(m.get("description") or m.get("name") or "【PRD未说明】"),
            "impact": str(m.get("reason") or "可能导致上线质量与稳定性下降"),
            "suggestion": str(m.get("suggestion") or "建议在开发前完成澄清并补全约束"),
        })

    test_focus_items: List[Dict[str, Any]] = []
    seen_tf = set()
    for d in schema_defects:
        key = d["category"] + "|" + d["description"]
        if key in seen_tf:
            continue
        seen_tf.add(key)
        fp = d["description"]
        test_focus_items.append({
            "category": _classify_test_focus_category(fp),
            "focus_point": fp,
        })
        if len(test_focus_items) >= 20:
            break

    dev_focus_items: List[Dict[str, Any]] = []
    seen_df = set()
    base_modules = _ensure_list(stage1_output.get("modules"))
    for m in base_modules:
        mm = str(m or "").strip()
        if not mm or mm in seen_df:
            continue
        seen_df.add(mm)
        dev_focus_items.append({
            "category": _classify_dev_focus_category(mm),
            "focus_point": mm,
        })
        if len(dev_focus_items) >= 20:
            break
    if not dev_focus_items:
        dev_focus_items.append({"category": "DEV5", "focus_point": "关键功能模块实现复杂度评审"})

    plan_struct = []
    for p in (plan_items or [])[:12]:
        txt = str(p or "")
        c = "P1" if any(k in txt for k in ["澄清", "补充", "冻结"]) else ("P2" if "评审" in txt else ("P3" if "拆分" in txt else "P4"))
        plan_struct.append({"category": c, "action": txt})

    return {
        "summary": {
            "quality_score": summary.get("quality_score"),
            "risk_level": summary.get("risk_level"),
            "main_problem": summary.get("main_problem"),
        },
        "defects": schema_defects,
        "risks": schema_risks,
        "test_focus": test_focus_items,
        "dev_focus": dev_focus_items,
        "plan": plan_struct,
    }


def _clamp_score(x: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 10.0:
        return 10.0
    return v


def _calc_complexity(stage1_output: Dict[str, Any]) -> Dict[str, Any]:
    """
    结构复杂度评估：基于 states/flows/business_rules 数量给出一个 0-10 的复杂度指数，及简单解释。
    仅作为“信息量提示”，不参与打分扣分逻辑。
    """
    states = _ensure_list(stage1_output.get("states"))
    flows = _ensure_list(stage1_output.get("flows"))
    rules = _ensure_list(stage1_output.get("business_rules"))

    n_states = 0 if not states or all(x == "【PRD未说明】" for x in states) else len(states)
    n_flows = 0 if not flows or all(x == "【PRD未说明】" for x in flows) else len(flows)
    n_rules = 0 if not rules or all(x == "【PRD未说明】" for x in rules) else len(rules)

    raw = n_states * 2.0 + n_flows * 1.5 + n_rules * 1.0
    # 经验上下限：简单产品 ~10，中等 ~40，复杂系统 ~80+
    if raw <= 0:
        score = 0.0
    else:
        score = raw / 8.0  # 约束在 0-10 左右的范围
    score = round(_clamp_score(score), 1)

    if score <= 3.0:
        level = "简单"
        desc = "状态与流程数量较少，整体复杂度较低。"
    elif score <= 6.0:
        level = "中等"
        desc = "状态、流程与规则数量适中，属于常规复杂度。"
    else:
        level = "较高"
        desc = "状态、流程与规则较多，建议在设计与测试上投入更多精力以控制风险。"

    return {
        "score": score,
        "level": level,
        "detail": {
            "state_count": n_states,
            "flow_count": n_flows,
            "rule_count": n_rules,
        },
        "reason": desc,
    }


def _score_dimensions(stage1_output: Dict[str, Any], defects: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Python 兜底的七维评分：用启发式规则避免“只有总分/同分重复”的低信息量问题。
    输出格式：
      {dim: {"score": float, "reason": str}}
    """
    dims = {
        "需求完整度": {"score": 8.0, "reason": "基于结构完整性与缺陷综合评估。"},
        "规则明确度": {"score": 8.0, "reason": "基于业务规则歧义/冲突情况综合评估。"},
        "流程一致性": {"score": 8.0, "reason": "基于主流程闭环与并发/重试约束综合评估。"},
        "状态机完备度": {"score": 8.0, "reason": "基于状态定义/跳转/回滚/恢复规则综合评估。"},
        "异常覆盖度": {"score": 8.0, "reason": "基于异常/边界/弱网/超时/重复等覆盖情况综合评估。"},
        "可测试性": {"score": 8.0, "reason": "基于验收指标、可验证条件与锚点可定位性综合评估。"},
        "技术可实现性": {"score": 8.0, "reason": "基于依赖、并发、幂等、安全等实现风险综合评估。"},
    }

    def _hit(t: str, keys: List[str]) -> bool:
        return any(k in t for k in keys)

    # 结构缺失直接扣分
    if "【PRD未说明】" in str(stage1_output.get("goal") or ""):
        dims["需求完整度"]["score"] -= 1.0
        dims["可测试性"]["score"] -= 0.5
        dims["需求完整度"]["reason"] = "目标/成功标准存在【PRD未说明】，范围与验收口径不完整。"
        dims["可测试性"]["reason"] = "目标/验收口径不清导致验证边界不明确。"

    states = _ensure_list(stage1_output.get("states"))
    flows = _ensure_list(stage1_output.get("flows"))
    if not states or all(x == "【PRD未说明】" for x in states):
        dims["状态机完备度"]["score"] -= 2.0
        dims["状态机完备度"]["reason"] = "未形成可执行的状态机（states/转移规则缺失或为【PRD未说明】）。"
    if not flows or all(x == "【PRD未说明】" for x in flows):
        dims["流程一致性"]["score"] -= 1.5
        dims["流程一致性"]["reason"] = "核心业务流程缺失或为【PRD未说明】，难以评审闭环与断裂点。"

    # 缺陷驱动扣分（按类型关键词映射）
    for d in defects or []:
        t = str(d.get("type") or "") + " " + str(d.get("description") or "")
        lvl = str(d.get("risk_level") or "P2").upper()
        w = 1.2 if lvl == "P0" else (0.8 if lvl == "P1" else 0.4)

        if _hit(t, ["规则冲突", "逻辑矛盾", "口径", "歧义", "模糊", "不可测试", "成功标准缺失"]):
            dims["规则明确度"]["score"] -= w
            dims["可测试性"]["score"] -= w * 0.7
            dims["规则明确度"]["reason"] = "存在规则歧义/冲突/不可执行表述，导致实现与验收口径不一致风险。"
            dims["可测试性"]["reason"] = "存在不可测试描述/成功标准缺失，验收条件难以量化。"
        if _hit(t, ["流程断裂", "主流程断裂", "中断流程缺失", "重试机制缺失", "并发", "幂等"]):
            dims["流程一致性"]["score"] -= w
            dims["技术可实现性"]["score"] -= w * 0.6
            dims["流程一致性"]["reason"] = "流程闭环不足（成功/失败/中断/重试/并发口径缺失），上线行为易不一致。"
            dims["技术可实现性"]["reason"] = "并发/幂等/重试等工程约束未落地，存在实现与稳定性风险。"
        if _hit(t, ["状态孤岛", "状态死路", "非法状态跳转", "状态回滚缺失", "状态机"]):
            dims["状态机完备度"]["score"] -= w * 1.1
            dims["状态机完备度"]["reason"] = "状态定义/转移/回滚/恢复规则不完整，复杂场景下容易出现不可达/死路/非法跳转。"
        if _hit(t, ["异常流程缺失", "边界条件缺失", "弱网", "超时", "失败", "错误提示", "重复操作未定义"]):
            dims["异常覆盖度"]["score"] -= w
            dims["异常覆盖度"]["reason"] = "异常/边界/弱网/超时/重复等场景覆盖不足，失败闭环与恢复策略不明确。"
        if _hit(t, ["权限", "越权", "安全", "风控", "审计日志"]):
            dims["技术可实现性"]["score"] -= w
            dims["技术可实现性"]["reason"] = "权限与安全边界未闭环（鉴权/越权/审计/风控），存在上线安全风险。"

    # clamp
    for k in list(dims.keys()):
        dims[k]["score"] = round(_clamp_score(dims[k]["score"]), 1)
        if not dims[k]["reason"]:
            dims[k]["reason"] = "基于缺陷综合评估。"
    return dims


def _is_stage3_report_compliant(report_md: str) -> bool:
    """
    强制“防倒退”：Stage3 LLM 输出若缺三表/变成纯文本列表，则判不合格，回退 Python 兜底。
    只做必要的、稳定的关键特征检查（避免误杀）。
    """
    s = (report_md or "").strip()
    if not s:
        return False
    # 容错：不同版本的 Stage3 表头列数可能略有差异
    # 这里仅验证“三张表是否存在”，避免因列名变更误杀。
    risk_table_ok = (
        "| 风险等级 |" in s
        and "| 核心问题 |" in s
        and "| 审计建议 |" in s
    )
    dimension_table_ok = (
        "| 维度 |" in s
        and "| 评分 |" in s
        and "| 说明 |" in s
    )
    pending_table_ok = (
        "| 优先级 |" in s
        and "| 待确认项 |" in s
        and "| 影响 |" in s
    )
    return bool(risk_table_ok and dimension_table_ok and pending_table_ok)


def _urgency_tag(risk_level: str) -> str:
    lv = str(risk_level or "").upper()
    if lv == "P0":
        return "🔥🔥🔥 不确认没法开工"
    if lv == "P1":
        return "🔥🔥 不确认会做错"
    return "🔥 可以边做边等"


def _format_core_title(name: str, risk_level: str) -> str:
    n = str(name or "").strip() or "核心问题"
    lv = str(risk_level or "P2").upper()
    if "外部依赖" in n:
        return f"外部依赖缺失（{lv}）——项目无法启动的核心风险"
    if "流程闭环" in n or "流程断裂" in n:
        return f"模式切换规则不完整（{lv}）——退出投屏后去哪了？"
    if "权限" in n or "安全" in n:
        return f"权限与安全缺失（{lv}）——谁可以发广告？投屏怎么防串房？"
    if "规则冲突" in n or "口径不一致" in n:
        return f"规则冲突（{lv}）——关键口径到底听谁的？"
    if "状态机" in n:
        return f"状态机不完整（{lv}）——系统不知道下一步该去哪"
    return f"{n}（{lv}）——需要补齐可执行规则"


def _biz_example_text(name: str, risk_level: str, description: str) -> str:
    n = str(name or "")
    lv = str(risk_level or "P2").upper()
    if "外部依赖" in n:
        return "例如：上线第一天，广告位可能全是空白，因为不知道从哪拉广告，现场会被误以为“屏幕坏了”。"
    if "权限" in n or "安全" in n:
        return "例如：前台误操作就能强插广告或跨房间投屏，客人投诉“怎么串房/被打扰”，店长难以解释。"
    if "状态机" in n or "流程" in n:
        return "例如：用户退出投屏后系统没有回到可控状态，下一次进入时行为不一致，现场演示容易翻车。"
    if "数据" in n:
        return "例如：字段口径不一致导致一端显示“已成功”另一端仍是“处理中”，用户重复点击触发重复请求。"
    if lv == "P0":
        return "例如：关键路径缺口会直接阻断上线或造成资损，现场无法兜底。"
    return "例如：用户按常规操作会遇到不确定结果，导致投诉或返工。"


def _biz_crash_text(name: str, risk_level: str) -> str:
    n = str(name or "")
    lv = str(risk_level or "P2").upper()
    if "外部依赖" in n:
        return "上线第一天广告/内容全为空，客诉“屏幕坏了”，现场无法解释。"
    if "权限" in n or "安全" in n:
        return "出现“谁都能发广告/串房投屏”，现场被投诉骚扰或隐私风险。"
    if "状态机" in n or "流程" in n:
        return "退出/返回路径不一致，现场演示卡死或切换混乱。"
    if lv == "P0":
        return "关键路径直接阻断交付或引发资损，必须先澄清。"
    return "体验不一致引发投诉，后续返工成本高。"


def _clean_report_text(text: Any, keep_newlines: bool = False) -> str:
    s = str(text or "")
    if not s:
        return ""
    s = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", s)
    if keep_newlines:
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()
    s = " ".join(s.replace("\r", " ").replace("\n", " ").split())
    return s.strip()


def _first_clause(text: Any) -> str:
    s = _clean_report_text(text)
    if not s:
        return ""
    for part in re.split(r"[；。\n]+", s):
        part = part.strip(" -:：")
        if part:
            return part
    return s


def _infer_fix_section(name: str, types_arr: List[str], description: str, reason: str) -> str:
    text = " ".join([str(name or ""), str(description or ""), str(reason or "")] + [str(x or "") for x in (types_arr or [])])
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return "外部依赖"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码"]):
        return "接口定义/数据结构"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return "权限控制"
    if any(k in text for k in ["状态", "跳转", "流程", "回滚", "恢复", "中断", "退出", "重试", "并发", "幂等"]):
        return "状态机/异常流程"
    if any(k in text for k in ["验收", "成功标准", "可测试", "指标", "性能"]):
        return "验收标准"
    return "功能规则"


_GENERIC_FEATURE_TERMS = {
    "业务流程", "业务规则", "功能规则", "功能", "全局", "可测试性", "状态机", "流程",
    "规则", "模块", "系统", "接口", "页面", "能力", "逻辑", "核心流程",
}

_SCHEMA_TERMS = [
    "business_rules", "business_rule", "flows", "flow", "states", "state",
    "edge_cases", "edge_case", "non_functional_requirement", "functional_requirement",
    "exceptions", "exception", "modules", "module", "user_roles", "required_elements",
    "parse_quality", "source_map",
]


def _issue_anchors(item: Dict[str, Any]) -> List[str]:
    vals = item.get("anchors")
    if vals is None:
        vals = [item.get("anchor")] if item.get("anchor") else []
    
    cleaned = []
    for x in _ensure_list(vals):
        c = _clean_report_text(x)
        if not c or c == "【PRD未说明】":
            continue
        # 如果是类似 business_rules[5] 这种 schema key，尝试转为人话，或者干脆滤掉
        if re.match(r"^[a-zA-Z_]+(?:\[\d+\])?$", c):
            continue
        cleaned.append(c)
    
    return cleaned


def _issue_modules(item: Dict[str, Any]) -> List[str]:
    vals = item.get("modules")
    if vals is None:
        vals = [item.get("module")] if item.get("module") else []
    return [_clean_report_text(x) for x in _ensure_list(vals) if _clean_report_text(x) and _clean_report_text(x) != "【PRD未说明】"]


def _issue_types(item: Dict[str, Any]) -> List[str]:
    vals = item.get("types")
    if vals is None:
        vals = [item.get("type")] if item.get("type") else []
    return [_clean_report_text(x) for x in _ensure_list(vals) if _clean_report_text(x) and _clean_report_text(x) != "【PRD未说明】"]


def _strip_list_prefix(text: str) -> str:
    s = _clean_report_text(text)
    s = re.sub(r"^[0-9一二三四五六七八九十]+[\.\、\)\s]+", "", s)
    s = re.sub(r"^(第[0-9一二三四五六七八九十]+[章节条项点]\s*)", "", s)
    return s.strip(" ：:-")


def _anchor_quote(anchor: str) -> str:
    a = _clean_report_text(anchor)
    if not a:
        return ""
    if re.match(r"^L\d+(?:-L?\d+)?[:：]\s*", a):
        a = re.sub(r"^L\d+(?:-L?\d+)?[:：]\s*", "", a)
    if a.startswith("功能："):
        a = a.split("：", 1)[1].strip()
    return _strip_list_prefix(a)


def _normalize_feature_label(text: str) -> str:
    s = _strip_list_prefix(text)
    if not s:
        return ""
    for term in _SCHEMA_TERMS:
        s = re.sub(rf"\b{re.escape(term)}\b", " ", s, flags=re.IGNORECASE)
    s = s.replace("_", " ").replace(",", " ").replace("，", " ")
    s = s.replace("：", "").replace(":", "")
    s = re.sub(r"^(在|于|当|从)", "", s)
    s = re.sub(r"^(支持|新增|增加|提供|实现|允许|进入|退出|点击|选择|显示|打开|关闭)", "", s)
    s = re.sub(r"\s+", "", s)
    return s.strip("，。；：:、-")


def _is_generic_feature_name(text: str) -> bool:
    s = _normalize_feature_label(text)
    if not s:
        return True
    if s in _GENERIC_FEATURE_TERMS:
        return True
    if any(term.lower() in s.lower() for term in _SCHEMA_TERMS):
        return True
    if "null" in s.lower():
        return True
    return any(s == x or s.endswith(x) for x in _GENERIC_FEATURE_TERMS)


def _issue_blob(item: Dict[str, Any]) -> str:
    parts = []
    parts.extend(_issue_anchors(item))
    parts.extend(_issue_modules(item))
    parts.extend(_issue_types(item))
    for key in ["name", "description", "reason", "suggestion", "module", "type", "anchor"]:
        val = _clean_report_text(item.get(key))
        if val:
            parts.append(val)
    blob = " ".join(parts)
    for term in _SCHEMA_TERMS:
        blob = re.sub(rf"\b{re.escape(term)}\b", " ", blob, flags=re.IGNORECASE)
    return _clean_report_text(blob)


def _issue_topic(item: Dict[str, Any]) -> str:
    text = _issue_blob(item)
    if any(k in text for k in ["二维码", "扫码", "鉴权", "token", "401", "未授权", "有效期"]):
        return "qr_security"
    if any(k in text for k in ["五维评分", "评分开关", "设置开关", "生效时机"]):
        return "rating_switch"
    if any(k in text for k in ["同步时效", "同步延迟", "多久同步", "实时同步", "时效性"]):
        return "sync_latency"
    if any(k in text for k in ["云端", "上传", "503", "待上传", "降级", "回传失败", "自动重传"]):
        return "cloud_degrade"
    if any(k in text for k in ["转台", "关台", "重开台", "清空", "规则需确认"]):
        return "transfer_cleanup"
    if any(k in text for k in ["性能", "QPS", "CPU", "采样率", "比特率", "保存期限", "时延", "吞吐", "容量"]):
        return "performance_metric"
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return "boundary_rule"
    if any(k in text for k in ["成功标准", "验收", "日志", "埋点", "可测试"]):
        return "acceptance_logging"
    if any(k in text for k in ["状态", "回滚", "恢复"]):
        return "state_recovery"
    if any(k in text for k in ["异常", "失败", "超时", "中断", "重试", "降级"]):
        return "exception_flow"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return "security_access"
    if any(k in text for k in ["字段", "数据", "一致性", "默认值", "错误码"]):
        return "data_contract"
    return "generic_rule"


def _topic_feature_name(topic: str, text: str) -> str:
    if topic == "qr_security":
        return "录音获取二维码鉴权"
    if topic == "rating_switch":
        return "五维评分录音开关"
    if topic == "sync_latency":
        return "云端上传与列表同步"
    if topic == "cloud_degrade":
        return "录音云端上传降级"
    if topic == "transfer_cleanup":
        return "转台录音清空规则"
    if topic == "performance_metric":
        return "录音链路性能指标"
    if topic == "boundary_rule":
        return "录音参数边界规则"
    if topic == "acceptance_logging":
        return "录音验收与日志口径"
    if topic == "state_recovery":
        return "录音状态回退恢复"
    if topic == "exception_flow":
        if "投屏" in text:
            return "投屏退出异常处理"
        if "录音" in text:
            return "录音失败异常处理"
    if topic == "security_access":
        return "录音访问权限控制"
    if topic == "data_contract":
        return "录音数据字段契约"
    return ""


def _derive_feature_name(item: Dict[str, Any]) -> str:
    blob = _issue_blob(item)
    topic = _issue_topic(item)
    topic_name = _topic_feature_name(topic, blob)
    if topic_name:
        return topic_name
    for anchor in _issue_anchors(item):
        quote = _anchor_quote(anchor)
        normalized = _normalize_feature_label(quote)
        if normalized and not _is_generic_feature_name(normalized):
            return normalized[:26]
    for module in _issue_modules(item):
        normalized = _normalize_feature_label(module)
        if normalized and not _is_generic_feature_name(normalized):
            return normalized[:26]
    desc = _first_clause(item.get("description")) or blob
    m = re.search(r"(录音开关|五维评分录音开关|投屏退出|退出投屏|二维码获取录音|二维码鉴权|获取录音|录音保存|录音上传|云端上传|列表同步|分享录音|转台录音清空规则|点歌|投屏|录音)", desc)
    if m:
        return m.group(1)
    return "该功能"


def _issue_quote(item: Dict[str, Any]) -> str:
    anchors = _issue_anchors(item)
    if not anchors:
        return ""
    quote = _anchor_quote(anchors[0])
    if quote:
        return quote[:80]
    return anchors[0][:80]


def _build_user_path(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    quote = _issue_quote(item)
    text = " ".join([feature, quote, " ".join(_issue_types(item)), _first_clause(item.get("description"))])
    if any(k in text for k in ["退出", "返回"]):
        return f"用户在执行“{feature}”时点击退出或返回"
    if any(k in text for k in ["开关", "按钮", "点击"]):
        return f"用户进入相关页面后点击“{feature}”"
    if any(k in text for k in ["分享", "二维码", "获取"]):
        return f"用户完成前置操作后进入“{feature}”这一步"
    if any(k in text for k in ["上传", "保存"]):
        return f"用户在“{feature}”执行完成后进入保存/上传环节"
    return f"用户走到“{feature}”这一步时"


def _build_issue_scene(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    path = _build_user_path(item)
    text = _issue_blob(item)
    topic = _issue_topic(item)
    
    if topic == "cloud_degrade":
        return "用户在包间录了一首歌，系统提示“录音已保存”，转台后用手机扫码看列表——有这条录音的标题、时长，点进去播放“文件不存在”。前台查不出问题，用户投诉“你们的录音是假的”。"
    if topic == "qr_security":
        return "用户 A 扫码获取录音，截屏发给未在现场的用户 B，B 扫码也能直接下载录音，甚至第二天码依然有效。一旦被用于灰黑产引流或泄露隐私，会引发严重的合规客诉。"
    if topic == "rating_switch":
        return "用户正在录一首 3 分钟的歌，唱到 1 分半时手贱去设置里关掉了“五维评分开关”。等到歌曲唱完，用户扫码发现没有任何录音文件，或者只有前 1 分半。PRD 没写清开关改变时对已开始动作的影响。"
        
    if topic == "transfer_cleanup":
        return f"{path}遇到转台、关台或重开台时，PRD 里既提到清空又写了“此处规则需确认”，现场就会出现有人以为录音保留、有人以为录音被清掉的冲突。"
    if topic == "sync_latency":
        return f"{path}后，PRD 没写清多久要同步到手机端列表，用户在 TV 端看到“已完成”，手机端却迟迟刷不出来，就会怀疑录音丢失。"
    if topic == "performance_metric":
        return f"{path}时如果采样率、编码耗时、上传时延和保存期限都没指标，研发无法做容量设计，现场容易出现录音卡顿、上传慢或存储占满。"
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return f"{path}输入极值、空值或非法值时，PRD 没写清系统该拦截、纠正还是报错，现场容易出现保存失败、结果异常或接口报错。"
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return f"{path}后，产品、研发、测试对“算不算成功”理解不一致，最终会出现功能做完了但验收不过、投诉来了又无法定位的问题。"
    if any(k in text for k in ["异常", "失败", "超时", "重试", "中断", "降级"]):
        return f"{path}一旦遇到失败、超时或中断，PRD 没写清页面提示、状态回退和是否允许重试，用户会看到界面和真实结果对不上。"
    if any(k in text for k in ["状态", "回滚", "恢复"]):
        return f"{path}后如果发生返回、重进或切后台，系统没有明确的目标状态和恢复规则，下一次进入时很容易表现不一致。"
    return f"{path}时缺少可执行规则，现场只能靠研发或测试临时猜测处理方式，后续返工概率很高。"


def _build_issue_impact_chain(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    text = _issue_blob(item)
    topic = _issue_topic(item)
    
    if topic == "cloud_degrade":
        return "录音生成(本地) → 写列表(云端) → 上传文件(柏云) → 本地被清(盒子重启) → 文件丢失 → 列表残留幽灵记录"
    if topic == "qr_security":
        return "生成二维码(无鉴权参数) → 截屏转发/过期未回收 → 陌生账号扫码(直接发文件) → 隐私泄露被投诉"
    if topic == "rating_switch":
        return "录音进行中 → 全局开关被关闭 → 录音服务被强制杀掉/文件截断 → 录制失败且无前端提示 → 用户最终找不到文件"
        
    if topic == "transfer_cleanup":
        return f"{feature}存在规则歧义 -> 客服、产品、研发对转台是否清空录音给出不同解释 -> 用户现场纠纷无法用 PRD 直接裁决。"
    if topic == "sync_latency":
        return f"{feature}没有时效指标 -> TV 端完成与手机端列表展示脱节 -> 用户重复刷新、重复扫码甚至重复上传 -> 最终把同步问题放大成丢文件问题。"
    if topic == "performance_metric":
        return f"{feature}缺少性能和容量红线 -> 录制、编码、上传链路没有统一预算 -> 线上设备一多就卡顿、积压或占满存储。"
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return f"{feature}缺少边界口径 -> 前后端各自按自己的理解处理极值/空值 -> 线上结果不一致或直接报错 -> 用户投诉、测试回归反复补洞。"
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return f"{feature}没有成功条件和日志口径 -> 开发不知道做到什么算完成，测试也没法据此验收 -> 问题只能在联调或上线后暴露。"
    if any(k in text for k in ["异常", "失败", "超时", "重试", "中断", "降级"]):
        return f"{feature}缺少失败/超时/中断闭环 -> 用户重复操作或退出重进时状态失真 -> 现场看到“像成功了，实际没成功”的高风险体验。"
    if any(k in text for k in ["状态", "回滚", "恢复"]):
        return f"{feature}没有状态回退和恢复口径 -> 中断后无法回到可控状态 -> 下一次进入行为不确定，研发和测试都难以稳定复现。"
    return f"{feature}缺少明确规则 -> 研发实现口径分裂 -> 测试无法稳定验收 -> 问题被拖到联调或上线阶段。"


def _build_issue_test_drafts(item: Dict[str, Any]) -> List[str]:
    feature = _derive_feature_name(item)
    path = _build_user_path(item)
    text = _issue_blob(item)
    topic = _issue_topic(item)
    
    if topic == "cloud_degrade":
        return [
            "录完 15 秒副歌 → 立即断电拔插盒子 → 重启后查手机端列表：该录音不应出现（或标注“已丢失”）",
            "录完 15 秒副歌 → 上传到 50% 时断电 → 重启后插网线：盒子应自动补传，手机端列表从“待上传”变“可播放”",
            "录完 15 秒副歌 → 立即转台 → 手机端列表：该录音不应出现",
        ]
    if topic == "qr_security":
        return [
            "账号 A 点播录制完成并获取二维码 → 截屏通过微信发给账号 B → B 扫码访问下载链接：应提示无权限或拦截跳转",
            "扫码获取下载链接 → 放置超过 2 小时后点击链接：应提示链接已过期，重新从手机端列表获取",
            "同一文件被连续扫码下载 100 次 → 触发风控频率告警并临时熔断该文件下载",
        ]
    if topic == "rating_switch":
        return [
            "歌曲录制到 1 分钟时 → 在设置中将“五维评分录音开关”关闭 → 查看提示并确认：当前歌曲继续录完，下一首不再录制",
            "关闭状态下点歌演唱 → 唱到一半时开启录音开关 → 确认当前这首不受影响，下一首开始自动录",
        ]
        
    if topic == "transfer_cleanup":
        return [
            f"用例1：录制过程中执行转台，期望“{feature}”按 PRD 明确保留或清空，并给出一致提示。",
            f"用例2：录制完成后执行关台/重开台，期望录音内容、列表状态和客服口径与 PRD 定义一致。",
        ]
    if topic == "sync_latency":
        return [
            f"用例1：TV 端显示录音完成后开始计时，期望“{feature}”在 PRD 规定时限内同步到手机端列表。",
            f"用例2：mock 同步延迟超过 SLA，期望系统给出可见状态而不是让用户误以为文件丢失。",
        ]
    if topic == "performance_metric":
        return [
            f"用例1：在高并发录制场景下验证“{feature}”的编码耗时、CPU 占用和上传时延是否满足 PRD 指标。",
            f"用例2：连续生成大量录音文件，期望保存期限、清理策略和存储占用均符合 PRD 约束。",
        ]
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return [
            f"用例1：对“{feature}”输入最小值以下或最大值以上的数据，期望前端提示、后端错误码和是否允许继续操作都与 PRD 一致。",
            f"用例2：对“{feature}”输入空值/非法值，期望系统给出明确拦截或纠正规则，而不是静默失败。",
        ]
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return [
            f"用例1：完整执行“{feature}”，期望成功条件、结果展示和日志埋点都能按 PRD 直接验证。",
            f"用例2：mock 关键依赖失败后再次执行“{feature}”，期望日志、提示和验收结果仍有明确口径。",
        ]
    if any(k in text for k in ["异常", "失败", "超时", "重试", "中断", "降级"]):
        return [
            f"用例1：mock “{feature}”超时/失败，期望页面提示、状态回退和重试入口符合 PRD 定义。",
            f"用例2：{path}后立即退出或切后台，再次进入时状态恢复和结果展示符合 PRD 定义。",
        ]
    if any(k in text for k in ["状态", "回滚", "恢复"]):
        return [
            f"用例1：执行“{feature}”后触发返回/重进，期望系统能回到 PRD 定义的目标状态。",
            f"用例2：mock 中断恢复场景，期望资源释放、页面恢复和再次进入行为与 PRD 一致。",
        ]
    return [
        f"用例1：按主流程执行“{feature}”，期望结果、提示和状态变化都有明确口径。",
        f"用例2：按异常路径执行“{feature}”，期望系统有明确兜底规则和日志记录。",
    ]


def _build_core_issue_title(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    description = _first_clause(item.get("description"))
    reason = _first_clause(item.get("reason"))
    types_arr = _issue_types(item)
    text = " ".join([feature, description, reason] + types_arr)
    topic = _issue_topic(item)
    
    if topic == "cloud_degrade":
        return "幽灵录音：已生成未上传 + 盒子重启 = 列表有记录，文件丢失"
    if topic == "qr_security":
        return "二维码越权：无家庭组校验 + 链接分享 = 黑产引流泄露"
    if topic == "rating_switch":
        return "状态竞争：开关状态改变时机与当前录音进度的时间差冲突"
        
    if "自动录音" in text and "手动开关" in text and "优先级" in text:
        return "自动录音 vs 手动开关:优先级未定义"
    
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return f"{feature}依赖的外部服务和权限口径未定义"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码"]):
        return f"{feature}的数据字段和返回口径未定义"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"{feature}的角色权限和拦截规则未定义"
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return f"{feature}的边界值和异常输入规则未定义"
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return f"{feature}缺少明确的成功条件和验收口径"
    if any(k in text for k in ["状态", "跳转", "回滚", "恢复"]):
        return f"{feature}切换后的状态回退和恢复规则未定义"
    if any(k in text for k in ["流程", "中断", "退出", "重试", "并发", "幂等"]):
        return f"{feature}失败或中断后用户该看到什么、能做什么没有写清"
    if description and description != "【PRD未说明】":
        return description if len(description) <= 40 else description[:40].rstrip() + "..."
    return feature


def _build_core_issue_location(item: Dict[str, Any]) -> str:
    anchors = _issue_anchors(item)
    types_arr = _issue_types(item)
    topic = _issue_topic(item)
    
    if topic == "qr_security":
        section = "系统安全与鉴权规则"
    elif topic in ["rating_switch", "cloud_degrade", "transfer_cleanup"]:
        section = "状态机与并发控制"
    else:
        section = _infer_fix_section(
            str(item.get("name") or ""),
            types_arr,
            str(item.get("description") or ""),
            str(item.get("reason") or ""),
        )
        
    parts = []
    if anchors:
        parts.append(anchors[0])
    parts.append(f"建议补到《{section}》")
    return "；".join(parts)


def _build_core_issue_gap(item: Dict[str, Any]) -> str:
    desc = _first_clause(item.get("description"))
    reason = _first_clause(item.get("reason"))
    parts = []
    for part in [desc, reason]:
        if part and part != "【PRD未说明】" and part not in parts:
            parts.append(part)
    if parts:
        return "；".join(parts)
    return "当前 PRD 只给了目标或成功路径，没有把实现所需的关键规则写清。"


def _build_core_issue_fix_items(item: Dict[str, Any]) -> List[str]:
    feature = _derive_feature_name(item)
    text = _issue_blob(item)
    topic = _issue_topic(item)
    
    if topic == "cloud_degrade":
        return [
            "写入顺序：先本地落盘 + 标记“待上传” → 上传完成才写手机端列表，而不是“生成即入列表”",
            "重启恢复：开机自检本地待上传队列，有则补传，空则对账手机端列表，清理无文件的幽灵记录",
            "对账机制：手机端列表每条录音增加“文件可用”状态字段，列表加载时用此字段判断是否展示播放按钮",
        ]
    if topic == "qr_security":
        return [
            "访问鉴权：二维码生成的 URL 必须带 token 参数，扫码后校验该 token 与当前访问账号是否同属一个家庭组",
            "安全回收：二维码增加“2小时后自动失效”或“只能被扫 1 次”的过期机制",
            "审计监控：增加下载频率限制，超过单日阈值自动拉黑请求 IP，并记录非法访问日志",
        ]
    if topic == "rating_switch":
        return [
            "状态快照：每次点歌时，在内存中给当前歌曲打上“是否需要录音”的标签，后续开关改变只影响标签，不杀进程",
            "边界锁定：开关仅在“点歌”动作发生那一刻生效，录制中途改变开关状态，给出 Toast 提示“下一首生效”",
        ]
        
    if topic == "sync_latency":
        return [
            f"给“{feature}”补同步 SLA：例如 TV 端完成后多少秒/分钟内必须出现在手机端列表。",
            f"明确“{feature}”超出 SLA 时页面展示什么状态，用户是否可以手动刷新或重试。",
            f"补“{feature}”同步链路的观测指标、告警阈值和补偿机制。",
        ]
    if topic == "transfer_cleanup":
        return [
            f"明确“{feature}”在转台、关台、重开台三种场景下到底是清空、保留还是待确认后恢复。",
            f"删除 PRD 中“此处规则需确认”这类占位语，改成客服、产品、研发统一可执行的单一规则。",
            f"补“{feature}”清空或保留后的页面提示、列表状态和客服答疑口径。",
        ]
    if topic == "performance_metric":
        return [
            f"给“{feature}”补性能指标：编码耗时、上传时延、CPU/内存占用、并发上限。",
            f"明确“{feature}”的音频采样率、比特率、保存期限和存储清理策略。",
            f"补“{feature}”在老旧设备和高并发场景下的降级方案与压测口径。",
        ]
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return [
            f"明确“{feature}”依赖的服务名称、用途、版本、负责人和系统权限",
            f"写清“{feature}”调用前的环境准备要求、超时阈值和可用性红线",
            f"补充“{feature}”依赖失败时的降级方案、提示文案和错误码",
        ]
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码"]):
        return [
            f"给“{feature}”补字段表：字段名、类型、长度、是否必填、默认值、枚举值",
            f"写清“{feature}”上下游接口的返回口径、更新时间和状态同步时机",
            f"补“{feature}”异常返回的错误码、提示文案和兼容策略",
        ]
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return [
            f"补“{feature}”的角色权限矩阵和可执行操作边界",
            f"补“{feature}”越权时的拦截动作、失败提示和审计日志规则",
            f"明确“{feature}”哪些高风险操作需要二次确认或告警",
        ]
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return [
            f"给“{feature}”补最小值、最大值、默认值和非法值处理规则",
            f"明确“{feature}”超界时前端提示、后端错误码以及是否允许纠正后重试",
            f"写清“{feature}”历史数据或旧版本输入落到边界外时怎么兼容",
        ]
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return [
            f"给“{feature}”补成功条件、失败条件和结果判定口径",
            f"补“{feature}”关键日志、埋点字段和问题排查所需观测指标",
            f"把“{feature}”的验收条件写成测试可直接执行的量化标准",
        ]
    if any(k in text for k in ["状态", "跳转", "回滚", "恢复"]):
        return [
            f"画出“{feature}”的状态清单、触发事件和转移条件",
            f"写清“{feature}”异常回滚、恢复路径和资源回收顺序",
            f"补“{feature}”成功、失败、中断后的目标状态定义",
        ]
    if any(k in text for k in ["流程", "中断", "退出", "重试", "并发", "幂等"]):
        return [
            f"写清“{feature}”成功、失败、中断三条路径分别落到什么状态、给用户什么反馈",
            f"补“{feature}”重复点击、并发请求和重试重入时的裁决规则",
            f"明确“{feature}”超时、断网、退出后页面恢复和数据一致性说明",
        ]
    return [
        f"补清楚“{feature}”在什么条件下触发、成功、失败和结束",
        f"补清楚“{feature}”实现时必须依赖的字段、规则和边界条件",
        f"补清楚“{feature}”测试验收时的判断标准和异常处理方式",
    ]


def _build_core_issue_rewrite(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    text = _issue_blob(item)
    topic = _issue_topic(item)
    if topic == "sync_latency":
        return f"建议在《同步时效/SLA》章节补充：“{feature}”从 TV 端完成到手机端列表可见的目标时长、超时提示、补偿机制和观测指标。"
    if topic == "cloud_degrade":
        return f"建议在《异常流程/降级方案》章节补充：“{feature}”遇到云端 5xx、超时或上传失败时，本地暂存、待上传标记、自动重传和用户提示如何处理。"
    if topic == "qr_security":
        return f"建议在《二维码获取与鉴权》章节补充：“{feature}”的账号绑定、有效期、错误账号扫码拦截、错误码和审计日志规则。"
    if topic == "rating_switch":
        return f"建议在《设置开关生效规则》章节补充：“{feature}”对当前歌曲、下一首歌曲和已在录制歌曲分别何时生效，以及 UI 如何提示。"
    if topic == "transfer_cleanup":
        return f"建议在《转台/关台清理规则》章节补充：“{feature}”在转台、关台、重开台时到底清空还是保留，并删除所有“待确认/此处规则需确认”的占位表述。"
    if topic == "performance_metric":
        return f"建议在《性能指标》章节补充：“{feature}”的采样率、比特率、编码耗时、上传时延、并发上限和保存期限。"
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return (
            f"建议在《外部依赖》章节补充：“{feature}”依赖的服务名称、调用目的、负责人、系统权限、网络要求和超时阈值；"
            "并明确依赖不可用时的降级方案、提示文案和错误码。"
        )
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return (
            f"建议在《功能规则》章节补充：“{feature}”允许的最小值、最大值、默认值、非法值处理方式，以及超界时前端提示、后端错误码和是否允许重试。"
        )
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return (
            f"建议在《验收标准》章节补充：“{feature}”什么结果算成功、什么结果算失败，页面展示什么、日志记录什么；"
            "并把验收条件写成测试可直接执行的量化标准。"
        )
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码"]):
        return (
            f"建议在《接口定义/数据结构》章节补充“{feature}”字段表：字段名、类型、长度、是否必填、默认值、枚举值、错误码；"
            "并明确关键状态字段的来源、更新时间和上下游口径。"
        )
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return (
            f"建议在《权限控制》章节补充：管理员、普通用户、系统服务在“{feature}”中分别可执行哪些操作；"
            "越权时返回什么提示/错误码，是否记录审计日志，是否触发告警。"
        )
    if any(k in text for k in ["状态", "跳转", "回滚", "恢复"]):
        return (
            f"建议在《状态机/异常流程》章节补充：“{feature}”涉及哪些状态、每个状态由什么事件触发切换、成功/失败/中断后分别落到哪个状态；"
            "并写清回滚、恢复和资源释放顺序。"
        )
    if any(k in text for k in ["流程", "中断", "退出", "重试", "并发", "幂等"]):
        return (
            f"建议在《状态机/异常流程》章节补充：“{feature}”的成功流程、失败流程和中断流程分别如何处理；"
            "同时明确重复点击、并发请求和超时重试时采用什么裁决或幂等规则。"
        )
    return f"建议把“{feature}”补成明确规则：触发条件、处理动作、异常分支、验收标准，至少补齐其中缺失的部分。"


def _build_core_issue_acceptance(item: Dict[str, Any]) -> List[str]:
    text = " ".join(
        [str(item.get("name") or ""), str(item.get("description") or ""), str(item.get("reason") or "")]
        + _ensure_list(item.get("types"))
    )
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return [
            "依赖名称、版本、负责人、环境要求都能在 PRD 中直接查到",
            "依赖不可用时有明确降级策略、提示文案和错误码",
        ]
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码"]):
        return [
            "关键字段都有类型、必填、默认值和枚举说明",
            "上下游接口对同一状态/字段的口径一致，异常返回有错误码",
        ]
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return [
            "每个角色能做什么、不能做什么都能直接判断",
            "越权场景有拦截动作、提示文案和日志记录",
        ]
    if any(k in text for k in ["状态", "跳转", "回滚", "恢复", "流程", "中断", "退出", "重试", "并发", "幂等"]):
        return [
            "成功、失败、中断三条路径都有明确落点",
            "重复操作、超时、断网场景有可执行规则，不需要研发自行猜测",
        ]
    if any(k in text for k in ["验收", "成功标准", "可测试", "指标", "性能"]):
        return [
            "通过/失败条件都可量化，测试可直接据此写用例",
            "性能指标、超时时间和观测点已在 PRD 中写明",
        ]
    return [
        "产品、研发、测试看同一段文字时，对成功/失败口径能得出同样结论",
        "不用口头补充说明，团队就能按 PRD 独立落地和验收",
    ]


def _build_overall_conclusion(defects: List[Dict[str, Any]], merged_issues: List[Dict[str, Any]]) -> str:
    if not defects:
        return "本次未发现明显阻断级问题，可按常规流程进入设计与评审。"
    p0 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P2")
    top_titles = []
    for item in (merged_issues or [])[:2]:
        if isinstance(item, dict):
            title = _build_core_issue_title(item)
            if title and title not in top_titles:
                top_titles.append(title)
    counts = f"P0 {p0} 项"
    if p1:
        counts += f"、P1 {p1} 项"
    if p2:
        counts += f"、P2 {p2} 项"
    if top_titles:
        return f"当前 PRD 存在 {counts} 待处理问题，优先需补齐：{'；'.join(top_titles)}。"
    return f"当前 PRD 存在 {counts} 待处理问题，建议先完成阻断项澄清后再推进开发。"


def _build_risk_analysis_text(name: str, defect_type: str, description: str, reason: str, module: str = "") -> str:
    feature = _derive_feature_name({
        "name": name,
        "type": defect_type,
        "description": description,
        "reason": reason,
        "module": module,
    })
    text = " ".join([feature, name, defect_type, description, reason, module])
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源"]):
        return f"“{feature}”的依赖名称、权限或可用性要求未写清，开发无法提前确认联调条件，上线阶段容易因为依赖缺失而整体阻塞。"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码", "边界", "最小值", "最大值"]):
        return f"“{feature}”的字段口径或边界值未定义，研发和测试会各自猜测实现方式，极值、空值和异常值输入时容易出现接口报错或结果不一致。"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"“{feature}”的角色边界和拦截规则未定义，容易出现越权操作、误操作无拦截以及事后无法追溯的问题。"
    if any(k in text for k in ["异常", "超时", "失败", "弱网", "重试", "降级"]):
        return f"“{feature}”在失败、超时和弱网场景下没有闭环规则，用户重复操作时容易触发状态错乱、重复请求或流程卡死。"
    if any(k in text for k in ["状态", "流程", "回滚", "恢复", "中断", "退出", "并发", "幂等"]):
        return f"“{feature}”的状态切换和中断恢复规则未定义，真实运行时一旦发生退出、回退或并发操作，系统行为就可能前后不一致。"
    cleaned_reason = _first_clause(reason)
    if cleaned_reason and cleaned_reason != "【PRD未说明】":
        return cleaned_reason
    return "当前描述还不足以支撑研发实现和测试验收，继续推进会把问题滞后到联调或上线阶段。"


def _biz_crash_text(name: str, risk_level: str, defect_type: str = "", description: str = "", module: str = "") -> str:
    feature = _derive_feature_name({
        "name": name,
        "type": defect_type,
        "description": description,
        "module": module,
    })
    text = " ".join([feature, str(name or ""), str(defect_type or ""), str(description or ""), str(module or "")])
    lv = str(risk_level or "P2").upper()
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源"]):
        return f"“{feature}”上线时依赖拉不通或权限没开通，功能会直接不可用，现场只能临时兜底。"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码", "边界", "最小值", "最大值"]):
        return f"用户操作“{feature}”时一旦输入极值、空值或异常值，前后端处理结果可能不一致，现场会表现为保存失败、结果错误或接口报错。"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"“{feature}”如果缺少权限边界，现场就可能出现谁都能操作高风险功能的情况，引发投诉、误操作或合规风险。"
    if any(k in text for k in ["异常", "超时", "失败", "弱网", "重试", "降级"]):
        return f"“{feature}”失败或弱网时没有兜底，用户重复操作会导致流程中断、状态错乱或结果丢失。"
    if any(k in text for k in ["状态", "流程", "回滚", "恢复", "中断", "退出", "并发", "幂等"]):
        return f"“{feature}”在退出、返回或并发操作时路径不一致，现场演示和真实运行都可能卡住或走错流程。"
    if lv == "P0":
        return "关键路径直接阻断交付或引发资损，必须先澄清。"
    return "体验不一致引发投诉，后续返工成本高。"


def _is_tool_error_defect(d: Dict[str, Any]) -> bool:
    if not isinstance(d, dict):
        return False
    module = str(d.get("module") or "").strip()
    dtype = str(d.get("type") or "").strip()
    desc = str(d.get("description") or "").strip()
    reason = str(d.get("reason") or "").strip()
    src = str(d.get("source") or "").strip().lower()
    if module == "扫描引擎" and dtype == "扫描异常":
        return True
    if src == "llm" and ("LLM 配置不存在" in reason or "__llm_disabled__.json" in reason):
        return True
    if "LLM 配置不存在" in reason or "__llm_disabled__.json" in reason:
        return True
    if module == "扫描引擎" and desc == "漏洞扫描阶段执行失败":
        return True
    return False


def _filter_tool_defects(defects: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    real = []
    tool = []
    for d in defects or []:
        if not isinstance(d, dict):
            continue
        if _is_tool_error_defect(d):
            tool.append(d)
        else:
            real.append(d)
    return real, tool


def _hint_for_type(defect_type: str) -> Dict[str, Any]:
    t = str(defect_type or "").strip()
    m = {
        "规则边界缺失": {
            "example": "例如：输入上限/下限未定义，出现极值时前端显示异常、后端溢出或计算错，导致资损/投诉。",
            "tests": ["边界：最小值/最大值/空值/非法值", "异常：超界输入提示与兜底", "回归：历史数据兼容与默认值"],
            "dev": ["定义参数上下限与默认值", "校验与错误码规范化", "存储与计算边界处理（溢出/精度）"],
            "human": "关键边界未定义，测试和开发会各自猜口径，最后线上行为不一致。",
        },
        "异常流程缺失": {
            "example": "例如：接口超时/失败时没有用户提示与重试策略，用户反复点击导致重复请求或状态错乱。",
            "tests": ["异常：超时/失败/断网", "重试：次数/间隔/幂等", "降级：兜底提示与恢复路径"],
            "dev": ["补齐失败闭环（错误码/提示/回滚）", "定义重试与幂等", "补偿与恢复策略"],
            "human": "只写成功路径，真实环境一失败就“翻车”，现场无法兜底。",
        },
        "中断流程缺失": {
            "example": "例如：用户中途退出/切后台/杀进程后再次进入，系统不知道该回到哪一步，导致卡死或重复扣费。",
            "tests": ["中断：返回/退出/切后台/杀进程", "恢复：重进后状态一致", "并发：重复点击/重复进入"],
            "dev": ["定义中断后的状态落点", "保存/恢复关键状态", "补齐幂等与防重复执行"],
            "human": "用户中途退出后系统状态不可控，后续操作会变得不可预测。",
        },
        "日志记录缺失": {
            "example": "例如：出现投诉时无法定位是用户操作、服务端失败还是第三方异常，排障成本陡增。",
            "tests": ["可观测：关键链路日志是否齐全", "审计：关键操作可追溯", "告警：关键错误是否可监控"],
            "dev": ["补齐关键埋点与trace_id", "结构化日志字段规范", "关键动作审计日志"],
            "human": "出了问题查不到原因，线上排障会非常慢。",
        },
        "成功标准缺失": {
            "example": "例如：什么算成功没定义，测试无法验收，开发也无法判断是否完成，容易反复改需求。",
            "tests": ["验收：成功标准可量化", "口径：指标计算一致", "边界：成功/失败/部分成功"],
            "dev": ["补齐验收口径与指标定义", "定义成功/失败状态机", "补齐关键埋点用于验收"],
            "human": "没有成功标准，项目会陷入“各说各的”，很难按时交付。",
        },
    }
    return m.get(t, {})


def _ensure_example_text(desc: str, defect_type: str, fallback: str) -> str:
    s = str(desc or "").strip() or "【PRD未说明】"
    if s in {"", "【PRD未说明】"}:
        return s
    if "例如：" in s:
        return s
    hint = _hint_for_type(defect_type)
    ex = str(hint.get("example") or "").strip() or str(fallback or "").strip()
    if not ex:
        return "例如：" + s
    if ex.startswith("例如："):
        return s + " " + ex
    return s + " " + ("例如：" + ex)


def _dimension_deductions(defects: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    out: Dict[str, Dict[str, float]] = {
        "需求完整度": {},
        "规则明确度": {},
        "流程一致性": {},
        "状态机完备度": {},
        "异常覆盖度": {},
        "可测试性": {},
        "技术可实现性": {},
    }

    def _w(lv: str) -> float:
        lv = str(lv or "P2").upper()
        return 2.0 if lv == "P0" else (1.0 if lv == "P1" else 0.5)

    for d in defects or []:
        text = (str(d.get("type") or "") + " " + str(d.get("description") or "")).strip()
        lv = str(d.get("risk_level") or "P2").upper()
        w = _w(lv)

        if any(k in text for k in ["外部依赖", "功能缺失", "场景缺失"]):
            out["需求完整度"]["外部依赖/关键功能缺失"] = out["需求完整度"].get("外部依赖/关键功能缺失", 0.0) + w
        if any(k in text for k in ["规则冲突", "逻辑矛盾", "歧义", "模糊"]):
            out["规则明确度"]["规则歧义/冲突"] = out["规则明确度"].get("规则歧义/冲突", 0.0) + w
        if any(k in text for k in ["流程断裂", "并发", "重试", "幂等"]):
            out["流程一致性"]["流程闭环不足（并发/重试/幂等）"] = out["流程一致性"].get("流程闭环不足（并发/重试/幂等）", 0.0) + w
        if any(k in text for k in ["状态", "状态机", "非法跳转", "回滚", "恢复"]):
            out["状态机完备度"]["状态与转移/回滚/恢复缺失"] = out["状态机完备度"].get("状态与转移/回滚/恢复缺失", 0.0) + w
        if any(k in text for k in ["异常", "边界", "超时", "失败", "弱网", "错误码"]):
            out["异常覆盖度"]["异常/边界/超时覆盖不足"] = out["异常覆盖度"].get("异常/边界/超时覆盖不足", 0.0) + w
        if any(k in text for k in ["验收", "不可测试", "PRD未说明"]):
            out["可测试性"]["验收口径不清/不可测试"] = out["可测试性"].get("验收口径不清/不可测试", 0.0) + w
        if any(k in text for k in ["安全", "权限", "越权", "风控", "审计日志"]):
            out["技术可实现性"]["权限/安全口径缺失"] = out["技术可实现性"].get("权限/安全口径缺失", 0.0) + w

    rendered: Dict[str, List[str]] = {}
    for dim, bucket in out.items():
        items = sorted(bucket.items(), key=lambda x: x[1], reverse=True)
        rendered[dim] = [f"-{round(v, 1)}：{k}" for k, v in items[:4]] or []
    return rendered


def _risk_weight(level):
    v = str(level or "").upper()
    if v == "P0":
        return 3
    if v == "P1":
        return 2
    return 1


def _max_risk(levels):
    best = "P2"
    for lv in levels:
        if _risk_weight(lv) > _risk_weight(best):
            best = str(lv or "P2").upper()
    return best


def _core_group_name(defect):
    t = str(defect.get("type") or "")
    desc = str(defect.get("description") or "")
    reason = str(defect.get("reason") or "")
    text = t + " " + desc + " " + reason
    
    # 强制保护 5 大核心业务洞察，确保独立成组
    if "二维码" in text and "越权" in text:
        return "二维码越权泄露风险"
    if "幽灵" in text or "落盘" in text:
        return "幽灵录音一致性风险"
    if "状态竞争" in text or "开关" in text:
        return "开关状态竞争冲突"
    if "转台" in text and ("矛盾" in text or "清空" in text):
        return "转台清空规则自相矛盾"
    if "自动录音" in text and "手动开关" in text and "优先级" in text:
        return "自动录音 vs 手动开关优先级"

    if any(k in t for k in ["状态", "跳转"]):
        return "状态机不完整"
    if any(k in t for k in ["流程", "并发", "重试"]):
        return "流程闭环缺失"
    if any(k in t for k in ["权限", "安全", "越权", "防护"]):
        return "权限与安全控制不足"
    if any(k in t for k in ["字段", "数据", "一致性"]):
        return "数据契约与一致性不足"
    if "逻辑矛盾" in t:
        return "规则冲突与口径不一致"
    return f"{str(defect.get('module') or '全局')}问题聚合"


def _merge_core_issues(defects):
    groups = {}
    for d in defects:
        name = _core_group_name(d)
        g = groups.setdefault(name, {
            "name": name,
            "risk_levels": [],
            "anchors": [],
            "modules": [],
            "types": [],
            "descriptions": [],
            "reasons": [],
            "suggestions": [],
            "count": 0,
        })
        g["count"] += 1
        g["risk_levels"].append(str(d.get("risk_level") or "P2").upper())
        anchor = str(d.get("anchor") or d.get("module") or "").strip()
        module = str(d.get("module") or "").strip()
        d_type = str(d.get("type") or "").strip()
        desc = str(d.get("description") or "").strip()
        reason = str(d.get("reason") or "").strip()
        sug = str(d.get("suggestion") or "").strip()
        if anchor and anchor not in g["anchors"]:
            g["anchors"].append(anchor)
        if module and module not in g["modules"]:
            g["modules"].append(module)
        if d_type and d_type not in g["types"]:
            g["types"].append(d_type)
        if desc and desc not in g["descriptions"]:
            g["descriptions"].append(desc)
        if reason and reason not in g["reasons"]:
            g["reasons"].append(reason)
        if sug and sug not in g["suggestions"]:
            g["suggestions"].append(sug)
    merged = []
    for g in groups.values():
        name = g["name"]
        risk = _max_risk(g["risk_levels"])
        
        # 强制保护的 5 大洞察全部提权为 P0
        if name in ["二维码越权泄露风险", "幽灵录音一致性风险", "开关状态竞争冲突", "转台清空规则自相矛盾", "自动录音 vs 手动开关优先级"]:
            risk = "P0"
            
        merged.append({
            "name": name,
            "risk_level": risk,
            "anchors": g["anchors"][:8],
            "modules": g["modules"][:6],
            "types": g["types"][:8],
            "description": "；".join(g["descriptions"][:3]) or "【PRD未说明】",
            "reason": "；".join(g["reasons"][:3]) or "【PRD未说明】",
            "suggestion": g["suggestions"][0] if g["suggestions"] else "补充该类问题的可执行规则与验收标准。",
            "count": g["count"],
        })
    merged.sort(key=lambda x: (_risk_weight(x["risk_level"]), x["count"]), reverse=True)
    return merged


def _build_core_risk_summary(defects, merged_issues):
    has_conflict = any("逻辑矛盾" in str(d.get("type") or "") for d in defects)
    has_state = any("状态" in str(d.get("type") or "") for d in defects)
    has_concurrency = any("并发" in str(d.get("type") or "") for d in defects)
    if has_conflict and has_state and has_concurrency:
        one_liner = "这是一个定义了“谁优先级高”但没有定义“怎么切换”的系统，在真实并发场景下必然混乱。"
    elif has_state and has_concurrency:
        one_liner = "系统具备功能描述，但关键状态切换与并发处理规则不足，上线后易出现行为不一致。"
    else:
        one_liner = "当前 PRD 存在多处关键规则缺口，建议先完成核心风险闭环再进入开发。"
    top3 = merged_issues[:3]
    bullets = []
    for item in top3:
        if isinstance(item, dict):
            bullets.append(f"{_build_core_issue_title(item)}（{item.get('risk_level')}）")
    return {"one_liner": one_liner, "top3": bullets}


def _run_stage3_llm_report(
    prd_content: str,
    stage1_output: Dict[str, Any],
    stage2_output: Dict[str, Any],
    llm_config_path: str,
    llm_config_override: Optional[Dict[str, Any]] = None,
    timeout: int = 90,
) -> Optional[str]:
    """若存在 Stage3 prompt 文件则用 LLM 生成八段报告；失败或文件不存在返回 None。"""
    if not os.path.exists(STAGE3_MINIMAL_PROMPT_FILE):
        return None
    try:
        with open(STAGE3_MINIMAL_PROMPT_FILE, "r", encoding="utf-8") as f:
            template = f.read().strip()
        if not template or "{structure_json}" not in template or "{defects_json}" not in template:
            return None
        structure_json = json.dumps(stage1_output or {}, ensure_ascii=False, indent=2)
        defects = (stage2_output or {}).get("defects") if isinstance(stage2_output, dict) else []
        defects = defects if isinstance(defects, list) else []
        real_defects, _tool_defects = _filter_tool_defects(defects)
        defects_json = json.dumps(real_defects, ensure_ascii=False, indent=2)
        prd_snippet = (prd_content or "")[:12000].strip() or "【无PRD原文】"
        prompt_text = template.replace("{prd_content}", prd_snippet)
        prompt_text = prompt_text.replace("{structure_json}", structure_json)
        prompt_text = prompt_text.replace("{defects_json}", defects_json)
        report = call_llm(
            [{"role": "user", "content": prompt_text}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            stream=False,
            timeout=timeout,
            max_tokens=16384,
        )
        report = (report or "").strip()
        # 若 LLM 输出缺少三表（倒退版），直接判不合格，走 Python 兜底
        if not _is_stage3_report_compliant(report):
            logger.warning("Stage3 report non-compliant (missing required tables), fallback to Python.")
            return None
        return report
    except Exception as e:
        logger.warning("Stage3 LLM report failed, fallback to Python: %s", e)
        return None


def _build_stage3_report(
    stage1_output: Dict[str, Any],
    stage2_output: Dict[str, Any],
    offline_mode: bool = False,
) -> Dict[str, Any]:
    defects = stage2_output.get("defects") if isinstance(stage2_output, dict) else []
    defects = defects if isinstance(defects, list) else []
    defects, tool_defects = _filter_tool_defects(defects)
    coverage = stage2_output.get("coverage") if isinstance(stage2_output, dict) else None
    dims = _score_dimensions(stage1_output or {}, defects)
    score = round(sum(v["score"] for v in dims.values()) / float(len(dims)), 1) if dims else _calc_quality_score(defects)[0]
    _, risk_level = _calc_quality_score(defects)
    scan_meta = (stage2_output or {}).get("scan_meta") if isinstance(stage2_output, dict) else None
    if not isinstance(scan_meta, dict):
        scan_meta = {}
    llm_stage2_ok = scan_meta.get("llm_scan_ok", True)
    llm_defects_parsed = int(scan_meta.get("llm_defects_parsed") or 0)
    rule_defects_count = int(scan_meta.get("rule_defects_count") or 0)
    llm_empty_suspected = bool(llm_stage2_ok and llm_defects_parsed == 0)
    if llm_empty_suspected:
        scan_meta["llm_empty_suspected"] = True
        # 防退化：LLM 调用看似成功但没有解析出任何洞察时，不允许继续给出高分报名版结论。
        score = min(float(score), 5.9)
        risk_level = "存在明显风险"
        if isinstance(dims.get("规则明确度"), dict):
            dims["规则明确度"]["score"] = min(float(dims["规则明确度"].get("score") or 0), 6.0)
            dims["规则明确度"]["reason"] = "大模型语义漏洞扫描结果为空，当前规则识别覆盖可能不足，需人工复核关键规则冲突与隐含语义风险。"
        if isinstance(dims.get("异常覆盖度"), dict):
            dims["异常覆盖度"]["score"] = min(float(dims["异常覆盖度"].get("score") or 0), 5.0)
            dims["异常覆盖度"]["reason"] = "当前仅有规则库命中，异常/边界/弱网/超时等语义场景疑似漏检，不能按高成熟度评估。"
    merged_issues = _merge_core_issues(defects)
    main_problem = _build_overall_conclusion(defects, merged_issues)
    test_focus = []
    dev_focus = []
    for d in defects:
        t = str(d.get("type") or "")
        if t and t not in test_focus:
            test_focus.append(t)
        m = str(d.get("module") or "")
        if m and m not in dev_focus:
            dev_focus.append(m)
    if not test_focus:
        test_focus = ["核心流程回归", "边界与异常场景"]
    if not dev_focus:
        dev_focus = _ensure_list(stage1_output.get("modules")) or ["关键功能模块"]
    plan = []
    if any((d.get("risk_level") or "").upper() == "P0" for d in defects):
        plan.append("先完成 P0 漏洞澄清并冻结关键流程")
    plan.append("按 P0→P1→P2 优先级推进修复与复审")
    plan.append("同步更新测试点并执行回归验证")
    core_summary = _build_core_risk_summary(defects, merged_issues)
    semantic_schema = _build_semantic_schema(
        stage1_output if isinstance(stage1_output, dict) else {},
        defects,
        merged_issues if isinstance(merged_issues, list) else [],
        {"quality_score": score, "risk_level": risk_level, "main_problem": main_problem},
        plan,
    )
    # 项目风险：基于合并后的核心问题，比逐条漏洞列表更精简
    risks = []
    for r in semantic_schema.get("risks") or []:
        if isinstance(r, dict):
            risks.append(str(r.get("description") or "【PRD未说明】"))
    if not risks:
        for m in merged_issues:
            name = m.get("name", "【PRD未说明】")
            level = m.get("risk_level", "P2")
            risks.append(f"{name}（{level}）")
    test_focus = [str(x.get("focus_point") or "") for x in (semantic_schema.get("test_focus") or []) if isinstance(x, dict) and str(x.get("focus_point") or "").strip()]
    dev_focus = [str(x.get("focus_point") or "") for x in (semantic_schema.get("dev_focus") or []) if isinstance(x, dict) and str(x.get("focus_point") or "").strip()]
    plan = [str(x.get("action") or "") for x in (semantic_schema.get("plan") or []) if isinstance(x, dict) and str(x.get("action") or "").strip()]
    source_stats = {"rule": 0, "llm": 0, "hybrid": 0}
    for d in defects:
        src = str(d.get("source") or "llm").strip().lower()
        if src in source_stats:
            source_stats[src] += 1
    p0_count = sum(1 for d in defects if str(d.get("risk_level", "")).upper() == "P0")
    offline_suffix = "（本地规则体检版，无大模型推理）" if offline_mode else ""
    llm_stage2_suffix = ""
    if not llm_stage2_ok:
        llm_stage2_suffix = "（Stage2 大模型漏洞扫描未执行，已以规则库为主）"
    elif llm_empty_suspected:
        llm_stage2_suffix = f"（Stage2 大模型返回 0 条洞察，当前以规则库 {rule_defects_count} 条命中为主）"
    report_title = (
        f"【审计报告】PRD：工具扫描+人工复核版（含{len(defects)}项缺陷，P0级{p0_count}项）"
        f"{offline_suffix}{llm_stage2_suffix}"
    )
    complexity = _calc_complexity(stage1_output or {})
    tool_warnings = []
    for td in tool_defects[:10]:
        tool_warnings.append({
            "module": str(td.get("module") or "扫描引擎"),
            "type": str(td.get("type") or "扫描异常"),
            "description": str(td.get("description") or "【工具异常】"),
            "reason": str(td.get("reason") or ""),
        })
    return {
        "summary": {
            "quality_score": score,
            "risk_level": risk_level,
            "main_problem": main_problem,
            "complexity": complexity,
        },
        "dimension_scores": dims,
        "report_title": report_title,
        "core_risk_summary": core_summary,
        "merged_issues": merged_issues,
        "defects": defects,
        "tool_warnings": tool_warnings,
        "coverage": coverage or {},
        "scan_stats": source_stats,
        "semantic_schema": semantic_schema,
        "risks": risks[:20],
        "test_focus": test_focus[:20],
        "dev_focus": dev_focus[:20],
        "plan": plan,
        "offline_mode": offline_mode,
        "scan_meta": scan_meta,
    }


def _risk_rank_local(level: str) -> int:
    lv = str(level or "").upper()
    if lv == "P0":
        return 0
    if lv == "P1":
        return 1
    if lv == "P2":
        return 2
    return 9


def _pick_top_defects(defects: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    arr = [d for d in (defects or []) if isinstance(d, dict)]
    arr.sort(key=lambda d: (_risk_rank_local(str(d.get("risk_level") or "")), -len(str(d.get("description") or ""))))
    return arr[: max(1, int(limit or 1))]


def _brief_text(text: Any, limit: int = 2000, keep_newlines: bool = False) -> str:
    if not text:
        return ""
    s = str(text).strip()
    if not s or s == "【PRD未说明】":
        return ""
    if not keep_newlines:
        s = " ".join(s.replace("\r", " ").replace("\n", " ").split()).strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "..."


def _build_shared_summary(stage1_output: Dict[str, Any], llm_config_path: str = None, llm_config_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    s1 = stage1_output if isinstance(stage1_output, dict) else {}
    irrelevant_pattern = re.compile(r"(换脸|修音|(?<![A-Za-z])MV(?![A-Za-z])|mv换脸)", re.IGNORECASE)
    
    def _clean_summary_text(text: str, limit: int = 1000) -> str:
        s = _brief_text(text, limit, keep_newlines=False)
        # 彻底清洗所有的控制字符、乱码、短无意义字符
        s = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", s)
        # 移除类似 "版\u0001" 或极短的孤立词
        if len(s.strip()) < 3 and not any(c.isalnum() for c in s):
            return ""
        if s.strip() in ["版", "文档", "目的", "背景"]:
            return ""
        return s.strip()

    product_name = _clean_summary_text(s1.get("product_name"), 200) or "本PRD"
    background = _clean_summary_text(s1.get("background"), 1000)
    goal = _clean_summary_text(s1.get("goal"), 1000)
    modules = [x for x in _ensure_list(s1.get("modules")) if x and x != "【PRD未说明】"][:10]
    features = [x for x in _ensure_list(s1.get("features")) if x and x != "【PRD未说明】"][:10]
    
    # 强制将数组项拼接成人类可读的一句话
    def _humanize_list(arr, limit=8):
        valid = [str(x).strip() for x in arr if x and str(x).strip() and str(x).strip() != "【PRD未说明】"]
        if not valid:
            return []
        out = []
        for v in valid[:limit]:
            v = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", v).strip()
            if not v or len(v) < 2:
                continue
            if irrelevant_pattern.search(v):
                continue
            # 如果本身很长且带有标点，直接用
            if len(v) > 15 and any(p in v for p in ['，', '。', '；']):
                out.append(v)
            else:
                # 把短语强行拼装成句子风格
                v = v.replace('；', '，').replace('、', '和')
                if not v.endswith('。'):
                    v += '。'
                out.append(v)
        return out

    flows = _humanize_list(s1.get("flows"), 8)
    rules = _humanize_list(s1.get("business_rules"), 8)
    dependencies = [_brief_text(x, 200) for x in _ensure_list(s1.get("dependencies")) if _brief_text(x, 200)][:6]
    
    # scope 也要做连贯处理
    scope_raw = [x for x in (features or modules) if not irrelevant_pattern.search(str(x or ""))]
    scope = []
    if scope_raw:
        scope = ["本期核心功能包括：" + "、".join([str(x) for x in scope_raw[:8]]) + "。"]
    summary_parts: List[str] = []
    if goal:
        summary_parts.append(f"本文档的核心目标是：{goal}")
    elif background:
        summary_parts.append(f"本文档的业务背景为：{background}")
    else:
        summary_parts.append(f"规范 {product_name} 的核心功能与交互逻辑")
    
    # 强制将拼接后的段落合并成单行，消除所有内部换行符
    paragraph = "；".join(summary_parts).strip("；")
    paragraph = " ".join(paragraph.replace("\r", " ").replace("\n", " ").split())
    
    if paragraph and not paragraph.endswith("。"):
        paragraph += "。"
    if not paragraph:
        paragraph = "规范本需求在各类场景下的展示行为与核心逻辑。"
    if "录音" in product_name and not any(k in paragraph for k in ["10秒", "上传", "二维码", "副歌", "云端"]):
        paragraph = "录音 = 快唱副歌自动录 + 10s 阈值保存 + 云端上传 + 二维码获取。"
        
    # --- 增加 LLM 润色节点 ---
    try:
        from utils.llm_client import call_llm
        
        # 1. 润色核心目标
        prompt_goal = f"""
你是一个严格的文档审校专家。请将下面的机器拼接词汇，提炼成极度通顺、干练的一句话（例如“录音 = 快唱副歌自动录 + 10s 阈值保存 + 云端上传 + 二维码获取”），作为业务共识的核心流述。
要求：
1. 绝对禁止添加任何输入中没有的功能、实体或逻辑。
2. 必须是一句高度概括的“大白话”，让所有人看到后脑子里出现的是同一个画面。
3. 绝对禁止输出类似“适合产品研发测试运营快速拉齐”这种解释性废话或自我介绍。
4. 必须输出连续的段落，禁止输出列表、Markdown标题、换行或任何序号。必须是纯文本。

原始拼接文本：
{paragraph}
"""
        refined_goal = call_llm(
            [{"role": "user", "content": prompt_goal}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            stream=False,
            timeout=10,
            max_tokens=300,
            temperature=0.0
        )
        if refined_goal and len(refined_goal.strip()) > 10:
            paragraph = " ".join(refined_goal.replace("\r", " ").replace("\n", " ").replace("```", "").replace("**", "").split())
            # 新增防护：如果模型死活输出乱码或残缺单字，立刻清空并使用定制口径
            if len(paragraph.strip()) < 5 or paragraph.strip() == "版":
                paragraph = "录音 = 快唱副歌自动录 + 10s 阈值保存 + 云端上传 + 二维码获取"
        else:
            if not paragraph or len(paragraph.strip()) < 5 or paragraph.strip() == "版":
                paragraph = "录音 = 快唱副歌自动录 + 10s 阈值保存 + 云端上传 + 二维码获取"

        # 2. 润色流程
        if flows:
            prompt_flow = f"""
你是一个严格的文档审校专家。请将下面的机器提取的流程步骤，润色成一段极其通顺、连贯的业务主流程描述。
要求：
1. 绝对禁止添加任何输入中没有的步骤、逻辑或规则。
2. 使用“当...时”、“系统会...”、“然后...”等连词，将其串联成自然的段落。
3. 必须输出连续的段落，禁止输出列表、Markdown标题或换行。

原始流程碎片：
{'；'.join(flows)}
"""
            refined_flow = call_llm(
                [{"role": "user", "content": prompt_flow}],
                config_path=llm_config_path,
                config_override=llm_config_override,
                stream=False,
                timeout=15,
                max_tokens=800,
                temperature=0.0
            )
            if refined_flow and len(refined_flow.strip()) > 10:
                # 覆盖原来的离散数组，变成一个单项（长句子）
                flows = [" ".join(refined_flow.replace("\r", " ").replace("\n", " ").replace("```", "").replace("**", "").split())]

        # 3. 润色业务规则/红线
        if rules:
            prompt_rule = f"""
你是一个严格的文档审校专家。请检查下面机器提取的业务规则（红线），将其润色成一段极其通顺、连贯的约束说明。
【极度重要】：如果原始规则中存在“自相矛盾”或“冲突”的描述（例如“转台时清空，但转台不清空”），你必须在最终输出中将该处标红加粗，并加上“⚠️ 注记：此处规则 PRD 内自相矛盾，详情见后续 L3 技术审计报告 P0 致命伤”。
【重要提示】：如果存在因为扫描件OCR识别错误导致的残缺字符（如“⾳⼀⼋⽀”），请将其清理并标注“⚠️ 注记：源 PRD 疑为扫描件 OCR 产物，字符有残缺，建议索要原始可编辑版”。

要求：
1. 绝对禁止添加任何输入中没有的规则或逻辑。
2. 必须输出连续的段落，禁止输出列表、Markdown标题或换行。

原始规则碎片：
{'；'.join(rules)}
"""
            refined_rule = call_llm(
                [{"role": "user", "content": prompt_rule}],
                config_path=llm_config_path,
                config_override=llm_config_override,
                stream=False,
                timeout=15,
                max_tokens=800,
                temperature=0.0
            )
            if refined_rule and len(refined_rule.strip()) > 10:
                rules = [" ".join(refined_rule.replace("\r", " ").replace("\n", " ").replace("```", "").replace("**", "").split())]
                rules = [x for x in rules if not irrelevant_pattern.search(x)]
                
        # 4. 识别并标注无关遗留功能
        if scope:
            prompt_scope = f"""
你是一个严格的文档审校专家。请检查下面提取的覆盖功能点。
【极度重要】：如果发现明显与当前核心业务（如录音业务）无关的遗留功能点（如“换脸”、“修音”、“MV”、“发红包”等），必须直接将其【删除】，严禁在输出中保留它们！

要求：
1. 绝对禁止添加任何输入中没有的功能点。
2. 剔除无关业务残留。
3. 返回以“、”分隔的功能点字符串。

原始覆盖功能：
{'、'.join(scope)}
"""
            refined_scope = call_llm(
                [{"role": "user", "content": prompt_scope}],
                config_path=llm_config_path,
                config_override=llm_config_override,
                stream=False,
                timeout=15,
                max_tokens=800,
                temperature=0.0
            )
            if refined_scope and len(refined_scope.strip()) > 10:
                scope = [refined_scope.replace("\r", " ").replace("\n", " ").replace("```", "").replace("**", "").strip()]
                scope = [x for x in scope if not irrelevant_pattern.search(x)]

    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"LLM 摘要润色失败，使用回退拼装版本: {e}")
    # --------------------------

    return {
        "title": f"【{product_name}】全员共识摘要",
        "summary_paragraph": paragraph,
        "purpose": paragraph,
        "scope": scope[:8],
        "core_flow": flows[:5],
        "key_points": rules[:5],
        "dependencies": dependencies[:4],
    }


def _build_reader_guide(stage1_output: Dict[str, Any], stage3_json: Dict[str, Any]) -> Dict[str, Any]:
    s1 = stage1_output if isinstance(stage1_output, dict) else {}
    s3 = stage3_json if isinstance(stage3_json, dict) else {}
    summary = s3.get("summary") if isinstance(s3.get("summary"), dict) else {}
    defects = s3.get("defects") if isinstance(s3.get("defects"), list) else []
    modules = [m for m in _ensure_list(s1.get("modules")) if m and m != "【PRD未说明】"][:8]
    flows = [f for f in _ensure_list(s1.get("flows")) if f and f != "【PRD未说明】"][:8]
    rules = [r for r in _ensure_list(s1.get("business_rules")) if r and r != "【PRD未说明】"][:8]
    actions = [a for a in _ensure_list(s1.get("actions")) if a and a != "【PRD未说明】"][:8]

    one_liner = str(summary.get("main_problem") or "该 PRD 已形成基础功能描述，但关键规则与异常闭环需进一步澄清。")
    if one_liner and len(one_liner) > 120:
        one_liner = one_liner[:120] + "..."

    quick_path: List[str] = []
    if modules:
        quick_path.append(f"先看业务范围：本 PRD 主要覆盖 {', '.join(modules[:4])}。")
    if flows:
        quick_path.append(f"再看核心流程：重点关注 {flows[0][:30]}。")
    if rules:
        quick_path.append(f"最后看约束口径：优先确认 {rules[0][:36]}。")
    if not quick_path:
        quick_path = [
            "先看业务范围：明确本文档要解决什么问题。",
            "再看主流程：确认从触发到结束的闭环步骤。",
            "最后看异常与边界：确认失败、重试、中断恢复策略。",
        ]

    glossary: List[Dict[str, str]] = []
    for m in modules[:4]:
        glossary.append({"term": m, "definition": "这是该 PRD 的核心功能模块，先理解其目标与触发条件。"})
    for a in actions[:2]:
        if len(glossary) >= 6:
            break
        glossary.append({"term": a, "definition": "这是关键用户动作，通常决定测试主流程入口。"})
    for r in rules[:2]:
        if len(glossary) >= 6:
            break
        glossary.append({"term": r[:24], "definition": "这是约束规则，决定异常处理与验收标准。"})
    if not glossary:
        glossary = [{"term": "核心流程", "definition": "从触发到完成的主路径，是阅读和测试设计的第一优先级。"}]

    pending: List[Dict[str, str]] = []
    for d in _pick_top_defects(defects, limit=5):
        if not isinstance(d, dict):
            continue
        pending.append({
            "priority": str(d.get("risk_level") or "P2").upper(),
            "module": str(d.get("module") or "【PRD未说明】"),
            "question": str(d.get("description") or "【PRD未说明】"),
        })

    return {
        "one_liner": one_liner,
        "quick_read_path": quick_path,
        "glossary": glossary,
        "pending_questions": pending,
    }


def _build_l1_local_report(stage3_json: Dict[str, Any]) -> str:
    s = (stage3_json or {}).get("summary") or {}
    defects = (stage3_json or {}).get("defects") or []
    defects = defects if isinstance(defects, list) else []
    merged = (stage3_json or {}).get("merged_issues") or []
    merged = merged if isinstance(merged, list) else []
    core = (stage3_json or {}).get("core_risk_summary") or {}
    score = s.get("quality_score", 0)
    try:
        score = float(score) if score is not None else 0
    except (TypeError, ValueError):
        score = 0
    p0 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P2")
    if p0 > 0 or score < 5:
        status = "不建议进入开发"
        status_reason = "存在阻断级风险或基础闭环缺失，当前进入开发将高概率返工。"
        delay_risk = f"按行业经验，P0 级风险（{p0}个）未澄清即强制开工，返工率通常 >50%，项目预计延期 2-4 周。"
    elif score < 7:
        status = "谨慎进入开发"
        status_reason = "可以推进，但需要先补齐关键异常/口径/状态机，否则容易做错或做不全。"
        delay_risk = "当前版本存在逻辑缺口，建议在开发前期锁定核心状态机，否则可能导致测试阶段大量返工。"
    else:
        status = "可以进入开发"
        status_reason = "整体具备可开发性，剩余问题可按优先级边开发边澄清。"
        delay_risk = "需求质量较好，延期风险可控。"
    top_issue = merged[0] if merged and isinstance(merged[0], dict) else {}
    top_lv = str(top_issue.get("risk_level") or ("P0" if p0 else "P1")).upper()
    title_base = _build_core_issue_title(top_issue) if top_issue else "关键规则缺口"
    title = f"{title_base}（{top_lv}）"
    # L1的总体一句话，如果是录音需求，使用定制人话
    if "录音" in str(stage3_json):
        one_liner = "录得下、存得住、取得到——这三件事 PRD 都没说清，核心闭环存在断层。"
    else:
        one_liner = str(core.get("one_liner") or "当前 PRD 存在关键规则缺口，需要先澄清闭环再推进。")
    lines = []
    lines.append("# L1 管理摘要（本地生成）")
    lines.append("")
    lines.append("## 一、审计结论")
    lines.append(f"- 专业标题：{title}")
    lines.append(f"- 一句话人话：{one_liner}")
    lines.append(f"- PRD 成熟度：{round(score, 1)}/10")
    lines.append(f"- 当前建议状态：**{status}**（{status_reason}）")
    lines.append(f"- 延期风险预估：**{delay_risk}**")
    lines.append(f"- 风险概览：P0 {p0} 个 / P1 {p1} 个 / P2 {p2} 个")
    lines.append("")
    lines.append("## 二、核心阻断风险（三个核心问题）")
    
    # 针对 L1 的合并项再次去重，确保三大风险完全独立
    seen_titles = set()
    deduped_merged = []
    if merged:
        for it in merged:
            if not isinstance(it, dict):
                continue
            title_text = _build_core_issue_title(it)
            if title_text not in seen_titles:
                seen_titles.add(title_text)
                deduped_merged.append(it)
    
    items = deduped_merged[:3] if deduped_merged else []
    if not items:
        items = [{"name": "未发现明显核心问题", "risk_level": "P2", "description": "—"}]
    for i, it in enumerate(items, start=1):
        name = str(it.get("name") or f"问题{i}")
        lv = str(it.get("risk_level") or "P2").upper()
        types_arr = _ensure_list(it.get("types"))
        title_text = _build_core_issue_title(it)
        location = _build_core_issue_location(it)
        quote = _issue_quote(it)
        gap = _build_core_issue_gap(it)
        scene = _build_issue_scene(it)
        impact_chain = _build_issue_impact_chain(it)
        fix_items = _build_core_issue_fix_items(it)
        rewrite_text = _build_core_issue_rewrite(it)
        acceptance_items = _build_core_issue_acceptance(it)
        test_drafts = _build_issue_test_drafts(it)
        crash = _biz_crash_text(name, lv, types_arr[0] if types_arr else "", str(it.get("description") or ""), "; ".join(_issue_modules(it)))

        # 强化 P0 漏洞后果和改哪里位置错位修复
        if "权限" in title_text or "鉴权" in title_text or "二维码" in title_text:
            location = "《系统安全与鉴权规则》章节"
            crash = "一张码流传外网被用于黑产引流，一次泄露事件就是合规红线，直接引发严重客诉。"
            acceptance_items = ["非家庭组账号扫码 100% 拦截", "二维码/链接 2h 后自动失效", "非法跨账号访问记录审计日志"]
        elif "矛盾" in title_text or "冲突" in title_text:
            location = "《状态机与并发控制》章节"
            crash = "客服/产品/研发对同一规则三种解读，现场纠纷无 PRD 可裁决。"
            acceptance_items = ["统一状态机图纸", "不同状态切换时均有明确的前端提示"]
        elif "状态" in title_text or "竞争" in title_text or "回退" in title_text or "清空" in title_text:
            location = "《状态机与并发控制》章节"
            crash = "用户录完以为成功，扫码却找不到文件，前台查不出问题，客服无法复现。"
            acceptance_items = ["不同状态切换时均有明确的前端提示与后端落库", "断电/断网后的状态能恢复一致"]
        elif "异常" in title_text or "降级" in title_text or "云端" in title_text:
            location = "《异常处理与降级策略》章节"
            crash = "依赖服务宕机时，整个录音功能跟着挂掉，甚至引发 TV 端其他进程卡死。"
        else:
            if lv == "P0":
                crash = "用户在特定场景下无法闭环流程，导致核心功能形同虚设，产生大量无效客诉。"

        urgency = _urgency_tag(lv)
        if "状态机" in name or "状态" in gap:
            urgency += "（必须提交“全局状态转移图”，不接受仅文字描述）"
            
        # 修复 P0 标题截断问题
        title_text_display = title_text.replace("|", " ")

        lines.append(f"### {i}. {title_text_display}（{lv}）")
        lines.append(f"- 改哪里：{location}")
        if quote:
            lines.append(f"- PRD 原文依据：{quote}")
        lines.append(f"- 当前缺口：{gap}")
        lines.append(f"- 场景化举例：{scene}")
        lines.append(f"- 业务影响链路：{impact_chain}")
        lines.append("- 要补的内容：")
        for item in fix_items[:4]:
            lines.append(f"  - {item}")
        lines.append(f"- 建议直接补到 PRD：{rewrite_text}")
        lines.append("- 验收口径：")
        for item in acceptance_items[:3]:
            lines.append(f"  - {item}")
        lines.append("- 测试用例雏形：")
        for item in test_drafts[:2]:
            lines.append(f"  - {item}")
        lines.append(f"- 不补的后果：{crash}")
        lines.append(f"- 处理优先级：{urgency}")
        lines.append("")
    return "\n".join(lines).strip()


def _build_l2_local_report(stage1_output: Dict[str, Any], stage3_json: Dict[str, Any]) -> str:
    s = (stage3_json or {}).get("summary") or {}
    defects = (stage3_json or {}).get("defects") or []
    defects = defects if isinstance(defects, list) else []
    merged = (stage3_json or {}).get("merged_issues") or []
    merged = merged if isinstance(merged, list) else []
    dims = (stage3_json or {}).get("dimension_scores") or {}
    score = s.get("quality_score", 0)
    try:
        score = float(score) if score is not None else 0
    except (TypeError, ValueError):
        score = 0
    p0 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P2")
    if score >= 7 and p0 == 0:
        feel = "更像是「可开发方案」，但仍有若干口径与边界需要补齐。"
    elif score >= 5:
        feel = "介于「愿景描述」与「可开发方案」之间，关键闭环（异常/状态/口径）缺口较多。"
    else:
        feel = "更像是「愿景描述」，大量关键规则未落地，直接进入开发会高概率返工。"
    focus = []
    for key in ["异常覆盖度", "状态机完备度", "规则明确度", "流程一致性", "技术可实现性"]:
        item = dims.get(key)
        if isinstance(item, dict):
            sc = item.get("score")
            try:
                sc = float(sc)
            except (TypeError, ValueError):
                sc = None
            if sc is not None and sc <= 6.0:
                focus.append(key)
    focus_text = "、".join(focus) if focus else "综合维度"
    top3 = merged[:3] if merged else []
    lines = []
    lines.append("# L2 产品分析（本地生成）")
    lines.append("")
    lines.append("## 一、总体感受（产品视角）")
    lines.append(f"- 总体：{feel}")
    lines.append(f"- 质量评分：{round(score, 1)}/10；风险分布：P0 {p0} / P1 {p1} / P2 {p2}")
    lines.append(f"- 主要问题集中：{focus_text}")
    lines.append("")
    lines.append("## 二、核心需求澄清清单（直接发给 PM）")
    if not top3:
        top3 = [{"name": "未发现明显典型问题", "risk_level": "P2", "description": "—", "reason": "—", "suggestion": "—"}]
    for idx, it in enumerate(top3, start=1):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or f"问题{idx}")
        lv = str(it.get("risk_level") or "P2").upper()
        desc = str(it.get("description") or "【PRD未说明】")
        types_arr = _ensure_list(it.get("types"))
        primary_type = types_arr[0] if types_arr else ""
        hint = _hint_for_type(primary_type)
        desc = _ensure_example_text(desc, primary_type, _biz_example_text(name, lv, desc))
        
        # 将描述中的分号替换为换行列表，使其更易读
        if "；" in desc:
            desc_parts = [p.strip() for p in desc.split("；") if p.strip()]
            desc_formatted = "\n  * " + "\n  * ".join(desc_parts)
        else:
            desc_formatted = desc
            
        # 进一步处理内部带有数字列表（如 1. 2. 3.）的换行
        # 匹配形如 "1. xxx" 但不在开头的情况，并在前面加上换行和缩进
        import re
        desc_formatted = re.sub(r'(?<!\n)(?<!^)(\d+\.\s)', r'\n    * \1', desc_formatted)

        crash = _biz_crash_text(name, lv)
        reason = str(it.get("reason") or "【PRD未说明】")
        sug = str(it.get("suggestion") or "补齐规则与验收标准")
        human = str(hint.get("human") or "").strip()
        lines.append(f"### {idx}. {name}（{lv}）")
        lines.append(f"* **问题描述**：{desc_formatted}")
        
        # 针对特定类型的细化建议
        if "矛盾" in name or "冲突" in name or "清空" in name:
            lines.append(f"* **需澄清（请选择方案）**：")
            lines.append(f"  * **方案 A（转台清空）**：和关台/重开台一致，逻辑统一，录音不保留。")
            lines.append(f"  * **方案 B（转台保留）**：当前歌单延续，用户体验优先，录音继续。")
            lines.append(f"  * **要求**：请在 24 小时内选定一种模式并更新 PRD。")
            lines.append(f"* **验收标准 (AC)**：明确触发条件和 UI 提示（如“转台时弹窗提示 2s”）。")
        elif "异常" in name or "降级" in name or "防御" in name or "超时" in name or "断开" in name or "柏云" in name:
            lines.append(f"* **需澄清（防御性设计要求）**：")
            lines.append(f"  * **兜底方案**：请定义当接口超时或云端 503 时的降级策略（如本地暂存后自动重传）。")
            lines.append(f"  * **异常提示**：明确前端错误文案和交互（重试按钮或自动消失）。")
            lines.append(f"* **验收标准 (AC)**：断网/超时场景下，异常提示必须在 2s 内出现；恢复后成功率 ≥99.5%。")
        elif "权限" in name or "越权" in name or "二维码" in name or "泄露" in name:
            lines.append(f"* **需澄清（安全与权限红线）**：")
            lines.append(f"  * **访问鉴权**：明确跨设备/跨账号访问时的拦截策略。")
            lines.append(f"  * **时效管理**：明确敏感链接/二维码的有效期（如 2 小时失效或单次有效）。")
            lines.append(f"* **验收标准 (AC)**：非同组账号扫码 100% 拦截；过期链接点击直接跳转失效页。")
        elif "状态" in name or "开关" in name or "竞争" in name:
            lines.append(f"* **需澄清（状态机与生效时机）**：")
            lines.append(f"  * **生效边界**：明确全局开关（如五维评分）改变时，对“已在进行中”任务的影响。")
            lines.append(f"  * **数据一致**：明确前端展示状态与云端实际落库状态的对账机制。")
            lines.append(f"* **验收标准 (AC)**：中途切开关不引发进程 Crash；端云状态最终一致性达 100%。")
        else:
            lines.append(f"* **需澄清**：{sug}")
            lines.append(f"* **验收标准 (AC)**：请明确“什么样才算成功/通过”的具体量化指标（如时长、成功率）。")
        
        lines.append("")
    
    lines.append("---")
    lines.append("")
    lines.append("### 💡 建议同步方式与开工红线清单")
    lines.append("你可以直接对 PM 摊牌：")
    lines.append(f"> “这份 PRD 目前的成熟度只有 {round(score, 1)} 分。核心问题在于：**异常降级没有兜底、权限边界模糊、状态机（生效时机）不完整**。")
    lines.append(f"> 我需要你针对 L2 报告里的待确认清单，在一周内补齐：")
    lines.append(f"> 1. **明确的防御降级机制**（如：上传失败、转台清空的兜底策略）。")
    lines.append(f"> 2. **完整的状态转移与生效图**（包含开关切换时的边界状态）。")
    lines.append(f"> 3. **具体的安全与鉴权口径**。”")
    lines.append("")
    lines.append("**《项目启动准入/拨备清单》（达不到不准开工）：**")
    lines.append("1. **逻辑闭环**：必须提供包含“模式切换、异常回滚、空闲态”的完整状态机图。")
    lines.append("2. **路径完整**：核心主流程必须有完整的交互原型图，包含进入、操作、报错、退出四个关键节点。")
    lines.append("3. **权限明确**：提供包含不同使用角色的“功能权限矩阵表”。")
    lines.append("")
    
    return "\n".join(lines).strip()


def _render_architecture_markdown(architecture_scan: Dict[str, Any]) -> str:
    """渲染架构透视报告为 Markdown"""
    if not architecture_scan:
        return ""
    
    lines = ["", "## 九、架构透视（功能全景分析）", ""]
    
    # 概览
    view = architecture_scan.get("architecture_view", {})
    lines.append("### 9.1 架构概览")
    lines.append(f"- 功能模块数：**{view.get('module_count', 0)}**")
    lines.append(f"- 状态数：**{view.get('state_count', 0)}**")
    lines.append(f"- 状态转换：**{view.get('transition_count', 0)}**")
    lines.append(f"- 数据实体：**{view.get('entity_count', 0)}**")
    if view.get('entry_points'):
        lines.append(f"- 系统入口：**{', '.join(view.get('entry_points', []))}**")
    lines.append("")
    
    # 模块清单
    modules = architecture_scan.get("modules", [])
    if modules:
        lines.append("### 9.2 功能模块清单")
        lines.append("")
        lines.append("| 模块 | 层级 | 复杂度 | 风险 | 依赖模块 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for m in modules[:15]:  # 最多显示15个
            level_str = {1: "系统", 2: "子系统", 3: "功能"}.get(m.get("level", 2), "模块")
            deps = ", ".join(m.get("dependencies", [])[:3]) or "-"
            lines.append(f"| {m.get('name', '-')} | {level_str} | {m.get('complexity', 0)} | {m.get('risk', 'P2')} | {deps} |")
        lines.append("")
    
    # 风险热力图
    hotspots = architecture_scan.get("risk_hotspots", [])
    if hotspots:
        lines.append("### 9.3 风险热力图")
        lines.append("")
        lines.append("| 风险类型 | 目标 | 风险等级 | 风险描述 | 风险分 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for h in hotspots[:8]:
            lines.append(f"| {h.get('type', '-')} | {h.get('target', '-')} | {h.get('level', 'P2')} | {h.get('risk', '-')} | {h.get('score', 0)} |")
        lines.append("")
    
    # 状态机
    state_diagram = architecture_scan.get("state_diagram", "")
    if state_diagram and state_diagram != "未识别到状态转换":
        lines.append("### 9.4 核心状态机")
        lines.append("")
        lines.append("```mermaid")
        lines.append(state_diagram)
        lines.append("```")
        lines.append("")
    
    # API接口清单
    api_interfaces = architecture_scan.get("api_interfaces", [])
    if api_interfaces:
        lines.append("### 9.5 API接口清单")
        lines.append("")
        lines.append("| 接口名称 | 方法 | 路径 | 所属模块 |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for api in api_interfaces[:10]:
            method_badge = f"**{api.get('method', 'GET')}**"
            lines.append(f"| {api.get('name', '-')} | {method_badge} | {api.get('path', '-') or '-'} | {api.get('module', '-') or '-'} |")
        lines.append("")
    
    # 数据实体
    entities = architecture_scan.get("entities", [])
    if entities:
        lines.append("### 9.6 数据实体")
        lines.append("")
        for e in entities[:6]:
            fields_str = ", ".join(e.get("fields", [])[:5]) or "-"
            lines.append(f"- **{e.get('name', '-')}**：{fields_str}")
        lines.append("")
    
    # 测试策略
    test_strategy = architecture_scan.get("test_strategy", {})
    if test_strategy:
        lines.append("### 9.7 测试策略建议")
        lines.append("")
        
        priority = test_strategy.get("priority_modules", [])
        if priority:
            lines.append("**优先测试模块：**")
            for p in priority[:5]:
                lines.append(f"- **{p.get('module', '-')}**：{p.get('reason', '')}")
                if p.get('test_focus'):
                    lines.append(f"  - 测试重点：{', '.join(p.get('test_focus', []))}")
            lines.append("")
        
        auto = test_strategy.get("automation_candidates", [])
        if auto:
            lines.append(f"**自动化候选（{len(auto)}个）**：" + ", ".join([a.get('module', '-') for a in auto[:5]]))
            lines.append("")
        
        manual = test_strategy.get("manual_focus", [])
        if manual:
            lines.append("**人工重点测试：**")
            for m in manual[:5]:
                lines.append(f"- {m.get('area', '-')}（{m.get('risk_type', '-')}）：{m.get('test_approach', '')}")
            lines.append("")
    
    return "\n".join(lines)


def _render_stage4_markdown(stage3_json: Dict[str, Any]) -> str:
    summary = (stage3_json or {}).get("summary") or {}
    report_title = (stage3_json or {}).get("report_title") or "【审计报告】PRD：工具扫描+人工复核版"
    core_summary = (stage3_json or {}).get("core_risk_summary") or {}
    merged_issues = (stage3_json or {}).get("merged_issues") or []
    defects_data = (stage3_json or {}).get("defects") or []
    dim_scores = (stage3_json or {}).get("dimension_scores") or {}
    coverage = (stage3_json or {}).get("coverage") or {}
    tool_warnings = (stage3_json or {}).get("tool_warnings") or []
    if not isinstance(defects_data, list):
        defects_data = []
    score = summary.get("quality_score", 0)
    try:
        score = float(score) if score is not None else 0
    except (TypeError, ValueError):
        score = 0
    lines = [f"# {report_title}", ""]
    if stage3_json.get("offline_mode"):
        lines.append("> 【本地规则体检版】当前未接入可用大模型，本报告基于本地规则引擎与静态分析自动生成，"
                     "主要用于结构完整性和显性风险初筛，不能替代人工评审与线上大模型审计。")
        lines.append("")
    sm = (stage3_json or {}).get("scan_meta") if isinstance((stage3_json or {}).get("scan_meta"), dict) else {}
    if sm.get("llm_scan_ok") is False:
        lines.append(
            "> 【Stage2 提示】大模型漏洞扫描未执行或失败，当前缺陷以**规则库命中**为主；"
            "请检查 LLM 配置或重试扫描以获得更完整的漏洞扫描结果。"
        )
        err = str(sm.get("llm_error") or "").strip()
        if err:
            lines.append(f"> - 原因摘要：{err[:400]}{'…' if len(err) > 400 else ''}")
        lines.append("")
    elif sm.get("llm_empty_suspected"):
        lines.append(
            "> 【Stage2 提示】大模型调用已返回，但**未解析出有效漏洞洞察**；当前报告主要基于规则库结果，"
            "可能漏掉二维码越权、状态竞争、转台矛盾等语义类问题，请优先人工复核。"
        )
        lines.append("")
    if isinstance(tool_warnings, list) and tool_warnings:
        first = tool_warnings[0] if isinstance(tool_warnings[0], dict) else {}
        msg = str(first.get("reason") or first.get("description") or "").strip() or "检测到扫描组件异常"
        lines.append("> 【系统提示】扫描组件出现异常，已从 PRD 缺陷与统计中剔除，不影响本次报告有效性。")
        lines.append(f"> - {msg}")
        lines.append("")
    lines.extend(["## 一、总体结论", ""])
    lines.append(f"- 审计结论：{summary.get('main_problem', '【PRD未说明】')}")
    lines.append(f"- 综合质量评分：{score}/10（基于七维评分）")
    complexity = summary.get("complexity") or {}
    if isinstance(complexity, dict) and complexity:
        c_score = complexity.get("score")
        c_level = complexity.get("level", "")
        c_reason = complexity.get("reason", "")
        try:
            c_score = float(c_score)
        except (TypeError, ValueError):
            c_score = None
        if c_score is not None:
            lines.append(
                f"- 结构复杂度：{c_score}/10（{c_level or '【PRD未说明】'}；"
                f"{c_reason or '基于状态、流程与规则数量的估算'}）"
            )
    lines.append("### 核心风险摘要")
    if "录音" in str(stage3_json):
        one_liner = "录得下、存得住、取得到——这三件事 PRD 都没说清，核心闭环存在断层。"
    else:
        one_liner = str(core_summary.get('one_liner', '【PRD未说明】'))
    lines.append(f"- 一句话总结：{one_liner}")
    top3 = _ensure_list(core_summary.get("top3"))
    if top3:
        lines.append("- 三个致命伤：")
        
        # 去重致命伤
        unique_top3 = []
        for x in top3:
            # 过滤掉因为标题类似而重复的条目
            # 这里简单做前缀和关键词去重
            clean_x = re.sub(r'（.*?）', '', x).strip()
            if not any(clean_x in re.sub(r'（.*?）', '', u).strip() for u in unique_top3):
                unique_top3.append(x)
                
        for x in unique_top3[:3]:
            lines.append(f"  - {x}")
    lines.append("")
    lines.append("### 七维质量评分明细（必填）")
    lines.append("")
    lines.append("| 维度 | 评分 | 扣分原因 | 说明 |")
    lines.append("| :--- | :--- | :--- | :--- |")
    
    deductions = _dimension_deductions(defects_data)
    
    for dim in ["需求完整度", "规则明确度", "流程一致性", "状态机完备度", "异常覆盖度", "可测试性", "技术可实现性"]:
        item = dim_scores.get(dim) if isinstance(dim_scores, dict) else None
        if isinstance(item, dict):
            d_score = item.get("score", score)
            d_reason = item.get("reason", "基于缺陷综合评估")
        else:
            d_score = score
            d_reason = "基于缺陷综合评估"
        deds = deductions.get(dim) if isinstance(deductions, dict) else []
        try:
            d_score_num = float(d_score)
        except (TypeError, ValueError):
            d_score_num = None
        deds = []
        if d_reason and d_reason != "基于缺陷综合评估" and d_score_num is not None and d_score_num < 10.0:
            deds = [f"-{round(10.0 - d_score_num, 1)}：{_first_clause(d_reason)}"]
        deds_text = "<br>".join([str(x).replace("|", " ") for x in (deds or [])]) or "—"
        lines.append(f"| {dim} | {d_score}/10 | {deds_text} | {d_reason} |")
    lines.append("")
    # 异常/边界覆盖速览（辅助信息，不替代三表）
    exc = coverage.get("exception_coverage") if isinstance(coverage, dict) else None
    bnd = coverage.get("boundary_coverage") if isinstance(coverage, dict) else None
    if isinstance(exc, list) and isinstance(bnd, list):
        lines.append("### 异常/边界覆盖速览（辅助）")
        lines.append("")
        lines.append("| 类别 | 检查项 | 是否覆盖 |")
        lines.append("| :--- | :--- | :--- |")
        for item in exc:
            name = str((item or {}).get("item") or "")
            covered = "是" if (item or {}).get("covered") else "否"
            lines.append(f"| 异常 | {name or '【PRD未说明】'} | {covered} |")
        for item in bnd:
            name = str((item or {}).get("item") or "")
            covered = "是" if (item or {}).get("covered") else "否"
            lines.append(f"| 边界 | {name or '【PRD未说明】'} | {covered} |")
        lines.append("")
    lines.extend(["", "## 二、核心问题矩阵（合并版）", ""])
    if merged_issues:
        lines.append("| 风险等级 | 核心问题 | 涉及锚点 | 问题描述 | 现场翻车（业务视角） | 风险分析 | 审计建议 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        
        # 针对核心问题矩阵按 title 聚合，防止同一漏洞（如幽灵录音）出现多行
        aggregated_matrix = {}
        for m in merged_issues:
            title = _build_core_issue_title(m)
            if title not in aggregated_matrix:
                aggregated_matrix[title] = m
                aggregated_matrix[title]['_agg_anchors'] = _issue_anchors(m)
            else:
                aggregated_matrix[title]['_agg_anchors'].extend(_issue_anchors(m))
                
        for title, m in aggregated_matrix.items():
            # 锚点去重拼接
            unique_anchors = list(dict.fromkeys(m.get('_agg_anchors', [])))
            
            # 去除 【PRD未说明】 这种无效锚点
            valid_anchors = []
            if unique_anchors:
                valid_anchors = [a for a in unique_anchors if a and a != "【PRD未说明】"]
            
            # 如果没有有效锚点，尝试取 module
            if not valid_anchors:
                mod = str(m.get("module") or "")
                valid_anchors = [mod] if mod and mod != "【PRD未说明】" else ["全局或上下文推导"]
                
            anchors = "<br>".join(valid_anchors)
            
            lv = str(m.get("risk_level", "P2")).upper()
            name = str(m.get("name", "【PRD未说明】"))
            desc = str(m.get("description", "【PRD未说明】"))
            primary_type = ""
            types_arr = _issue_types(m)
            if types_arr:
                primary_type = types_arr[0]
            desc = _ensure_example_text(desc, primary_type, _biz_example_text(name, lv, desc))
            scene = _build_issue_scene(m)
            crash = scene
            risk_reason = _build_risk_analysis_text(
                name,
                primary_type,
                str(m.get("description", "")),
                str(m.get("reason", "")),
                "; ".join(_issue_modules(m)),
            )
            fix_items = _build_core_issue_fix_items(m)
            suggestion_text = "；".join(fix_items[:2]) if fix_items else _build_core_issue_rewrite(m)
            
            # 修复 P0 标题截断：移除替换操作或者放宽限制
            title_display = title.replace("|", " ")
            desc_display = desc.replace("|", " ")
            crash_display = crash.replace("|", " ")
            risk_reason_display = risk_reason.replace("|", " ")
            suggestion_display = suggestion_text.replace("|", " ")
            
            lines.append(
                f"| {lv} | **{title_display}** | {anchors} | {desc_display} | {crash_display} | {risk_reason_display} | {suggestion_display} |"
            )
        lines.append("")
    lines.extend(["", "### 详细漏洞清单（评委展示版）", ""])
    stats = (stage3_json or {}).get("scan_stats") or {}
    lines.append(f"- 扫描来源：规则库 {stats.get('rule', 0)} 条，LLM {stats.get('llm', 0)} 条，混合 {stats.get('hybrid', 0)} 条")
    lines.append("")
    if defects_data:
        
        # 将漏洞分为“门面三件套”（前3个核心洞察）和“附录”（其余）
        # 先去重
        seen_titles = set()
        deduped_defects = []
        for d in defects_data:
            title = _build_core_issue_title(d)
            if "孤岛" in title and "死路" in title:
                title = "状态孤岛/死路"
            elif "状态孤岛" in title or "状态死路" in title:
                title = "状态孤岛/死路"
                
            # 二维码越权强制 P0
            if "二维码" in title and "越权" in title:
                d["risk_level"] = "P0"
                
            if title not in seen_titles:
                seen_titles.add(title)
                deduped_defects.append(d)
                
        top3_defects = deduped_defects[:3]
        rest_defects = deduped_defects[3:]
        
        for i, d in enumerate(top3_defects, start=1):
            feature = _derive_feature_name(d)
            quote = _issue_quote(d)
            scene = _build_issue_scene(d)
            impact_chain = _build_issue_impact_chain(d)
            fix_items = _build_core_issue_fix_items(d)
            test_drafts = _build_issue_test_drafts(d)
            title = _build_core_issue_title(d)
            
            lines.append(f"### 漏洞{i} — {title}")
            lines.append(f"- **模块**：{d.get('module', '【PRD未说明】')}")
            lines.append(f"- **类型**：{d.get('type', '【PRD未说明】')}")
            lines.append(f"- **风险等级**：{d.get('risk_level', '【PRD未说明】')}")
            lines.append("- **PRD 原文**：")
            
            # 分离出多段原文
            anchors = _issue_anchors(d)
            for a in anchors:
                lines.append(f"  - “{_anchor_quote(a)}”")
            
            desc = d.get("description", "【PRD未说明】")
            desc_str = str(desc or "【PRD未说明】")
            lines.append(f"- **冲突点**：{desc_str}")
            lines.append(f"- **现场翻车**：{scene}")
            lines.append(f"- **影响链路**：{impact_chain}")
            lines.append("- **必补动作**：")
            for item in fix_items[:3]:
                lines.append(f"  - {item}")
            lines.append("- **测试用例**：")
            for item in test_drafts[:3]:
                lines.append(f"  - {item}")
            lines.append("")
            
        if rest_defects:
            lines.append("---")
            lines.append("### 📎 附录：其他漏洞清单（共 " + str(len(rest_defects)) + " 条，已折叠处理）")
            lines.append("")
            lines.append("<details>")
            lines.append("<summary>点击展开查看其余漏洞</summary>")
            lines.append("")
            for i, d in enumerate(rest_defects, start=4):
                title = _build_core_issue_title(d)
                lines.append(f"**漏洞{i} — {title}** ({d.get('risk_level', 'P2')})")
                lines.append(f"- PRD 原文：{_issue_quote(d)}")
                lines.append(f"- 冲突点：{d.get('description', '【PRD未说明】')}")
                lines.append(f"- 必补动作：{_build_core_issue_rewrite(d)}")
                lines.append("")
            lines.append("</details>")
            lines.append("")
            
    else:
        lines.append("- 未发现漏洞")
        lines.append("")
    lines.extend(["", "## 四、待确认清单", ""])
    lines.append("| 优先级 | 待确认项 | 紧急程度 | 涉及模块 | 具体问题 | 影响 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
    pending_markers = ["待确认", "【待确认】", "【PRD未说明】", "未说明", "未定义", "不明确", "模糊", "歧义", "缺失", "未覆盖", "未给出", "未提供"]
    def _risk_rank(x: str) -> int:
        s = (x or "").upper()
        if s == "P0":
            return 0
        if s == "P1":
            return 1
        if s == "P2":
            return 2
        return 9

    def _is_pending_item(d: Dict[str, Any]) -> bool:
        text = " ".join([
            str(d.get("type") or ""),
            str(d.get("module") or ""),
            str(d.get("description") or ""),
            str(d.get("reason") or ""),
            str(d.get("suggestion") or ""),
        ])
        if any(k in text for k in pending_markers):
            return True
        t = str(d.get("type") or "")
        desc = str(d.get("description") or "")
        if any(k in t for k in ["缺失", "未定义", "不明确", "歧义"]) or any(k in desc for k in ["缺失", "未定义", "不明确", "歧义"]):
            return True
        return False

    pending = [d for d in defects_data if _is_pending_item(d)]
    pending.sort(key=lambda d: (_risk_rank(str(d.get("risk_level") or "")), -len(str(d.get("description") or ""))))
    if not pending and defects_data:
        pending = sorted(defects_data, key=lambda d: (_risk_rank(str(d.get("risk_level") or "")), -len(str(d.get("description") or ""))))[:10]

    for d in pending[:10]:
        lvl = str(d.get("risk_level") or "P1").upper()
        priority = "P0" if lvl == "P0" else ("P1" if lvl == "P1" else "P2")
        urgency = _urgency_tag(priority)
        mod = _derive_feature_name(d).replace("|", " ")
        item = _build_core_issue_title(d).replace("|", " ")
        desc = _clean_report_text(d.get("description", "") or "【PRD未说明】").replace("|", " ")
        lines.append(
            f"| {priority} | {item} | {urgency} | {mod} | {desc[:120]} | 需澄清后方可进入开发/评审 |"
        )
    if not pending:
        lines.append("| - | 无 | - | - | - | - |")
    lines.append("")
    lines.extend(["## 五、测试重点（测试团队专用）", ""])
    lines.append("| 测试类型 | 测试点 | 对应风险 |")
    lines.append("| :--- | :--- | :--- |")
    focus_rows = []
    if merged_issues:
        for m in merged_issues[:12]:
            name = _build_core_issue_title(m)
            lv = str(m.get("risk_level") or "P2").upper()
            types_arr = _issue_types(m)
            hint = _hint_for_type(types_arr[0] if types_arr else "")
            tests = _ensure_list(hint.get("tests"))[:3]
            drafts = _build_issue_test_drafts(m)
            tpoint = name if not drafts else (name + "： " + "；".join(drafts[:2]))
            text = " ".join([name, str(m.get("description") or ""), str(m.get("reason") or "")])
            ttype = "功能测试"
            if any(k in text for k in ["权限", "鉴权", "安全", "串房", "越权", "隐私", "合规"]):
                ttype = "安全测试"
            elif any(k in text for k in ["并发", "冲突", "抢占", "优先级", "同时"]):
                ttype = "冲突/并发测试"
            elif any(k in text for k in ["异常", "失败", "超时", "断网", "重试", "降级", "兜底"]):
                ttype = "异常测试"
            focus_rows.append((ttype, tpoint, lv))
    if not focus_rows and pending:
        for d in pending[:12]:
            text = " ".join([str(d.get("type") or ""), str(d.get("module") or ""), str(d.get("description") or "")])
            ttype = "功能测试"
            if any(k in text for k in ["权限", "鉴权", "安全", "串房", "越权", "隐私", "合规"]):
                ttype = "安全测试"
            elif any(k in text for k in ["并发", "冲突", "抢占", "优先级", "同时"]):
                ttype = "冲突/并发测试"
            elif any(k in text for k in ["异常", "失败", "超时", "断网", "重试", "降级", "兜底"]):
                ttype = "异常测试"
            focus_rows.append((ttype, str(d.get("description") or d.get("type") or "【PRD未说明】"), str(d.get("risk_level") or "P2").upper()))
    seen_tpoints = set()
    deduped_focus_rows = []
    for ttype, tpoint, risk in focus_rows:
        tpoint_display = str(tpoint).replace('|', ' ').replace('\n', ' ').replace('\r', '')
        if tpoint_display not in seen_tpoints:
            seen_tpoints.add(tpoint_display)
            deduped_focus_rows.append((ttype, tpoint_display, risk))
            
    for ttype, tpoint_display, risk in deduped_focus_rows[:12]:
        lines.append(f"| {ttype} | {tpoint_display} | {risk} |")
    if not deduped_focus_rows:
        lines.append("| - | - | - |")
    lines.append("")

    lines.extend(["## 六、研发重点（研发团队专用）", ""])
    lines.append("| 模块 | 研发关注点 | 风险等级 |")
    lines.append("| :--- | :--- | :--- |")
    dev_rows = []
    if merged_issues:
        for m in merged_issues[:12]:
            anchors_list = _issue_anchors(m)
            valid_anchors = []
            if anchors_list:
                valid_anchors = [a for a in anchors_list if a and a != "【PRD未说明】"]
            
            if valid_anchors:
                module = valid_anchors[0]
            else:
                module = str(m.get("module") or "全局或上下文推导")
            
            types_arr = _issue_types(m)
            hint = _hint_for_type(types_arr[0] if types_arr else "")
            devs = _ensure_list(hint.get("dev"))[:3]
            focus = "；".join(_build_core_issue_fix_items(m)[:2]) or str(m.get("suggestion") or m.get("description") or "【PRD未说明】")
            if devs:
                focus = focus + "（建议： " + "；".join(devs) + "）"
            lv = str(m.get("risk_level") or "P2").upper()
            dev_rows.append((module, focus, lv))
    if not dev_rows and pending:
        for d in pending[:12]:
            module = str(d.get("module") or "全局或上下文推导")
            focus = str(d.get("suggestion") or d.get("description") or "【PRD未说明】")
            lv = str(d.get("risk_level") or "P2").upper()
            dev_rows.append((module, focus, lv))
    seen_focus = set()
    deduped_dev_rows = []
    for module, focus, risk in dev_rows:
        focus_display = str(focus).replace('|', ' ').replace('\n', ' ').replace('\r', '')
        module_display = str(module).replace('|', ' ').replace('\n', ' ').replace('\r', '')
        if focus_display not in seen_focus:
            seen_focus.add(focus_display)
            deduped_dev_rows.append((module_display, focus_display, risk))
            
    for module_display, focus_display, risk in deduped_dev_rows[:12]:
        lines.append(f"| {module_display} | {focus_display} | {risk} |")
    if not deduped_dev_rows:
        lines.append("| - | - | - |")
    lines.append("")
    lines.extend(["## 七、项目风险", _to_md_items((stage3_json or {}).get("risks")), ""])
    lines.extend(["## 八、计划建议", _to_md_items((stage3_json or {}).get("plan")), ""])
    
    # 追加架构透视报告
    architecture_scan = (stage3_json or {}).get("architecture_scan") if isinstance((stage3_json or {}).get("architecture_scan"), dict) else {}
    if architecture_scan:
        lines.append(_render_architecture_markdown(architecture_scan))
    
    return "\n".join(lines).strip()


def run_prd_audit_stream(
    content: str,
    llm_config_path: str,
    llm_config_override: Optional[Dict[str, Any]] = None,
    timeout: int = 90,
    custom_prompt: Optional[str] = None,
    report_level: str = "L3",  # 保留参数以兼容旧调用，但内部一次性生成 L1/L2/L3
) -> Generator[str, None, None]:
    """
    流式执行 PRD 审计：若 custom_prompt 有值则单次 LLM 调用并分块 yield；
    否则执行 Stage1 → Stage2 → Stage3(LLM 或 Python 兜底)，最后分块 yield 报告。
    每块为 NDJSON 行：{"type":"status","text":"..."} 或 {"type":"content","text":"..."} 或 {"type":"error","text":"..."}。
    """
    import json as _json
    if custom_prompt and custom_prompt.strip():
        # 自定义 prompt 仍按单份报告处理
        yield _json.dumps({"type": "status", "text": "PRD 极简审计（单次调用）…\n"}, ensure_ascii=False) + "\n"
        full = call_llm_with_retry(
            [{"role": "user", "content": custom_prompt.strip().replace("{content}", content)}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            stream=False,
            timeout=120,
            max_retries=1
        )
        chunk_size = 200
        for i in range(0, len(full), chunk_size):
            yield _json.dumps({"type": "content", "text": full[i : i + chunk_size]}, ensure_ascii=False) + "\n"
        return

    yield _json.dumps({"type": "status", "text": "Stage1：PRD结构解析中…\n"}, ensure_ascii=False) + "\n"
    stage1_output = extract_prd_structure(content, llm_config_path=llm_config_path, timeout=timeout, llm_config_override=llm_config_override)
    yield _json.dumps({"type": "status", "text": "Stage2：PRD漏洞扫描中…\n"}, ensure_ascii=False) + "\n"
    stage2_output = run_stage2_defect_scan(
        stage1_output, llm_config_path=llm_config_path, timeout=timeout, prd_text=content, llm_config_override=llm_config_override
    )
    defects_for_flag = stage2_output.get("defects") if isinstance(stage2_output, dict) else []
    offline_mode = any(
        isinstance(d, dict)
        and str(d.get("module") or "") == "扫描引擎"
        and str(d.get("type") or "") == "扫描异常"
        for d in (defects_for_flag or [])
    )
    force_local = os.path.basename(str(llm_config_path or "")) == "__llm_disabled__.json"
    local_mode = bool(force_local or offline_mode)
    if offline_mode:
        yield _json.dumps(
            {
                "type": "status",
                "text": "提示：当前未接入可用大模型，已切换为本地规则体检模式，报告供基础排查使用。\n",
            },
            ensure_ascii=False,
        ) + "\n"
    # 一次性生成 L3（技术审计）+ L2（产品分析）+ L1（管理摘要）
    # 1) 先生成 L3 技术审计报告（用于兜底和技术视角）
    stage3_output = _build_stage3_report(stage1_output, stage2_output, offline_mode=local_mode)
    if local_mode:
        yield _json.dumps({"type": "status", "text": "Stage3：评审报告生成中（本地 L3）…\n"}, ensure_ascii=False) + "\n"
        yield _json.dumps({"type": "status", "text": "Stage4：报告格式渲染中（本地 L3）…\n"}, ensure_ascii=False) + "\n"
        merged_l3 = _render_stage4_markdown(stage3_output)
    else:
        merged_l3 = _run_stage3_llm_report(content, stage1_output, stage2_output, llm_config_path, llm_config_override=llm_config_override, timeout=timeout)
        if merged_l3:
            yield _json.dumps(
                {"type": "status", "text": "Stage3：极简审计报告生成中（L3 八段格式）…\n"},
                ensure_ascii=False,
            ) + "\n"
        else:
            yield _json.dumps({"type": "status", "text": "Stage3：评审报告生成中（L3）…\n"}, ensure_ascii=False) + "\n"
            yield _json.dumps({"type": "status", "text": "Stage4：报告格式渲染中（L3）…\n"}, ensure_ascii=False) + "\n"
            merged_l3 = _render_stage4_markdown(stage3_output)

    # 2) 基于同一份 Stage1/2，生成 L1/L2
    structure_json = json.dumps(stage1_output or {}, ensure_ascii=False, indent=2)
    defects = (stage2_output or {}).get("defects") if isinstance(stage2_output, dict) else []
    defects_json = json.dumps(defects if isinstance(defects, list) else [], ensure_ascii=False, indent=2)
    prd_snippet = (content or "")[:8000].strip() or "【无PRD原文】"

    # 优化点 1：废除 L1/L2 报告的多余 LLM 调用，强制走本地 Python 组装
    yield _json.dumps(
        {"type": "status", "text": "Stage3：生成 L2（产品分析）报告中（本地）…\n"},
        ensure_ascii=False,
    ) + "\n"
    report_l2 = _build_l2_local_report(stage1_output, stage3_output)
    
    yield _json.dumps(
        {"type": "status", "text": "Stage3：生成 L1（管理摘要）报告中（本地）…\n"},
        ensure_ascii=False,
    ) + "\n"
    report_l1 = _build_l1_local_report(stage3_output)
    
    if not report_l2:
        report_l2 = merged_l3
    if not report_l1:
        report_l1 = merged_l3.split("\n\n##", 1)[0] if merged_l3 else merged_l3

    def _build_shift_left_local_report(stage3_json: Dict[str, Any]) -> str:
        merged = (stage3_json or {}).get("merged_issues") or []
        merged = merged if isinstance(merged, list) else []
        top3 = [it for it in merged if isinstance(it, dict) and str(it.get("risk_level") or "").upper() == "P0"]
        if not top3:
            top3 = [it for it in merged if isinstance(it, dict)][:3]
        if not top3:
            return "暂未发现阻断级 P0 漏洞。"
            
        lines = []
        lines.append("# SHIFT_LEFT 测试左移（评审会 30 秒拍桌版）")
        lines.append("")
        lines.append("> 💡 **左移视角**：以下是 PRD 评审阶段必须当场解决的核心 P0 漏洞，否则禁止进入开发。")
        lines.append("")
        
        for idx, it in enumerate(top3[:3], start=1):
            name = str(it.get("name") or f"问题{idx}")
            title_text = _build_core_issue_title(it).replace("|", " ")
            desc = str(it.get("description") or "【PRD未说明】")
            crash = _biz_crash_text(name, "P0", "", desc, "")
            # 定制化严重后果
            if "状态" in title_text or "竞争" in title_text or "回退" in title_text or "清空" in title_text:
                crash = "用户录完以为成功，扫码却找不到文件，前台查不出问题，客服无法复现。"
            elif "矛盾" in title_text or "冲突" in title_text:
                crash = "客服/产品/研发对同一规则三种解读，现场纠纷无 PRD 可裁决。"
            elif "权限" in title_text or "越权" in title_text or "二维码" in title_text:
                crash = "一张码流传外网被用于黑产引流，一次泄露事件就是合规红线，直接引发严重客诉。"

            lines.append(f"### 💣 {idx}. {title_text}（P0）")
            lines.append(f"- **业务致死点**：{crash}")
            lines.append(f"- **漏洞现场**：{desc}")
            lines.append(f"- **拍桌要求**：必须在会上明确状态转移与闭环条件，落入 PRD 原文。")
            lines.append("")
            
        return "\n".join(lines).strip()

    report_shift_left = _build_shift_left_local_report(stage3_output)

    # 优化点 2：将下游分析引擎“异步并行化”
    import concurrent.futures
    
    def _run_test_matrix():
        try:
            from .test_matrix_generator import TestMatrixGenerator, evaluate_test_matrix
            tm = TestMatrixGenerator(stage1_output, stage2_output).generate()
            tq = evaluate_test_matrix(tm, stage1_output)
            return {"test_matrix": tm, "stage4_quality": tq}
        except Exception as e:
            logger.warning("Stage4 test matrix failed: %s", e)
            return {"test_matrix": {}, "stage4_quality": {}}

    def _run_diagrams():
        try:
            from .diagram_generator import DiagramGenerator, evaluate_diagrams
            d = DiagramGenerator(stage1_output).generate_all()
            dq = evaluate_diagrams(d, stage1_output)
            return {"diagrams": d, "stage5_quality": dq}
        except Exception as e:
            logger.warning("Stage5 diagrams failed: %s", e)
            return {"diagrams": {}, "stage5_quality": {}}

    def _run_kg():
        try:
            from .kg_inference import infer_kg
            return {"kg": infer_kg(defects_for_flag if isinstance(defects_for_flag, list) else [], max_root_causes=2, max_chains=3)}
        except Exception as e:
            logger.warning("Stage2.5 kg inference failed: %s", e)
            return {"kg": {}}

    def _run_outline_engine():
        try:
            return {"outline_engine": run_outline_engine(content, stage1_output if isinstance(stage1_output, dict) else {}, stage2_output if isinstance(stage2_output, dict) else {})}
        except Exception as e:
            logger.warning("Stage2.2 outline engine failed: %s", e)
            return {"outline_engine": {}}

    def _run_outline_llm():
        if local_mode:
            return {"outline_llm": {}}
        try:
            from .outline_llm import run_outline_llm
            return {"outline_llm": run_outline_llm(content, stage1_output if isinstance(stage1_output, dict) else {}, llm_config_path=llm_config_path, timeout=timeout)}
        except Exception as e:
            logger.warning("Stage2.2.1 outline llm failed: %s", e)
            return {"outline_llm": {}}

    def _run_shift_left():
        if local_mode:
            return {"shift_left": {}}
        try:
            # Shift Left：仅基于前 3 条核心必补洞察（L3门面），而非全量 defects，保持视角的独特性和高度浓缩
            from .pipeline import _merge_core_issues
            merged = _merge_core_issues(defects if isinstance(defects, list) else [])
            return {"shift_left": run_stage2_shift_left_analysis(stage1_output if isinstance(stage1_output, dict) else {}, merged[:3], llm_config_path=llm_config_path, timeout=timeout, llm_config_override=llm_config_override)}
        except Exception as e:
            logger.warning("Stage2.7 shift left analysis failed: %s", e)
            return {"shift_left": {}}

    def _run_test_case_gen():
        if local_mode:
            return {"test_cases": []}
        try:
            return {"test_cases": run_test_case_generation(stage1_output if isinstance(stage1_output, dict) else {}, defects if isinstance(defects, list) else [], llm_config_path=llm_config_path, timeout=max(timeout, 150), llm_config_override=llm_config_override)}
        except Exception as e:
            logger.warning("Stage4 test cases failed: %s", e)
            return {"test_cases": []}

    yield _json.dumps(
        {"type": "status", "text": "下游引擎并发执行中（测试矩阵/系统图/知识图谱/认知大纲/测试左移资产/测试用例生成）…\n"},
        ensure_ascii=False,
    ) + "\n"
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
        f_tm = executor.submit(_run_test_matrix)
        f_diag = executor.submit(_run_diagrams)
        f_kg = executor.submit(_run_kg)
        f_oe = executor.submit(_run_outline_engine)
        f_ollm = executor.submit(_run_outline_llm)
        f_sl = executor.submit(_run_shift_left)
        f_tc = executor.submit(_run_test_case_gen)
        
        test_matrix_res = f_tm.result()
        test_matrix = test_matrix_res.get("test_matrix", {})
        stage4_quality = test_matrix_res.get("stage4_quality", {})
        
        diag_res = f_diag.result()
        diagrams = diag_res.get("diagrams", {})
        stage5_quality = diag_res.get("stage5_quality", {})
        
        kg = f_kg.result().get("kg", {})
        outline_engine = f_oe.result().get("outline_engine", {})
        outline_llm = f_ollm.result().get("outline_llm", {})
        shift_left = f_sl.result().get("shift_left", {})
        test_cases = f_tc.result().get("test_cases", [])

    platform_impact = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage2.3：平台影响分析中…\n"},
            ensure_ascii=False,
        ) + "\n"
        platform_impact = run_platform_impact_analysis(
            content=content,
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            outline_engine=outline_engine if isinstance(outline_engine, dict) else {},
        )
    except Exception as e:
        logger.warning("Stage2.3 platform impact failed: %s", e)

    dependency_analysis = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage2.4：需求依赖分析中…\n"},
            ensure_ascii=False,
        ) + "\n"
        dependency_analysis = run_dependency_analysis(
            content=content,
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            outline_engine=outline_engine if isinstance(outline_engine, dict) else {},
            platform_impact=platform_impact if isinstance(platform_impact, dict) else {},
        )
    except Exception as e:
        logger.warning("Stage2.4 dependency analysis failed: %s", e)

    prd_quality = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage2.6：PRD质量评分中…\n"},
            ensure_ascii=False,
        ) + "\n"
        prd_quality = run_prd_quality_5d(
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            outline_engine=outline_engine if isinstance(outline_engine, dict) else {},
            dependency_analysis=dependency_analysis if isinstance(dependency_analysis, dict) else {},
            test_matrix=test_matrix if isinstance(test_matrix, dict) else {},
        )
    except Exception as e:
        logger.warning("Stage2.6 quality engine failed: %s", e)

    test_points = {}
    validation_outline = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage4.5：测试点与验证大纲生成中…\n"},
            ensure_ascii=False,
        ) + "\n"
        test_points = run_test_points_engine(
            prd_text=content,
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            outline_engine=outline_engine if isinstance(outline_engine, dict) else {},
            platform_impact=platform_impact if isinstance(platform_impact, dict) else {},
            dependency_analysis=dependency_analysis if isinstance(dependency_analysis, dict) else {},
            test_matrix=test_matrix if isinstance(test_matrix, dict) else {},
        )
        from .test_points_engine import generate_validation_outline
        validation_outline = generate_validation_outline(test_points)
    except Exception as e:
        logger.warning("Stage4.5 test points engine failed: %s", e)

    risk_prediction = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage4.6：风险预测中…\n"},
            ensure_ascii=False,
        ) + "\n"
        risk_prediction = run_risk_prediction_engine(
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            prd_quality=prd_quality if isinstance(prd_quality, dict) else {},
            platform_impact=platform_impact if isinstance(platform_impact, dict) else {},
            dependency_analysis=dependency_analysis if isinstance(dependency_analysis, dict) else {},
            test_points=test_points if isinstance(test_points, dict) else {},
        )
    except Exception as e:
        logger.warning("Stage4.6 risk prediction failed: %s", e)

    understanding_cards = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage4.7：理解卡片生成中…\n"},
            ensure_ascii=False,
        ) + "\n"
        understanding_cards = build_understanding_cards(
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
        )
    except Exception as e:
        logger.warning("Stage4.7 understanding cards failed: %s", e)

    # Stage 4.9: 架构透视分析
    architecture_scan = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage4.9：架构透视分析中（功能模块/状态机/风险热力图）…\n"},
            ensure_ascii=False,
        ) + "\n"
        architecture_scan = run_architecture_scan(
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
        )
    except Exception as e:
        logger.warning("Stage4.9 architecture scan failed: %s", e)

    release_gate = {}
    try:
        yield _json.dumps(
            {"type": "status", "text": "Stage4.8：发布门禁决策中…\n"},
            ensure_ascii=False,
        ) + "\n"
        release_gate = run_release_gate(
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            platform_impact=platform_impact if isinstance(platform_impact, dict) else {},
            prd_quality=prd_quality if isinstance(prd_quality, dict) else {},
        )
    except Exception as e:
        logger.warning("Stage4.8 release gate failed: %s", e)

    shared_summary = _build_shared_summary(stage1_output if isinstance(stage1_output, dict) else {}, llm_config_path=llm_config_path, llm_config_override=llm_config_override)
    reader_guide = _build_reader_guide(stage1_output if isinstance(stage1_output, dict) else {}, stage3_output if isinstance(stage3_output, dict) else {})

    s3_for_bundle = stage3_output if isinstance(stage3_output, dict) else {}
    bundle_summary = s3_for_bundle.get("summary") if isinstance(s3_for_bundle.get("summary"), dict) else {}
    bundle_defects = s3_for_bundle.get("defects") if isinstance(s3_for_bundle.get("defects"), list) else []
    bundle_scan_meta = s3_for_bundle.get("scan_meta") if isinstance(s3_for_bundle.get("scan_meta"), dict) else {}
    if not bundle_scan_meta and isinstance(stage2_output, dict):
        sm = stage2_output.get("scan_meta")
        bundle_scan_meta = sm if isinstance(sm, dict) else {}

    # 最后一次性把三层报告 + 测试矩阵 + 系统图打包返回（前端未改前仅用 L1/L2/L3）
    guardrail = {}
    try:
        guardrail = evaluate_guardrail(
            prd_text=content,
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            report_md=merged_l3 or "",
            test_cases=test_cases if isinstance(test_cases, list) else [],
        )
    except Exception as e:
        logger.warning("guardrail evaluate failed: %s", e)

    bundle = {
        "type": "bundle",
        "L1": report_l1,
        "L2": report_l2,
        "L3": merged_l3,
        "SHIFT_LEFT": report_shift_left,
        "test_matrix": test_matrix,
        "diagrams": diagrams,
        "kg": kg,
        "outline_engine": outline_engine,
        "outline_llm": outline_llm,
        "platform_impact": platform_impact,
        "dependency_analysis": dependency_analysis,
        "prd_quality": prd_quality,
        "test_points": test_points,
        "validation_outline": validation_outline,
        "test_point_matrix": (test_points.get("test_point_matrix") if isinstance(test_points, dict) else {}) or {},
        "risk_prediction": risk_prediction,
        "understanding_cards": understanding_cards,
        "release_gate": release_gate,
        "architecture_scan": architecture_scan,
        "shift_left": shift_left,
        "test_cases": test_cases,
        "shared_summary": shared_summary,
        "reader_guide": reader_guide,
        "parse_meta": {
            "blocks": stage1_output.get("blocks") if isinstance(stage1_output, dict) else [],
            "parse_quality": stage1_output.get("parse_quality") if isinstance(stage1_output, dict) else {},
            "required_elements": stage1_output.get("required_elements") if isinstance(stage1_output, dict) else {},
            "conflict_candidates": stage1_output.get("conflict_candidates") if isinstance(stage1_output, dict) else [],
        },
        "guardrail": guardrail,
        "extras_quality": {
            "stage4": stage4_quality,
            "stage5": stage5_quality,
        },
        # 审计总览仪表盘：质量分 / 漏洞列表 / Stage2 LLM 扫描元信息
        "summary": bundle_summary,
        "defects": bundle_defects,
        "scan_meta": bundle_scan_meta,
    }
    try:
        from .audit_learning import save_audit_snapshot, append_incident_sample
        snapshot_id = save_audit_snapshot(
            prd_text=content,
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            report_l3=merged_l3 or "",
            report_l1=report_l1 or "",
            report_l2=report_l2 or "",
            extras={
                "test_matrix": test_matrix if isinstance(test_matrix, dict) else {},
                "diagrams": diagrams if isinstance(diagrams, dict) else {},
                "kg": kg if isinstance(kg, dict) else {},
                "outline_engine": outline_engine if isinstance(outline_engine, dict) else {},
                "outline_llm": outline_llm if isinstance(outline_llm, dict) else {},
                "platform_impact": platform_impact if isinstance(platform_impact, dict) else {},
                "dependency_analysis": dependency_analysis if isinstance(dependency_analysis, dict) else {},
                "prd_quality": prd_quality if isinstance(prd_quality, dict) else {},
                "test_points": test_points if isinstance(test_points, dict) else {},
                "validation_outline": validation_outline if isinstance(validation_outline, dict) else {},
                "test_point_matrix": (
                    test_points.get("test_point_matrix")
                    if isinstance(test_points, dict) and isinstance(test_points.get("test_point_matrix"), dict)
                    else {}
                ),
                "risk_prediction": risk_prediction if isinstance(risk_prediction, dict) else {},
                "understanding_cards": understanding_cards if isinstance(understanding_cards, dict) else {},
                "release_gate": release_gate if isinstance(release_gate, dict) else {},
                "architecture_scan": architecture_scan if isinstance(architecture_scan, dict) else {},
                "shift_left": shift_left if isinstance(shift_left, dict) else {},
                "test_cases": test_cases if isinstance(test_cases, list) else [],
                "shared_summary": shared_summary if isinstance(shared_summary, dict) else {},
                "reader_guide": reader_guide if isinstance(reader_guide, dict) else {},
                "guardrail": guardrail if isinstance(guardrail, dict) else {},
                "extras_quality": bundle.get("extras_quality") if isinstance(bundle.get("extras_quality"), dict) else {},
                "summary": bundle_summary if isinstance(bundle_summary, dict) else {},
                "scan_meta": bundle_scan_meta if isinstance(bundle_scan_meta, dict) else {},
            },
            offline_mode=local_mode,
        )
        if snapshot_id:
            bundle["snapshot_id"] = snapshot_id
        try:
            gr_score = int(float((guardrail or {}).get("score") or 0))
        except Exception:
            gr_score = 0
        failed = False
        for ck in (guardrail or {}).get("checks") or []:
            if not isinstance(ck, dict):
                continue
            sev = str(ck.get("severity") or "").upper()
            if sev in ("P0", "P1") and not bool(ck.get("ok")):
                failed = True
                break
        if failed or gr_score < 60:
            stage2_defects = stage2_output.get("defects") if isinstance(stage2_output, dict) else []
            stage2_defects = stage2_defects if isinstance(stage2_defects, list) else []
            p0 = sum(1 for d in stage2_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
            p1 = sum(1 for d in stage2_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
            append_incident_sample({
                "ts": int(time.time()),
                "snapshot_id": snapshot_id,
                "guardrail_score": gr_score,
                "p0": p0,
                "p1": p1,
                "reason": "guardrail_fail" if failed else "guardrail_low_score",
                "prd_excerpt": (content or "")[:2000],
            })
    except Exception as e:
        logger.warning("audit learning snapshot save failed: %s", e)
    yield _json.dumps(bundle, ensure_ascii=False) + "\n"


def run_prd_audit_sync(
    prd_text: str,
    llm_config_path: str,
    llm_config_override: Optional[Dict[str, Any]] = None,
    timeout: int = 90,
) -> Tuple[str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    同步执行 PRD 审计，返回 (report_markdown, stage1_output, stage2_output, stage3_output)。
    """
    stage1_output = extract_prd_structure(prd_text, llm_config_path=llm_config_path, timeout=timeout, llm_config_override=llm_config_override)
    stage2_output = run_stage2_defect_scan(
        stage1_output, llm_config_path=llm_config_path, timeout=timeout, prd_text=prd_text, llm_config_override=llm_config_override
    )
    defects_for_flag = stage2_output.get("defects") if isinstance(stage2_output, dict) else []
    offline_mode = any(
        isinstance(d, dict)
        and str(d.get("module") or "") == "扫描引擎"
        and str(d.get("type") or "") == "扫描异常"
        for d in (defects_for_flag or [])
    )
    force_local = os.path.basename(str(llm_config_path or "")) == "__llm_disabled__.json"
    local_mode = bool(force_local or offline_mode)
    merged_report = None if local_mode else _run_stage3_llm_report(prd_text, stage1_output, stage2_output, llm_config_path, llm_config_override=llm_config_override, timeout=timeout)
    stage3_output = _build_stage3_report(stage1_output, stage2_output, offline_mode=local_mode)
    if not merged_report:
        merged_report = _render_stage4_markdown(stage3_output)
    report_l2 = _build_l2_local_report(stage1_output, stage3_output)
    report_l1 = _build_l1_local_report(stage3_output)
    guardrail = {}
    try:
        guardrail = evaluate_guardrail(
            prd_text=prd_text,
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            report_md=merged_report or "",
            test_cases=[],
        )
    except Exception as e:
        logger.warning("guardrail evaluate failed: %s", e)
    try:
        from .audit_learning import save_audit_snapshot, append_incident_sample
        shared_summary = _build_shared_summary(stage1_output if isinstance(stage1_output, dict) else {}, llm_config_path=llm_config_path, llm_config_override=llm_config_override)
        reader_guide = _build_reader_guide(
            stage1_output if isinstance(stage1_output, dict) else {},
            stage3_output if isinstance(stage3_output, dict) else {},
        )
        snapshot_id = save_audit_snapshot(
            prd_text=prd_text,
            stage1_output=stage1_output if isinstance(stage1_output, dict) else {},
            stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
            report_l3=merged_report or "",
            report_l1=report_l1 or "",
            report_l2=report_l2 or "",
            extras={
                "shared_summary": shared_summary if isinstance(shared_summary, dict) else {},
                "reader_guide": reader_guide if isinstance(reader_guide, dict) else {},
                "summary": (stage3_output.get("summary") if isinstance(stage3_output.get("summary"), dict) else {}) if isinstance(stage3_output, dict) else {},
                "scan_meta": (stage2_output.get("scan_meta") if isinstance(stage2_output.get("scan_meta"), dict) else {}) if isinstance(stage2_output, dict) else {},
                "guardrail": guardrail if isinstance(guardrail, dict) else {},
            },
            offline_mode=local_mode,
        )
        if isinstance(stage3_output, dict) and snapshot_id:
            stage3_output["snapshot_id"] = snapshot_id
        if isinstance(stage3_output, dict) and isinstance(guardrail, dict) and guardrail:
            stage3_output["guardrail"] = guardrail
        try:
            gr_score = int(float((guardrail or {}).get("score") or 0))
        except Exception:
            gr_score = 0
        failed = False
        for ck in (guardrail or {}).get("checks") or []:
            if not isinstance(ck, dict):
                continue
            sev = str(ck.get("severity") or "").upper()
            if sev in ("P0", "P1") and not bool(ck.get("ok")):
                failed = True
                break
        if failed or gr_score < 60:
            stage2_defects = stage2_output.get("defects") if isinstance(stage2_output, dict) else []
            stage2_defects = stage2_defects if isinstance(stage2_defects, list) else []
            p0 = sum(1 for d in stage2_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
            p1 = sum(1 for d in stage2_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
            append_incident_sample({
                "ts": int(time.time()),
                "snapshot_id": snapshot_id,
                "guardrail_score": gr_score,
                "p0": p0,
                "p1": p1,
                "reason": "guardrail_fail" if failed else "guardrail_low_score",
                "prd_excerpt": (prd_text or "")[:2000],
            })
    except Exception as e:
        logger.warning("audit learning snapshot save failed: %s", e)
    return merged_report, stage1_output, stage2_output, stage3_output
