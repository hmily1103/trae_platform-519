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


def _l3_substance_for_topic(item: Dict[str, Any]) -> str:
    """
    用于 L3 topic/现场叙述：尽量使用缺陷『描述+理由+建议+类型+锚点』，避免把合并类名称里的
    「...恢复/状态...」等词误当成缺陷本体，导致全是 state_recovery/同一套车。
    """
    if not isinstance(item, dict):
        return ""
    return " ".join(
        [
            str(item.get("type") or ""),
            str(item.get("description") or ""),
            str(item.get("reason") or ""),
            str(item.get("suggestion") or ""),
            " ".join(_issue_anchors(item)),
        ]
    )


def _l3_cross_client_signal_text(text: str) -> bool:
    """
    是否应归入「多入口/多终端一致/同步时序对账」叙事（L3 合并行标题与套话用）。
    已收窄：单出现“手机/列表/上传”等常见词**不再**自动等于跨端一致，避免大段“组织入口/主数据”
    在**任意垂类** PRD（仅含端侧/渠道词）中误伤。
    """
    s = str(text or "")
    if not s:
        return False
    if "一致性（多端）" in s or "多终端" in s or "多屏" in s or "多入口" in s:
        return True
    if any(
        t in s
        for t in (
            "多端",
            "跨端",
            "双端",
            "端间",
            "多客户端",
        )
    ):
        return True
    if re.search(
        r"(多.{0,4}端|端云|最终一致|强一致|弱一致|多入口.{0,6}一[致时]|对账|主数据|归属.{0,4}不清|同步.{0,4}时[效序])",
        s,
    ):
        return True
    if ("组织入口" in s) or ("主数据" in s and any(k in s for k in ("多入口", "多端", "对账", "时序", "根"))):
        return True
    ch_mobile = ("手机", "移动", "掌端", "手端", "H5", "iOS", "Android")
    ch_other = ("PC", "Web", "管理端", "大屏", "车机", "工控", "台机", "座席", "固件", "边缘", "网关", "监视", "工位")
    if any(a in s for a in ch_mobile) and any(b in s for b in ch_other) and re.search(
        r"(多.{0,2}端|双端|跨端|一致|对账|同步|时序|多入口|刷新)", s
    ):
        return True
    if ("手机" in s or "移动" in s) and re.search(
        r"(多.{0,2}端|双端|跨端|同步|一致|对账|时序|端间|多入口)", s
    ):
        return True
    if ("上传" in s and "云" in s) and re.search(
        r"(一致|多.{0,2}端|跨端|同步|对账|时序|多端)", s
    ):
        return True
    return False


def _l3_concurrency_signal_text(text: str) -> bool:
    """
    是否出现并发/冲突/互斥/限流等泛化信号，与具体业务领域无关。
    """
    s = str(text or "")
    if not s:
        return False
    toks = (
        "多人",
        "多请求",
        "同时",
        "并发",
        "高并发",
        "冲突",
        "抢占",
        "互斥",
        "幂等",
        "排队",
        "限流",
        "锁",
        "竞态",
        "重入",
    )
    return any(t in s for t in toks)


def _l3_state_interrupt_signal_text(text: str) -> bool:
    """
    是否属于「中断/退出/回滚/恢复」类问题；优先于「跨端一致」归类，避免仅因 PRD 中含「手机/列表」等词就误判为同步类。
    """
    s = str(text or "")
    if not s:
        return False
    if any(
        k in s
        for k in (
            "中途退出",
            "未定义用户中途",
            "用户中途",
            "环境重置",
            "切后台",
            "再次进入",
            "状态清理",
            "中断流程",
            "没有回滚",
            "无回滚",
            "回滚逻辑",
        )
    ):
        return True
    if "中断流程" in s or ("中断" in s and "缺失" in s):
        return True
    if "回滚" in s and any(x in s for x in ("异常", "状态", "恢复", "后")):
        return True
    if any(k in s for k in ("回到哪个状态", "退出后", "非法跳转")):
        return True
    return False


def _l3_strip_title_bold_marks(s: str) -> str:
    return re.sub(r"^\*+|\*+$", "", (s or "").strip())


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
        return f"模式切换规则不完整（{lv}）——退出当前模式后去哪了？"
    if "权限" in n or "安全" in n:
        return f"权限与安全缺失（{lv}）——资源操作是否受控？怎么防越权？"
    if "规则冲突" in n or "口径不一致" in n:
        return f"规则冲突（{lv}）——关键口径到底听谁的？"
    if "状态机" in n:
        return f"状态机不完整（{lv}）——系统不知道下一步该去哪"
    return f"{n}（{lv}）——需要补齐可执行规则"


def _biz_example_text(name: str, risk_level: str, description: str) -> str:
    n = str(name or "")
    lv = str(risk_level or "P2").upper()
    if "外部依赖" in n:
        return "例如：上线第一天，核心内容或数据可能全是空白，因为没有定义来源，现场会被误以为“系统坏了”。"
    if "权限" in n or "安全" in n:
        return "例如：用户或前台误操作就能修改敏感状态或跨越权限，客人投诉“被干扰”，店长难以解释。"
    if "状态机" in n or "流程" in n:
        return "例如：用户退出某模式后系统没有回到可控状态，下一次进入时行为不一致，现场演示容易翻车。"
    if "数据" in n:
        return "例如：字段口径不一致导致一端显示“已成功”另一端仍是“处理中”，用户重复点击触发重复请求。"
    if lv == "P0":
        return "例如：关键路径缺口会直接阻断上线或造成资损，现场无法兜底。"
    return "例如：用户按常规操作会遇到不确定结果，导致投诉或返工。"


def _biz_crash_text(name: str, risk_level: str) -> str:
    n = str(name or "")
    lv = str(risk_level or "P2").upper()
    if "外部依赖" in n:
        return "上线第一天核心数据全为空，客诉“系统不可用”，现场无法解释。"
    if "权限" in n or "安全" in n:
        return "出现“越权操作/任意访问”，现场被投诉骚扰或隐私风险。"
    if "状态机" in n or "流程" in n:
        return "退出/返回路径不一致，现场演示卡死或状态混乱。"
    if lv == "P0":
        return "关键路径直接阻断交付或引发资损，必须先澄清。"
    return f"{n or '当前问题'}如果继续按模糊口径推进，问题会在联调和上线阶段集中暴露，返工成本明显升高。"


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


def _extract_specific_feature_from_text(text: str) -> str:
    s = _clean_report_text(text)
    if not s:
        return ""
    candidates = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,24}", s)
    for cand in candidates:
        normalized = _normalize_business_subject(cand)
        if normalized:
            return normalized[:26]
    return ""


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


def _looks_like_anchor_label(text: str) -> bool:
    raw = _clean_report_text(text)
    if not raw:
        return False
    if re.fullmatch(r"L\d{3,}(?:-L?\d{3,})?", raw, flags=re.IGNORECASE):
        return True
    compact = re.sub(r"[\s:：\-–—_]", "", raw)
    return bool(re.fullmatch(r"L\d{3,}(?:L?\d{3,})?", compact, flags=re.IGNORECASE))


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


def _strip_subject_noise(text: str) -> str:
    s = _clean_report_text(text)
    if not s:
        return ""
    s = re.sub(r"^\[[^\]]+\]\s*", "", s)
    s = re.sub(r"^[\(（][^\)）]{1,8}[\)）]\s*", "", s)
    s = _strip_list_prefix(s)
    s = re.sub(r"(未说明|没说明|未写清|没写清|没有写清|未定义|没定义|没有定义|待确认|需确认|规则需确认|口径未定义).*$", "", s)
    s = re.sub(r"(系统会提示|系统提示|页面提示|前端提示).*$", "", s)
    return s.strip("，。；：:、- ")


def _is_generic_feature_name(text: str) -> bool:
    s = _normalize_feature_label(text)
    if not s:
        return True
    if _looks_like_anchor_label(text) or _looks_like_anchor_label(s):
        return True
    if s in _GENERIC_FEATURE_TERMS:
        return True
    if any(term.lower() in s.lower() for term in _SCHEMA_TERMS):
        return True
    if "null" in s.lower():
        return True
    return any(s == x or s.endswith(x) for x in _GENERIC_FEATURE_TERMS)


def _looks_like_pseudo_subject(text: str) -> bool:
    s = _strip_subject_noise(text)
    if not s:
        return True
    if _looks_like_anchor_label(s):
        return True
    if s.startswith(("谁", "哪些", "哪个", "何时", "是否")):
        return True
    if any(k in s for k in ["是否", "怎么", "如何", "能否", "有没有", "未说明", "没写清", "未定义", "待确认", "需确认"]):
        return True
    if s.startswith(("用户", "系统", "页面", "接口", "服务", "前端", "后端")):
        return True
    if re.match(r"^(当|如果|若|在|退出|进入|点击|切换|返回)", s) and len(s) >= 8:
        return True
    if len(s) >= 10 and any(k in s for k in ["点击", "进入", "退出", "返回", "切换", "提示", "显示", "检测", "允许", "重试", "失败", "恢复", "上传", "保存", "访问", "转发"]):
        return True
    return False


def _normalize_business_subject(text: str) -> str:
    s = _normalize_feature_label(_strip_subject_noise(text))
    if not s:
        return ""
    if _is_generic_feature_name(s):
        return ""
    if _looks_like_pseudo_subject(s):
        return ""
    if len(s) > 20:
        return ""
    return s[:20]


def _append_subject_suffix(base: str, suffix: str) -> str:
    s = _normalize_business_subject(base)
    if not s:
        return ""
    if suffix in s or s.endswith(suffix):
        return s
    return f"{s}{suffix}"


def _pattern_subject_from_text(text: str, base: str = "") -> str:
    if not text:
        return ""
    if any(k in text for k in ["打断", "中断", "续办", "续播", "重播", "重来", "跳过", "恢复原状态"]):
        return _append_subject_suffix(base, "恢复规则") or "被打断后的恢复规则"
    if any(k in text for k in ["返回", "重进", "切后台", "回到前台", "再次进入"]):
        return "返回重进后的状态恢复"
    if any(k in text for k in ["失败", "重试"]):
        return "失败重试规则"
    if any(k in text for k in ["字段", "返回", "错误码", "默认值", "枚举值"]):
        return _append_subject_suffix(base, "接口口径") or "接口数据口径"
    if any(k in text for k in ["权限", "鉴权", "越权", "访问", "入口", "链接", "凭证"]):
        return _append_subject_suffix(base, "访问控制") or "访问控制规则"
    return ""


def _derive_raw_subject_candidate(item: Dict[str, Any]) -> str:
    for anchor in _issue_anchors(item):
        candidate = _normalize_business_subject(_anchor_quote(anchor))
        if candidate:
            return candidate
    for module in _issue_modules(item):
        candidate = _normalize_business_subject(module)
        if candidate:
            return candidate
    desc = _first_clause(item.get("description"))
    for text in [desc, item.get("reason") or "", _issue_quote(item)]:
        candidate = _extract_specific_feature_from_text(text)
        if candidate:
            return candidate
    return ""


def _derive_topic_subject(item: Dict[str, Any], text: str, base: str) -> str:
    topic = _issue_topic(item)
    patterned = _pattern_subject_from_text(text, base)
    if patterned:
        return patterned
    if topic == "cloud_degrade":
        return "同步失败后的状态恢复" if any(k in text for k in ["上传", "回传", "同步", "保存", "提交"]) else "异常中断后的状态恢复"
    if topic == "qr_security":
        return _append_subject_suffix(base, "访问控制") or "访问控制规则"
    if topic == "rating_switch":
        return "配置变更生效规则"
    if topic == "sync_latency":
        return "跨端状态同步时效"
    if topic == "transfer_cleanup":
        return "上下文切换后的状态清理"
    if topic == "performance_metric":
        return "性能容量约束"
    if topic == "boundary_rule":
        return _append_subject_suffix(base, "边界规则") or "边界输入处理规则"
    if topic == "acceptance_logging":
        return _append_subject_suffix(base, "验收口径") or "验收与日志口径"
    if topic == "state_recovery":
        return "状态回退与恢复规则"
    if topic == "exception_flow":
        return "失败处理规则"
    if topic == "security_access":
        return _append_subject_suffix(base, "访问控制") or "访问控制规则"
    if topic == "data_contract":
        return _append_subject_suffix(base, "接口口径") or "接口数据口径"
    return ""


def _is_rule_style_subject(text: str) -> bool:
    s = _clean_report_text(text)
    return any(k in s for k in ["规则", "口径", "时效", "控制", "恢复", "处理", "约束", "清理"])


def _subject_object_name(text: str) -> str:
    s = _clean_report_text(text)
    if s.endswith("访问控制"):
        base = s[:-4].strip()
        return base or "相关入口"
    if s.endswith("接口口径"):
        base = s[:-4].strip()
        return base or "相关接口"
    if s.endswith("边界规则"):
        base = s[:-4].strip()
        return base or "相关输入"
    if s.endswith("验收口径"):
        base = s[:-4].strip()
        return base or "相关结果"
    return s or "相关场景"


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
    # 顺序很重要：先把“多端同步/上传/云端”等高命中主题放在前面，
    # 避免“扫码”等泛词误伤（例如录音同步场景里也可能出现扫码相关表述）。
    if any(k in text for k in ["五维评分", "评分开关", "设置开关", "生效时机"]):
        return "rating_switch"
    if any(k in text for k in ["同步时效", "同步延迟", "多久同步", "实时同步", "时效性", "多端", "跨端", "TV端", "手机端", "列表更新", "一致性", "对账"]):
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
    if any(k in text for k in ["字段", "数据", "一致性", "默认值", "错误码"]):
        return "data_contract"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return "security_access"
    # 二维码/敏感链接：需要更强的证据，不要仅凭“扫码”二字误判
    if any(k in text for k in ["二维码", "敏感链接", "凭证", "token", "401", "未授权访问"]):
        return "qr_security"
    if ("扫码" in text or "鉴权" in text) and any(k in text for k in ["越权", "未授权", "泄露", "分享", "截屏", "截图", "转发", "链接", "二维码"]):
        return "qr_security"
    return "generic_rule"


def _topic_feature_name(topic: str, text: str) -> str:
    # 彻底废弃白名单匹配，由 LLM 提取的模块名和上下文来决定，此处返回空以触发降级策略
    return ""


def _derive_feature_name(item: Dict[str, Any]) -> str:
    blob = _issue_blob(item)
    topic_name = _topic_feature_name(_issue_topic(item), blob)
    if topic_name:
        return topic_name
    base = _derive_raw_subject_candidate(item)
    topic_subject = _derive_topic_subject(item, blob, base)
    if topic_subject:
        return topic_subject[:26]
    if base:
        return base[:26]
    return "关键业务规则"


def _issue_quote(item: Dict[str, Any]) -> str:
    def _looks_like_table_or_roster_line(s: str) -> bool:
        """
        过滤“排期表/负责人名单/表头”这类噪声行。
        这类内容会严重降低 L1/L2 的“原文证据”可读性，看起来像抓错了原文。
        """
        raw = str(s or "")
        t = _clean_report_text(raw, keep_newlines=False)
        if not t:
            return True
        # 兼容“断词/表格空格”的关键词匹配（例如：负 责 人 / 完 成 时 间）
        condensed = re.sub(r"\s+", "", t)
        head_patterns = [
            r"负\s*责\s*人",
            r"完\s*成\s*时\s*间",
            r"上\s*线\s*时\s*间",
            r"节\s*点",
            r"里\s*程\s*碑",
            r"排\s*期",
            r"小\s*程\s*序",
            r"服\s*务\s*端",
            r"客\s*户\s*端|客\s*⼾\s*端|客\s*户\s*端",  # 兼容“⼾”
            r"U\s*I\s*设\s*计|UI\s*设\s*计",
            r"测\s*试",
            r"开\s*发",
        ]
        for pat in head_patterns:
            if re.search(pat, t) or re.search(pat, condensed):
                return True
        # 进一步兜底：极常见的表头字段只要出现，就直接判定为表头/名单
        if any(k in condensed for k in ["负责人", "完成时间", "上线时间", "里程碑", "排期"]):
            return True
        # 名单：大量人名+空格/分隔
        if len(re.findall(r"[\u4e00-\u9fff]{2,3}", t)) >= 8 and (" " in t or "\t" in t):
            return True
        # 过多“短词拼接”的表格行
        tokens = re.split(r"\s+", t)
        if len(tokens) >= 10 and sum(1 for x in tokens if 1 <= len(x) <= 4) >= 8:
            return True
        return False

    anchors = _issue_anchors(item)
    if not anchors:
        return ""
    for a in anchors[:6]:
        quote = _anchor_quote(a)
        cand = quote or a
        cand = str(cand or "").strip()
        if not cand:
            continue
        if not _l3_is_usable_quote(cand):
            continue
        if _looks_like_table_or_roster_line(cand):
            continue
        return cand[:80]
    return ""


def _build_user_path(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    quote = _issue_quote(item)
    text = " ".join([feature, quote, " ".join(_issue_types(item)), _first_clause(item.get("description"))])
    if _is_rule_style_subject(feature):
        if any(k in text for k in ["退出", "返回"]):
            return f"用户在“{feature}”对应场景里点击退出或返回"
        if any(k in text for k in ["开关", "按钮", "点击"]):
            return f"用户在“{feature}”对应场景里触发关键操作"
        if any(k in text for k in ["分享", "二维码", "获取"]):
            return f"用户进入“{feature}”对应场景后"
        if any(k in text for k in ["上传", "保存"]):
            return f"用户进入“{feature}”对应场景的保存或上传环节"
        return f"用户走到“{feature}”对应场景时"
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
        return f"用户在使用“{feature}”时，遇到云端服务降级或网络异常，系统未给出明确提示或本地状态未同步，导致后续使用出现数据不一致或流程卡死。"
    if topic == "qr_security":
        return f"用户生成敏感链接或凭证后，截屏发给未授权用户，未授权用户也能直接访问并进行高危操作，导致隐私泄露或合规风险。"
    if topic == "rating_switch":
        return f"用户正在执行耗时任务时，触发全局配置变更，任务被强制中断或状态错乱，且前端无任何提示，用户误以为操作成功。"
        
    if topic == "transfer_cleanup":
        return f"遇到上下文或环境发生重置时，PRD 中既提到保留又提到清空，现场出现客服、研发、产品三方理解不一致的冲突。"
    if topic == "sync_latency":
        return f"{path}后，PRD 没写清跨端或跨系统多久完成同步，用户在一个端上看到“已完成”，另一个端却迟迟没有更新，现场会误判为功能失效。"
    if topic == "performance_metric":
        return f"{path}时如果响应时延、资源占用、并发上限和超时阈值都没指标，研发无法做容量设计，现场容易出现卡顿、超时或资源被打满。"
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
        return "本地操作成功 → 云端数据同步失败/延迟 → 状态不一致或数据覆盖 → 用户使用异常"
    if topic == "qr_security":
        return "生成未鉴权敏感凭证/链接 → 截图转发或过期未回收 → 未授权账号扫码或点击 → 发生越权访问与隐私泄露"
    if topic == "rating_switch":
        return f"正在执行“{feature}” → 触发全局配置变更 → 后台强杀任务或状态未对齐 → 用户流程中断且无前端提示"
        
    if topic == "transfer_cleanup":
        return f"{feature}存在规则歧义 -> 客服、产品、研发对状态重置时的规则给出不同解释 -> 用户现场纠纷无法用 PRD 直接裁决。"
    if topic == "sync_latency":
        return f"{feature}没有时效指标 -> 多端状态展示脱节 -> 用户重复操作导致系统积压或状态错乱 -> 最终把同步延迟放大成功能故障。"
    if topic == "performance_metric":
        return f"{feature}缺少性能和容量红线 -> 响应、计算、传输链路没有统一预算 -> 流量一高就卡顿、积压或资源耗尽。"
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
            f"用例1：执行“{feature}”后立即断电重启，验证是否有产生幽灵记录或数据丢失",
            f"用例2：执行“{feature}”时发生网络中断，验证是否有明确提示或本地状态保持",
            f"用例3：断网恢复后，验证“{feature}”的待处理任务是否能正确重传与同步",
        ]
    if topic == "qr_security":
        return [
            f"用例1：未授权账号扫码或访问“{feature}”的敏感链接，应提示无权限或拦截跳转",
            f"用例2：访问超期的“{feature}”敏感链接，应提示链接已过期并拒绝服务",
            f"用例3：高频请求“{feature}”敏感数据，应触发风控频率限制和告警",
        ]
    if topic == "rating_switch":
        return [
            f"用例1：正在执行“{feature}”任务时更改相关配置开关，验证当前任务不受影响",
            f"用例2：配置开关更改后，验证下一次新任务是否正确应用了新规则",
        ]
        
    if topic == "transfer_cleanup":
        return [
            f"用例1：执行环境重置或打断操作时，验证“{feature}”是否按 PRD 明确策略进行状态保持或清理。",
            f"用例2：验证恢复后，前端展示、后端状态落库与用户预期是否一致。",
        ]
    if topic == "sync_latency":
        return [
            f"用例1：验证“{feature}”在主端操作完成后，是否在 PRD 规定的 SLA 内同步到其余终端。",
            f"用例2：mock 同步延迟超过 SLA，期望系统给出可见的加载中或异常状态提示。",
        ]
    if topic == "performance_metric":
        return [
            f"用例1：在高负载场景下验证“{feature}”的处理耗时、CPU 占用和传输时延是否满足 PRD 指标。",
            f"用例2：连续执行大量请求，期望超时策略、重试次数和资源占用均符合 PRD 约束。",
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
    subject_is_rule = _is_rule_style_subject(feature)
    
    if topic == "cloud_degrade":
        return f"{feature}未定义" if subject_is_rule else f"{feature}异常处理：服务异常或网络中断时状态丢失"
    if topic == "qr_security":
        return f"{feature}未定义" if subject_is_rule else f"{feature}权限漏洞：未授权访问与越权操作风险"
    if topic == "rating_switch":
        return f"{feature}未定义" if subject_is_rule else f"{feature}状态竞争：配置变更与当前任务状态冲突"
        
    if "自动任务" in text and "手动干预" in text and "优先级" in text:
        return "自动任务 vs 手动干预:优先级未定义"

    
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return f"{feature}未定义" if subject_is_rule else f"{feature}依赖的外部服务和权限口径未定义"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码"]):
        return f"{feature}未定义" if subject_is_rule else f"{feature}的数据字段和返回口径未定义"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"{feature}未定义" if subject_is_rule else f"{feature}的角色权限和拦截规则未定义"
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return f"{feature}未定义" if subject_is_rule else f"{feature}的边界值和异常输入规则未定义"
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return f"{feature}未定义" if subject_is_rule else f"{feature}缺少明确的成功条件和验收口径"
    if any(k in text for k in ["状态", "跳转", "回滚", "恢复"]):
        return f"{feature}未定义" if subject_is_rule else f"{feature}切换后的状态回退和恢复规则未定义"
    if any(k in text for k in ["流程", "中断", "退出", "重试", "并发", "幂等"]):
        return f"{feature}未定义" if subject_is_rule else f"{feature}失败或中断后用户该看到什么、能做什么没有写清"
    if description and description != "【PRD未说明】":
        return description if len(description) <= 40 else description[:40].rstrip() + "..."
    return feature


def _build_issue_meeting_tag(item: Dict[str, Any]) -> str:
    text = _issue_blob(item)
    topic = _issue_topic(item)

    if topic == "cloud_degrade":
        return "异常恢复没定"
    if topic == "qr_security":
        return "权限规则没定"
    if topic == "rating_switch":
        return "状态竞争没定"
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return "依赖口径没定"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码", "返回"]):
        return "接口口径没定"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return "权限规则没定"
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return "边界规则没定"
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return "验收口径没定"
    if any(k in text for k in ["打断", "中断", "续播", "重播", "下一条", "恢复原状态", "回到原流程"]):
        return "恢复规则没定"
    if any(k in text for k in ["状态", "跳转", "回滚", "切换", "回到哪个状态"]):
        return "回退路径没定"
    if any(k in text for k in ["流程", "中断", "退出", "重试", "并发", "幂等", "超时", "失败"]):
        return "失败处理没定"
    return "关键规则没定"


def _build_issue_meeting_explanation(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    feature_obj = _subject_object_name(feature)
    gap = _build_core_issue_gap(item)
    text = _issue_blob(item)
    topic = _issue_topic(item)

    if topic == "cloud_degrade":
        return "弱网、超时、服务异常时，本地状态是否保留、是否重试、是否提示用户都没定义。"
    if topic == "qr_security":
        return f"谁能访问“{feature_obj}”、访问范围怎么限制、越权时如何拦截，PRD 没写清。"
    if topic == "rating_switch":
        return "配置变化和当前任务冲突时，谁生效、何时生效、是否允许中途打断都没有统一口径。"
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源", "权限申请"]):
        return "依赖哪个外部服务、失败后怎么降级、权限谁来申请都没写清。"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码", "返回"]):
        return "接口字段、状态值、返回口径和更新时间没写清，前后端和测试会各自理解。"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return "谁能看、谁能操作、越权时怎么拦截没有统一口径。"
    if any(k in text for k in ["边界", "最小值", "最大值", "空值", "非法值"]):
        return "空值、非法值、最小值、最大值怎么处理没写清。"
    if any(k in text for k in ["成功标准", "验收", "可测试", "日志"]):
        return "什么算成功、什么算失败、用户看到什么结果没有统一口径。"
    if any(k in text for k in ["打断", "中断", "续播", "重播", "下一条", "恢复原状态", "回到原流程"]):
        return "流程被打断后是继续、重来、跳过还是回到原流程，PRD 没写清。"
    if any(k in text for k in ["状态", "跳转", "回滚", "切换", "回到哪个状态"]):
        return "切换失败或中断后系统回到哪个状态没有统一口径。"
    if any(k in text for k in ["流程", "中断", "退出", "重试", "并发", "幂等", "超时", "失败"]):
        return "失败、中断、退出后用户能做什么、系统怎么兜底没有写清。"
    if gap and gap != "当前 PRD 只给了目标或成功路径，没有把实现所需的关键规则写清。":
        return gap
    return f"围绕“{feature}”的关键业务规则还没写清，开发、测试和验收很难统一口径。"


def _build_issue_meeting_statement(item: Dict[str, Any]) -> str:
    return f"{_build_issue_meeting_tag(item)}：{_build_issue_meeting_explanation(item)}"


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
    feature_obj = _subject_object_name(feature)
    text = _issue_blob(item)
    topic = _issue_topic(item)
    
    if topic == "cloud_degrade":
        return [
            f"异常处理：明确“{feature}”遇到弱网、超时或服务异常时，本地状态是否保留、何时允许重试。",
            f"恢复规则：明确“{feature}”恢复后如何与远端或其他端状态对齐，避免用户看到假成功。",
            f"实现方案：工程上可选本地暂存、补偿机制、状态对账等方案，最终由研发评审确定。",
        ]
    if topic == "qr_security":
        return [
            f"访问控制：明确谁可以查看和操作“{feature_obj}”，未授权访问时如何拦截。",
            f"时效规则：明确“{feature_obj}”的访问入口或访问凭证是否过期、何时失效、失效后如何提示。",
            f"实现方案：具体采用登录态、一次性凭证、设备绑定还是其他方式，由研发方案评审确定。",
        ]
    if topic == "rating_switch":
        return [
            f"状态锁定：启动“{feature}”任务时锁定配置快照，运行中全局配置改变仅影响下一次任务",
            f"打断处理：如需中途干预，必须给出明确的前端中断提示，并处理好资源回收与状态恢复",
        ]
        
    if topic == "sync_latency":
        return [
            f"给“{feature}”补同步 SLA：如主端完成后多少秒内必须同步至其他终端。",
            f"明确超出 SLA 时，前端展示什么占位或异常状态，用户是否可以重试。",
            f"补齐数据流转链路的观测指标、告警阈值和兜底补偿机制。",
        ]
    if topic == "transfer_cleanup":
        return [
            f"明确“{feature}”在各种环境切换、退出、打断场景下到底是清空、保留还是挂起恢复。",
            f"删除 PRD 中“此处规则需确认”这类占位语，改成可执行的单一业务规则。",
            f"补齐触发这些策略时的页面提示与客服答疑口径。",
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
            f"访问控制：补清楚“{feature}”的角色权限矩阵和可执行操作边界。",
            f"异常拦截：明确“{feature}”发生越权访问时的拦截动作、失败提示和审计记录要求。",
            f"工程选项：哪些高风险操作需要二次确认、设备绑定或其他保护机制，由研发评审确定。",
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
    feature_obj = _subject_object_name(feature)
    text = _issue_blob(item)
    topic = _issue_topic(item)
    if topic == "sync_latency":
        return f"建议在《同步时效/SLA》章节补充：“{feature}”从触发完成到各端可见的目标时长、超时提示、补偿机制和观测指标。"
    if topic == "cloud_degrade":
        return f"建议在《异常流程/降级方案》章节补充：“{feature}”遇到服务异常、接口超时或处理失败时，本地状态是否保留、恢复后如何对账、是否允许重试以及用户提示如何处理。"
    if topic == "qr_security":
        return f"建议在《权限控制》章节补充：“{feature_obj}”谁可以查看和操作、未授权访问时如何拦截、访问入口是否过期以及失效后如何提示；具体实现方式由研发方案评审确定。"
    if topic == "rating_switch":
        return f"建议在《配置生效规则》章节补充：“{feature}”对当前进行中任务、下一次任务和已落地结果分别何时生效，以及 UI 如何提示。"
    if topic == "transfer_cleanup":
        return f"建议在《状态重置与恢复规则》章节补充：“{feature}”在退出、重开、切换上下文等场景下到底清空、保留还是挂起恢复，并删除所有“待确认”的占位表述。"
    if topic == "performance_metric":
        return f"建议在《性能指标》章节补充：“{feature}”的响应时延、资源占用、并发上限、超时阈值和容量约束。"
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
            f"建议在《权限控制》章节补充：谁可以查看和操作“{feature}”、越权时如何拦截、访问入口是否过期以及异常访问是否需要记录审计；"
            "具体采用何种鉴权方式，由研发方案评审确定。"
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
    subject_is_rule = _is_rule_style_subject(feature)
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源"]):
        return f"“{feature}”没写清，开发无法提前确认联调条件，上线阶段容易因为依赖缺失而整体阻塞。" if subject_is_rule else f"“{feature}”的依赖名称、权限或可用性要求未写清，开发无法提前确认联调条件，上线阶段容易因为依赖缺失而整体阻塞。"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码", "边界", "最小值", "最大值"]):
        return f"“{feature}”没写清，研发和测试会各自猜测实现方式，极值、空值和异常值输入时容易出现接口报错或结果不一致。" if subject_is_rule else f"“{feature}”的字段口径或边界值未定义，研发和测试会各自猜测实现方式，极值、空值和异常值输入时容易出现接口报错或结果不一致。"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"“{feature}”没写清，容易出现越权操作、误操作无拦截以及事后无法追溯的问题。" if subject_is_rule else f"“{feature}”的角色边界和拦截规则未定义，容易出现越权操作、误操作无拦截以及事后无法追溯的问题。"
    if any(k in text for k in ["异常", "超时", "失败", "弱网", "重试", "降级"]):
        return f"“{feature}”没写清，用户重复操作时容易触发状态错乱、重复请求或流程卡死。" if subject_is_rule else f"“{feature}”在失败、超时和弱网场景下没有闭环规则，用户重复操作时容易触发状态错乱、重复请求或流程卡死。"
    if any(k in text for k in ["状态", "流程", "回滚", "恢复", "中断", "退出", "并发", "幂等"]):
        return f"“{feature}”没写清，真实运行时一旦发生退出、回退或并发操作，系统行为就可能前后不一致。" if subject_is_rule else f"“{feature}”的状态切换和中断恢复规则未定义，真实运行时一旦发生退出、回退或并发操作，系统行为就可能前后不一致。"
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
    subject_is_rule = _is_rule_style_subject(feature)
    if any(k in text for k in ["外部依赖", "SDK", "API", "第三方", "数据源"]):
        return f"“{feature}”没写清，上线时依赖拉不通或权限没开通，功能会直接不可用，现场只能临时兜底。" if subject_is_rule else f"“{feature}”上线时依赖拉不通或权限没开通，功能会直接不可用，现场只能临时兜底。"
    if any(k in text for k in ["字段", "数据", "一致性", "类型", "长度", "必填", "默认值", "错误码", "边界", "最小值", "最大值"]):
        return f"“{feature}”没写清，用户一旦输入极值、空值或异常值，前后端处理结果可能不一致，现场会表现为保存失败、结果错误或接口报错。" if subject_is_rule else f"用户操作“{feature}”时一旦输入极值、空值或异常值，前后端处理结果可能不一致，现场会表现为保存失败、结果错误或接口报错。"
    if any(k in text for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"“{feature}”没写清，现场就可能出现谁都能操作高风险功能的情况，引发投诉、误操作或合规风险。" if subject_is_rule else f"“{feature}”如果缺少权限边界，现场就可能出现谁都能操作高风险功能的情况，引发投诉、误操作或合规风险。"
    if any(k in text for k in ["异常", "超时", "失败", "弱网", "重试", "降级"]):
        return f"“{feature}”没写清，失败或弱网时用户重复操作会导致流程中断、状态错乱或结果丢失。" if subject_is_rule else f"“{feature}”失败或弱网时没有兜底，用户重复操作会导致流程中断、状态错乱或结果丢失。"
    if any(k in text for k in ["状态", "流程", "回滚", "恢复", "中断", "退出", "并发", "幂等"]):
        return f"“{feature}”没写清，退出、返回或并发操作时路径容易不一致，现场演示和真实运行都可能卡住或走错流程。" if subject_is_rule else f"“{feature}”在退出、返回或并发操作时路径不一致，现场演示和真实运行都可能卡住或走错流程。"
    if lv == "P0":
        return "关键路径直接阻断交付或引发资损，必须先澄清。"
    return f"“{feature}”当前口径不够具体，继续推进会把问题推迟到联调和上线阶段，返工成本明显升高。"


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


def _defect_business_subject(defect: Dict[str, Any]) -> str:
    if not isinstance(defect, dict):
        return ""
    anchor = _anchor_quote(str(defect.get("anchor") or ""))
    candidate = _normalize_business_subject(anchor)
    if candidate:
        return candidate
    module = _normalize_business_subject(str(defect.get("module") or ""))
    if module:
        return module
    text = " ".join([
        str(defect.get("description") or ""),
        str(defect.get("reason") or ""),
    ])
    candidate = _extract_specific_feature_from_text(text)
    if candidate:
        return candidate[:20]
    return ""


def _core_group_title(subject: str, fallback: str, suffix: str) -> str:
    base = _normalize_business_subject(subject)
    if not base:
        return fallback
    if suffix and suffix in base:
        return base
    return f"{base}{suffix}"


def _core_group_name(defect, prd_content=""):
    t = str(defect.get("type") or "")
    desc = str(defect.get("description") or "")
    reason = str(defect.get("reason") or "")
    text = t + " " + desc + " " + reason
    subject = _defect_business_subject(defect)
    
    # ---- 新增防误报“安全防火墙” ----
    # 如果系统明显不是包含账户、交易、后台权限的系统，强行拦截安全/越权相关的幻觉
    if any(k in text for k in ["越权", "鉴权", "横向越权", "信息泄露", "权限", "安全", "Token"]):
        # 简单做个业务嗅探：如果 PRD 原文没有提过账号、登录、支付、管理后台，就拦截
        if not any(k in prd_content for k in ["账号", "登录", "注册", "支付", "管理后台", "Token", "鉴权", "权限"]):
            # 降级为普通逻辑冲突
            return "边缘规则冲突与未定义"

    # 动态提权并重新归类高危语义，彻底移除对特定业务名词的依赖
    if any(k in text for k in ["越权", "鉴权", "横向越权", "信息泄露"]):
        return _core_group_title(subject, "未授权访问与越权风险", "访问与权限风险")
    if any(k in text for k in ["死锁", "并发冲突", "状态竞争", "竞争", "抢占", "打断", "优先级", "同时"]):
        return _core_group_title(subject, "多任务并发与状态调度冲突", "多人或同时操作规则未定义")
    if any(k in text for k in ["丢失", "落盘", "持久化", "回退", "恢复", "退出", "重进"]):
        return _core_group_title(subject, "中断恢复与状态机未闭环", "中断恢复与状态规则未定义")
    if any(k in text for k in ["矛盾", "冲突", "歧义", "自相矛盾", "不一致"]):
        return _core_group_title(subject, "核心业务规则自相矛盾", "业务规则前后不一致")
    if any(k in text for k in ["网络", "超时", "失败", "断网", "异常"]):
        return _core_group_title(subject, "异常分支与兜底流程缺失", "失败处理与兜底规则缺失")
        
    if any(k in t for k in ["状态", "跳转"]):
        return _core_group_title(subject, "中断恢复与状态机未闭环", "状态流转规则未定义")
    if any(k in t for k in ["流程", "重试"]):
        return _core_group_title(subject, "异常分支与兜底流程缺失", "失败处理与重试规则缺失")
    if any(k in t for k in ["字段", "数据", "不可测试", "口径"]):
        return _core_group_title(subject, "数据契约与验收标准缺失", "数据口径与验收标准缺失")
    if "逻辑矛盾" in t:
        return _core_group_title(subject, "核心业务规则自相矛盾", "业务规则前后不一致")
    return _core_group_title(subject, f"{str(defect.get('module') or '全局')}边缘规则缺失", "边缘规则缺失")


def _merge_core_issues(defects, prd_content=""):
    groups = {}
    for d in defects:
        name = _core_group_name(d, prd_content)
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
    p0_count = 0
    for g in groups.values():
        name = g["name"]
        risk = _max_risk(g["risk_levels"])
        
        # 强制提权机制
        if name in ["未授权访问与越权风险", "多任务并发与状态调度冲突", "中断恢复与状态机未闭环", "异常分支与兜底流程缺失"] or any(
            key in name for key in ["访问与权限风险", "多人或同时操作规则未定义", "中断恢复与状态规则未定义", "失败处理与兜底规则缺失"]
        ):
            risk = "P0"
            
        # 如果是 P0，检查是否已经超过 3 个
        if risk == "P0":
            p0_count += 1
            if p0_count > 3:
                risk = "P1" # 强制降级，避免 P0 泛滥
            
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
    
    # 根据是否有 P0 动态生成极具老板体感的金句
    p0_count = sum(1 for m in merged_issues if str(m.get("risk_level", "")).upper() == "P0")
    if p0_count > 0:
        one_liner = "这份 PRD 不是不能做，而是现在做一定边做边改。"
    else:
        one_liner = "这份 PRD 主干清晰，抓紧补齐边缘细节即可交付。"
        
    top3 = merged_issues[:3]
    bullets = []
    for item in top3:
        if isinstance(item, dict):
            bullets.append(f"{_build_issue_meeting_statement(item)}（{item.get('risk_level')}）")
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
    # 报告标题不附带 Stage2/模型状态尾缀，避免「工具未调通」观感；扫描元信息在仪表盘或 JSON 中可见。
    report_title = (
        f"【审计报告】PRD：工具扫描+人工复核版（含{len(defects)}项缺陷，P0级{p0_count}项）"
        f"{offline_suffix}"
    )
    complexity = _calc_complexity(stage1_output or {})
    tool_warnings = []
    for td in tool_defects[:10]:
        raw_r = str(td.get("reason") or "")
        # 不向终稿透传长连接/读超时等排障信息，保持「审计官」视角信噪比
        if any(
            x in raw_r
            for x in ("Read timed out", "HTTPSConnectionPool", "ConnectionError", "timed out", "Timeout", "10054")
        ):
            raw_r = "本次漏洞扫描服务响应异常，已忽略该次调用详情；请稍后重试扫描或检查网络/密钥。"
        tool_warnings.append({
            "module": str(td.get("module") or "扫描引擎"),
            "type": str(td.get("type") or "扫描异常"),
            "description": str(td.get("description") or "【工具异常】"),
            "reason": raw_r,
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
        "stage1_snapshot": {
            "blocks": stage1_output.get("blocks") if isinstance(stage1_output, dict) else [],
            "source_map": stage1_output.get("source_map") if isinstance(stage1_output, dict) else {},
        },
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


def _l3_clean_problem_text(text: Any) -> str:
    s = _clean_report_text(text, keep_newlines=True)
    if not s:
        return "【PRD未说明】"
    s = re.sub(r"\s*例如：.*$", "", s, flags=re.DOTALL)
    s = s.strip("；。 ，,\n")
    return s or "【PRD未说明】"


def _l3_heading_like_quote(text: str) -> bool:
    s = _clean_report_text(text)
    if not s or s == "【PRD未说明】":
        return True
    if re.fullmatch(r"【[^】]{1,20}】(?:规范|规则|说明|流程|模块|配置|展示规则)?", s):
        return True
    if len(s) <= 18 and not any(p in s for p in "，。；：、()（）"):
        if any(k in s for k in ["规范", "规则", "说明", "模块", "配置", "流程", "展示"]):
            return True
    return False


def _l3_is_usable_quote(text: str) -> bool:
    s = _clean_report_text(text)
    if not s or s == "【PRD未说明】":
        return False
    if _looks_like_anchor_label(s):
        return False
    if _l3_heading_like_quote(s):
        return False
    if re.search(r"[\u2E80-\u2FDF]", s):
        return False
    if s.endswith(("：", ":")):
        return False
    if re.match(r"^(?:[-*•]|[a-zA-Z]\.|[0-9一二三四五六七八九十]+[\.\)、])", s) and len(s) < 18:
        return False
    if len(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]", s)) < 10:
        return False
    if len(s) <= 4 and not any(ch in s for ch in "，。；：、（）()“”\"' "):
        return False
    return True


def _l3_issue_text(item: Dict[str, Any]) -> str:
    return " ".join(
        [
            str(item.get("name") or ""),
            str(item.get("type") or ""),
            str(item.get("description") or ""),
            str(item.get("reason") or ""),
            str(item.get("suggestion") or ""),
            " ".join(_issue_anchors(item)),
            " ".join(_issue_modules(item)),
        ]
    )


def _l3_exception_topic_hit(sub: str) -> bool:
    """是否优先按「失败/弱网/兜底」类异常主路径落 topic（在 state_recovery 之前判断）。"""
    s = str(sub or "")
    if not s:
        return False
    if re.search(
        r"(只描述成功路径|缺少失败处理|没[有写].{0,2}失败|无失败(?!的)|网络异常|空间不足|只写了成功|弱网|断网|超时(?!上)|可重试|重试(?!后)|降级|503|保存失败|上传失败|没有失败处理|异常处理机制)",
        s,
    ):
        return True
    if "失败" in s and re.search(
        r"(处理(?!人)|机制|提示|反馈|重试|兜底|上传|保存|路径|异常(?!的)|时)",
        s,
    ):
        return True
    return False


def _l3_render_topic(item: Dict[str, Any]) -> str:
    text = _l3_issue_text(item)
    # 缺陷「实质」文本：不含合并类 name，避免把「状态…恢复…」等归并名误判为 state
    sub = _l3_substance_for_topic(item) or text
    # 全文+实质：补「入口词在 anchor/name、细在 description」的错位
    comb = re.sub(r"\s+", " ", f"{text} {sub}").strip()
    definition_ambiguity_hit = bool(
        re.search(
            r"(定义模糊|何为|判断依据|未定义.{0,8}(是|指)|未明确.{0,8}(是|指)|口径模糊|术语模糊|标签.*未定义|类型.*未定义)",
            sub,
        )
    )
    access_control_hit = bool(
        re.search(
            r"(越权|鉴权|未授权|未鉴权|未登录|谁可|权限|访问|隔离|风控|安全|外泄|重放|角色|账号|会话|租户|组织|空间)",
            sub,
        )
    )
    # 顺序：复合安全/设备现场 → 安全关键词 → 失败/弱网/兜底 → 中断/回退 →
    # 可测/验收/日志 → 并发/覆盖 → 边界/字段 → 真·多端一致
    if (
        re.search(
            r"(二维码|扫码|外链|分享|深度链接|小程序|已唱|获取图标|受控|凭证|外显|下载|取回)",
            comb,
        )
        and access_control_hit
    ) or (
        any(
            k in sub
            for k in [
                "二维码",
                "扫码",
                "未授权",
                "越权",
                "401",
                "未鉴权",
                "风控",
                "未授权访问",
                "外泄",
            ]
        )
        and access_control_hit
    ):
        return "security_access"
    if re.search(
        r"(关台|重开台|开台|合台|转台|翻台|待机|断?电|上电|重启)",
        sub,
    ) and re.search(
        r"(清(?!晰|除\.)|清屏|清状态|清缓存|不清空|待机|断?电|上电|系统关机|全断电|热启动|冷启动|完全断电|重开(?!发)|重启(?!发))",
        sub,
    ):
        return "device_lifecycle"
    if _l3_exception_topic_hit(sub):
        return "exception_flow"
    if _l3_state_interrupt_signal_text(sub) or re.search(
        r"(回到哪个状态|再次进入|切后台|用户中途|未定义用户中途|中途退出|环境重置|状态清理|无回滚|没有回滚|没有回退逻辑|没有回退)",
        sub,
    ) or re.search(
        r"(没有回滚|回滚逻辑|异常(?!的).{0,4}后.{0,4}状态)",
        sub,
    ):
        return "state_recovery"
    if definition_ambiguity_hit and not re.search(
        r"(同步|一致|对账|时效|列表|权限|越权|鉴权|失败|超时|弱网|回滚|恢复|退出|重进|日志|验收|埋点)",
        sub,
    ):
        return "generic_rule"
    if any(
        k in sub
        for k in [
            "成功条件",
            "成功标准",
            "未说明成功",
            "验收",
            "日志",
            "埋点",
            "可测试",
            "审计",
            "追溯",
        ]
    ):
        return "acceptance_logging"
    if any(
        k in sub
        for k in [
            "多人同时操作",
            "高并发",
            "排队",
            "限流",
            "锁",
            "幂等",
            "重复调用",
            "冲突裁决",
        ]
    ) or re.search(
        r"(后一(步|次|笔|项)?|前一(步|次|笔|项)?|连续(操作|触发|提交)|覆盖风险|多请求|竞态|重复触发)",
        sub,
    ) or "并发" in sub and re.search(
        r"(覆盖|冲突|多请求|竞态|连续|后一(步|次|项)?|前一(步|次|项)?)", sub
    ):
        return "concurrency_control"
    if any(k in sub for k in ("边界", "最小值", "最大值", "空值", "非法值")):
        return "boundary_rule"
    if any(
        k in sub
        for k in [
            "字段",
            "数据",
            "返回",
            "错误码",
            "默认值",
            "类型",
            "长度",
            "必填",
        ]
    ):
        return "data_contract"
    if _l3_cross_client_signal_text(text) or "一致性（多端）" in text:
        return "cross_end_sync"
    itp = _issue_topic(item)
    if itp in ("sync_latency",) and re.search(r"(同步|一致|对账|时效|列表|刷新|跨端|多端)", sub):
        return "cross_end_sync"
    return itp


def _l3_focus_subject(item: Dict[str, Any]) -> str:
    desc = _l3_clean_problem_text(item.get("description"))
    reason = _clean_report_text(item.get("reason"))
    text = " ".join([desc, reason, _l3_issue_text(item)])
    topic = _l3_render_topic(item)
    feature = _derive_feature_name(item)
    if topic == "state_recovery":
        if "中途退出" in text or "未定义用户中途" in text or "用户中途" in text:
            return "中途退出后的状态清理"
        return "状态回退与恢复"
    if topic == "exception_flow":
        return "失败路径处理"
    if topic == "concurrency_control":
        return "多人同时操作" if "多人同时操作" in text else "并发冲突处理"
    if topic == "acceptance_logging":
        if "日志" in text:
            return "关键操作日志"
        return "成功判定与验收"
    if topic == "cross_end_sync":
        return "跨端与数据一致"
    if topic == "security_access":
        return "访问控制与权限边界"
    if topic == "device_lifecycle":
        return "关台/重开与设备或会话环境"
    if topic == "boundary_rule":
        return "输入上下限与边界"
    if "多人同时操作" in text:
        return "多人同时操作"
    if "高并发" in text:
        return "高并发场景"
    if any(k in text for k in ["是否允许重试", "重试机制"]):
        return "失败后的重试处理"
    if "失败处理" in text or "只描述成功路径" in text:
        return "失败路径处理"
    if "日志" in text:
        return "关键操作日志"
    if any(k in text for k in ["成功条件", "成功标准", "验收"]):
        return "成功判定"
    if "回滚逻辑" in text:
        return "异常后的回滚恢复"
    if "重复调用" in text:
        return "重复调用处理"
    if "中途退出" in text:
        return "中途退出后的状态清理"
    if any(k in text for k in ["最小值", "最大值", "边界"]):
        return "输入上下限"
    if feature and feature != "关键业务规则" and not _is_rule_style_subject(feature):
        return feature
    return feature or "关键业务规则"


def _l3_title(item: Dict[str, Any]) -> str:
    desc = _l3_clean_problem_text(item.get("description"))
    text = _l3_issue_text(item)
    sub = _l3_substance_for_topic(item) or text
    topic = _l3_render_topic(item)
    subject = _l3_focus_subject(item)
    if re.search(r"(自动(开启|触发|执行|生效)|自动录制|自动录音)", sub) and re.search(
        r"(开关|关闭状态|全局设置|手动|优先级|为关闭状态|是否仍|是否继续|是否强制)",
        sub,
    ):
        return "自动触发与手动配置的优先级未定义"
    if re.search(r"(启动中|初始化失败|启动失败|初始化|中间态|加载中)", sub) and re.search(
        r"(展示什么|提示|状态|反馈|从关闭到开启|异常态)",
        sub,
    ):
        return "启动中间态与初始化失败反馈未定义"
    if re.search(
        r"(生效范围|仅对当前用户生效|整个设备|所有用户生效|多用户场景|设备级|账户级|用户级)",
        sub,
    ):
        return "配置开关生效范围与权限边界未定义"
    if "是否允许重试" in text:
        return "失败后是否允许重试未定义"
    # 安全/设备/现场：必须在“只描述成功路径/缺少失败处理”等**合并名**早退之前
    if topic == "security_access":
        return "受控资源与快捷入口的鉴权、隔离与失败/越权态未定义"
    if topic == "device_lifecycle":
        return "关台/待机/重开/重启等运行形态的业务含义与状态影响未定义"
    if "多人同时操作" in text:
        return "多人同时操作的冲突裁决未定义"
    if "高并发" in text:
        return "高并发下的排队限流与幂等规则未定义"
    if "关键操作没有日志要求" in sub or "关键操作没有日志" in sub:
        return "关键操作日志与审计要求未定义"
    if "未说明成功条件" in sub or (
        "成功条件" in sub and topic == "acceptance_logging"
    ):
        return "成功判定与验收口径未定义"
    if "回滚逻辑" in text:
        return "异常后的回滚与状态恢复未定义"
    if "接口重复调用" in text or "重复调用" in text:
        return "接口重复调用的幂等与回退规则未定义"
    if "中途退出" in text:
        return "中途退出后的状态清理规则未定义"
    if ("最小值" in sub or "最大值" in sub) and topic == "boundary_rule":
        return "输入上下限与边界处理规则未定义"
    if topic == "exception_flow":
        if re.search(
            r"(上云|云\s*端|手机端|外部服务|服务.?(宕|不)|服务不可用|同步.{0,6}(云|端|手机)|依赖.{0,6}(云|网|外))",
            sub,
        ):
            return "业务链路容错与失败降级机制未定义"
        if re.search(
            r"(只描述成功路径|缺少失败处理|没[有写].{0,2}失败|只写了成功|没有失败处理)",
            sub,
        ) or re.search(
            r"(只描述成功路径|缺少失败处理|没有失败处理)",
            str(item.get("name") or ""),
        ):
            if not re.search(
                r"(上云|云\s*端|手机端|服务.{0,2}不|依赖.{0,4}(云|网|外))",
                sub,
            ):
                return "失败路径与异常提示未定义"
        return f"{subject}未定义"
    if topic == "concurrency_control":
        return f"{subject}未定义"
    if topic == "state_recovery":
        return f"{subject}未定义"
    if topic == "acceptance_logging":
        return f"{subject}未定义"
    if topic == "cross_end_sync":
        return f"{subject}与多端/列表一致及同步时序未定义"
    if topic == "data_contract":
        return f"{subject}未定义"
    if topic == "boundary_rule":
        return f"{subject}未定义"
    if desc != "【PRD未说明】":
        return desc
    return _build_core_issue_title(item)


def _l3_meeting_tag(item: Dict[str, Any]) -> str:
    text = _l3_issue_text(item)
    sub = _l3_substance_for_topic(item) or text
    topic = _l3_render_topic(item)
    if topic == "state_recovery":
        return "状态与中断口径没定"
    if topic == "cross_end_sync":
        return "多端同步口径没定"
    if topic == "security_access":
        return "权限与安全边界没定"
    if topic == "device_lifecycle":
        return "运行环境边界没定"
    if topic == "exception_flow":
        return "失败路径没定"
    if topic == "concurrency_control":
        return "并发裁决没定"
    if topic == "acceptance_logging":
        return "日志与追溯要求没定" if "日志" in sub and "成功标准" not in sub and "验收" not in sub else "验收与可测口径没定"
    if topic == "boundary_rule":
        return "边界规则没定"
    if topic == "data_contract":
        return "接口口径没定"
    if "是否允许重试" in text:
        return "重试规则没定"
    if re.search(
        r"(只描述成功路径|缺少失败处理|没有失败处理)",
        sub,
    ) and topic not in ("security_access", "device_lifecycle"):
        return "失败路径没定"
    if "多人同时操作" in text or "高并发" in text:
        return "并发裁决没定"
    if "关键操作没有日志" in sub or ( "日志" in sub and "追溯" in sub):
        return "日志要求没定"
    if "成功条件" in sub or "成功标准" in sub or "未说明成功" in sub or "验收" in sub:
        return "验收口径没定"
    if re.search(
        r"(无回滚|没有回滚|没有回退|回到哪个状态|再次进入|中途退出|用户中途|未定义用户中途|状态清理)",
        sub,
    ):
        return "状态恢复没定"
    if "最小值" in sub or "最大值" in sub or "边界" in sub:
        return "边界规则没定"
    return _build_issue_meeting_tag(item)


def _l3_meeting_explanation(item: Dict[str, Any]) -> str:
    text = _l3_issue_text(item)
    sub = _l3_substance_for_topic(item) or text
    topic = _l3_render_topic(item)
    if topic == "state_recovery":
        return "异常/中断/退出/回滚后，应回到哪个状态、资源与缓存如何清理、用户再进入看到什么，没有可验收的闭环表述。"
    if topic == "cross_end_sync":
        return "多端/列表的数据归属、写入顺序、触发刷新与一致性口径没写清，开发与测试会对“用户看到的结果”给出不同解释。"
    if topic == "security_access":
        return "谁能访问哪些能力、在哪个维度隔离（账号/会话/门店/房间）、越权如何拦截与审计没有统一口径。"
    if topic == "device_lifecycle":
        return (
            "关台/待机/重开/断电/上电与业务状态（是否清会话、缓存或资源）若未一一对齐，"
            "现场会出现“以为还在上一场、实际已清屏或反着来”的纠纷。"
        )
    if topic == "exception_flow":
        return "只写了或只暗示了主路径，失败、超时、弱网时用户提示、错误态、是否可重试、资源如何收尾没写清。"
    if topic == "concurrency_control":
        return "多请求/连续操作/同一资源上的并发时，先停谁、先写谁、是否可覆盖/排队/幂等没写清。"
    if topic == "acceptance_logging" and "日志" in sub and re.search(
        r"(成功标准|可测试|未说明成功)", sub
    ) is None:
        return "关键操作要记录什么日志、出了问题如何追溯、哪些字段要可审计都没写清。"
    if topic == "acceptance_logging":
        return "什么算成功、什么算失败、测试按什么标准验收都没写清。"
    if topic == "boundary_rule":
        return "输入上下限、越界后的提示和处理方式都没写清。"
    if topic == "data_contract":
        return "关键字段/状态值/错误码/返回体与前后端对账方式没写清。"
    if "是否允许重试" in text:
        return "失败后是否允许重试、提示什么、状态是否回退都没写清。"
    if (
        re.search(r"(只描述成功路径|缺少失败处理|没有失败处理)", sub)
        and topic not in ("security_access", "device_lifecycle")
    ):
        return "只写了成功路径，失败、超时、弱网时页面提示和兜底动作没写清。"
    if "多人同时操作" in text:
        return "多人同时操作时谁先生效、后到请求怎么处理、是否排队都没写清。"
    if "高并发" in text:
        return "高并发下是否限流、排队、加锁或幂等处理都没写清。"
    if "关键操作没有日志要求" in text:
        return "关键操作要记录什么日志、出了问题如何追溯都没写清。"
    if "未说明成功条件" in text or "成功条件" in text:
        return "什么算成功、什么算失败、测试按什么标准验收都没写清。"
    if "回滚逻辑" in text:
        return "异常后是否回滚、回到哪个状态、用户看到什么结果都没写清。"
    if "接口重复调用" in text or "重复调用" in text:
        return "重复调用时是否拦截、复用结果还是再次执行都没写清。"
    if "中途退出" in text:
        return "用户中途退出后状态是保留、清空还是恢复，PRD 没写清。"
    if "最小值" in text or "最大值" in text:
        return "输入上下限、越界后的提示和处理方式都没写清。"
    return _build_issue_meeting_explanation(item)


def _l3_meeting_statement(item: Dict[str, Any]) -> str:
    return f"{_l3_meeting_tag(item)}：{_l3_meeting_explanation(item)}"


def _l3_fix_items(item: Dict[str, Any]) -> List[str]:
    subject = _l3_focus_subject(item)
    topic = _l3_render_topic(item)
    text = _l3_issue_text(item)
    if "是否允许重试" in text:
        return [
            f"重试策略：明确“{subject}”在什么错误条件下允许重试、最多重试几次、间隔多久。",
            f"交互口径：明确“{subject}”重试按钮何时可点、重试中是否防重复点击、失败后如何提示用户。",
            f"结果一致性：明确“{subject}”重试成功、重试失败和用户放弃后的状态落点与日志记录要求。",
        ]
    if topic == "state_recovery":
        return [
            f"状态归位：明确“{subject}”在异常、中断、退出、重进后应回到哪个状态。",
            f"资源处理：写清“{subject}”回滚、恢复、资源释放和再次进入的顺序。",
            f"用户反馈：明确状态恢复失败时页面提示、运营/客服可引用的可观测字段。",
        ]
    if topic == "security_access":
        return [
            f"角色与范围：明确“{subject}”在哪些角色/场景可访问，隔离维度是账号、会话、设备还是房间/门店。",
            f"越权拦截：写清“{subject}”越权时的提示、错误码、以及是否记录审计日志。",
            f"敏感能力：对“{subject}”涉及的能力（如分享/外链/下载/展示）明确鉴权、过期、撤销与追踪要求。",
        ]
    if topic == "device_lifecycle":
        return [
            f"词义与触发：在 PRD 中逐条定义「关台/待机/重开/全断电/软重启/会话结束」等事件分别指什么、由哪些用户或系统动作触发。",
            f"状态与资源：写清各事件对会话、缓存、本地作品/队列、展示态的影响（清、不清、延迟清）。",
            f"可验收口径：为每种事件补充用户可见提示与可观测/对账项，避免多现场解释不一致。",
        ]
    if topic == "exception_flow":
        return [
            f"失败闭环：明确“{subject}”失败、超时、弱网时页面提示、错误态和是否允许重试。",
            f"状态处理：明确“{subject}”失败后状态是否回退、保留中间结果还是直接终止。",
            f"恢复口径：明确重试成功、重试失败和用户放弃后的结果展示与数据一致性规则。",
        ]
    if topic == "concurrency_control":
        return [
            f"冲突裁决：明确“{subject}”多人同时操作或高并发到达时谁先执行、谁后生效。",
            f"并发保护：补齐“{subject}”的排队、限流、加锁或幂等策略，避免重复执行。",
            f"结果反馈：明确被拦截、排队、覆盖或失败时，用户分别看到什么提示。",
        ]
    if topic == "cross_end_sync":
        return [
            f"数据主键与归属：明确“{subject}”在各客户端、各业务入口下的身份标识、资源主键与租户/组织/空间维度的唯一性规则。",
            f"同步触发与展示：写清“{subject}”何时拉取/推送/订阅、失败重试与最终一致性的可感知表现。",
            f"对账与排障：补齐“{subject}”的查询、补单、重放、以及多入口结果不一致时的处理与提示。",
        ]
    if topic == "acceptance_logging":
        return [
            f"验收标准：明确“{subject}”什么算成功、什么算失败、什么情况算部分成功。",
            f"日志要求：补齐“{subject}”关键动作、结果状态、错误原因的日志与埋点字段。",
            f"测试口径：把“{subject}”验收条件写成测试可直接执行的量化判断标准。",
        ]
    if topic == "boundary_rule":
        return [
            f"边界规则：明确“{subject}”最小值、最大值、默认值和越界处理方式。",
            f"异常提示：写清“{subject}”输入非法值时的前端提示、后端返回和是否允许纠正后重试。",
            f"兼容处理：补充历史数据或旧版本输入越界时的兼容策略。",
        ]
    if topic == "data_contract":
        return [
            f"接口定义：给“{subject}”补字段表、返回值、错误码和状态枚举说明。",
            f"同步口径：写清“{subject}”上下游接口的更新时间、来源和一致性约束。",
            f"异常返回：明确字段缺失、非法值和异常返回时的兼容策略。",
        ]
    return _build_core_issue_fix_items(item)


def _l3_test_drafts(item: Dict[str, Any]) -> List[str]:
    subject = _l3_focus_subject(item)
    topic = _l3_render_topic(item)
    text = _l3_issue_text(item)
    if "是否允许重试" in text:
        return [
            f"用例1：首次执行“{subject}”失败后点击重试，验证重试入口、次数限制和间隔符合 PRD。",
            f"用例2：连续多次重试“{subject}”，验证是否防重复触发、最终结果展示和日志记录符合 PRD。",
        ]
    if topic == "state_recovery":
        return [
            f"用例1：执行“{subject}”中途退出/切后台/再次进入，验证系统能回到 PRD 定义的目标状态。",
            f"用例2：mock 异常回滚场景，验证资源释放、页面提示和重进后的状态一致性。",
        ]
    if topic == "security_access":
        return [
            f"用例1：在 PRD 定义的各隔离维度上验证未授权/越权访问的拦截、提示与审计落库。",
            f"用例2：模拟分享/链接/扫码头失效或重放，验证与 PRD 一致的拦截与可观测性。",
        ]
    if topic == "device_lifecycle":
        return [
            f"用例1：按 PRD 列举的「关台/待机/重开/断电/软重启」组合，核对每条路径下业务状态、缓存与列表是否与定义一致。",
            f"用例2：从待机/唤醒、软重启、全断电上电后再次进入，验证与「不清空/延迟清空」等口径一致。",
        ]
    if topic == "exception_flow":
        return [
            f"用例1：mock “{subject}”失败/超时/弱网，验证页面提示、错误态和是否允许重试符合 PRD。",
            f"用例2：失败后立刻重试或放弃，验证状态回退、结果展示和数据一致性符合 PRD。",
        ]
    if topic == "concurrency_control":
        return [
            f"用例1：两人同时触发“{subject}”，验证谁先执行、谁被拦截或排队符合 PRD。",
            f"用例2：高频重复点击“{subject}”，验证是否限流、幂等或复用上一次结果。",
        ]
    if topic == "cross_end_sync":
        return [
            f"用例1：在 PRD 定义的多入口/多客户端路径下完成主操作，验证各端在约定时效内状态收敛且结果一致。",
            f"用例2：模拟网络或服务端延迟/失败/重试，验证多入口最终一致性与用户提示符合 PRD。",
        ]
    if topic == "acceptance_logging":
        return [
            f"用例1：完整执行“{subject}”，验证成功/失败判定、结果展示和验收标准可直接落地。",
            f"用例2：触发异常路径，验证日志字段、埋点和问题追溯信息满足 PRD 要求。",
        ]
    if topic == "boundary_rule":
        return [
            f"用例1：对“{subject}”输入最小值以下和最大值以上的数据，验证系统处理符合 PRD。",
            f"用例2：对“{subject}”输入空值/非法值，验证提示、拦截和纠正逻辑符合 PRD。",
        ]
    if topic == "data_contract":
        return [
            f"用例1：校验“{subject}”关键字段、错误码和状态值是否与 PRD 定义一致。",
            f"用例2：mock 字段缺失或异常返回，验证前后端兼容和错误提示是否符合 PRD。",
        ]
    return _build_issue_test_drafts(item)


def _l3_scene(item: Dict[str, Any]) -> str:
    subject = _l3_focus_subject(item)
    topic = _l3_render_topic(item)
    text = _l3_issue_text(item)
    if "是否允许重试" in text:
        return f"用户第一次执行“{subject}”失败后，不知道还能不能再试、该等多久、点哪里重试，现场容易出现反复点击或直接放弃。"
    if topic == "state_recovery":
        return (
            f"用户在“{subject}”相关流程中**退出、切换、切后台**或**异常中断**后，再进入或再打开时，"
            f"界面所展示状态与**落盘/上云/对账**等真实结果可能不一致，现场对账和纠纷难收口。"
        )
    if topic == "security_access":
        return (
            f"用户或访客通过**扫码/外链/深度链接/小程序**等路径取**资源、凭证或受控内容**时，"
            f"若未写清**谁可访问、有效期、失效与无资源时如何提示与归档**，"
            f"现场易出现“有入口却拿不到/或越权看到不该看的”的投诉，责任难界定。"
        )
    if topic == "device_lifecycle":
        return (
            f"在「{subject}」上若未与业务状态/缓存策略一对一映射，**待机与全断电、软重启与关台**会被不同现场解释成不同含义，"
            f"出现“应清未清/不应清却清”的客诉与对账困难。"
        )
    if topic == "exception_flow":
        return (
            f"在「{subject}」上若**保存/上传/写库**出现失败、超时、弱网，而**各端/各入口**的提示、重试、**残留/草稿/幂等**策略未写清，"
            f"用户会误认已成功或反复重试，**跨端或事后对账**时难解释**是否生成了可引用的成功结果/交付物**。"
        )
    if topic == "concurrency_control" and _l3_concurrency_signal_text(text):
        return (
            f"多用户或连续请求在短时间内同时作用于「{subject}」而 PRD 未写清生效顺序、互斥/排队与可观测提示时，"
            f"先写后写或重复触发可能互相覆盖，现场与日志难对同一结果复现、解释一致。"
        )
    if topic == "concurrency_control":
        return (
            f"多人或**连续操作**时若「{subject}」的**先后生效/互斥/排队**与可观测提示未写清，"
            f"现场容易出现**结果互相覆盖**、**先后两笔/两段**对不上、用户误认上一操作已**安全提交**等不一致。"
        )
    if topic == "cross_end_sync":
        return (
            "在多个客户端、渠道或组织入口同时依赖同一条主数据时，若状态刷新、归属与可见范围未写清，"
            "用户会感知为「这一侧已成功、另一侧滞后或显示不一致」，现场投诉与对账会集中暴露。"
        )
    if topic == "acceptance_logging":
        return f"“{subject}”做完后即使现场出问题，也可能因为没有统一验收标准和日志要求而无法快速判断责任与原因。"
    if topic == "boundary_rule":
        return f"用户输入边界值或非法值时，“{subject}”没有统一处理规则，现场容易出现报错、静默失败或前后端口径不一致。"
    return _build_issue_scene(item)


def _l3_impact_chain(item: Dict[str, Any]) -> str:
    subject = _l3_focus_subject(item)
    topic = _l3_render_topic(item)
    text = _l3_issue_text(item)
    if "是否允许重试" in text:
        return f"{subject}没有重试策略 -> 用户重复点击或直接放弃 -> 请求重复触发或流程中断 -> 现场结果与真实状态不一致"
    if topic == "state_recovery":
        return f"{subject}没有回退与恢复规则 -> 中断后无法回到可控状态 -> 再次进入行为不确定 -> 测试难以稳定复现"
    if topic == "security_access":
        return (
            f"{subject}缺少访问边界与审计口径 -> 越权访问或误操作难以及时发现 -> 数据泄露/纠纷风险上升"
        )
    if topic == "device_lifecycle":
        return (
            f"{subject}缺少现场口径 -> 同一名词在不同现场/不同设备电源与休眠路径下被实现成不同策略 -> 用户可感知结果与运营可解释性分裂"
        )
    if topic == "exception_flow":
        return f"{subject}缺少失败闭环 -> 用户重复操作或误判成功 -> 状态与结果失真 -> 联调和上线阶段集中暴露问题"
    if topic == "concurrency_control":
        return f"{subject}没有并发裁决 -> 多请求互相覆盖或重复执行 -> 结果不一致 -> 现场投诉与数据对账风险上升"
    if topic == "cross_end_sync":
        return (
            f"{subject}缺少主键/归属/时序/刷新等口径 -> 多入口状态不一致或可见延迟 -> 用户重复操作或误判成功/失败 -> 对账与投诉风险上升"
        )
    if topic == "acceptance_logging":
        return f"{subject}没有验收和日志口径 -> 开发与测试各自理解 -> 出问题后无法追溯 -> 返工和排障成本上升"
    if topic == "boundary_rule":
        return f"{subject}没有边界/越界规则 -> 非法输入在前后端与设备侧表现分裂 -> 难以稳定复现与对账"
    if topic == "data_contract":
        return f"{subject}字段/状态/错误码口径不统一 -> 多模块各自解释 -> 联调返工与线上异常难定位"
    return _build_issue_impact_chain(item)


def _l3_risk_reason(item: Dict[str, Any]) -> str:
    subject = _l3_focus_subject(item)
    topic = _l3_render_topic(item)
    text = _l3_issue_text(item)
    if "是否允许重试" in text:
        return f"“{subject}”没写清，失败后用户可能连续触发同一动作，也可能误以为不可再试，最终导致重复请求、状态不一致和投诉。"
    if topic == "state_recovery":
        return f"“{subject}”没写清，中断、退出和重进后的状态不可控，研发和测试都难以统一实现与复现。"
    if topic == "security_access":
        return f"“{subject}”没写清，访问控制与隔离维度不明确，越权和误伤都会发生，且事后很难复盘。"
    if topic == "device_lifecycle":
        return f"“{subject}”没写清，一线现场只能凭经验补口径，各端实现一上线就对不齐用户预期。"
    if topic == "exception_flow":
        return f"“{subject}”没写清，失败后用户能否重试、系统是否回退都要靠实现时猜测，极易引发状态错乱和现场误判。"
    if topic == "concurrency_control" and _l3_concurrency_signal_text(text):
        return (
            f"“{subject}”没写清，多请求/多用户下的生效顺序、覆盖规则与可观测性假设不一致，"
            f"现网与测试环境都难稳定复现同一结果。"
        )
    if topic == "concurrency_control":
        return f"“{subject}”没写清，真实运行时多人操作和高并发请求会出现互相覆盖、重复执行或前后口径不一致。"
    if topic == "cross_end_sync":
        return (
            f"“{subject}”没写清，多入口下的数据标识、时序与对账口径在实现上易分歧，"
            f"联调、验收与现网对同一状态难以给出一致解释。"
        )
    if topic == "acceptance_logging":
        return f"“{subject}”没写清，开发不知道做到什么算完成，测试也无法直接据此验收和追溯。"
    if topic in ("boundary_rule", "data_contract"):
        return f"“{subject}”在 PRD 中缺少可核对的明确定义，研发与测试会各自取不同解释，上线后集中暴露为口径/数据问题。"
    return _build_risk_analysis_text(
        str(item.get("name") or ""),
        str(item.get("type") or ""),
        str(item.get("description") or ""),
        str(item.get("reason") or ""),
        "; ".join(_issue_modules(item)),
    )


def _l3_match_merged_issue_defects(issue: Dict[str, Any], defects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    target = str((issue or {}).get("name") or "").strip()
    matched: List[Dict[str, Any]] = []
    if not target:
        return matched
    for defect in defects or []:
        if not isinstance(defect, dict):
            continue
        if _core_group_name(defect) == target:
            matched.append(defect)
    matched.sort(
        key=lambda d: (
            _risk_rank_local(str(d.get("risk_level") or "")),
            -(
                1
                if _l3_is_usable_quote(_issue_quote(d))
                else 0
            ),
            -len(_l3_clean_problem_text(d.get("description"))),
            -len(_clean_report_text(d.get("reason"))),
        )
    )
    return matched


def _l3_pick_seed_issue(issue: Dict[str, Any], defects: List[Dict[str, Any]]) -> Dict[str, Any]:
    matched = _l3_match_merged_issue_defects(issue, defects)
    if matched:
        issue_topic = _l3_render_topic(issue if isinstance(issue, dict) else {})
        matched.sort(
            key=lambda d: (
                1 if _l3_render_topic(d) == issue_topic else 0,
                _risk_rank_local(str(d.get("risk_level") or "")),
                1 if _l3_is_usable_quote(_issue_quote(d)) else 0,
                len(_l3_clean_problem_text(d.get("description"))),
                len(_clean_report_text(d.get("reason"))),
            ),
            reverse=True,
        )
        return matched[0]
    return issue if isinstance(issue, dict) else {}


def _l3_issue_keywords(issue: Dict[str, Any], defects: List[Dict[str, Any]]) -> List[str]:
    keywords: List[str] = []

    def add(value: Any):
        s = _clean_report_text(value)
        if not s or s == "【PRD未说明】":
            return
        s = re.sub(r"[“”\"'《》【】\(\)（）]", " ", s)
        for part in re.split(r"[，。；：、/\s]+", s):
            token = part.strip()
            if len(token) < 2:
                continue
            if token in {"未定义", "未说明", "缺失", "处理", "规则", "场景", "问题", "模块", "流程"}:
                continue
            if token not in keywords:
                keywords.append(token)

    add((issue or {}).get("name"))
    add((issue or {}).get("description"))
    add((issue or {}).get("reason"))
    for item in defects or []:
        if not isinstance(item, dict):
            continue
        add(item.get("type"))
        add(item.get("description"))
        add(item.get("reason"))

    topic = _l3_render_topic((defects or [issue])[0] if defects else issue or {})
    topic_keywords = {
        "exception_flow": ["失败", "超时", "弱网", "断网", "重试", "提示", "错误态"],
        "concurrency_control": ["并发", "多人同时操作", "排队", "限流", "幂等", "重复调用"],
        "state_recovery": ["回滚", "恢复", "退出", "重进", "切后台", "状态"],
        "acceptance_logging": ["日志", "埋点", "审计", "成功条件", "验收", "追溯"],
        "boundary_rule": ["最小值", "最大值", "边界", "空值", "非法值"],
        "data_contract": ["字段", "返回", "错误码", "默认值", "状态值"],
        "security_access": ["权限", "鉴权", "越权", "风控"],
    }
    for token in topic_keywords.get(topic, []):
        if token not in keywords:
            keywords.append(token)
    return keywords[:20]


def _l3_block_sentence_candidates(block: Dict[str, Any]) -> List[str]:
    title = _clean_report_text((block or {}).get("title"))
    content = str((block or {}).get("content") or "")
    pieces: List[str] = []
    if content.strip():
        pieces.extend(re.split(r"[\r\n]+|(?<=[。！？；])", content))
    out: List[str] = []
    for piece in pieces:
        s = _clean_report_text(piece)
        if not s:
            continue
        if len(s) < 10:
            continue
        if title and s == title:
            continue
        if s.endswith(("：", ":")):
            continue
        if re.match(r"^(?:[-*•]|[a-zA-Z]\.|[0-9一二三四五六七八九十]+[\.\)、])", s) and len(s) < 20:
            continue
        if _l3_is_usable_quote(s) and s not in out:
            out.append(s)
    return out


def _l3_quote_score(sentence: str, keywords: List[str]) -> int:
    s = _clean_report_text(sentence)
    if not _l3_is_usable_quote(s):
        return -99
    score = 0
    hits = sum(1 for kw in keywords if kw and kw in s)
    score += hits * 3
    if any(p in s for p in ["，", "。", "；", "："]):
        score += 2
    if any(k in s for k in ["如果", "当", "则", "是否", "必须", "应", "需要", "成功", "失败", "退出", "恢复", "提示", "重试", "并发", "同时"]):
        score += 3
    if 14 <= len(s) <= 120:
        score += 2
    if len(s) < 14:
        score -= 2
    if re.search(r"[「」•]", s):
        score -= 2
    if re.search(r"[\u2E80-\u2FDF]", s):
        score -= 6
    return score


def _l3_collect_stage1_quotes(issue: Dict[str, Any], defects: List[Dict[str, Any]], stage1_snapshot: Dict[str, Any], limit: int = 3) -> List[str]:
    blocks = (stage1_snapshot or {}).get("blocks") if isinstance(stage1_snapshot, dict) else []
    if not isinstance(blocks, list):
        return []
    keywords = _l3_issue_keywords(issue, defects)
    if not keywords:
        return []
    ranked_blocks = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        title = _clean_report_text(block.get("title"))
        content = _clean_report_text(block.get("content"), keep_newlines=True)
        haystack = f"{title}\n{content}"
        if not haystack.strip():
            continue
        score = 0
        for kw in keywords:
            if kw and kw in haystack:
                score += max(1, min(len(kw), 6))
        if score <= 0:
            continue
        ranked_blocks.append((score, len(content), block))
    ranked_blocks.sort(key=lambda x: (-x[0], -x[1]))
    quotes: List[str] = []
    for _, _, block in ranked_blocks[:8]:
        sentences = _l3_block_sentence_candidates(block)
        scored_sentences = []
        for sentence in sentences:
            hits = sum(1 for kw in keywords if kw and kw in sentence)
            score = _l3_quote_score(sentence, keywords)
            if hits < 2 or score < 8:
                continue
            scored_sentences.append((score, sentence))
        scored_sentences.sort(key=lambda x: (-x[0], -len(x[1])))
        for _, sentence in scored_sentences:
            if sentence not in quotes:
                quotes.append(sentence)
            if len(quotes) >= limit:
                return quotes
    return quotes


def _l3_collect_issue_quotes(
    issue: Dict[str, Any],
    defects: List[Dict[str, Any]],
    stage1_snapshot: Optional[Dict[str, Any]] = None,
    limit: int = 3,
) -> List[str]:
    quotes: List[str] = []
    candidates = list(defects or [])
    if isinstance(issue, dict):
        candidates.append(issue)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for anchor in _issue_anchors(item):
            quote = _anchor_quote(anchor)
            if _l3_is_usable_quote(quote) and quote not in quotes:
                quotes.append(quote)
                if len(quotes) >= limit:
                    return quotes
        quote = _issue_quote(item)
        if _l3_is_usable_quote(quote) and quote not in quotes:
            quotes.append(quote)
            if len(quotes) >= limit:
                return quotes
    for quote in _l3_collect_stage1_quotes(issue, defects, stage1_snapshot or {}, limit=limit):
        if quote not in quotes:
            quotes.append(quote)
            if len(quotes) >= limit:
                return quotes
    return quotes


def _l3_issue_module_label(item: Dict[str, Any]) -> str:
    subject = _l3_focus_subject(item)
    if subject and subject != "关键业务规则":
        return subject
    feature = _derive_feature_name(item)
    if feature and feature != "关键业务规则" and not _is_rule_style_subject(feature):
        return feature
    modules = _issue_modules(item)
    if modules:
        return modules[0]
    return str(item.get("module") or "全局或上下文推导")


def _l3_dedupe_issue_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped = {}
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        key = f"{card.get('title') or ''}|{card.get('problem') or ''}"
        if key not in deduped:
            deduped[key] = dict(card)
            deduped[key]["quotes"] = list(card.get("quotes") or [])
            deduped[key]["related_defects"] = list(card.get("related_defects") or [])
            continue
        current = deduped[key]
        for quote in card.get("quotes") or []:
            if quote not in current["quotes"]:
                current["quotes"].append(quote)
        for defect in card.get("related_defects") or []:
            if defect not in current["related_defects"]:
                current["related_defects"].append(defect)
        current["level"] = min([current.get("level", "P2"), card.get("level", "P2")], key=_risk_rank_local)
    arr = list(deduped.values())
    arr.sort(key=lambda x: (_risk_rank_local(x.get("level", "P2")), -len(str(x.get("problem") or ""))))
    return arr


def _l3_defect_badge(seed: Optional[Dict[str, Any]]) -> str:
    if not isinstance(seed, dict):
        return ""
    rid = str(seed.get("id") or "").strip()
    if rid and re.match(r"^[A-Za-z0-9_\-]+$", rid):
        return f"`{rid}`"
    anch = str(seed.get("anchor") or "").strip()
    if anch and len(anch) <= 48:
        return f"`{anch}`"
    return ""


def _l3_infer_cluster_display_name(base_title: str) -> str:
    t = str(base_title or "").strip()
    if any(k in t for k in ("端云", "列表一致", "同步与列表", "多端同步", "跨端与数据")):
        return "跨端与一致规则簇"
    if "失败" in t or "异常" in t:
        return "失败与异常路径规则簇"
    if "并发" in t or "冲突" in t or "同时" in t:
        return "并发与冲突裁决规则簇"
    if len(t) > 20:
        return t[:18] + "…规则簇"
    return f"{t}（合并类）"


def _l3_merge_p1_card_group(group: List[Dict[str, Any]], base_title: str) -> Dict[str, Any]:
    """将同名 P1 多行压成一行：子点用 <br> 列表展示。"""
    n = len(group)
    first = dict(group[0])
    bt = _l3_strip_title_bold_marks(str(base_title or ""))
    cname = _l3_infer_cluster_display_name(bt)
    cname = _l3_strip_title_bold_marks(cname)
    first["title"] = f"**{cname}**（含 {n} 项子缺陷）"
    all_quotes: List[str] = []
    prob_lines: List[str] = []
    seen_q: set = set()
    for i, c in enumerate(group, start=1):
        for q in c.get("quotes") or []:
            qs = str(q or "").strip()
            if qs and qs not in seen_q:
                seen_q.add(qs)
                all_quotes.append(qs)
        sd = c.get("seed") if isinstance(c.get("seed"), dict) else {}
        badge = _l3_defect_badge(sd) or f"子点{i}"
        p = str(c.get("problem") or "").strip()
        if len(p) > 100:
            p = p[:98] + "…"
        prob_lines.append(f"- {p}（{badge}）")
    first["quotes"] = all_quotes[:8]
    first["problem"] = "<br>".join(prob_lines) if prob_lines else str(first.get("problem") or "")
    # 现场/风险：同类「跨端/多入口」问题给一段合并口径，与逐条互补；不限定某一行业 PRD。
    text_blob = " ".join(_l3_issue_text(c.get("seed") or {}) for c in group if isinstance(c.get("seed"), dict))
    if _l3_cross_client_signal_text(text_blob):
        first["scene"] = (
            "多入口、多角色同时依赖同一条主数据时，若主键/归属/时序、刷新与失败提示在 PRD 中未闭环，"
            "各实现方易对「谁看见什么、何时算成功」理解不一，现场与运营对账会集中爆雷。"
        )
        first["risk_reason"] = (
            "未写清可观测、可对账的口径时，联调与现网难统一，问题定责与复现成本高。"
        )
    else:
        first["scene"] = str(group[0].get("scene") or "").strip()
        first["risk_reason"] = str(group[0].get("risk_reason") or "").strip()
    fix_all: List[str] = []
    seen_f: set = set()
    for c in group:
        for f in c.get("fix_items") or []:
            fs = str(f or "").strip()
            if fs and fs not in seen_f:
                seen_f.add(fs)
                fix_all.append(fs)
    first["fix_items"] = fix_all[:5]
    rel: List[Any] = []
    for c in group:
        for d in c.get("related_defects") or []:
            if d not in rel:
                rel.append(d)
    first["related_defects"] = rel
    return first


def _l3_cluster_p1_by_identical_title(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """核心问题矩阵：将同等级且标题完全相同的卡片合并为一条，减少重复刷屏。"""
    if not cards:
        return []
    n = len(cards)
    used = [False] * n
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(cards):
        if used[i]:
            continue
        lv = str(c.get("level") or "P2").upper()
        tit = str(c.get("title") or "").strip()
        if not tit:
            out.append(c)
            used[i] = True
            continue
        group_idx = [i]
        for j in range(i + 1, n):
            if used[j]:
                continue
            cj = cards[j]
            if str(cj.get("level") or "").upper() == lv and str(cj.get("title") or "").strip() == tit:
                group_idx.append(j)
        if len(group_idx) >= 2:
            group = [cards[k] for k in group_idx]
            for k in group_idx:
                used[k] = True
            if lv == "P1":
                out.append(_l3_merge_p1_card_group(group, tit))
            else:
                merged_card = dict(group[0])
                merged_card["title"] = f"**{_l3_strip_title_bold_marks(tit)}**（含 {len(group)} 项子缺陷）"
                all_quotes: List[str] = []
                seen_q: set = set()
                prob_lines: List[str] = []
                rel: List[Any] = []
                fix_all: List[str] = []
                seen_f: set = set()
                for idx, gc in enumerate(group, start=1):
                    for q in gc.get("quotes") or []:
                        qs = str(q or "").strip()
                        if qs and qs not in seen_q:
                            seen_q.add(qs)
                            all_quotes.append(qs)
                    sd = gc.get("seed") if isinstance(gc.get("seed"), dict) else {}
                    badge = _l3_defect_badge(sd) or f"子点{idx}"
                    p = str(gc.get("problem") or "").strip()
                    if len(p) > 100:
                        p = p[:98] + "…"
                    prob_lines.append(f"- {p}（{badge}）")
                    for d in gc.get("related_defects") or []:
                        if d not in rel:
                            rel.append(d)
                    for f in gc.get("fix_items") or []:
                        fs = str(f or "").strip()
                        if fs and fs not in seen_f:
                            seen_f.add(fs)
                            fix_all.append(fs)
                merged_card["quotes"] = all_quotes[:8]
                merged_card["problem"] = "<br>".join(prob_lines) if prob_lines else str(merged_card.get("problem") or "")
                merged_card["related_defects"] = rel
                merged_card["fix_items"] = fix_all[:5]
                out.append(merged_card)
        else:
            out.append(c)
            used[i] = True
    return out


def _l3_summary_from_cards(issue_cards: List[Dict[str, Any]], defects: List[Dict[str, Any]]) -> Tuple[str, str, List[str]]:
    p0 = sum(1 for c in issue_cards if str(c.get("level") or "").upper() == "P0")
    p1 = sum(1 for c in issue_cards if str(c.get("level") or "").upper() == "P1")
    p2 = sum(1 for c in issue_cards if str(c.get("level") or "").upper() == "P2")
    if not issue_cards:
        p0 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P0")
        p1 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P1")
        p2 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P2")
    counts = f"P0 {p0} 项"
    if p1:
        counts += f"、P1 {p1} 项"
    if p2:
        counts += f"、P2 {p2} 项"
    top_titles: List[str] = []
    top3: List[str] = []
    # 摘要优先展示高风险项，但若 P0 不足 3 条，则继续补 P1/P2，避免只剩 1 条。
    title_seen: set = set()
    meeting_seen: set = set()
    for card in issue_cards:
        level = str(card.get("level") or "P2").upper()
        title = str(card.get("title") or "").strip()
        if title in title_seen:
            continue
        title_seen.add(title)
        if title and title not in top_titles:
            top_titles.append(title)
        statement = str(card.get("meeting_statement") or "").strip()
        if statement and statement not in meeting_seen:
            bullet = f"{statement}（{level}）"
            meeting_seen.add(statement)
        else:
            bullet = f"{title}（{level}）" if title else ""
        if bullet and bullet not in top3:
            top3.append(bullet)
        if len(top3) >= 3:
            break
    if top_titles:
        main_problem = f"当前 PRD 存在 {counts} 待处理问题，优先需补齐：{'；'.join(top_titles[:3])}。"
    else:
        main_problem = f"当前 PRD 存在 {counts} 待处理问题，建议先完成阻断项澄清后再推进开发。"
    one_liner = "这份 PRD 不是不能做，而是现在做一定边做边改。" if p0 > 0 else "这份 PRD 主干清晰，抓紧补齐边缘细节即可交付。"
    return main_problem, one_liner, top3[:3]


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
        
    # --- 增加 LLM 润色节点 ---
    try:
        from utils.llm_client import call_llm
        
        # 1. 润色核心目标
        prompt_goal = f"""
你是一个严格的文档审校专家。请将下面的机器拼接词汇，提炼成极度通顺、干练的一句话（例如“电商下单 = 挑选商品 + 优惠券抵扣 + 在线支付 + 生成订单”），作为业务共识的核心流述。
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
                paragraph = "规范本需求在各类场景下的展示行为与核心逻辑"
        else:
            if not paragraph or len(paragraph.strip()) < 5 or paragraph.strip() == "版":
                paragraph = "规范本需求在各类场景下的展示行为与核心逻辑"

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
【极度重要】：如果发现明显与当前核心业务无关的遗留功能点（如模板中带过来的历史示例功能、旧项目名、无关模块名等），必须直接将其【删除】，严禁在输出中保留它们！

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


def _build_l1_issue_quote(item: Dict[str, Any]) -> str:
    quote = _issue_quote(item)
    if quote:
        return quote
    return "【未定位到可直接引用的 PRD 原文】"


def _build_l1_issue_impact(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    feature_obj = _subject_object_name(feature)
    text = _issue_blob(item)
    if _build_l2_issue_kind(item) == "conflict":
        return (
            "可执行产品规则在**入口/端/操作路径**上口径互斥，研发/测试/运营在联调期会各取一端，验收无法对齐；"
            "若含开停录/上传/外显/计费，现场易出现客诉与**合规/隐私**争议。"
        )
    topic = _issue_topic(item)
    if topic == "qr_security":
        return f"“{feature_obj}”的权限边界不清，容易引发越权操作和合规风险。"
    if topic == "cloud_degrade":
        return f"“{feature}”在弱网、超时或重启场景下口径不一，开发实现和验收结果会不一致。"
    if topic == "rating_switch":
        return "配置变化与进行中任务的生效口径不清，现场容易出现状态冲突。"
    if topic == "transfer_cleanup":
        return "上下文切换、重启或环境重置后的清理与恢复规则不清，现场容易出现多种解释。"
    if topic == "sync_latency":
        return f"“{feature}”没有统一同步时效，用户会看到多端状态不一致。"
    if any(k in text for k in ["字段", "数据", "一致性", "默认值", "错误码"]):
        return f"“{feature_obj}”的接口口径不统一，开发和测试会各自按自己的理解实现。"
    if any(k in text for k in ["状态", "回滚", "恢复", "切换"]):
        return f"“{feature}”没写清，切换失败或中断后系统行为会前后不一致。"
    if any(k in text for k in ["失败", "超时", "中断", "重试", "断网"]):
        return f"“{feature}”没写清，失败后用户能做什么、系统如何兜底无法统一。"
    return f"“{feature}”的关键规则不清，继续开发容易在联调和验收阶段返工。"


def _l1_classify_for_fatal_lane(item: Dict[str, Any]) -> str:
    """
    L1「三致命伤」行首归属：A=商业/互斥/资损口径，B=体验/多端/状态/异常/联调，C=安全/权限/敏感路径。
    与固定栏位文案对应，避免把「跨端同步」等填进安全栏的观感错位。
    """
    if not isinstance(item, dict):
        return "B"
    kind = _build_l2_issue_kind(item)
    topic = _issue_topic(item)
    name = str(item.get("name") or "")
    text = f"{_issue_blob(item)} {name}"
    if kind == "security" or topic in ("qr_security", "security_access"):
        return "C"
    if re.search(
        r"越权|未授权访问|未鉴权|隐私(?!点)\s*风险|凭证明文|敏感链|401|403",
        text,
    ) and re.search(
        r"越权|未授权|鉴权|泄露|隐私|二维码|凭证|安全|权限", text
    ):
        return "C"
    if kind == "conflict" or re.search(
        r"资损|营收|计费|对账(?!性)|合规模|商业(?!化)|可受理客诉|互相打架|规则冲突|逻辑矛盾|互斥"
        r"|合规风险|客诉(?!点)",
        text,
    ):
        return "A"
    if kind in ("state", "exception", "dispatch", "concurrency", "generic") or topic in (
        "sync_latency",
        "cloud_degrade",
        "state_recovery",
        "exception_flow",
        "transfer_cleanup",
        "rating_switch",
        "data_contract",
        "performance_metric",
        "acceptance_logging",
        "generic_rule",
    ):
        return "B"
    if topic in ("boundary_rule",):
        return "A" if re.search(r"合规模|资损|对账|计费", text) else "B"
    return "B"


def _l1_best_in_fatal_lane(
    pool: List[Dict[str, Any]], letter: str
) -> Optional[Dict[str, Any]]:
    cands = [
        x
        for x in pool
        if isinstance(x, dict) and _l1_classify_for_fatal_lane(x) == letter
    ]
    if not cands:
        return None
    cands.sort(
        key=lambda d: (
            _risk_rank_local(str(d.get("risk_level") or "")),
            -len(str(d.get("description") or "")) - len(_build_core_issue_title(d)),
        )
    )
    return cands[0]


def _build_l1_issue_action(item: Dict[str, Any]) -> str:
    feature = _derive_feature_name(item)
    feature_obj = _subject_object_name(feature)
    topic = _issue_topic(item)
    text = _issue_blob(item)
    if topic == "qr_security":
        return f"补齐“{feature_obj}”谁可访问、何时失效、越权时如何拦截。"
    if topic == "cloud_degrade":
        return f"补齐“{feature}”在弱网、超时、重启后的状态保留、恢复与重试规则。"
    if topic == "rating_switch":
        return "补齐配置变更对当前任务、下一次任务和页面提示的生效规则。"
    if topic == "transfer_cleanup":
        return "补齐上下文切换、重启、环境重置场景后的清理、保留与恢复规则。"
    if topic == "sync_latency":
        return f"补齐“{feature}”的同步时效、超时提示和补偿口径。"
    if any(k in text for k in ["字段", "数据", "一致性", "默认值", "错误码"]):
        return f"补齐“{feature_obj}”的字段定义、状态值、错误码和更新时机。"
    if any(k in text for k in ["状态", "回滚", "恢复", "切换"]):
        return f"补齐“{feature}”成功、失败、中断后的目标状态和恢复规则。"
    if any(k in text for k in ["失败", "超时", "中断", "重试", "断网"]):
        return f"补齐“{feature}”失败后的提示、重试条件和兜底处理。"
    return f"补齐“{feature}”的触发条件、处理动作和验收口径。"


def _dedupe_defects_for_l3_matrix(defects: List[Dict[str, Any]], limit: int = 60) -> List[Dict[str, Any]]:
    """与合并矩阵重复行去重，减轻「同义重复」；不改变上游 defects 原列表。"""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for d in defects or []:
        if not isinstance(d, dict):
            continue
        key = (
            str(d.get("risk_level") or "").upper(),
            (str(d.get("module") or ""))[:40],
            (str(d.get("anchor") or ""))[:80],
            (str(d.get("description") or ""))[:60],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
        if len(out) >= int(limit or 60):
            break
    return out


def _l1_gate_traffic(p0: int, p1: int, score: float) -> Tuple[str, str]:
    """L1 红绿灯：与 P0/质量分弱绑定，定性描述；细则见 L3。"""
    try:
        sc = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        sc = 0.0
    if p0 > 0 or sc < 5.0:
        return (
            "🔴 **建议打回**",
            "存在 P0 级问题或综合质量分过低，不建议全量排期，直至主要阻塞项在 PRD/评审中闭环。",
        )
    if sc < 6.5 and p1 >= 2:
        return (
            "🟡 **限制开工**",
            "P1 项较多且综合分偏低，宜先冻结关键规则与红线条款，再对全量开发发排。",
        )
    if sc < 7.0:
        return (
            "🟡 **限制开工**",
            "可并行准备设计与澄清，但须按 L3 缺口分层排期，避免主链上待确认点堆积到联调。",
        )
    return (
        "🟢 **准予开工**",
        "在已列风险有跟踪与验收路径的前提下，可按迭代进入开发；仍须以 L3 为执行依据。",
    )


def _l1_risk_forecast_qualitative(p0: int, p1: int, p2: int, score: float) -> str:
    """不编造具体 %/周，只给可复核的定性预判。"""
    if p0 > 0:
        return "在关键状态/异常/裁决规则未落盘前全量实现，主链与回归返工**风险高**；与「打回/限制」结论同向，详见 L3。"
    if p1 > 0 or (score is not None and float(score) < 7.0):
        return "在待确认项与边界未冻结前开干，**联调与用例**大概率反复，成本高于一次性澄清。"
    return "在持续跟踪 L3 缺口关闭的前提下，交付与验收路径**相对可预期**。"


def _l1_build_fatal_three(merged: List[Dict[str, Any]], defects: List[Dict[str, Any]]) -> List[str]:
    seen: set = set()
    pool: List[Dict[str, Any]] = []
    for it in merged or []:
        if not isinstance(it, dict):
            continue
        title_text = _build_core_issue_title(it)
        if title_text in seen:
            continue
        seen.add(title_text)
        pool.append(it)
    if not pool:
        return [
            "【A 业务/价值】本轮未从合并问题中形成独立致命条，**以 L3 缺陷全表为准**补充。",
            "【B 稳定性/体验】本轮未从合并问题中形成独立致命条，**以 L3 缺陷全表为准**补充。",
            "【C 安全/控制】本轮未从合并问题中形成独立致命条，**以 L3 缺陷全表为准**补充。",
        ]
    slot_a = _l1_best_in_fatal_lane(pool, "A")
    slot_b = _l1_best_in_fatal_lane(pool, "B")
    slot_c = _l1_best_in_fatal_lane(pool, "C")

    def fmt(label: str, it: Optional[Dict[str, Any]]) -> str:
        if not it:
            return f"{label}（**本轮合并未单列为致命条，以 L3 矩阵为母表**）"
        t = _build_core_issue_title(it)
        lv = str(it.get("risk_level") or "P2").upper()
        return f"{label} **{t}**（{lv}）：{_build_l1_issue_impact(it)[:220]}"

    return [
        fmt("【A】对业务/商业、合规或资损的影响：", slot_a),
        fmt("【B】对体验与稳定性（状态、异常、联调/演示）：", slot_b),
        fmt("【C】对安全、权限与数据边界的控制：", slot_c),
    ]


def _build_l1_local_report(stage3_json: Dict[str, Any]) -> str:
    s = (stage3_json or {}).get("summary") or {}
    defects = (stage3_json or {}).get("defects") or []
    defects = defects if isinstance(defects, list) else []
    merged = (stage3_json or {}).get("merged_issues") or []
    merged = merged if isinstance(merged, list) else []
    core = (stage3_json or {}).get("core_risk_summary") or {}
    score = s.get("quality_score", 0)
    try:
        score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    p0 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P2")
    gate_title, gate_note = _l1_gate_traffic(p0, p1, score)
    forecast = _l1_risk_forecast_qualitative(p0, p1, p2, score)
    one_liner = str(core.get("one_liner") or "需结合 L3 全量问题与红线条款评估后再排期。")

    seen_titles = set()
    deduped_merged: List[Dict[str, Any]] = []
    for it in merged:
        if not isinstance(it, dict):
            continue
        title_text = _build_core_issue_title(it)
        if title_text in seen_titles:
            continue
        seen_titles.add(title_text)
        deduped_merged.append(it)

    fatal3 = _l1_build_fatal_three(deduped_merged, defects)
    lines: List[str] = []
    lines.append("# 第一部分：L1 管理摘要（面向决策层，本地生成）")
    lines.append("> 目的：约 30 秒内了解是否具备全量排期/开工条件；**执行与引用以 L3 为母表**。")
    lines.append("")
    lines.append("## 1. 审计结论")
    lines.append(f"- **当前建议**：{gate_title} — {gate_note}")
    lines.append(f"- **综合质量分**：{round(score, 1)}/10（与 Stage3 七维/缺陷综合一致，口径以本次扫描为准）")
    lines.append(f"- **P0 / P1 / P2 计数**：{p0} / {p1} / {p2}；**一句话概览**：{one_liner}")
    lines.append(f"- **核心风险预判（定性）**：{forecast}")
    lines.append("")
    lines.append("## 2. 三个致命伤（业务语言版，与 L3 同源合并项）")
    if len([x for x in fatal3 if isinstance(x, str) and x.strip()]) < 3:
        fatal3 = (core.get("top3") if isinstance(core.get("top3"), list) else [])[:3] or fatal3
    for line in fatal3:
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## 3. 项目启动红线（准入准则，达不到技术团队可拒绝全量排期）")
    lines.append("- **逻辑闭环**：须具备含「模式切换、异常回滚、**空闲态归位**」的完整**状态转移/状态机说明**（可文档或图），并与 PRD 锚点可核对；缺口见 L3 对应项。")
    lines.append("- **路径完整**：核心主流程须具备**进入、操作、报错/失败、退出**等节点说明或原型/交互稿，缺口见 L3。")
    lines.append("- **规则裁决**：存在多任务/多流程抢占时，须具备**优先级裁决/冲突处理**的可验收表述；缺口见 L3。")
    if any(
        isinstance(x, dict) and _build_l2_issue_kind(x) == "conflict" for x in deduped_merged
    ):
        lines.append(
            "- **互斥可落地**：若存在**入口/端/能力**上互斥的强制业务规则，须具备**裁决顺序、手动否决权、产品提示/阻断**的可验收表述，并与 PRD 锚点可核对；缺口见 L3。"
        )
    lines.append("")
    lines.append("## 4. 决策参考（可转发 PO / PM）")
    if deduped_merged:
        lines.append("| 序 | 合并类核心问题 | 风险 | 可指向 L2/L3 |")
        lines.append("| :-- | :-- | :-- | :-- |")
        for i, it in enumerate(deduped_merged[:5], start=1):
            tit = _build_core_issue_title(it)
            lv = str(it.get("risk_level") or "P2").upper()
            lines.append(f"| {i} | {tit.replace('|', ' ')} | {lv} | 详见 L2 澄清与 L3 行级缺陷 |")
    else:
        lines.append("- 本轮无合并类核心问题，可直接以 L3 缺陷全表为决策依据。")
    lines.append("")
    return "\n".join(lines).strip()


def _build_l2_issue_kind(item: Dict[str, Any]) -> str:
    text = _issue_blob(item)
    name = str(item.get("name") or "")
    types_blob = " ".join(_ensure_list(item.get("types")))
    topic = _issue_topic(item)
    feature = _derive_feature_name(item)
    head = f"{name}{text}{types_blob}"
    # 业务规则/入口互斥 必须优先于「端云/状态机」等泛化信号，避免与具体垂类强绑：用语义（矛盾/互斥+开关/自动/入口）
    if any(
        k in head
        for k in (
            "逻辑矛盾",
            "自相矛盾",
            "规则冲突",
        )
    ) or re.search(r"(存在|的|为)(矛盾|冲突|不一致|互斥)(，|。|$)", name + text):
        return "conflict"
    if ("矛盾" in text or "冲突" in text) and any(
        x in text
        for x in (
            "开关",
            "控制",
            "自动",
            "默认",
            "策略",
            "入口",
            "强开",
            "强关",
            "手动",
        )
    ):
        return "conflict"
    if re.search(
        r"(开|关|启|停).{0,12}(与|和).{0,12}(自动|默认|强制|策略|另一入口|另一终端)",
        text,
    ) and re.search(r"(互斥|矛盾|冲突|不一致|打架)", text):
        return "conflict"
    # 用「描述+理由+类型」判断异常主路径，避免与合并名里的「…恢复…」等词串台；失败/存盘/上云须优先于「调度/抢占」类标题
    def _l2m_sub() -> str:
        return f"{str(item.get('description') or '')} {str(item.get('reason') or '')} {' '.join(_ensure_list(item.get('types')))}".strip()

    sub_k = _l2m_sub()
    l2_exception_strong = bool(
        re.search(
            r"(只写|只描述)成功|缺少失败|没[有写].{0,2}失败|无失败(?!的)|失败(?!的).{0,3}(处理|机制|提示|重试|路径|分支)|"
            r"保存失败|上传失败|写库|落地失败|没有失败|网络异常|空间不足|弱网|断网|超时(?!上)|"
            r"卡死|转圈|503|错误码|未定义.*异常|异常提示(?!词)",
            sub_k,
        )
    )
    # 外显/受控资源入口（扫码/外链/取回/列表等）+ 主路径外缺失败：与纯「L0001 主路径无失败」区分，避免 L2 两条同题撞车
    comb_entry = f"{name} {sub_k} {text} {types_blob}"
    if l2_exception_strong and re.search(
        r"(二维码|扫码|外链|深度链接|深链|小程序|已唱|获取(?!的)|受控|凭证|分享(?!的)|下传|"
        r"上云(?!的)|回传|同步(?!的).{0,4}(云|端|手|移动))",
        comb_entry,
    ):
        return "security"
    exception_hit = any(
        k in (sub_k + text) for k in ("弱网", "断网", "超时", "失败", "降级", "兜底", "卡死", "转圈", "不可用", "重试", "重试后")
    ) or l2_exception_strong
    dispatch_name_hit = any(
        k in name
        for k in ["优先级", "调度", "抢占", "插播", "恢复机制", "恢复规则", "多任务并发", "状态调度"]
    )
    # 「重试」易与「失败可重试」混：调度类恢复词不含单独「重试」；以续播/回主链等为准
    dispatch_context_hit = any(k in (sub_k + text) for k in ["优先级", "调度", "抢占", "插播", "中断", "打断", "冲突裁决", "同时触发", "生效顺序"])
    dispatch_recovery_hit = any(
        k in (sub_k + text) for k in ("续播", "重播", "补播", "回到主流程", "回到原流程", "恢复原状态", "恢复机制", "恢复规则")
    )
    dispatch_context_name = any(
        k in name
        for k in ("调度", "抢占", "恢复机制", "恢复规则", "并发与状态", "多任务", "打断", "插播", "多任务并发")
    )
    dispatch_hit = bool(
        (dispatch_name_hit and dispatch_recovery_hit)
        or (dispatch_context_name and dispatch_context_hit and dispatch_recovery_hit)
        or ("多任务并发与状态调度" in name)
    )
    if l2_exception_strong or (
        re.search(
            r"(只写|只描述)成功|没有失败(?!的)?|缺[少无].{0,2}失败|失败.{0,3}(处理|机制|提示|重试(?!后)|路径)|"
            r"保存失败|上传失败|写库|落地|网络异常|空间不足|弱网|断网|超时(?!上)|"
            r"只写了成功|主路径(?!的)",
            sub_k,
        )
    ):
        return "exception"
    state_hit = any(k in text for k in ["终态", "状态机", "状态", "回退", "恢复", "落盘", "对账", "一致性", "端云", "接口", "数据覆盖", "错误码", "口径"])
    security_hit = topic == "qr_security" or any(k in text for k in ["权限", "鉴权", "越权", "泄露"])
    strong_state_hit = any(k in text for k in ["终态", "落盘", "对账", "一致性", "端云", "数据覆盖", "接口", "错误码"])
    physical_mode_hit = any(k in text for k in ["横屏", "竖屏", "旋转", "屏幕方向", "物理模式", "模式切换", "可逆路径", "回到可用", "回到空闲态"])
    state_name_hit = any(k in name for k in ["状态机", "中断恢复", "一致性", "对账", "终态", "回退", "闭环", "物理模式", "兼容"])
    feature_state_hit = any(k in feature for k in ["状态", "恢复", "回退", "回程", "终态", "可逆", "兼容"])
    concurrency_hit = any(k in text for k in ["高并发", "并发", "重复操作", "幂等", "排队", "限流", "锁", "串行化", "请求竞争", "冲突裁决", "先入为主", "后来居上"])
    # 勿用「一致性」作并发名命中，否则与跨端/同步类问题混淆
    concurrency_name_hit = any(k in name for k in ["并发", "幂等", "排队", "限流", "冲突裁决"])
    if "多任务并发与状态调度冲突" in name:
        return "dispatch"
    if dispatch_hit and not strong_state_hit:
        return "dispatch"
    if concurrency_hit or concurrency_name_hit:
        return "concurrency"
    if strong_state_hit or physical_mode_hit or state_name_hit or feature_state_hit or topic in ["state_recovery", "transfer_cleanup", "rating_switch", "sync_latency"]:
        return "state"
    if exception_hit or "异常分支与兜底流程缺失" in name:
        return "exception"
    if state_hit:
        return "state"
    if security_hit or "未授权访问与越权风险" in name:
        return "security"
    return "generic"


def _build_l2_issue_title(item: Dict[str, Any]) -> str:
    kind = _build_l2_issue_kind(item)
    lv = str(item.get("risk_level") or "P2").upper()
    base = _build_issue_meeting_statement(item).replace("|", " ")
    if kind == "dispatch":
        return f"全局优先级裁决与中断恢复机制（{lv}）"
    if kind == "exception":
        return f"异常分支与失败退路定义缺失（{lv}）"
    if kind == "security":
        return f"外显/外链/扫码等受控入口的鉴权、失效与失败态未定义（{lv}）"
    if kind == "concurrency":
        return f"多人或重复操作时结果怎么算（{lv}）"
    if kind == "conflict":
        return f"互斥业务规则与执行优先级调停（{lv}）"
    if kind == "state":
        sbt = f"{str(item.get('description') or '')} {str(item.get('name') or '')} {str(item.get('reason') or '')}"
        if any(
            w in sbt
            for w in ("横屏", "竖屏", "物理模式", "旋转", "可逆", "切屏", "分屏", "多窗口")
        ):
            return f"显示/物理模式与可逆状态闭环（{lv}）"
        if any(
            w in sbt
            for w in ("中途退出", "重进", "再进入", "清屏", "环境重置", "返回", "回到空闲", "切后台", "再打开")
        ):
            return f"退出、重进与资源/状态落点（{lv}）"
        return f"状态转移、回滚与终态可预期（{lv}）"
    return base + f"（{lv}）"


def _l2_risk_scope_prefix(item: Dict[str, Any]) -> str:
    t = _build_core_issue_title(item).replace("|", " ").strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) > 88:
        t = t[:86] + "…"
    return f"**（本条与标题对齐）**「{t}」\n" if t else ""


def _build_l2_risk_analysis(item: Dict[str, Any], prefix_scope: bool = True) -> str:
    head = _l2_risk_scope_prefix(item) if prefix_scope else ""
    kind = _build_l2_issue_kind(item)
    topic = _issue_topic(item)
    feature = _derive_feature_name(item)
    if kind == "conflict":
        body = (
            "两条可执行规则若对「同一动作」指向不同产品行为，研发/测试会各取一端实现与验收，联调会长期对不上；"
            "若涉及**能力启停、授权、外显/上云/计费**等，还可能引发客诉与合规/隐私风险，需要产品明确优先级与提示策略。"
        )
        return head + body
    if kind == "dispatch":
        target = "相关流程" if _is_rule_style_subject(feature) else f"“{feature}”"
        body = f"当多个流程或任务同时发生时，如果{target}**谁先执行、被谁打断、被打断后是否可恢复与如何可见**，没有可验收的口径，现场会前后**结果不一致**、联调**各执一词**。"
        return head + body
    if kind == "exception":
        target = "关键流程" if _is_rule_style_subject(feature) else f"“{feature}”"
        body = f"若{target}在**失败/超时/弱网/存盘/上云**上缺少**错误态、可重试策略与可观测结果**，用户会遇到“**以为已生效/已保存**但并未落库”的落差，研发与测试也会**按不同假设定验收**。"
        return head + body
    if kind == "concurrency":
        body = "若同一能力被**连续或并发**打中而**未定义队列/互斥/幂等/可观测**口径，就易出现**覆盖、重复**与**端云不一致**。"
        return head + body
    if kind == "state":
        body = "若**退出/重进/切后台/异常中断**后的**清屏、资源与终态落点**不闭环，则易出现**脏状态、重进后不可复现、现场对账**困难。"
        return head + body
    if kind == "security" or topic == "qr_security":
        body = (
            "若**受控外显/外链/扫码/列表或文件取回**在**谁可访问、是否越权、凭证是否仍有效、何时应失效**上写不清，**且**在**失败/超时/资源不存在**时"
            "缺少**可提示、可重试、可审计/可对账**的落地要求，会同时出现**难复盘**与"
            "「**用户以为能取/已取到，实际未授权或未落库**」的落差，现场与运营难解释责任。"
        )
        return head + body
    if topic in ["cloud_degrade", "exception_flow", "state_recovery", "transfer_cleanup"]:
        return head + f"“{feature}”在中断、弱网或切换场景下缺少**统一、可复现**的恢复与对账口径，**前后台与多端**难以对齐。"
    h = _biz_crash_text(
        str(item.get("name") or ""),
        str(item.get("risk_level") or "P2"),
        str((_ensure_list(item.get("types")) or [""])[0]),
        str(item.get("description") or ""),
        "; ".join(_issue_modules(item)),
    )
    return head + h


def _l2_pm_on_site(merged: Dict[str, Any], seed: Dict[str, Any], issue_kind: str) -> str:
    """
    L2 面向 PM 的「现场翻车」：按本条合并项/种子缺陷独立生成，避免与上一条复用同一段跨端/失败套话。
    在含业务关键词时落为可感知场景，否则仍用 L3 通用现场（平台化，不绑单一行业文案库）。
    """
    base = merged if isinstance(merged, dict) else {}
    sd = seed if isinstance(seed, dict) else base
    t = f"{_issue_blob(base)} {_issue_blob(sd)}"
    if issue_kind == "conflict":
        if any(
            k in t
            for k in (
                "主控",
                "大屏",
                "座席",
                "工位",
                "管台",
                "管理端",
                "自助",
                "移动",
                "触屏",
                "投屏",
            )
        ) and any(
            a in t
            for a in (
                "自动",
                "强开",
                "强关",
                "默开",
                "默关",
                "默认",
                "关断",
                "开启",
                "外显",
                "上云",
                "采集",
            )
        ):
            return (
                "若用户在**主控/座席/大屏/管理台**等一侧已**关闭/拒绝**某类能力，而**移动/自助/另一入口**仍按**自动/默认**执行，"
                "流程结束后易出现**与预期不一致的生成物或外显/落盘结果**——现场易客诉，并需产品明确**提示与隐私/合规口径**（含敏感采集或外显场景）。"
            )
        return (
            "当两条规则对**同一用户动作**给出不同指令时，若产品不裁定优先级，现场会表现为**随入口而变、不可对账**；"
            "若还涉及外显、上传、计费，风险会进一步放大。"
        )
    if issue_kind == "exception" and any(k in t for k in ("上传", "云端", "网络", "媒体文件", "保存", "失败")):
        return (
            "在「保存/上传/回写云端」等路径上，若失败、超时、弱网时**各端提示与落盘结果**未写清，"
            "用户会以为已保存成功，**回其他端或跨日对账**时才发现缺件，现场与客服都难解释。"
        )
    return _l3_scene(sd)


def _clean_l2_problem_desc(desc: str, issue_kind: str) -> str:
    s = _clean_report_text(desc, keep_newlines=True)
    if not s:
        return "【PRD未说明】"
    s = re.sub(r"\s*例如：.*$", "", s, flags=re.DOTALL)
    if issue_kind == "dispatch":
        parts = re.split(r"[；。\n]+", s)
        kept = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            # Keep dispatch-specific clauses, drop pure concurrency-only fragments.
            if re.search(r"(并发|限流|排队|锁|幂等|重复操作)", p) and not re.search(r"(优先级|调度|抢占|插播|打断|中断|恢复|续播|重播|补播|生效顺序|冲突裁决)", p):
                continue
            kept.append(p)
        if kept:
            s = "；".join(kept)
    if issue_kind == "state":
        parts = re.split(r"[；。]\s*", s)
        kept = [p.strip() for p in parts if p.strip() and not re.search(r"失败.*允许重试|允许重试.*失败", p)]
        if kept:
            s = "；".join(kept)
    return s.strip("；。 ，,\n") or "【PRD未说明】"


def _l2_collect_prd_quotes(issue: Dict[str, Any], prd_content: str, limit: int = 3) -> List[str]:
    text = _clean_report_text(prd_content, keep_newlines=True)
    if not text:
        return []
    keywords = _l3_issue_keywords(issue or {}, [issue] if isinstance(issue, dict) else [])
    if not keywords:
        return []
    pieces = re.split(r"[\r\n]+|(?<=[。！？；])", text)
    scored: List[Tuple[int, str]] = []
    for piece in pieces:
        s = _clean_report_text(piece)
        if not _l3_is_usable_quote(s):
            continue
        hits = sum(1 for kw in keywords if kw and kw in s)
        score = _l3_quote_score(s, keywords)
        # L2 needs a traceable sentence when possible, but the matching threshold
        # cannot be so strict that all issues are filtered out on shorter PRDs.
        if hits < 1 or score < 2:
            continue
        scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], -len(x[1])))
    out: List[str] = []
    for _, s in scored:
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _l2_quote_in_current_doc(quote: str, stage1_snapshot: Optional[Dict[str, Any]] = None, prd_content: str = "") -> bool:
    s = _clean_report_text(quote)
    if not _l3_is_usable_quote(s):
        return False
    if prd_content and s in _clean_report_text(prd_content, keep_newlines=True):
        return True
    blocks = (stage1_snapshot or {}).get("blocks") if isinstance(stage1_snapshot, dict) else []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            haystack = _clean_report_text(
                f"{block.get('title') or ''}\n{block.get('content') or ''}",
                keep_newlines=True,
            )
            if s and s in haystack:
                return True
    return False


def _l2_quote_fits_issue(item: Dict[str, Any], quote: str) -> bool:
    s = _clean_report_text(quote)
    if not _l3_is_usable_quote(s):
        return False
    # 移除严苛的负向匹配，因为缺陷往往是“未说明”的规则，原文天然不包含“并发”“超时”等词
    return True


def _build_l2_issue_quote(
    item: Dict[str, Any],
    stage1_snapshot: Optional[Dict[str, Any]] = None,
    prd_content: str = "",
    defects: Optional[List[Dict[str, Any]]] = None,
) -> str:
    if isinstance(defects, list) and defects:
        try:
            for d in _l3_match_merged_issue_defects(item, defects)[:4]:
                if not isinstance(d, dict):
                    continue
                q = _issue_quote(d)
                if not (q and str(q).strip()) or q == "【未定位到可直接引用的 PRD 原文】":
                    anch = str(d.get("anchor") or "").strip()
                    if anch:
                        q = _anchor_quote(anch) or q
                if q and str(q).strip() and _l2_quote_fits_issue(item, str(q)) and _l2_quote_in_current_doc(
                    str(q), stage1_snapshot=stage1_snapshot, prd_content=prd_content
                ):
                    return str(q).strip()[:200]
        except Exception:
            pass
    for anchor in _issue_anchors(item):
        quote = _anchor_quote(anchor)
        if _l2_quote_fits_issue(item, quote) and _l2_quote_in_current_doc(quote, stage1_snapshot=stage1_snapshot, prd_content=prd_content):
            return quote[:80]
    stage1_quotes = _l3_collect_stage1_quotes(item, [item] if isinstance(item, dict) else [], stage1_snapshot or {}, limit=1)
    if stage1_quotes:
        for quote in stage1_quotes:
            if _l2_quote_fits_issue(item, quote) and _l2_quote_in_current_doc(quote, stage1_snapshot=stage1_snapshot, prd_content=prd_content):
                return quote[:80]
    prd_quotes = _l2_collect_prd_quotes(item, prd_content, limit=1)
    if prd_quotes:
        for quote in prd_quotes:
            if _l2_quote_fits_issue(item, quote) and _l2_quote_in_current_doc(quote, stage1_snapshot=stage1_snapshot, prd_content=prd_content):
                return quote[:80]
    if isinstance(defects, list) and defects:
        try:
            for d in _l3_match_merged_issue_defects(item, defects)[:2]:
                if not isinstance(d, dict):
                    continue
                a = str(d.get("anchor") or "").strip()
                if a and 4 <= len(a) <= 180 and re.match(
                    r"^(business_|flows?|features?|states?|R\d+|D\d+|L\d+|.+\[(\d+)\]|[\w.]+/[\w.]+)",
                    a,
                    re.I,
                ):
                    return (
                        f"**{a}**（L3/结构体已挂锚点；**审计批注**：句面若**仅成功径**，**失败/超时/重进**的真空由本条 `问题描述` 与 L3 行表补齐后方可验收。）"[
                            :220
                        ]
                    )
        except Exception:
            pass
    return "【原文锚点不足，以下问题基于规则归纳】"


def _l2_has_traceable_quote(
    item: Dict[str, Any],
    stage1_snapshot: Optional[Dict[str, Any]] = None,
    prd_content: str = "",
    defects: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    return _build_l2_issue_quote(
        item, stage1_snapshot=stage1_snapshot, prd_content=prd_content, defects=defects
    ) != "【原文锚点不足，以下问题基于规则归纳】"


def _l2_kind_summary_phrase(kind: str) -> str:
    mapping = {
        "dispatch": "多个流程同时发生时谁先谁后没定",
        "exception": "失败、超时后的处理办法没定",
        "concurrency": "多人或多次同时操作时怎么处理没定",
        "state": "中途退出或切换后的状态回到哪里没定",
        "conflict": "互斥规则下哪条算数、开停与提示怎么统一没定",
        "security": "谁能看、谁能操作的边界没定",
    }
    return mapping.get(kind, "关键规则仍有缺口")


def _l2_kind_action_item(kind: str) -> str:
    mapping = {
        "dispatch": "**多个流程同时发生时怎么处理**（明确谁先执行、谁能打断谁、被打断后怎么办）。",
        "exception": "**失败或超时后怎么办**（明确页面提示、等待多久算超时、失败后是否允许继续）。",
        "concurrency": "**多人或多次同时操作时怎么处理**（明确连续点击、多人同时操作时最终按哪个结果算）。",
        "state": "**中途退出或切换后的状态规则**（明确退出、中断、重进后系统应该回到哪里）。",
        "conflict": "**互斥规则下谁先生效**（明确手动/全局开关与自动策略的优先级、冲突时如何提示、是否需二次确认）。",
        "security": "**访问和操作边界**（明确谁能看、谁能操作、越权时怎么拦截）。",
    }
    return mapping.get(kind, "**验收口径与成功标准**（明确什么情况算通过，避免联调阶段反复返工）。")


def _l2_kind_redline_item(kind: str) -> str:
    mapping = {
        "dispatch": "**规则唯一**：必须明确多个流程同时发生时谁先执行、谁后执行、被打断后怎么恢复。",
        "exception": "**失败有退路**：必须明确超时多久、失败提示什么、失败后能不能重试、用户下一步怎么做。",
        "concurrency": "**不能重复生效**：必须明确连续点击、多人同时操作时最终以哪次为准，避免重复执行或结果互相覆盖。",
        "state": "**状态能回得去**：必须明确退出、中断、重进、模式切换后系统回到哪个状态，避免留下脏状态。",
        "conflict": "**冲突有裁决**：对同一动作互相打架的规则，必须定唯一优先级与可验收提示，禁止依赖研发临场猜。",
        "security": "**权限有边界**：必须明确谁能访问、谁能操作、越权时系统怎么拦截。",
    }
    return mapping.get(kind, "**成功标准**：必须明确核心流程什么情况算成功、什么情况算失败，并给出可验证指标。")


def _l2_pick_focus_issues(
    issues: List[Dict[str, Any]],
    stage1_snapshot: Optional[Dict[str, Any]] = None,
    prd_content: str = "",
    limit: int = 3,
    defects: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    ranked: List[Tuple[int, int, int, Dict[str, Any]]] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        risk_rank = _risk_rank_local(str(it.get("risk_level") or ""))
        traceable_rank = (
            0
            if _l2_has_traceable_quote(it, stage1_snapshot=stage1_snapshot, prd_content=prd_content, defects=defects)
            else 1
        )
        detail_rank = -len(str(it.get("description") or ""))
        ranked.append((risk_rank, traceable_rank, detail_rank, it))
    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
    by_order = [t[3] for t in ranked]
    n = len(by_order)
    if n == 0 or limit <= 0:
        return []
    if limit == 1:
        return [by_order[0]]
    out: List[Dict[str, Any]] = []
    used = [False] * n
    for slot in range(min(limit, n)):
        if slot == 0:
            out.append(by_order[0])
            used[0] = True
            continue
        kinds = {_build_l2_issue_kind(x) for x in out}
        picked: Optional[int] = None
        for i, it in enumerate(by_order):
            if used[i]:
                continue
            if _build_l2_issue_kind(it) not in kinds:
                picked = i
                break
        if picked is None:
            for i, it in enumerate(by_order):
                if not used[i]:
                    picked = i
                    break
        if picked is None:
            break
        out.append(by_order[picked])
        used[picked] = True
    return out


def _l2_expand_kinds(top_kinds: List[str], limit: int = 3) -> List[str]:
    ordered = [k for k in top_kinds if k]
    for candidate in ["state", "conflict", "dispatch", "concurrency", "exception", "security", "generic"]:
        if candidate not in ordered:
            ordered.append(candidate)
        if len(ordered) >= limit:
            break
    return ordered[:limit]


def _build_l2_local_report(stage1_output: Dict[str, Any], stage3_json: Dict[str, Any], prd_content: str = "") -> str:
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
    deduped_merged = []
    seen_titles = set()
    for it in merged:
        if not isinstance(it, dict):
            continue
        title_text = _build_core_issue_title(it)
        if title_text in seen_titles:
            continue
        seen_titles.add(title_text)
        deduped_merged.append(it)
    stage1_snapshot = {"blocks": stage1_output.get("blocks") if isinstance(stage1_output, dict) else []}
    traceable_merged = [
        it
        for it in deduped_merged
        if isinstance(it, dict)
        and _l2_has_traceable_quote(it, stage1_snapshot=stage1_snapshot, prd_content=prd_content, defects=defects)
    ]
    summary_source = deduped_merged
    p0 = sum(1 for d in summary_source if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in summary_source if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in summary_source if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P2")
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
    top3 = _l2_pick_focus_issues(
        deduped_merged, stage1_snapshot=stage1_snapshot, prd_content=prd_content, limit=3, defects=defects
    )
    top_kinds = []
    for it in top3:
        if not isinstance(it, dict):
            continue
        kind = _build_l2_issue_kind(it)
        if kind not in top_kinds:
            top_kinds.append(kind)
    l2_top3_kind_list = [_build_l2_issue_kind(x) for x in top3 if isinstance(x, dict)]
    lines = []
    lines.append("# 第二部分：L2 产品分析（面向 PM/PO，本地生成）")
    lines.append("> 目的：明确缺陷，以「可选项 + 验收（AC）」帮助 PO 拍板；**原文依据在 PRD/锚点列，执行拆任务见 L3**。")
    lines.append("")
    lines.append("## 一、总体感受（产品视角）")
    lines.append(f"- 总体：{feel}")
    lines.append(f"- 质量评分：{round(score, 1)}/10；核心问题分布（按全部合并后问题统计）：P0 {p0} / P1 {p1} / P2 {p2}")
    lines.append(f"- 主要问题集中：{focus_text}")
    if traceable_merged:
        lines.append("- L2 优先展示能回溯到 PRD 原文的问题；若原文锚点不足，会明确标注为规则归纳结论。")
    else:
        lines.append("- 本次 L2 未定位到足够强的原文锚点，以下问题以规则归纳为主，建议后续补充更细的原文描述。")
    lines.append("")
    lines.append("## 二、核心需求澄清清单（直接发给 PM）")
    lines.append("> 以下仅展示优先级最高、最影响开工判断的 3 项核心问题。")
    lines.append("")
    if not top3:
        lines.append("> 暂未提取到可用问题项，本次 L2 不输出问题清单。建议先检查 Stage3 输出或补充更细的原文锚点后重新生成。")
        lines.append("")
    for idx, it in enumerate(top3, start=1):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or f"问题{idx}")
        lv = str(it.get("risk_level") or "P2").upper()
        issue_kind = _build_l2_issue_kind(it)
        desc = str(it.get("description") or "【PRD未说明】")
        types_arr = _ensure_list(it.get("types"))
        primary_type = types_arr[0] if types_arr else ""
        desc = _clean_l2_problem_desc(desc, issue_kind)
        
        # 将描述中的分号替换为换行列表，使其更易读
        if "；" in desc:
            desc_parts = [p.strip() for p in desc.split("；") if p.strip()]
            desc_formatted = "\n  * " + "\n  * ".join(desc_parts)
        else:
            desc_formatted = desc
            
        # 进一步处理内部带有数字列表（如 1. 2. 3.）的换行
        # 匹配形如 "1. xxx" 但不在开头的情况，并在前面加上换行和缩进
        desc_formatted = re.sub(r"(?<!\n)(?<!^)(\d+\.\s)", r"\n    * \1", desc_formatted)

        sug = str(it.get("suggestion") or "补齐规则与验收标准")
        meeting_title = _build_l2_issue_title(it)
        quote = _build_l2_issue_quote(
            it, stage1_snapshot=stage1_snapshot, prd_content=prd_content, defects=defects
        )
        risk_analysis = _build_l2_risk_analysis(it)
        try:
            sd = _l3_pick_seed_issue(it, defects) if isinstance(it, dict) and defects else it
            if not isinstance(sd, dict):
                sd = it
            on_site = _l2_pm_on_site(it, sd, issue_kind) if isinstance(sd, dict) else "【见问题描述与风险分析】"
        except Exception:
            on_site = "【见问题描述与风险分析】"
        lines.append(f"### {idx}. {meeting_title}")
        lines.append(f"* **现场翻车（用户视角）**：{on_site}")
        lines.append(f"* **问题描述**：{desc_formatted}")
        lines.append(f"* **PRD原文依据**：{quote}")
        lines.append(f"* **风险分析**：{risk_analysis}")
        dup_kind_in_top3 = l2_top3_kind_list.count(issue_kind) > 1

        # 针对特定类型的细化建议，移除“转台清空”等旧有业务名词的硬编码
        if issue_kind == "dispatch":
            lines.append(f"* **需澄清（多个流程同时发生时怎么处理）**：")
            if dup_kind_in_top3:
                lines.append(
                    f"  * **对题范围**：**仅**讨论本条「{meeting_title}」在 `问题描述` 中的**多流程/打断/恢复**；"
                    f"**失败/上云/弱网/存盘**若与「调度/抢占」**不是同一件业务**，请**拆项**在「异常/退路」类澄清，勿混在一条里。"
                )
            lines.append(f"  * **谁优先**：明确多个流程、任务或模式同时触发时，最终谁先执行，谁可以打断谁。")
            lines.append(f"  * **被打断后怎么办**：明确被打断的流程是继续、重来、跳过，还是回到原来的主流程。")
            lines.append(f"* **验收标准 (AC)**：多个流程同时发生时，系统必须始终给出同一种处理结果；PRD 需要明确先后顺序和被打断后的处理方式。")
        elif issue_kind == "exception":
            lines.append(f"* **需澄清（失败了怎么办）**：")
            if dup_kind_in_top3:
                lines.append(
                    f"  * **对题范围**：**仅**讨论本条「{meeting_title}」的**错误路径、提示与可重试**；**不要**把「**谁优先打断谁**」当成本条（那是**调度/并发**或**互斥规则**）。"
                )
            lines.append(f"  * **多久算超时**：明确等待多久还没成功，就算失败或超时。")
            lines.append(f"  * **失败后用户看到什么**：明确失败提示、是否能重试、是回上一页还是停留当前页。")
            lines.append(f"* **验收标准 (AC)**：失败或超时时，页面需要在明确时间内给出提示；PRD 需要写清楚失败后的处理动作和用户下一步。")
        elif issue_kind == "concurrency":
            lines.append(f"* **需澄清（多人或多次同时操作时怎么处理）**：")
            if dup_kind_in_top3:
                lines.append(
                    f"  * **对题范围**：**仅**讨论本条「{meeting_title}」的**连点/多请求/覆盖**；**互斥/入口打架**在「**互斥业务规则**」与「**多流程调度**」中另列，勿在一条里**混写**两种性质。"
                )
            lines.append(f"  * **谁说了算**：如果同一个动作被连续点击，或多人同时发起操作，最终是按第一次、最后一次，还是排队处理，PRD 里要写清楚。")
            lines.append(f"  * **会不会重复生效**：要明确哪些操作只能成功一次，避免用户多点几次后出现重复执行、结果互相覆盖，或者页面和后台结果对不上。")
            lines.append(f"* **验收标准 (AC)**：同一操作连续触发时只能生效一次；多人同时操作时系统结果必须稳定一致；PRD 必须明确冲突时谁优先、失败后页面怎么提示。")
        elif issue_kind == "state":
            lines.append(f"* **需澄清（中途退出或切换后系统回到哪里）**：")
            if dup_kind_in_top3:
                lines.append(
                    f"  * **对题范围**：**仅**讨论本条「{meeting_title}」的**退出/重进/清屏/终态**；与**失败/上传/上云**类澄清**分开写**，避免**成功路径外显**和**断线退路**在 PRD 里**混成同一句**。"
                )
            lines.append(f"  * **退出后回到哪里**：明确用户退出、中断、异常结束后，系统应该停在哪个状态。")
            lines.append(f"  * **重新进入时看到什么**：明确再次进入时是继续之前的流程、回到初始页，还是展示失败结果。")
            lines.append(f"* **验收标准 (AC)**：退出、中断、重进后系统状态必须可预期；PRD 需要明确每种情况下页面展示和最终落点。")
        elif issue_kind == "conflict":
            lines.append(f"* **需澄清（互斥规则如何裁决与提示）**：")
            if dup_kind_in_top3:
                lines.append(
                    f"  * **对题范围**：**仅**讨论本条「{meeting_title}」的**两规则/两入口**谁先生效、如何提示；**不**用本条替代「**失败/超时/存盘/上云**」题（那属于**失败退路**；**多流程打断**在**调度**或**并发**条）。"
                )
            lines.append(
                f"  * **优先级与否决权**：当手动/全局状态与某条业务「自动执行」规则冲突时，以谁为准；「手动关」等是否对自动规则拥有否决权。"
            )
            lines.append(
                f"  * **提示口径与阻断**：在无法同时满足时，是阻断主流程、弹窗二次确认、还是改引导语（如「需先开启/关闭 X 才能继续」）。"
            )
            lines.append(
                f"  * **可见一致**：涉及**上云/外显/启停/计费/敏感外显**等，需写清各端/各入口的**同一句产品结论**，避免一入口一套说法。"
            )
            lines.append(
                f"* **验收标准 (AC)**：在明确「冲突条件」下，**所有相关入口**裁决一致、用户可解释；对**敏感能力**（如**启停、上云/外显**）不得**静默**与用户预期相反。"
            )
        elif issue_kind == "security":
            lines.append(
                f"* **需澄清（外显/外链/扫码/取回：谁能用、失效应提示什么）**："
            )
            lines.append(
                f"  * **谁可外显/取回**：在账号/设备/会话/组织/房间等隔离维度，谁可见入口、谁可拉取或展示、访客与会员是否分链。"
            )
            lines.append(
                f"  * **码/链/资源生命周期**：二维码/短链/深链/文件的**有效时长、重放、续期、撤销、一次有效**，以及**云侧/本地资源不存在、生成失败、丢失**时分别的提示与退路。"
            )
            if dup_kind_in_top3:
                lines.append(
                    f"  * **对题范围**：**仅**收敛本条在「**有外显/外链/扫码/取回入口**」的专项；**主路径/存盘/上云/通用**失败与重试的通用题仍在「**异常分支与失败退路**」中闭环，**勿**在评审中混成同一句，避免**看似两条实则一句**的假象。"
                )
            lines.append(
                f"  * **可观测/审计/对账**：未授权、已过期、被重放、越权、弱网/超时/失败时，**分别**在 PRD 中写清**错误态、可重试条件**与**日志/埋点/客服可用字段**。"
            )
            lines.append(
                f"* **验收标准 (AC)**：在「未授权/无资源/已过期/越权/弱网/超时」下，**不得**出现**无任何提示**却使用户**误以为已外显/已取回/已保存成功**的情况；**提示与可重试**在 PRD 中可联调、可拍板、可写用例验收。"
            )
        elif "矛盾" in name or "冲突" in name or "歧义" in name:
            lines.append(f"* **需澄清（请选择方案）**：")
            lines.append(f"  * **方案 A（严格阻断）**：执行严格的条件检查，不满足时直接阻断当前流程并给出错误提示，保证数据与状态绝对一致。")
            lines.append(f"  * **方案 B（兼容降级）**：跳过或静默处理冲突点，优先保证用户的核心主流程继续，不强行中断。")
            lines.append(f"  * **要求**：请在 24 小时内选定一种模式并更新 PRD。")
            lines.append(f"* **验收标准 (AC)**：明确触发冲突时的 UI 提示与兜底行为（如“阻断时弹窗提示 2s”）。")
        elif "异常" in name or "降级" in name or "防御" in name or "超时" in name or "断开" in name:
            lines.append(f"* **需澄清（防御性设计要求）**：")
            lines.append(f"  * **兜底方案**：请定义当接口超时或依赖服务不可用时的降级策略（如本地暂存后自动重试）。")
            lines.append(f"  * **异常提示**：明确前端错误文案和交互（重试按钮或自动消失）。")
            lines.append(f"* **验收标准 (AC)**：断网/超时场景下，异常提示必须在 2s 内出现；恢复后成功率 ≥99.5%。")
        elif "权限" in name or "越权" in name or "鉴权" in name or "泄露" in name:
            lines.append(f"* **需澄清（安全与权限红线）**：")
            lines.append(f"  * **访问鉴权**：明确跨设备/跨账号/越权访问时的拦截策略。")
            lines.append(f"  * **时效管理**：明确敏感凭证/会话/链接的有效期（如 2 小时失效或单次有效）。")
            lines.append(f"* **验收标准 (AC)**：越权访问 100% 拦截；过期链接点击直接跳转失效页。")
        elif "状态" in name or "开关" in name or "竞争" in name or "并发" in name:
            lines.append(f"* **需澄清（状态机与生效时机）**：")
            lines.append(f"  * **生效边界**：明确全局状态或配置改变时，对“已在进行中”任务的干预与打断策略。")
            lines.append(f"  * **数据一致**：明确前端展示状态与服务端实际状态的对账与同步机制。")
            lines.append(f"* **验收标准 (AC)**：高频并发或中途切状态不引发进程 Crash；端云状态最终一致性达 100%。")
        else:
            lines.append(f"* **需澄清**：{sug}")
            lines.append(f"* **验收标准 (AC)**：请明确“什么样才算成功/通过”的具体量化指标（如时长、成功率）。")
        
        lines.append("")
    
    lines.append("---")
    lines.append("")
    lines.append("### 💡 建议同步方式与开工红线清单")
    lines.append("你可以直接对 PM 摊牌：")
    summary_phrases = [_l2_kind_summary_phrase(k) for k in top_kinds[:3]] or ["关键规则仍有缺口"]
    lines.append(f"> “这份 PRD 目前的成熟度只有 {round(score, 1)} 分。核心问题在于：**{'、'.join(summary_phrases)}**。")
    lines.append(f"> 我需要你针对 L2 报告里的待确认清单，在一周内补齐：")
    action_items = [_l2_kind_action_item(k) for k in _l2_expand_kinds(top_kinds, limit=3)]
    lines.append(f"> 1. {action_items[0]}")
    lines.append(f"> 2. {action_items[1]}")
    lines.append(f"> 3. {action_items[2]}”")
    lines.append("")
    lines.append("**《项目启动准入/拨备清单》（达不到不准开工）：**")
    redline_items = [_l2_kind_redline_item(k) for k in _l2_expand_kinds(top_kinds, limit=3)]
    lines.append(f"1. {redline_items[0]}")
    lines.append(f"2. {redline_items[1]}")
    lines.append(f"3. {redline_items[2]}")
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
    stage1_snapshot = (stage3_json or {}).get("stage1_snapshot") or {}
    prd_content = str((stage3_json or {}).get("prd_content") or "")
    dim_scores = (stage3_json or {}).get("dimension_scores") or {}
    coverage = (stage3_json or {}).get("coverage") or {}
    if not isinstance(defects_data, list):
        defects_data = []
    score = summary.get("quality_score", 0)
    try:
        score = float(score) if score is not None else 0
    except (TypeError, ValueError):
        score = 0
    issue_cards = []
    def _any_stage1_quote() -> str:
        """从 Stage1 blocks 中挑一句“像原文”的句子，兜底用，避免整篇报告只有套话。"""
        try:
            blocks = (stage1_snapshot or {}).get("blocks") if isinstance(stage1_snapshot, dict) else []
            if not isinstance(blocks, list):
                return ""
            for block in blocks[:12]:
                if not isinstance(block, dict):
                    continue
                for s in _l3_block_sentence_candidates(block)[:6]:
                    if _l3_is_usable_quote(s):
                        return s[:120]
        except Exception:
            return ""
        return ""

    def _fallback_issue_quotes(issue: Dict[str, Any], seed: Dict[str, Any], limit: int = 2) -> List[str]:
        """当 defects/issue 没有可用 anchor 时，用 Stage1 切块内容回填引用。"""
        try:
            out = _l3_collect_stage1_quotes(issue or {}, [seed] if isinstance(seed, dict) else [], stage1_snapshot or {}, limit=limit)
            if out:
                return out
        except Exception:
            pass
        any_q = _any_stage1_quote()
        if any_q:
            return [any_q]

        # 再兜底：直接从 PRD 原文里按关键词抓“可引用句/行”，避免整篇报告都是“未定位原文”。
        # 注意：这里只做轻量检索，不引入额外 LLM 成本。
        try:
            text = (prd_content or "").strip()
            if not text:
                return []
            # 抽取关键词（尽量来自 PRD 中可能出现的真实词）
            raw = " ".join([
                str((issue or {}).get("name") or ""),
                str((issue or {}).get("description") or ""),
                str((seed or {}).get("module") or ""),
                str((seed or {}).get("description") or ""),
                str((seed or {}).get("anchor") or ""),
            ])
            raw = re.sub(r"\s+", " ", raw).strip()
            if not raw:
                return []
            kws = []
            # 中文连续片段
            for m in re.findall(r"[\u4e00-\u9fff]{2,8}", raw):
                if m not in kws:
                    kws.append(m)
            # 英文/数字片段
            for m in re.findall(r"[A-Za-z0-9_./-]{3,32}", raw):
                if m not in kws:
                    kws.append(m)
            kws = kws[:8]
            if not kws:
                return []
            # 逐行匹配（优先短句，避免整段粘贴）
            hits: List[str] = []
            for ln in text.splitlines():
                s = ln.strip().strip("•-—*")
                if not s:
                    continue
                if len(s) < 8 or len(s) > 220:
                    continue
                if any(k in s for k in kws):
                    if _l3_is_usable_quote(s):
                        hits.append(s)
                if len(hits) >= max(1, int(limit or 1)):
                    break
            return hits[: max(1, int(limit or 1))]
        except Exception:
            return []

    for issue in merged_issues:
        if not isinstance(issue, dict):
            continue
        related_defects = _l3_match_merged_issue_defects(issue, defects_data)
        seed = _l3_pick_seed_issue(issue, defects_data)
        quotes = _l3_collect_issue_quotes(issue, related_defects, stage1_snapshot=stage1_snapshot, limit=3)
        if not quotes:
            quotes = _fallback_issue_quotes(issue, seed, limit=2)
        level = str(issue.get("risk_level") or seed.get("risk_level") or "P2").upper()
        problem = _l3_clean_problem_text(seed.get("description") or issue.get("description"))
        module_label = _l3_issue_module_label(seed).replace("|", " ")
        fix_items = _l3_fix_items(seed)
        tests = _l3_test_drafts(seed)
        title = _l3_title(seed).replace("|", " ")
        meeting_statement = _l3_meeting_statement(seed).replace("|", " ")
        scene = _l3_scene(seed).replace("|", " ")
        impact_chain = _l3_impact_chain(seed).replace("|", " ")
        risk_reason = _l3_risk_reason(seed).replace("|", " ")
        issue_cards.append({
            "issue": issue,
            "seed": seed,
            "related_defects": related_defects,
            "quotes": quotes,
            "level": level,
            "problem": problem.replace("|", " "),
            "module_label": module_label,
            "fix_items": [str(x).replace("|", " ") for x in fix_items],
            "tests": [str(x).replace("|", " ") for x in tests],
            "title": title,
            "meeting_statement": meeting_statement,
            "scene": scene,
            "impact_chain": impact_chain,
            "risk_reason": risk_reason,
        })
    issue_cards = _l3_dedupe_issue_cards(issue_cards)
    issue_cards_matrix = _l3_cluster_p1_by_identical_title(issue_cards)
    summary_cards = issue_cards_matrix if issue_cards_matrix else issue_cards
    if issue_cards:
        main_problem, one_liner_override, top3_override = _l3_summary_from_cards(summary_cards, defects_data)
    else:
        main_problem = str(summary.get("main_problem", "【PRD未说明】"))
        one_liner_override = str(core_summary.get("one_liner", "【PRD未说明】"))
        top3_override = _ensure_list(core_summary.get("top3"))
    p0_issue_count = sum(1 for c in issue_cards if str(c.get("level") or "").upper() == "P0")
    if p0_issue_count or issue_cards:
        report_title = re.sub(r"P0级\d+项", f"P0级{p0_issue_count}项", str(report_title))
    lines = [f"# {report_title}", ""]
    lines.append("> **第三部分：L3 技术审计报告**（与 **L1 管理摘要**、**L2 产品拍板**同源；**逐条缺陷与矩阵**以本段为准执行与对账。）")
    lines.append("")
    if stage3_json.get("offline_mode"):
        lines.append("> 【本地规则体检版】当前未接入可用大模型，本报告基于本地规则引擎与静态分析自动生成，"
                     "主要用于结构完整性和显性风险初筛，不能替代人工评审与线上大模型审计。")
        lines.append("")
    # 不输出 Stage2/模型超时/排障类提示与原始 llm_error，避免正文顶部出现「机器人噪音」；技术细节以接口返回的 scan_meta 或仪表盘为准。
    lines.extend(["## 一、总体结论", ""])
    lines.append(f"- 审计结论：{main_problem}")
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
    one_liner = one_liner_override
    lines.append(f"- 一句话总结：{one_liner}")
    top3 = top3_override
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
    if issue_cards_matrix:
        lines.append("| 风险等级 | 核心问题 | PRD原文依据 | 问题描述 | 现场翻车（业务视角） | 风险分析 | 审计建议 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for card in issue_cards_matrix[:12]:
            quotes_arr = card.get("quotes") if isinstance(card, dict) else None
            quotes_arr = quotes_arr if isinstance(quotes_arr, list) else []
            evidence = "<br>".join([f"“{str(q).replace('|', ' ')}”" for q in quotes_arr if str(q or "").strip()])
            if not evidence:
                # 对用户更友好：很多“缺陷”本质就是 PRD 未提及/未定义，而不是系统“定位失败”。
                # 证据列显式表达“未说明”，避免误解为工具坏了。
                missing_topic = str(card.get("title") or card.get("module_label") or "该规则").strip()
                missing_topic = re.sub(r"\s+", " ", missing_topic)[:32]
                evidence = f"【PRD未说明：{missing_topic}（建议补写）】"
            suggestion_text = "；".join(card["fix_items"][:2]) if card["fix_items"] else _build_core_issue_rewrite(card["seed"])
            lines.append(
                f"| {card['level']} | **{card['title']}** | {evidence} | {card['problem']} | {card['scene']} | {card['risk_reason']} | {suggestion_text.replace('|', ' ')} |"
            )
        lines.append("")
    if defects_data:
        lines.extend(["", "## 三、详细漏洞矩阵（研发/测试，逐条去重）", ""])
        lines.append("> 与「二、核心问题矩阵（合并类）」互补；本表**一行一条缺陷**（经锚点+描述前若干字去重），便于**设计/用例/任务**对齐。必补可来自 `suggestion` 或系统归纳。")
        lines.append("")
        lines.append("| 风险等级 | 问题分类 | 涉及锚点 | 缺陷描述 | 必补动作 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for d in _dedupe_defects_for_l3_matrix(defects_data, limit=60):
            lv_ = str(d.get("risk_level") or "P2").upper()
            typ_ = str(d.get("type") or "【未分类】").replace("|", " ")
            rid_ = str(d.get("id") or "").strip()
            anch_ = str(d.get("anchor") or d.get("module") or "【PRD未说明】").replace("|", " ")[:120]
            if rid_:
                anch_ = f"{anch_}（`{rid_}`）"[:200]
            desc_ = _clean_report_text(str(d.get("description") or "【PRD未说明】")).replace("|", " ")[:400]
            sug_ = str(d.get("suggestion") or "").replace("|", " ").strip()
            if not sug_ and isinstance(d, dict):
                fi_ = _l3_fix_items(d)
                sug_ = "；".join([str(x) for x in fi_[:2]]) if fi_ else _build_core_issue_rewrite(d)
            lines.append(f"| {lv_} | {typ_} | {anch_} | {desc_} | {str(sug_ or '【见 L2/L3 建议列】')[:300]} |")
        lines.append("")
    lines.extend(["", "### 详细漏洞清单（评委展示版）", ""])
    stats = (stage3_json or {}).get("scan_stats") or {}
    lines.append(f"- 扫描来源：规则库 {stats.get('rule', 0)} 条，LLM {stats.get('llm', 0)} 条，混合 {stats.get('hybrid', 0)} 条")
    lines.append("")
    render_issue_cards = issue_cards_matrix if issue_cards_matrix else issue_cards
    if defects_data:
        top_issue_cards = render_issue_cards[:3] if render_issue_cards else []
        rest_issue_cards = render_issue_cards[3:] if len(render_issue_cards) > 3 else []

        for i, card in enumerate(top_issue_cards, start=1):
            seed = card["seed"]
            lines.append(f"### 漏洞{i} — {card['meeting_statement']}")
            lines.append(f"- **模块/场景**：{card['module_label']}")
            lines.append(f"- **类型**：{seed.get('type', '【PRD未说明】')}")
            lines.append(f"- **风险等级**：{card['level']}")
            lines.append(f"- **归并来源**：{max(1, len(card['related_defects']))} 条同类漏洞")
            lines.append("- **PRD 原文**：")
            if card["quotes"]:
                for quote in card["quotes"]:
                    lines.append(f"  - “{quote}”")
            else:
                lines.append("  - 【未定位到可直接引用的 PRD 原文】")
            lines.append(f"- **冲突点**：{card['problem']}")
            lines.append(f"- **现场翻车**：{card['scene']}")
            lines.append(f"- **影响链路**：{card['impact_chain']}")
            lines.append("- **必补动作**：")
            for item in card["fix_items"][:3]:
                lines.append(f"  - {item}")
            lines.append("- **测试用例**：")
            for item in card["tests"][:3]:
                lines.append(f"  - {item}")
            lines.append("")

        if rest_issue_cards:
            lines.append("---")
            lines.append("### 📎 附录：其他漏洞清单（共 " + str(len(rest_issue_cards)) + " 条，已折叠处理）")
            lines.append("")
            lines.append("<details>")
            lines.append("<summary>点击展开查看其余漏洞</summary>")
            lines.append("")
            for i, card in enumerate(rest_issue_cards, start=4):
                lines.append(f"**漏洞{i} — {card['meeting_statement']}** ({card['level']})")
                if card["quotes"]:
                    lines.append(f"- PRD 原文：{card['quotes'][0]}")
                else:
                    lines.append("- PRD 原文：【未定位到可直接引用的 PRD 原文】")
                lines.append(f"- 冲突点：{card['problem']}")
                lines.append(f"- 必补动作：{'；'.join(card['fix_items'][:2]) or _build_core_issue_rewrite(card['seed'])}")
                lines.append("")
            lines.append("</details>")
            lines.append("")
        if not issue_cards:
            for i, defect in enumerate(_pick_top_defects(defects_data, limit=3), start=1):
                lines.append(f"### 漏洞{i} — {_l3_meeting_statement(defect)}")
                lines.append(f"- **模块/场景**：{_l3_issue_module_label(defect)}")
                lines.append(f"- **类型**：{defect.get('type', '【PRD未说明】')}")
                lines.append(f"- **风险等级**：{str(defect.get('risk_level') or 'P2').upper()}")
                q = _issue_quote(defect)
                if not q:
                    qs = _fallback_issue_quotes(defect if isinstance(defect, dict) else {}, defect if isinstance(defect, dict) else {}, limit=1)
                    q = qs[0] if qs else ""
                lines.append(f"- **PRD 原文**：{q or '【未定位到可直接引用的 PRD 原文】'}")
                lines.append(f"- **冲突点**：{_l3_clean_problem_text(defect.get('description'))}")
                lines.append(f"- **必补动作**：{'；'.join(_l3_fix_items(defect)[:2]) or _build_core_issue_rewrite(defect)}")
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

    pending_cards = []
    pending_source_cards = issue_cards_matrix if issue_cards_matrix else issue_cards
    if pending_source_cards:
        for card in pending_source_cards[:10]:
            pending_cards.append({
                "priority": str(card.get("level") or "P2").upper(),
                "item": str(card.get("meeting_statement") or "【PRD未说明】").replace("|", " "),
                "module": str(card.get("module_label") or "全局或上下文推导").replace("|", " "),
                "problem": str(card.get("problem") or "【PRD未说明】").replace("|", " "),
            })
    else:
        pending = [d for d in defects_data if _is_pending_item(d)]
        pending.sort(key=lambda d: (_risk_rank(str(d.get("risk_level") or "")), -len(str(d.get("description") or ""))))
        if not pending and defects_data:
            pending = sorted(defects_data, key=lambda d: (_risk_rank(str(d.get("risk_level") or "")), -len(str(d.get("description") or ""))))[:10]
        for d in pending[:10]:
            lvl = str(d.get("risk_level") or "P1").upper()
            pending_cards.append({
                "priority": "P0" if lvl == "P0" else ("P1" if lvl == "P1" else "P2"),
                "item": _l3_meeting_statement(d).replace("|", " "),
                "module": _l3_issue_module_label(d).replace("|", " "),
                "problem": _clean_report_text(d.get("description", "") or "【PRD未说明】").replace("|", " "),
            })

    for row in pending_cards[:10]:
        priority = str(row.get("priority") or "P2").upper()
        urgency = _urgency_tag(priority)
        mod = str(row.get("module") or "全局或上下文推导")
        item = str(row.get("item") or "【PRD未说明】")
        desc = str(row.get("problem") or "【PRD未说明】")
        lines.append(f"| {priority} | {item} | {urgency} | {mod} | {desc[:120]} | 需澄清后方可进入开发/评审 |")
    if not pending_cards:
        lines.append("| - | 无 | - | - | - | - |")
    lines.append("")
    lines.extend(["## 五、测试重点（测试团队专用）", ""])
    lines.append("| 测试类型 | 测试点 | 对应风险 |")
    lines.append("| :--- | :--- | :--- |")
    focus_rows = []
    focus_source_cards = issue_cards_matrix if issue_cards_matrix else issue_cards
    if focus_source_cards:
        for card in focus_source_cards[:12]:
            seed = card["seed"]
            name = _l3_meeting_tag(seed)
            lv = card["level"]
            drafts = card["tests"]
            tpoint = name if not drafts else (name + "： " + "；".join(drafts[:2]))
            text = " ".join([name, card["problem"], str(seed.get("reason") or "")])
            ttype = "功能测试"
            topic = _l3_render_topic(seed if isinstance(seed, dict) else {})
            if topic == "security_access":
                ttype = "安全测试"
            elif topic in ("data_contract", "cross_end_sync"):
                ttype = "接口/联调测试"
            elif topic == "concurrency_control":
                ttype = "冲突/并发测试"
            elif topic == "exception_flow":
                ttype = "异常测试"
            elif any(k in text for k in ["权限", "鉴权", "安全", "串房", "越权", "隐私", "合规"]):
                ttype = "安全测试"
            elif any(k in text for k in ["并发", "冲突", "抢占", "优先级", "同时"]):
                ttype = "冲突/并发测试"
            elif any(k in text for k in ["异常", "失败", "超时", "断网", "重试", "降级", "兜底"]):
                ttype = "异常测试"
            focus_rows.append((ttype, tpoint, lv))
    if not focus_rows and pending_cards:
        for d in pending_cards[:12]:
            text = " ".join([str(d.get("type") or ""), str(d.get("module") or ""), str(d.get("description") or "")])
            ttype = "功能测试"
            if any(k in text for k in ["权限", "鉴权", "安全", "串房", "越权", "隐私", "合规"]):
                ttype = "安全测试"
            elif any(k in text for k in ["字段", "错误码", "返回体", "状态值", "同步", "时效", "对账", "联调"]):
                ttype = "接口/联调测试"
            elif any(k in text for k in ["并发", "冲突", "抢占", "优先级", "同时"]):
                ttype = "冲突/并发测试"
            elif any(k in text for k in ["异常", "失败", "超时", "断网", "重试", "降级", "兜底"]):
                ttype = "异常测试"
            focus_rows.append((ttype, str(d.get("problem") or d.get("item") or "【PRD未说明】"), str(d.get("priority") or "P2").upper()))
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
    dev_source_cards = issue_cards_matrix if issue_cards_matrix else issue_cards
    if dev_source_cards:
        for card in dev_source_cards[:12]:
            module = card["module_label"]
            focus = "；".join(card["fix_items"][:2]) or str(card["seed"].get("suggestion") or card["problem"] or "【PRD未说明】")
            lv = card["level"]
            dev_rows.append((module, focus, lv))
    if not dev_rows and pending_cards:
        for d in pending_cards[:12]:
            module = str(d.get("module") or "全局或上下文推导")
            focus = str(d.get("problem") or d.get("item") or "【PRD未说明】")
            lv = str(d.get("priority") or "P2").upper()
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
    risks_for_render = []
    plans_for_render = []
    risk_source_cards = issue_cards_matrix if issue_cards_matrix else issue_cards
    if risk_source_cards:
        seen_risks = set()
        for card in risk_source_cards[:5]:
            risk_line = f"{card['meeting_statement']}；若不补齐，{card['risk_reason']}"
            if risk_line not in seen_risks:
                seen_risks.add(risk_line)
                risks_for_render.append(risk_line)
        top_titles = []
        for card in risk_source_cards[:5]:
            title = str(card.get("title") or "").strip()
            if title and title not in top_titles:
                top_titles.append(title)
            if len(top_titles) >= 3:
                break
        if top_titles:
            plans_for_render.append("先冻结并补齐前 3 个核心问题的 PRD 规则口径：" + "；".join(top_titles))
        plans_for_render.append("把补齐后的状态/异常/权限规则同步到测试用例和验收标准，再进入开发排期。")
        plans_for_render.append("修订后再次执行一次本地 L3 审计，确认矩阵、待确认清单和测试重点口径一致。")
    lines.extend(["## 七、项目风险", _to_md_items(risks_for_render or (stage3_json or {}).get("risks")), ""])
    lines.extend(["## 八、计划建议", _to_md_items(plans_for_render or (stage3_json or {}).get("plan")), ""])
    
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
    # 重要：Stage2 LLM 失败不应强制整份报告“只走本地”，否则用户明明开启 use_llm 也只得到套话体检版。
    # local_mode 仅由“显式禁用 LLM”触发；Stage2 失败则通过 scan_meta 提示，但 Stage3 仍尝试走 LLM。
    local_mode = bool(force_local)
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
    # 给 Stage4 渲染层一个“原文兜底”来源（只保留前 4w 字符，足够引用且避免无意义膨胀）
    try:
        stage3_output["prd_content"] = (content or "")[:40000]
    except Exception:
        pass
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
    report_l2 = _build_l2_local_report(stage1_output, stage3_output, prd_content=content)
    
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
            # 定制化严重后果，移除旧有硬编码名词
            if "状态" in title_text or "竞争" in title_text or "回退" in title_text or "数据丢失" in title_text:
                crash = "用户操作后流程无法正确闭环，状态不一致导致前台展示错乱，排障困难。"
            elif "矛盾" in title_text or "冲突" in title_text or "歧义" in title_text:
                crash = "客服/产品/研发对同一规则存在多种解读，现场纠纷无统一口径可裁决。"
            elif "权限" in title_text or "越权" in title_text or "鉴权" in title_text:
                crash = "敏感资产或凭证流传外网被恶意利用，直接引发严重的合规红线与安全客诉。"

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
    report_l2 = _build_l2_local_report(stage1_output, stage3_output, prd_content=prd_text)
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
