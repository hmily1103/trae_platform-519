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

# 纯 PRD 分析模式：仅保留 Stage1/2/3 与 L1/L2/L3 主报告，跳过测试与扩展资产生成。
PRD_ANALYSIS_ONLY_MODE = True

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

    # 并发/多人同时操作：优先产出“裁决/互斥/幂等”类标题，避免被“字段/接口”分支抢走导致标题错位
    if re.search(r"(并发|多人|同时操作|重复点击|抢占|互斥|幂等|排队|限流)", text):
        return f"{feature}未定义" if subject_is_rule else f"{feature}并发裁决与互斥规则未定义"
    
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
    prd_content = str((stage2_output or {}).get("prd_content") or "")
    defects = stage2_output.get("defects") if isinstance(stage2_output, dict) else []
    defects = defects if isinstance(defects, list) else []
    defects, tool_defects = _filter_tool_defects(defects)
    # 推断扫描来源分布：用于 scan_meta 自洽（平台通用，避免 UI 误判“LLM=0”）
    inferred_source_stats = {"rule": 0, "llm": 0, "hybrid": 0}
    for d in defects:
        if not isinstance(d, dict):
            continue
        src = str(d.get("source") or "").strip().lower()
        # Stage2 历史兼容：未标 source 的默认视为 llm（旧链路就是这么做的）
        if not src:
            src = "llm"
        if src in inferred_source_stats:
            inferred_source_stats[src] += 1

    def _evidence_quotes_for_defect(defect: Dict[str, Any]) -> List[str]:
        """
        平台通用：将 L0001-L0022 / L0015-L0016 这类范围锚点还原成可复制原句，避免前端检索命中为 0。
        """
        if not isinstance(defect, dict):
            return []
        anchor = str(defect.get("anchor") or "").strip()
        if not anchor:
            return []
        # 1) 直接包含 Lxxxx: 原句（最可靠）
        m = re.findall(r"(?:^|[；;\n])\s*(L\d{3,5})\s*[:：]\s*([^\n；;]{6,220})", anchor)
        out: List[str] = []
        for _, txt in m[:2]:
            q = _clean_report_text(txt)
            if q and _l3_is_usable_quote(q) and q not in out:
                out.append(q)
        if out:
            return out[:2]
        # 2) 纯范围锚点：L0003-L0012 / L0015-L0016
        mm = re.fullmatch(r"(L\d{3,5})\s*-\s*(L?\d{3,5})", _clean_report_text(anchor), flags=re.I)
        if not mm:
            # 也兼容 anchor 里夹杂范围但无冒号的情况
            mm2 = re.search(r"(L\d{3,5})\s*-\s*(L?\d{3,5})", anchor, flags=re.I)
            mm = mm2
        if not mm:
            # 3) “功能：xxx” 这类不算原文引用
            return []
        try:
            s1 = int(re.sub(r"\D", "", mm.group(1) or "0") or 0)
            s2 = int(re.sub(r"\D", "", mm.group(2) or "0") or 0)
        except Exception:
            return []
        if s1 <= 0 or s2 <= 0:
            return []
        start = min(s1, s2)
        end = max(s1, s2)
        if not prd_content:
            return []
        lines = (prd_content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # 取范围内的“像原文的句子”，避免标题/空行
        for i in range(start, min(end, len(lines)) + 1):
            raw = lines[i - 1].strip().strip("•-—*")
            q = _clean_report_text(raw)
            if not q:
                continue
            if not _l3_is_usable_quote(q):
                continue
            out.append(q[:200])
            if len(out) >= 2:
                break
        return out[:2]

    # 把 evidence_quotes 写入 defects（供前端直接展示）
    for d in defects:
        if not isinstance(d, dict):
            continue
        if isinstance(d.get("evidence_quotes"), list) and d.get("evidence_quotes"):
            continue
        eq = _evidence_quotes_for_defect(d)
        if eq:
            d["evidence_quotes"] = eq

    # 归一化：低证据的“模板洞察”不应推高 P0（平台通用，不绑定任何业务）
    # 典型：状态孤岛/死路/非法跳转 在 PRD 未提供状态转移表时无法验证，应归为“状态机缺失”类缺口。
    for d in defects:
        if not isinstance(d, dict):
            continue
        dtype = str(d.get("type") or "").strip()
        desc_blob = " ".join(
            [
                dtype,
                str(d.get("description") or ""),
                str(d.get("reason") or ""),
            ]
        )
        # 兼容：type/描述可能被拼接或改写，优先按关键词命中
        if not re.search(r"(状态孤岛|状态死路|非法状态跳转|状态不可达|无法结束|不合理跳转|没有入口|没有出口)", desc_blob):
            continue
        quotes = d.get("evidence_quotes") if isinstance(d.get("evidence_quotes"), list) else []
        has_quote = any(_l3_is_usable_quote(str(q or "")) for q in (quotes or [])) or _l3_is_usable_quote(_issue_quote(d))
        if has_quote:
            continue
        # 降级为“缺失定义”，避免用不可验证的模板推高门禁
        d["type"] = "状态机缺失"
        d["risk_level"] = "P1"  # 仍然重要，但不应等同“已证实的P0缺陷”
        desc = str(d.get("description") or "")
        if "状态机" not in desc and "转移" not in desc:
            d["description"] = "未提供状态/事件/转移条件/终态表，无法验证可达性/终止性/合法跳转。"
        if not str(d.get("reason") or "").strip():
            d["reason"] = "该类结论需要可回溯的状态转移定义作为证据，否则属于缺失定义而非已证实缺陷。"
        if not str(d.get("suggestion") or "").strip():
            d["suggestion"] = "补充状态机定义（状态、触发事件、转移条件、终态）并给出异常回滚与恢复路径。"

    coverage = stage2_output.get("coverage") if isinstance(stage2_output, dict) else None
    dims = _score_dimensions(stage1_output or {}, defects)
    score = round(sum(v["score"] for v in dims.values()) / float(len(dims)), 1) if dims else _calc_quality_score(defects)[0]
    _, risk_level = _calc_quality_score(defects)
    scan_meta = (stage2_output or {}).get("scan_meta") if isinstance(stage2_output, dict) else None
    if not isinstance(scan_meta, dict):
        scan_meta = {}
    llm_stage2_ok = scan_meta.get("llm_scan_ok", True)
    # 修复：scan_meta 缺字段/字段为 0 时，按 defects 推断回填，避免“LLM 实际有返回但 UI 显示 0 条”
    if scan_meta.get("rule_defects_count") is None:
        scan_meta["rule_defects_count"] = int(inferred_source_stats.get("rule") or 0)
    if scan_meta.get("llm_defects_parsed") is None:
        scan_meta["llm_defects_parsed"] = int(inferred_source_stats.get("llm") or 0)
    # 兼容：字段存在但为 0，且推断显示非 0 时，也回填（避免误杀 llm_empty_suspected）
    try:
        _llm_parsed_raw = int(scan_meta.get("llm_defects_parsed") or 0)
    except (TypeError, ValueError):
        _llm_parsed_raw = 0
    if _llm_parsed_raw == 0 and int(inferred_source_stats.get("llm") or 0) > 0:
        scan_meta["llm_defects_parsed"] = int(inferred_source_stats.get("llm") or 0)
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
    # scan_stats 以推断结果为准（与 scan_meta 同源）
    source_stats = dict(inferred_source_stats)
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
    # 排期/负责人/表格类内容：可被行号还原，但通常不构成业务规则证据（平台通用过滤）
    if re.search(
        r"(负责人|完成时间|上线时间|排期|里程碑|节点|UI设计|小程序开发|服务端开发|客户端开发|测试负责人|开发负责人)",
        s,
    ):
        return False
    # 无断句符号且较长：常见于表格字段堆叠/标题行，不适合作为可复制引用句
    if len(s) >= 60 and not re.search(r"[，。；：、,.!?！？]", s):
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


def _l3_if_then_action(item: Dict[str, Any]) -> str:
    """
    将 L3 建议落成可执行口径，优先输出 IF/THEN 风格动作。
    """
    text = _l3_issue_text(item)
    topic = _issue_topic(item)
    issue_id = str(item.get("id") or "").upper()
    if issue_id == "D006" or topic in ("device_lifecycle",):
        return "IF 发生关台/重启/断电, THEN 明确未认领录音的清空/保留规则并记录清理原因与会话ID。"
    if issue_id == "D014" or topic in ("conflict_rule", "concurrency_control"):
        return "IF 自动开启与手动开关同时命中, THEN 按优先级矩阵裁决并落字段 effective_rule/decision_source。"
    # 权限/访问控制：不要误套“上传失败重传”这类异常动作
    if topic in ("security_access", "qr_security"):
        return "IF 通过扫码/外链/小程序等入口访问受控资源, THEN 校验身份与权限并校验有效期/一次性，越权时返回统一错误码并留审计日志。"
    # 多端一致/同步：强调时效、触发与可观测
    if topic in ("sync_latency", "cross_end_sync"):
        return "IF 多端/多入口依赖同一结果, THEN 明确同步时效阈值、刷新触发与最终一致口径，并落字段 sync_ts/sync_status 便于对账。"
    # 失败/超时/弱网：强调错误态、重试与兜底（不绑定具体业务词）
    if topic in ("exception_flow", "cloud_degrade"):
        return "IF 依赖失败或超时, THEN 明确错误态与提示、重试次数/间隔/终止条件，以及失败后的状态回退与补偿策略。"
    # 中断/重进/回滚：强调唯一落点与资源清理
    if topic == "state_recovery" or re.search(r"(退出|重进|切后台|恢复)", text):
        return "IF 用户中断后重进, THEN 系统落到唯一状态并回填 state_before/state_after/resume_policy。"
    if topic in ("transfer_cleanup",):
        return "IF 发生退出/切换/重进/环境重置, THEN 明确清理/保留的状态与资源清单，并保证再次进入的落点唯一可复现。"
    if topic == "data_contract" or re.search(r"(字段|错误码|状态值|接口)", text):
        return "IF 接口返回异常/缺字段, THEN 按统一错误码降级并记录 result_status/error_code。"
    if topic in ("acceptance_logging",):
        return "IF 核心流程达到终态, THEN 输出可验收的成功/失败判定与最小观测字段（日志/埋点/错误码），确保可复现可对账。"
    if topic in ("boundary_rule",):
        return "IF 输入触达边界值或非法值, THEN 明确最小/最大/默认与越界处理方式，并给出用户提示与错误码。"
    return "IF 命中该风险场景, THEN 按 PRD 固化可验收阈值、终态与最小观测字段。"


def _l3_build_risk_clusters(cards: List[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    """
    将碎片问题归并为风险簇，便于研发按模块一次消灭多条缺陷。
    """
    if not cards:
        return []
    def _low_signal_template(seed: Dict[str, Any]) -> bool:
        """
        平台通用：过滤/降权“模板洞察”类 seed（如状态孤岛/死路/非法跳转但缺少可追溯 quote），
        避免风险簇标题与动作被低信号项带偏。
        """
        if not isinstance(seed, dict):
            return True
        t = str(seed.get("type") or "")
        if t in ("状态孤岛", "状态死路", "非法状态跳转"):
            return not _l3_is_usable_quote(_issue_quote(seed))
        return False
    grouped: Dict[str, Dict[str, Any]] = {}
    for c in cards:
        if not isinstance(c, dict):
            continue
        seed = c.get("seed") if isinstance(c.get("seed"), dict) else {}
        topic = _issue_topic(seed)
        kind = _build_l2_issue_kind(seed) if isinstance(seed, dict) else "generic"
        # topic 纠偏（平台通用）：避免因为“同步/列表”等词误把“退出/中断/清理”归到 sync_latency
        seed_text = _l3_issue_text(seed) if isinstance(seed, dict) else ""
        # 设备/会话生命周期必须优先归一，避免误落到 sync_latency
        if re.search(r"(关台|待机|重开台|重开|重启|断电|上电|全断电|软重启|会话结束)", seed_text):
            topic = "device_lifecycle"
        if kind == "state" and topic in ("sync_latency", "cross_end_sync"):
            if re.search(r"(中断|退出|重进|切后台|清理|环境重置|回滚|恢复)", seed_text):
                topic = "transfer_cleanup"
        if kind == "security" and topic not in ("security_access", "qr_security"):
            if re.search(r"(扫码|二维码|越权|鉴权|有效期|失效|重放|访问边界)", seed_text):
                topic = "security_access"
        # 异常/弱网/超时：即便 kind 被打成 generic，也要避免误套 sync_latency 动作
        if topic in ("sync_latency", "cross_end_sync") and re.search(r"(失败|超时|弱网|断网|重试|降级|错误码|异常)", seed_text):
            topic = "exception_flow"
        # 冲突/裁决：优先级、开关、手动/自动打架时，不应归入 sync_latency
        if topic in ("sync_latency", "cross_end_sync", "generic_rule") and re.search(
            r"(优先级|裁决|互斥|冲突|手动|自动|开关|谁先生效|覆盖|按.{0,6}为准|decision_source|effective_rule)",
            seed_text,
        ):
            topic = "conflict_rule"
        module = str(c.get("module_label") or "跨模块").strip() or "跨模块"
        # key 加入 kind，避免“权限/状态/异常”等跨域条目被同 topic 名误合并
        key = f"{kind}|{topic}|{module}"
        cur = grouped.get(key)
        if not cur:
            cur = {
                "topic": topic or "generic",
                "kind": kind or "generic",
                "module": module,
                "level": str(c.get("level") or "P2").upper(),
                "count": 0,
                "titles": [],
                "seed": seed,
                "actions": [],
            }
            grouped[key] = cur
        cur["count"] += max(1, len(c.get("related_defects") or []))
        lv = str(c.get("level") or "P2").upper()
        if _risk_rank_local(lv) < _risk_rank_local(str(cur.get("level") or "P2")):
            cur["level"] = lv
            # 避免用低信号模板覆盖 seed
            if seed and not _low_signal_template(seed):
                cur["seed"] = seed
        tit = str(c.get("title") or "").strip()
        if tit and tit not in cur["titles"]:
            cur["titles"].append(tit)
        for act in (c.get("fix_items") or [])[:2]:
            a = str(act or "").strip()
            if a and a not in cur["actions"]:
                cur["actions"].append(a)
    out = list(grouped.values())
    out.sort(key=lambda x: (_risk_rank_local(str(x.get("level") or "")), -int(x.get("count") or 0)))
    return out[:limit]


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
    if defects:
        # L3 作为 Ground Truth：摘要计数优先使用去重后的缺陷母表，与详细矩阵保持一致。
        p0 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P0")
        p1 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P1")
        p2 = sum(1 for d in defects if str(d.get("risk_level") or "").upper() == "P2")
    else:
        p0 = sum(1 for c in issue_cards if str(c.get("level") or "").upper() == "P0")
        p1 = sum(1 for c in issue_cards if str(c.get("level") or "").upper() == "P1")
        p2 = sum(1 for c in issue_cards if str(c.get("level") or "").upper() == "P2")
    counts = f"P0 {p0} 项"
    if p1:
        counts += f"、P1 {p1} 项"
    if p2:
        counts += f"、P2 {p2} 项"
    top_titles: List[str] = []
    top3: List[str] = []
    title_seen: set = set()
    lane_seen: set = set()
    meeting_seen: set = set()

    def _lane_of_card(card: Dict[str, Any]) -> str:
        txt = " ".join([
            str(card.get("title") or ""),
            str(card.get("meeting_statement") or ""),
            str(card.get("problem") or ""),
        ])
        if re.search(r"(失败|超时|弱网|上传|回写|重试|降级|容错)", txt):
            return "容错链路"
        if re.search(r"(优先级|裁决|冲突|互斥|自动开启|手动开关|总开关|播控栏)", txt):
            return "优先级裁决"
        if re.search(r"(状态机|状态|重进|退出|关台|重启|断电|清理|恢复)", txt):
            return "状态一致性"
        if re.search(r"(权限|鉴权|越权|安全|失效|重放)", txt):
            return "安全边界"
        return "通用闭环"

    sorted_cards = sorted(
        [c for c in issue_cards if isinstance(c, dict)],
        key=lambda c: (
            _risk_rank_local(str(c.get("level") or "")),
            -len(str(c.get("problem") or "")) - len(str(c.get("title") or "")),
        ),
    )
    for card in sorted_cards:
        level = str(card.get("level") or "P2").upper()
        title = str(card.get("title") or "").strip()
        lane = _lane_of_card(card)
        if title and title not in top_titles:
            top_titles.append(title)
        if lane in lane_seen:
            continue
        lane_seen.add(lane)
        statement = str(card.get("meeting_statement") or "").strip()
        if statement and statement not in meeting_seen:
            bullet = f"{lane}：{statement}（{level}）"
            meeting_seen.add(statement)
        else:
            bullet = f"{lane}：{title}（{level}）" if title else ""
        if bullet and bullet not in top3:
            top3.append(bullet)
        if len(top3) >= 3:
            break

    if len(top3) < 3:
        for card in sorted_cards:
            level = str(card.get("level") or "P2").upper()
            title = str(card.get("title") or "").strip()
            if not title or title in title_seen:
                continue
            title_seen.add(title)
            bullet = f"{title}（{level}）"
            if bullet not in top3:
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


def _build_shared_summary(
    stage1_output: Dict[str, Any],
    llm_config_path: str = None,
    llm_config_override: Optional[Dict[str, Any]] = None,
    prd_text: str = "",
) -> Dict[str, Any]:
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

    def _clean_summary_item(text: str, limit: int = 160) -> str:
        s = _clean_summary_text(text, limit)
        if not s:
            return ""
        s = re.sub(r"^(本期核心功能包括|核心功能包括|支持功能包括|功能包括)[：:]\s*", "", s)
        s = re.sub(r"^适合[^。]{0,80}(快速拉齐|看懂|理解)[^。]*[。]?", "", s)
        s = re.sub(r"^(新手导读|认知大纲|总览)\s*", "", s)
        s = re.sub(r"(且|并且)?不(会)?被认领", "，不形成有效结果", s)
        s = re.sub(r"[。]{2,}", "。", s)
        s = re.sub(r"([。！？；])([、，])", r"\1", s)
        s = re.sub(r"([、，]){2,}", "、", s)
        s = re.sub(r"([，,])\s*([，,])+", r"\1", s)
        s = s.strip("，、；。 ")
        return s

    def _looks_like_summary_noise(text: str) -> bool:
        s = _clean_summary_text(text, 800)
        if not s:
            return False
        noise_hits = 0
        if re.search(
            r"(负责人|完成时间|上线时间|评审时间|客户端开发|服务端开发|测试负责人|UI设计|开发负责人|测试负责人|服务器|小程序开发)",
            s,
        ):
            noise_hits += 1
        if re.search(r"([一二三四五六七八九十]+、|[0-9]+\.)\s*(背景|目标|需求描述|功能|展示|规则|流程|范围)", s):
            noise_hits += 1
        if len(re.findall(r"[：:]", s)) >= 4:
            noise_hits += 1
        if len(re.findall(r"(负责人|时间|背景|目标|需求描述|功能|规则)", s)) >= 3:
            noise_hits += 1
        return noise_hits >= 2

    def _normalize_summary_source_text(text: str) -> str:
        s = _brief_text(text, 4000, keep_newlines=True)
        if not s:
            return ""
        replace_map = {
            "⼀": "一",
            "⼆": "二",
            "⼈": "人",
            "⼊": "入",
            "⼤": "大",
            "⼩": "小",
            "⼿": "手",
            "⼦": "子",
            "⼝": "口",
            "⼼": "心",
            "⼾": "户",
            "⽤": "用",
            "⽰": "示",
            "⾳": "音",
            "⽂": "文",
            "⽀": "支",
            "⻚": "页",
        }
        for src, dst in replace_map.items():
            s = s.replace(src, dst)
        s = re.sub(r"[\t\r\f\v]+", " ", s)
        s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
        s = re.sub(r"(?<=[A-Za-z])\s+(?=[A-Za-z])", "", s)
        s = re.sub(r"\s{2,}", " ", s)
        return s.strip()

    def _looks_like_scope_constraint(text: str) -> bool:
        s = _clean_summary_text(text, 300)
        if not s:
            return False
        constraint_hits = 0
        if re.search(
            r"(型号|机型|版本|版型|系统|平台|设备|终端|客户端|服务端|浏览器|操作系统|渠道|环境|适配|兼容|支持范围|适用于|仅支持|限于|范围|清单|列表|配置|参数|规格|套餐|标准版|专业版|企业版|定制版)",
            s,
            re.IGNORECASE,
        ):
            constraint_hits += 1
        if re.search(r"[：:]", s):
            constraint_hits += 1
        if s.count("、") >= 1 or s.count("，") >= 2 or s.count(",") >= 2 or "/" in s:
            constraint_hits += 1
        if len(s) <= 40:
            constraint_hits += 1
        intent_hits = 0
        if re.search(
            r"(用于|为了|以便|实现|提供|帮助|完成|达成|规范|优化|提升|建立|打通|确保|交付|减少|降低|沉淀|构建|形成|支撑)",
            s,
        ):
            intent_hits += 1
        return constraint_hits >= 2 and intent_hits == 0

    def _build_goal_from_scope(scope_items: List[str], product_label: str) -> str:
        clean_items: List[str] = []
        for item in scope_items[:3]:
            text = _clean_summary_item(item, 80)
            if not text or _looks_like_scope_constraint(text):
                continue
            text = re.sub(r"^(点击|通过|当|在|若|如果|用户)", "", text).strip()
            text = re.sub(r"(后|时)$", "", text).strip()
            clean_items.append(text)
        if clean_items:
            subject = clean_items[0]
            if len(subject) > 28:
                subject = subject[:28].rstrip("，、； ")
            return f"明确{subject}的核心流程、状态规则与交互边界"
        return f"规范 {product_label} 的核心功能与交互逻辑"

    def _build_scope_items(scope_items: List[str]) -> List[str]:
        out: List[str] = []
        seen: set = set()
        for item in scope_items[:12]:
            text = _clean_summary_item(item, 160)
            if not text or irrelevant_pattern.search(text):
                continue
            # 范围/适配约束更适合放到红线口径，不放到“核心功能”里。
            if _looks_like_scope_constraint(text):
                continue
            if len(text) < 4:
                continue
            if text not in seen:
                seen.add(text)
                out.append(text)
        return out

    def _normalize_purpose_sentence(text: str, scope_items: List[str], product_label: str) -> str:
        s = _clean_summary_item(text, 220)
        if not s:
            return _build_goal_from_scope(scope_items, product_label)
        s = re.sub(r"^适合[^。]{0,80}(快速拉齐|看懂|理解)[^。]*[。]?", "", s)
        s = re.sub(r"^在[^，。；]{0,80}?(上|中|下)", "", s)
        s = re.sub(r"^(面向|针对)[^，。；]{0,60}", "", s)
        if re.search(r"(型号|机型|版本|系统|设备|终端)", s) and re.search(r"(实现|支持|提供)", s):
            s = re.sub(r"^为[^，。；]{0,80}?(实现|支持|提供)", r"\1", s)
        s = s.strip("，、；。 ")
        clauses = [x.strip("，、；。 ") for x in re.split(r"[，。；]", s) if x and x.strip("，、；。 ")]
        if not clauses:
            return _build_goal_from_scope(scope_items, product_label)
        purpose = clauses[0]
        if _looks_like_scope_constraint(purpose):
            for clause in clauses[1:]:
                if not _looks_like_scope_constraint(clause):
                    purpose = clause
                    break
        if len(purpose) < 6 or _looks_like_scope_constraint(purpose):
            purpose = _build_goal_from_scope(scope_items, product_label)
        purpose = purpose.strip("，、；。 ")
        if purpose and not purpose.endswith("。"):
            purpose += "。"
        return purpose

    def _normalize_summary_items(items: List[str], limit: int = 5, keep_constraints: bool = False) -> List[str]:
        def _looks_like_broken_rule_line(text: str) -> bool:
            s = _clean_summary_item(text, 220)
            if not s:
                return True
            if re.search(
                r"(清空|开启|关闭|显示|保存|上传).{0,20}但.{0,20}(不清空|不开启|不关闭|不显示|不保存|不上传)",
                s,
            ):
                return True
            parts = [p.strip() for p in re.split(r"[，；]", s) if p and p.strip()]
            if len(parts) >= 2:
                verb_pattern = r"(是|为|可|会|需|应|支持|展示|开启|关闭|保存|清空|上传|进入|关联|点击|点播|触发|提示|生成|返回|播放)"
                for p in parts:
                    if len(p) <= 8 and not re.search(verb_pattern, p):
                        return True
            return False

        out: List[str] = []
        seen: set = set()
        for raw in items:
            text = _clean_summary_item(raw, 180)
            if not text or irrelevant_pattern.search(text):
                continue
            if not keep_constraints and _looks_like_scope_constraint(text):
                continue
            if keep_constraints and _looks_like_broken_rule_line(text):
                continue
            text = re.sub(r"\s+", " ", text)
            if len(text) < 4:
                continue
            if text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def _looks_like_generic_purpose(text: str) -> bool:
        s = _clean_summary_item(text, 120)
        if not s:
            return True
        if len(s) <= 10:
            return True
        if len(s) <= 22 and not re.search(r"(保存|上传|同步|获取|列表|下载|状态|边界|结果)", s):
            return True
        return bool(
            re.fullmatch(
                r"(实现|支持|提供|规范).{0,10}(相关)?(功能|能力|逻辑|流程)(与|及|、)?.{0,8}。",
                s,
            )
        )

    def _purpose_candidate_score(text: str) -> int:
        s = _clean_summary_item(text, 120)
        if not s:
            return -99
        score = 0
        if _looks_like_scope_constraint(s):
            score -= 6
        if re.search(r"(功能|能力|流程|作品|资源|文件|列表|服务)", s):
            score += 4
        if re.search(r"(保存|上传|获取|同步|播放|录制|提交|生成|回写)", s):
            score += 3
        if re.search(r"(列表|扫码|取回|下载|云端|上传)", s):
            score += 2
        if re.search(r"(展示|页面|图标|icon|按钮|小字提示|提示语|右侧展示|弹窗|入口)", s, re.I):
            score -= 3
        if len(s) >= 12:
            score += 1
        return score

    def _extract_purpose_focus(text: str) -> str:
        s = _clean_summary_item(text, 120)
        if not s:
            return ""
        s = re.sub(r"^(用户|系统|平台|终端|设备|客户端|服务端|TV屏幕|电视端|触摸屏|播控栏|设置页)", "", s).strip()
        s = re.sub(r"^(点击|点播|通过|当|在|若|如果|并|且)", "", s).strip()
        m = re.search(r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24}(功能|能力|流程|作品|资源|文件|列表|服务))", s)
        if m:
            return m.group(1)
        m = re.search(r"(支持|实现|提供|管理|处理)([\u4e00-\u9fffA-Za-z0-9_-]{2,24})", s)
        if m:
            return m.group(2)
        s = re.sub(r"[，。；].*$", "", s).strip()
        return s[:18].strip("，、；。 ")

    def _build_specific_purpose_candidate(scope_items: List[str], flow_items: List[str], product_label: str) -> str:
        candidates: List[str] = []
        for text in scope_items[:4] + flow_items[:3] + rules[:3]:
            s = _clean_summary_item(text, 120)
            if not s or _looks_like_scope_constraint(s):
                continue
            if len(s) >= 6:
                candidates.append(s)
        if candidates:
            candidates.sort(key=lambda x: (_purpose_candidate_score(x), len(x)), reverse=True)
            core = _extract_purpose_focus(candidates[0]) or product_label
            merged = " ".join(candidates[:5])
            trigger_hit = bool(re.search(r"(点击|点播|选择|触发|开启|关闭|打开|扫码)", merged))
            result_hit = bool(re.search(r"(保存|上传|同步|获取|进入|生成|回写|归入|下载)", merged))
            process_hit = bool(re.search(r"(展示|录制|执行|处理|播放|回放|开始)", merged))
            archive_hit = bool(re.search(r"(保存|上传|同步|回写|归入|进入.*列表|落库)", merged))
            fetch_hit = bool(re.search(r"(获取|取回|下载|扫码|列表)", merged))
            status_hit = bool(re.search(r"(录制中|展示.*状态|提示|反馈)", merged))
            if archive_hit and fetch_hit:
                return f"围绕{core}，明确触发方式、执行过程、结果归档与用户取回链路。"
            if trigger_hit and process_hit and result_hit and status_hit:
                return f"围绕{core}，明确触发方式、状态反馈、执行规则与结果链路。"
            if trigger_hit and result_hit:
                return f"围绕{core}，明确触发方式、执行规则与结果链路。"
            if trigger_hit or process_hit:
                return f"围绕{core}，明确核心流程、录制规则与交互边界。"
            return f"明确{core}的能力边界与结果规则。"
        return f"规范 {product_label} 的核心功能与交互逻辑。"

    def _build_flow_items(flow_items: List[str], scope_items: List[str], rule_items: List[str], limit: int = 3) -> List[str]:
        def _flow_signature(text: str) -> str:
            if re.search(r"(录制中|展示.*状态|TV右侧展示)", text):
                return "display-status"
            if re.search(r"(快唱副歌|手机端点播|自动开启|自动关闭|点播其他歌曲)", text):
                return "auto-trigger"
            if re.search(r"(保存|上传|进入.*列表|归入|获取|下载|扫码|回写|落库)", text):
                return "result-chain"
            if re.search(r"(重新播放|回放)", text):
                return "replay"
            return re.sub(r"\s+", "", text)[:24]

        def _flow_bucket(text: str) -> str:
            if re.search(r"(录制中|展示.*状态|TV右侧展示)", text):
                return "process"
            if re.search(r"(保存|上传|同步|进入.*列表|归入|获取|下载|生成作品|回写|落库)", text):
                return "result"
            if re.search(r"(点击|点播|选择|触发|开启|关闭|打开|扫码|进入)", text):
                return "trigger"
            if re.search(r"(展示|开始|录制|执行|处理中|弹出|提示)", text):
                return "process"
            return "other"

        def _step_score(text: str, bucket: str) -> int:
            score = 0
            if bucket == "trigger":
                if re.search(r"(点击|点播|选择|扫码|触发)", text):
                    score += 4
                if re.search(r"(展示.*状态|录制中)", text):
                    score -= 3
                if re.search(r"(保存|上传|进入.*列表|获取|下载)", text):
                    score -= 3
            elif bucket == "process":
                if re.search(r"(录制中|展示.*状态|开始录制|自动开启|自动关闭|执行)", text):
                    score += 4
                if re.search(r"(保存|上传|进入.*列表|获取|下载)", text):
                    score -= 2
            elif bucket == "result":
                if re.search(r"(保存|生成作品)", text):
                    score += 3
                if re.search(r"(上传|同步|进入.*列表|归入|获取|下载|回写|落库)", text):
                    score += 4
            return score

        def _pick_best_step(items: List[str], bucket: str) -> str:
            if not items:
                return ""
            ranked = sorted(
                items,
                key=lambda t: (_step_score(t, bucket), len(_clean_summary_item(t, 180))),
                reverse=True,
            )
            return ranked[0]

        def _build_result_step(items: List[str]) -> str:
            if not items:
                return ""
            ranked = sorted(
                items,
                key=lambda t: (_step_score(t, "result"), len(_clean_summary_item(t, 180))),
                reverse=True,
            )
            save_step = next((t for t in ranked if re.search(r"(保存|生成作品)", t)), "")
            deliver_step = next((t for t in ranked if re.search(r"(上传|同步|进入.*列表|归入|获取|下载|回写|落库)", t)), "")
            if save_step and deliver_step and save_step != deliver_step:
                combined = f"{save_step}；{deliver_step}"
                return _clean_summary_item(combined, 220)
            return ranked[0]

        def _has_result_delivery(text: str) -> bool:
            return bool(re.search(r"(上传|同步|进入.*列表|归入|获取|下载|回写|落库)", text or ""))

        candidates: List[str] = []
        seen: set = set()
        for source in (flow_items, scope_items, rule_items):
            for raw in source:
                text = _clean_summary_item(raw, 180)
                if not text or text in seen:
                    continue
                if source is rule_items and _looks_like_scope_constraint(text):
                    continue
                if re.search(r"(展示方式|展示设备|触摸屏和电视端|触摸屏、电视端|仅支持|适用于|型号|版本)", text):
                    continue
                if source is not flow_items and not re.search(
                    r"(点击|点播|选择|触发|开始|开启|关闭|展示|保存|上传|进入|生成|返回|播放|结束|停止|提交|同步|获取|下载)",
                    text,
                ):
                    continue
                seen.add(text)
                candidates.append(text)
        if not candidates:
            return []

        buckets: Dict[str, List[str]] = {"trigger": [], "process": [], "result": [], "other": []}
        for text in candidates:
            buckets[_flow_bucket(text)].append(text)

        out: List[str] = []
        seen_signatures: set = set()
        first_trigger = _pick_best_step(buckets["trigger"], "trigger")
        if first_trigger:
            sig = _flow_signature(first_trigger)
            out.append(first_trigger)
            seen_signatures.add(sig)
        first_process = _pick_best_step(
            [t for t in buckets["process"] if _flow_signature(t) not in seen_signatures and _flow_signature(t) != "replay"],
            "process",
        )
        if first_process:
            sig = _flow_signature(first_process)
            out.append(first_process)
            seen_signatures.add(sig)
        first_result = _build_result_step([t for t in buckets["result"] if _flow_signature(t) not in seen_signatures])
        if first_result:
            sig = _flow_signature(first_result)
            out.append(first_result)
            seen_signatures.add(sig)
        if first_result and not _has_result_delivery(first_result):
            extra_delivery = next(
                (
                    t
                    for t in candidates
                    if _has_result_delivery(t) and _flow_signature(t) not in seen_signatures
                ),
                "",
            )
            if extra_delivery:
                out[-1] = _clean_summary_item(f"{out[-1]}；{extra_delivery}", 220)
                seen_signatures.add(_flow_signature(extra_delivery))
        for text in candidates:
            sig = _flow_signature(text)
            if sig in seen_signatures:
                continue
            if text not in out:
                out.append(text)
                seen_signatures.add(sig)
            if len(out) >= limit:
                break
        return out[:limit]

    def _build_conflict_hint(scope_items: List[str], rule_items: List[str]) -> str:
        combined = [x for x in (scope_items[:6] + rule_items[:6]) if x]
        if not combined:
            return ""
        auto_hit = any(re.search(r"(自动(开启|执行|触发|生效)|默认(开启|执行|生效)|系统自动)", x) for x in combined)
        manual_hit = any(
            re.search(
                r"(最终是否.*根据用户.*(开关|设置|选择).*(决定|控制)|最终.*由用户.*(开关|设置|选择)决定|根据用户.*(开关|设置|手动|播控栏).*(决定|控制)|由用户.*(开关|设置|手动|播控栏).*(决定|控制)|依据.*(开关|设置).*(决定|控制)|手动(开启|关闭)|用户.*(开启|关闭|控制).*(开关|设置))",
                x,
            )
            for x in combined
        )
        dual_switch_hit = any(re.search(r"(播控栏.*开关|录音开关)", x) for x in combined) and any(
            re.search(r"(设置页.*开关|总开关|图标.*展示)", x) for x in combined
        )
        if dual_switch_hit:
            return "播控栏开关与设置页开关同时参与录制决策，需明确两者的作用范围、优先级，以及图标展示与实际录制是否共用同一套规则。"
        if auto_hit and manual_hit:
            return "存在“自动执行”与“用户开关/手动控制”并存的规则，需明确冲突时以谁为准，否则不同入口会得出相反结果。"
        return ""

    def _refine_key_points(rule_items: List[str], scope_items: List[str]) -> List[str]:
        refined: List[str] = []
        seen: set = set()
        for raw in rule_items:
            text = _clean_summary_item(raw, 220)
            if not text:
                continue
            if re.search(r"(版本|版型|型号|机型|系统|终端|展示渠道|展示端|触摸屏|电视端)", text):
                text = "适用范围需限定到明确的版本、运行环境与展示终端，避免不同环境下口径不一致。"
            elif re.search(r"(图标|icon|展示)", text) and re.search(r"(开关|设置|启停|录制|生效)", text):
                text = "开关控制、图标展示与实际能力启停需保持一致，避免出现“可见但不可用”或“已执行但无提示”。"
            elif re.search(r"(保存|上传|同步|进入.*列表|归入|获取|下载|回写|落库)", text):
                text = "结果链路需明确保存条件、上传时机与用户可见结果，避免各端对完成状态理解不一致。"
            if text not in seen:
                seen.add(text)
                refined.append(text)
        if not refined:
            return rule_items[:5]
        return refined[:5]

    def _summary_term_bigram_guard(output: str, source_text: str) -> bool:
        out = _clean_summary_item(output, 220)
        src = _clean_summary_text(source_text, 2000)
        if not out or not src:
            return True
        allowed = {
            "核心", "流程", "状态", "规则", "边界", "交互", "能力", "功能", "系统", "用户",
            "实现", "支持", "明确", "需求", "逻辑", "目标", "产品", "场景", "主线", "结果",
        }
        suspicious: set = set()
        for token in re.findall(r"[\u4e00-\u9fff]{2,12}", out):
            for i in range(len(token) - 1):
                bg = token[i : i + 2]
                if bg in allowed:
                    continue
                if bg not in src:
                    suspicious.add(bg)
        return len(suspicious) <= 1

    def _collect_summary_source_text() -> str:
        chunks: List[str] = []
        raw_prd = _normalize_summary_source_text(prd_text)
        if raw_prd:
            chunks.append(raw_prd)
        for key in ("goal", "background"):
            text = _normalize_summary_source_text(s1.get(key))
            if text:
                chunks.append(text)
        blocks = s1.get("blocks") if isinstance(s1.get("blocks"), list) else []
        for block in blocks[:8]:
            if not isinstance(block, dict):
                continue
            text = _normalize_summary_source_text(
                f"{block.get('title') or ''}\n{block.get('content') or ''}"
            )
            if text:
                chunks.append(text)
        merged = "\n".join(x for x in chunks if x).strip()
        if not merged:
            return ""
        return merged[:4000]

    def _guess_product_name(raw_text: str) -> str:
        text = _normalize_summary_source_text(raw_text)
        if not text:
            return ""
        for pat in (
            r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24}(?:功能|模块|系统|平台|服务|流程|方案|能力))",
            r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24})\s*(?:需求|PRD)",
        ):
            m = re.search(pat, text)
            if m:
                return m.group(1).strip("：:，、；。 ")
        return ""

    def _extract_fallback_summary_candidates(raw_text: str) -> Dict[str, List[str]]:
        text = _normalize_summary_source_text(raw_text)
        if not text:
            return {"scope": [], "flow": [], "rules": []}
        normalized = text
        normalized = re.sub(r"\b[a-zA-Z]\.\s*", "；", normalized)
        normalized = re.sub(r"([。！？；;])", r"\1\n", normalized)
        normalized = re.sub(r"(?:^|[\n\s])(?:[一二三四五六七八九十]+、|\d+[\.、）)])\s*", "\n", normalized)
        pieces: List[str] = []
        for raw in re.split(r"[\n]+", normalized):
            item = _clean_summary_item(raw, 220)
            if not item or _looks_like_summary_noise(item) or len(item) < 4:
                continue
            if re.search(r"(负责人|完成时间|上线时间|UI设计|开发|测试|服务端|客户端)", item):
                continue
            if item not in pieces:
                pieces.append(item)
        scope_candidates: List[str] = []
        flow_candidates: List[str] = []
        rule_candidates: List[str] = []
        for item in pieces:
            item_lower = item.lower()
            scope_hit = bool(
                re.search(
                    r"(功能|模块|能力|开关|列表|图标|二维码|入口|页面|作品|文件|资源|按钮|状态|录制|上传|下载|取回|展示|同步|保存|回写|扫码)",
                    item,
                )
            )
            flow_hit = bool(
                re.search(
                    r"(点击|点播|扫码|扫描|打开|关闭|进入|生成|上传|保存|下载|获取|展示|开始|结束|返回|提交|同步|弹出|自动开启|自动关闭)",
                    item,
                )
            )
            rule_hit = bool(
                re.search(
                    r"(自动|手动|默认|仅支持|需明确|必须|应当|大于|小于|阈值|开关|优先级|失效|有效期|清空|保留|失败|超时|异常|重试|谁为准|是否录制|边界)",
                    item,
                )
            ) or _looks_like_scope_constraint(item)
            if scope_hit and not _looks_like_scope_constraint(item) and item not in scope_candidates:
                scope_candidates.append(item)
            if flow_hit and item not in flow_candidates:
                flow_candidates.append(item)
            if rule_hit and item not in rule_candidates:
                rule_candidates.append(item)
            if "icon" in item_lower and item not in rule_candidates:
                rule_candidates.append(item)
        return {
            "scope": scope_candidates[:8],
            "flow": flow_candidates[:8],
            "rules": rule_candidates[:8],
        }

    def _extract_first_json_obj(text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        if not raw:
            return {}

        def _scan_json_obj(buf: str) -> Dict[str, Any]:
            start = buf.find("{")
            if start < 0:
                return {}
            depth = 0
            in_str = None
            esc = False
            for i in range(start, len(buf)):
                ch = buf[i]
                if esc:
                    esc = False
                    continue
                if in_str:
                    if ch == "\\":
                        esc = True
                        continue
                    if ch == in_str:
                        in_str = None
                    continue
                if ch in ('"', "'"):
                    in_str = ch
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        seg = buf[start : i + 1]
                        try:
                            obj = json.loads(seg)
                            return obj if isinstance(obj, dict) else {}
                        except Exception:
                            break
            return {}

        fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.I)
        for block in fenced_blocks:
            obj = _scan_json_obj(str(block or "").strip())
            if obj:
                return obj
        obj = _scan_json_obj(raw)
        if obj:
            return obj
        for idx, ch in enumerate(raw):
            if ch != "{":
                continue
            obj = _scan_json_obj(raw[idx:])
            if obj:
                return obj
        return {}

    def _normalize_summary_token(text: str) -> str:
        s = _normalize_summary_source_text(text)
        if not s:
            return ""
        s = s.strip("：:，、；。()（）[]【】 ")
        # 英文/数字类枚举按大小写不敏感处理，中文原样保留
        if re.search(r"[A-Za-z0-9]", s):
            s = s.lower()
        return s

    def _split_enum_items(text: str) -> List[str]:
        if not text:
            return []
        cleaned = _normalize_summary_source_text(text)
        cleaned = re.sub(r"^[^：:]*[：:]\s*", "", cleaned)
        cleaned = re.sub(r"[。；;].*$", "", cleaned)
        out: List[str] = []
        for part in re.split(r"[、，,/ ]+", cleaned):
            item = _normalize_summary_token(part)
            if item and item not in out:
                out.append(item)
        return out

    def _extract_summary_facts(raw_text: str) -> Dict[str, Any]:
        text = _normalize_summary_source_text(raw_text)
        box_models: List[str] = []
        inline_models: List[str] = []
        system_fields: List[str] = []
        system_models: List[str] = []
        versions: List[str] = []
        terminals: List[str] = []

        def _norm_model_token(token: str) -> str:
            s = _normalize_summary_token(token)
            if not s:
                return ""
            # 常见别名：PAI2/pai2 -> 派2
            if re.fullmatch(r"pai\s*2", s, re.I) or s in ("pai2",):
                return "派2".lower()
            return s.lower()

        def _push_model(dst: List[str], token: str) -> None:
            norm = _norm_model_token(token)
            if not norm:
                return
            if not re.fullmatch(r"(?:x\d+|派\d+)", norm, re.I):
                return
            if norm not in dst:
                dst.append(norm)
        for m in re.finditer(r"(?:盒子型号|型号|机型)[：:]\s*([^\n。；]{1,80})", text, re.I):
            for token in _split_enum_items(m.group(1)):
                _push_model(box_models, token)
        # 表格/换行场景兜底：如“盒子型号 x9、x7”或“盒子型号：x9、x7、”被分隔符打断，导致上面的 key:value 未命中
        # 仅在同一行出现“型号/机型/盒子”时才抓取，避免把正文示例误判为范围。
        for ln in (text or "").splitlines():
            line = _normalize_summary_source_text(ln)
            if not line:
                continue
            if not re.search(r"(盒子|机顶盒).{0,6}(型号|机型)|(?:盒子型号|型号|机型)", line, re.I):
                continue
            toks = re.findall(r"[xX]\d+|派\d+|PAI2", line, flags=re.I)
            if not toks:
                continue
            for tok in toks:
                _push_model(box_models, tok)

        # 降级提取：只在“支持/适配/范围/仅支持”邻近区域里识别型号，避免把示例/编号误当范围
        for m in re.finditer(
            r"(?:支持|适配|范围|仅支持|只支持)[^。\n]{0,24}((?:[xX]\d+|派\d+|PAI2)(?:[、，/ ]+(?:[xX]\d+|派\d+|PAI2))*)",
            text,
            re.I,
        ):
            for token in re.findall(r"[xX]\d+|派\d+|PAI2", m.group(1), re.I):
                _push_model(inline_models, token)
        for m in re.finditer(r"(?:系统|OS|平台)[：:]\s*([^\n。；]{1,40})", text, re.I):
            field_text = _normalize_summary_source_text(m.group(1))
            if field_text and field_text not in system_fields:
                system_fields.append(field_text)
            for token in _split_enum_items(m.group(1)):
                if re.fullmatch(r"(?:[xX]\d+|派\d+)", token):
                    _push_model(system_models, token)

        # 型号抽取口径（平台通用）：
        # - 显式“型号:”通常最可靠，但可能只列出部分（例如遗漏派生机型），因此用“并集补齐”而非二选一覆盖。
        # - 系统/平台字段与“支持/适配/范围”邻近提取用于补全，不改变显式枚举的排序优先级。
        models: List[str] = []
        for src in (box_models, system_models, inline_models):
            for x in src or []:
                if x and x not in models:
                    models.append(x)
        for token in ["定制版", "标准版", "专业版", "企业版"]:
            if token in text and token not in versions:
                versions.append(token)
        for token in ["触摸屏", "电视端", "TV屏幕", "TV端", "手机端", "小程序"]:
            if token in text and token not in terminals:
                terminals.append(token)
        clear_triggers = [x for x in ["盒子重启", "重启", "关台", "重开台"] if x in text]
        clear_triggers = [x for i, x in enumerate(clear_triggers) if x not in clear_triggers[:i]]
        keep_exception = "转台" if re.search(r"转台.{0,8}不清空|不清空.{0,8}转台", text) else ""
        setting_switch = bool(re.search(r"设置.{0,10}开关|总开关|设置页.{0,10}开关", text))
        control_switch = bool(re.search(r"播控栏.{0,10}开关", text))
        auto_rule = bool(re.search(r"(快唱副歌|手机端点播).{0,16}(自动开启|自动打开|自动录音)|自动开启", text))
        return {
            "box_models": box_models[:6],
            "models": models[:6],
            "system_fields": system_fields[:4],
            "system_models": system_models[:4],
            "versions": versions[:6],
            "terminals": terminals[:6],
            "clear_triggers": clear_triggers[:6],
            "keep_exception": keep_exception,
            "setting_switch": setting_switch,
            "control_switch": control_switch,
            "auto_rule": auto_rule,
        }

    def _build_scope_fact_items(facts: Dict[str, Any]) -> List[str]:
        items: List[str] = []
        models = [str(x) for x in (facts.get("box_models") or facts.get("models") or []) if x]
        versions = [str(x) for x in facts.get("versions", []) if x]
        terminals = [str(x) for x in facts.get("terminals", []) if x]
        if models:
            items.append("型号范围：" + "、".join(models))
        if versions:
            items.append("版本范围：" + "、".join(versions))
        if terminals:
            items.append("终端范围：" + "、".join(terminals))
        return items[:3]

    def _dedupe_clear_triggers(items: List[str]) -> List[str]:
        deduped = [str(x) for x in (items or []) if str(x or "").strip()]
        seen: set = set()
        out: List[str] = []
        for raw in deduped:
            text = str(raw).strip()
            if not text:
                continue
            if text == "重启" and "盒子重启" in deduped:
                continue
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out[:6]

    def _merge_scope_with_facts(scope_items: List[str], facts: Dict[str, Any], limit: int = 8) -> List[str]:
        merged: List[str] = []
        seen: set = set()
        for item in (_build_scope_fact_items(facts) + list(scope_items or [])):
            text = _clean_summary_item(item, 180)
            if not text:
                continue
            norm = _normalize_summary_token(text)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            merged.append(text)
            if len(merged) >= limit:
                break
        return merged

    def _build_scope_capability_items(items: List[str], limit: int = 5) -> List[str]:
        out: List[str] = []
        seen: set = set()
        for raw in items:
            text = _clean_summary_item(raw, 180)
            if not text or irrelevant_pattern.search(text):
                continue
            if _looks_like_summary_noise(text):
                continue
            if re.search(r"(背景|目标|需求描述|功能说明|规则说明)", text):
                continue
            if _looks_like_scope_constraint(text):
                continue
            if re.search(r"(大于\s*10秒|不满\s*10秒|重启|关台|重开台|转台|优先级|裁决|谁为准|冲突|重新播放|重播)", text):
                continue
            if re.search(r"^(打开|关闭|开启|关闭时|打开时|若|如果|则|否则)\b", text):
                continue
            if re.search(r"(点播|自动开启|自动打开|根据用户|最终是否|保存作品|清空|不清空)", text):
                continue
            if not re.search(r"(录音|录制|开关|图标|icon|二维码|扫码|小程序|列表|获取|上传|状态|录制中|展示)", text, re.I):
                continue
            norm = _normalize_summary_token(text)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def _build_fixed_core_flow(
        flow_items: List[str],
        scope_items: List[str],
        rule_items: List[str],
        facts: Dict[str, Any],
        raw_text: str,
        limit: int = 3,
    ) -> List[str]:
        text = _normalize_summary_source_text(raw_text + "\n" + "\n".join((flow_items or [])[:8] + (scope_items or [])[:8] + (rule_items or [])[:8]))
        out: List[str] = []
        seen: set = set()

        def _push(item: str) -> None:
            s = _clean_summary_item(item, 220)
            if not s:
                return
            norm = _normalize_summary_token(s)
            if not norm or norm in seen:
                return
            seen.add(norm)
            out.append(s)

        auto_hit = bool(re.search(r"(手机端点播|快唱副歌).{0,24}(自动开启|自动打开|录音自动开启)|点播其他歌曲.{0,12}(关闭|录音功能关闭)", text))
        if auto_hit:
            _push("手机端点播快唱副歌歌曲时触发录音，点播其他歌曲时关闭录音。")
        else:
            trigger = next((x for x in flow_items if re.search(r"(点击|点播|扫码|进入|触发)", str(x))), "")
            if trigger:
                _push(trigger)

        display_hit = bool(re.search(r"(录制中|TV右侧展示|右侧展示|展示录音状态|录音状态)", text))
        if display_hit:
            _push("系统执行录音并在 TV 端展示录制中状态。")
        else:
            execute = next((x for x in flow_items if re.search(r"(展示|开始|录制|执行|状态)", str(x))), "")
            if execute:
                _push(execute)

        result_hit = bool(re.search(r"(上传|云端|手机端录音列表|已唱列表|二维码|小程序|获取|下载|列表)", text))
        if result_hit:
            _push("录音满足保存条件后上传云端，用户可通过列表或扫码进入小程序获取录音。")
        else:
            result = next((x for x in flow_items + rule_items if re.search(r"(保存|上传|获取|扫码|小程序|列表|下载)", str(x))), "")
            if result:
                _push(result)

        if not out:
            out = _build_flow_items(flow_items, scope_items, rule_items, limit=limit)
        return out[:limit]

    def _infer_summary_subject(product_label: str, capability_items: List[str], flow_items: List[str], raw_text: str) -> str:
        text = _normalize_summary_source_text(raw_text)
        for pat in (
            r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24}录音功能)",
            r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24}录音)",
            r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24}歌曲)",
        ):
            m = re.search(pat, text)
            if m:
                candidate = m.group(1).strip("，、；。 ")
                if not re.search(r"(背景|目标|需求描述|规则|流程|展示|状态|点击|图标|开关)", candidate, re.I):
                    return candidate
        focus = _extract_purpose_focus(" ".join(capability_items[:4] + flow_items[:3]))
        if focus:
            subject = focus
        else:
            subject = product_label
        if re.search(r"(背景|目标|需求描述|规则|流程)", subject):
            subject = product_label
        if re.search(r"(TV屏幕|电视端|触摸屏)", subject) and "录音" not in subject:
            subject = product_label
        if re.search(r"(展示规则|状态展示|功能tv|tv屏幕展示)", subject, re.I):
            subject = product_label
        if re.search(r"(背景|目标|需求描述|规则|流程|TV屏幕|电视端|触摸屏|展示规则|展示|状态|图标|开关)", str(subject), re.I):
            subject = product_label or "该功能"
        return subject

    def _build_fixed_summary_purpose(
        base_text: str,
        product_label: str,
        capability_items: List[str],
        flow_items: List[str],
        key_points: List[str],
        raw_text: str,
    ) -> str:
        base = _clean_summary_item(base_text, 220)
        merged = "\n".join(capability_items[:5] + flow_items[:3] + key_points[:3] + [raw_text[:600]])
        record_hit = bool(re.search(r"(录音|录制)", merged))
        upload_hit = bool(re.search(r"(上传|云端|同步|回写|落库)", merged))
        fetch_hit = bool(re.search(r"(获取|扫码|小程序|列表|下载|取回)", merged))
        if base and not _looks_like_scope_constraint(base):
            if not re.search(r"(大于\s*10秒|不满\s*10秒|重启|关台|重开台|转台|型号范围|版本范围|终端范围|仅支持|背景|目标|需求描述|展示规则|TV屏幕)", base, re.I):
                # 若原文已明确结果链路，则目的句也需带出上传/获取，不再只停留在能力描述。
                if record_hit and upload_hit and fetch_hit and not re.search(r"(上传|获取|扫码|列表|小程序|下载)", base):
                    base = ""
                elif re.search(r"(录音|录制|上传|获取|扫码|列表|下载|状态|结果链路)", base):
                    if not base.endswith("。"):
                        base += "。"
                    return base
        subject = _infer_summary_subject(product_label, capability_items, flow_items, raw_text)
        status_hit = bool(re.search(r"(录制中|状态|图标|展示)", merged))
        if record_hit and upload_hit and fetch_hit:
            return f"实现{subject}的录制、上传与用户获取闭环。"
        if record_hit and upload_hit:
            return f"明确{subject}的触发方式、录制规则与结果链路。"
        if record_hit and status_hit:
            return f"明确{subject}的触发方式、状态反馈与录制规则。"
        if record_hit:
            return f"明确{subject}的核心流程、录制规则与交互边界。"
        return _build_goal_from_scope(capability_items, product_label)

    def _build_fixed_fact_points(
        rule_items: List[str],
        flow_items: List[str],
        scope_items: List[str],
        facts: Dict[str, Any],
        raw_text: str,
        limit: int = 5,
    ) -> List[str]:
        out: List[str] = []
        seen: set = set()

        def _push(text: str) -> None:
            s = _clean_summary_item(text, 220)
            if not s:
                return
            norm = _normalize_summary_token(s)
            if not norm or norm in seen:
                return
            seen.add(norm)
            out.append(s)

        combined = [x for x in (rule_items + flow_items + scope_items) if x]
        source_text = _normalize_summary_source_text(raw_text + "\n" + "\n".join(combined[:12]))
        if re.search(r"(大于\s*10秒|超过\s*10秒|10秒)", source_text):
            _push("录音时长需以大于10秒为保存门槛，不满足条件时不保存。")
        clear_triggers = _dedupe_clear_triggers(facts.get("clear_triggers", []))
        keep_exception = str(facts.get("keep_exception") or "")
        if clear_triggers and keep_exception:
            trigger_text = "、".join(clear_triggers)
            _push(f"{trigger_text}会清空未认领内容，{keep_exception}不清空。")
        if re.search(r"(图标|icon|录制中|状态)", source_text, re.I) and re.search(r"(开关|设置|播控栏|录制)", source_text):
            _push("图标展示、录制中状态与实际录制启停需保持一致，避免外显状态与真实执行结果不一致。")
        for raw in rule_items:
            text = _clean_summary_item(raw, 220)
            if not text:
                continue
            if re.search(r"(型号|机型|版本|系统|终端|触摸屏|电视端|手机端|小程序)", text):
                continue
            if re.search(r"(优先级|裁决|谁为准|冲突|自动开启|手动|重新播放|重播)", text, re.I):
                continue
            if re.search(r"(大于\s*10秒|不满\s*10秒|重启|关台|重开台|转台|上传|获取|扫码|图标|icon|录制中|保存)", text, re.I):
                _push(text)
            if len(out) >= limit:
                break
        return out[:limit]

    def _build_pending_decision_items(
        rule_items: List[str],
        flow_items: List[str],
        scope_items: List[str],
        facts: Dict[str, Any],
        raw_text: str,
        limit: int = 4,
    ) -> List[str]:
        out: List[str] = []
        seen: set = set()

        def _push(text: str) -> None:
            s = _clean_summary_item(text, 220)
            if not s:
                return
            norm = _normalize_summary_token(s)
            if not norm or norm in seen:
                return
            seen.add(norm)
            out.append(s)

        combined = [x for x in (rule_items + flow_items + scope_items) if x]
        source_text = _normalize_summary_source_text(raw_text + "\n" + "\n".join(combined[:12]))

        conflict_hint = _build_conflict_hint(scope_items, combined)
        if conflict_hint:
            _push("需补齐开关优先级裁决表：设置总开关、播控栏开关与自动触发规则分别作用于什么场景，冲突时谁为准。")

        if re.search(r"(重新播放该歌曲|重新播放|重播该歌曲)", source_text):
            _push("“重新播放该歌曲”的定义需澄清：是自动重播、提供入口，还是录制完成后的回看动作；触发时机需明确。")

        if re.search(r"(最终是否录制根据用户的开关决定|根据用户的开关决定|用户的开关决定)", source_text):
            _push("“最终是否录制由用户开关决定”需明确对应的是哪一个开关，以及它与自动开启规则的覆盖关系。")

        auto_manual_conflict_hit = (
            bool(re.search(r"(自动开启|自动打开|自动录音|快唱副歌)", source_text))
            and bool(re.search(r"(用户.*开关决定|手动|播控栏开关|设置.*开关|总开关)", source_text))
        )
        if auto_manual_conflict_hit:
            _push("自动开启规则与用户开关控制同时存在，需明确优先级矩阵后才能作为既定口径执行。")
        if re.search(r"(上传|云端|同步|进入.*列表|归入|小程序|扫码|获取|下载)", source_text):
            _push("需明确结果链路的保存条件、上传时机，以及用户从列表或扫码进入小程序后看到的最终结果。")

        for raw in rule_items:
            text = _clean_summary_item(raw, 220)
            if not text:
                continue
            if re.search(r"(优先级|裁决|谁为准|待确认|待裁决|待澄清|最终是否)", text, re.I):
                _push(text)
            if len(out) >= limit:
                break
        return out[:limit]

    def _finalize_shared_summary_slots(
        purpose_text: str,
        scope_candidates: List[str],
        flow_candidates: List[str],
        rule_candidates: List[str],
        facts: Dict[str, Any],
        raw_text: str,
        product_label: str,
        llm_first: bool = False,
    ) -> Dict[str, Any]:
        scope_capabilities = _build_scope_capability_items(scope_candidates, limit=5)
        flow_candidates = [
            _clean_summary_item(x, 180)
            for x in flow_candidates
            if x and not _looks_like_summary_noise(x) and not re.search(r"(背景|目标|需求描述|功能说明|规则说明)", str(x))
        ]
        flow_candidates = [x for x in flow_candidates if x]
        scope_fact_items = _build_scope_fact_items(facts)
        scope_fact_for_merge = [x for x in scope_fact_items if x]
        seen_scope_norm = {_normalize_summary_token(s) for s in scope_fact_for_merge}
        llm_scope_deduped = [x for x in scope_capabilities if _normalize_summary_token(x) not in seen_scope_norm]
        final_scope = scope_fact_for_merge + llm_scope_deduped
        if len(final_scope) < 8:
            extra = [x for x in _merge_scope_with_facts(scope_capabilities, facts, limit=8) if x not in final_scope]
            final_scope += extra[: 8 - len(final_scope)]
        final_scope = final_scope[:8]
        flow_support_rules = [
            _clean_summary_item(x, 180)
            for x in rule_candidates
            if x and not re.search(r"(重启|关台|重开台|转台|优先级|裁决|谁为准|型号|版本|终端|触摸屏|电视端|手机端|小程序)", str(x))
        ]
        flow_support_rules = [x for x in flow_support_rules if x]
        if llm_first:
            final_flow = _normalize_direct_summary_list(flow_candidates, limit=5)
            if not final_flow:
                final_flow = _build_fixed_core_flow(flow_candidates, scope_capabilities, flow_support_rules, facts, raw_text, limit=3)
            llm_points = _normalize_direct_summary_list(rule_candidates, limit=6)
            pending_points: List[str] = []
            final_fact_points: List[str] = []
            pending_markers = re.compile(r"(优先级|裁决|谁为准|待确认|待裁决|待澄清|需明确|需澄清|触发时机|覆盖关系)", re.I)
            for text in llm_points:
                if pending_markers.search(text):
                    if text not in pending_points:
                        pending_points.append(text)
                else:
                    if text not in final_fact_points:
                        final_fact_points.append(text)
            final_fact_points = final_fact_points[:5]
            pending_points = pending_points[:4]
            if not final_fact_points:
                final_fact_points = _build_fixed_fact_points(rule_candidates, flow_candidates, final_scope, facts, raw_text, limit=5)
            final_purpose = _clean_summary_item(purpose_text, 220)
            if final_purpose and not final_purpose.endswith("。"):
                final_purpose += "。"
            if not final_purpose:
                final_purpose = _build_fixed_summary_purpose(
                    purpose_text,
                    product_label,
                    scope_capabilities,
                    final_flow,
                    final_fact_points + pending_points,
                    raw_text,
                )
        else:
            final_flow = _build_fixed_core_flow(flow_candidates, scope_capabilities, flow_support_rules, facts, raw_text, limit=3)
            final_fact_points = _build_fixed_fact_points(rule_candidates, flow_candidates, final_scope, facts, raw_text, limit=5)
            pending_points = _build_pending_decision_items(rule_candidates, flow_candidates, final_scope, facts, raw_text, limit=4)
            final_purpose = _build_fixed_summary_purpose(
                purpose_text,
                product_label,
                scope_capabilities,
                final_flow,
                final_fact_points + pending_points,
                raw_text,
            )
        return {
            "purpose": final_purpose,
            "scope": final_scope[:8],
            "scope_fact_items": scope_fact_items[:3],
            "scope_capability_items": scope_capabilities[:5],
            "core_flow": final_flow[:5],
            "key_points": final_fact_points[:5],
            "fact_points": final_fact_points[:5],
            "pending_points": pending_points[:4],
        }

    def _normalize_direct_summary_list(items: Any, limit: int = 6) -> List[str]:
        out: List[str] = []
        seen: set = set()
        for raw in _ensure_list(items):
            text = _clean_summary_item(raw, 220)
            if not text:
                continue
            if text not in seen:
                seen.add(text)
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def _validate_direct_summary(candidate: Dict[str, Any], facts: Dict[str, Any]) -> Tuple[bool, List[str]]:
        if not isinstance(candidate, dict):
            return False, ["LLM 未返回有效 JSON 结构"]
        purpose = _clean_summary_item(candidate.get("purpose") or candidate.get("summary_paragraph") or "", 220)
        scope_items = _normalize_direct_summary_list(candidate.get("scope"), limit=8)
        flow_items = _normalize_direct_summary_list(candidate.get("core_flow"), limit=5)
        point_items = _normalize_direct_summary_list(candidate.get("key_points"), limit=6)
        combined = "\n".join([purpose] + scope_items + flow_items + point_items)
        combined_norm = _normalize_summary_source_text(combined).lower()
        failures: List[str] = []
        for group_key, label in [("models", "型号"), ("versions", "版本"), ("terminals", "终端")]:
            expected = [_normalize_summary_token(x) for x in facts.get(group_key, []) if x]
            if expected:
                missing = [x for x in expected if x and x not in combined_norm]
                if missing:
                    failures.append(f"{label}缺失：{', '.join(missing)}")
        if len(facts.get("models") or []) >= 2:
            narrow_hit = re.search(r"(仅支持|只支持|仅适配|只适配)[^。；\n]{0,30}((?:x\d+|派\d+)(?:、(?:x\d+|派\d+))*)", combined, re.I)
            if narrow_hit:
                mentioned = {_normalize_summary_token(x) for x in re.findall(r"[xX]\d+|派\d+", narrow_hit.group(2))}
                expected = {_normalize_summary_token(x) for x in facts.get("models", [])}
                if mentioned and mentioned != expected:
                    failures.append("范围收缩：摘要出现更窄的型号范围")
        clear_triggers = _dedupe_clear_triggers(facts.get("clear_triggers", []))
        keep_exception = str(facts.get("keep_exception") or "")
        if clear_triggers and keep_exception:
            has_clear_pair = any(x in combined for x in clear_triggers) and "清空" in combined
            has_keep_pair = keep_exception in combined and bool(re.search(r"(不清空|严禁清空|不得清空|保留)", combined))
            if not (has_clear_pair and has_keep_pair):
                failures.append("红线缺失：未同时覆盖清空条件与不清空例外")
        if facts.get("setting_switch") and facts.get("control_switch") and facts.get("auto_rule"):
            entity_ok = (
                bool(re.search(r"(设置.{0,8}开关|总开关|设置页.{0,8}开关)", combined))
                and bool(re.search(r"播控栏.{0,8}开关", combined))
                and bool(re.search(r"(快唱副歌|自动开启|自动触发|手机端点播)", combined))
            )
            decision_ok = bool(re.search(r"(优先级|裁决表|裁决矩阵|优先级裁决|谁为准)", combined))
            if not (entity_ok and decision_ok):
                failures.append("冲突缺失：未点名双开关与自动规则，或未要求优先级裁决")
        return len(failures) == 0, failures

    def _build_direct_llm_summary(
        product_label: str,
        raw_text: str,
        facts: Dict[str, Any],
        fallback_summary: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        if os.path.basename(str(llm_config_path or "")) == "__llm_disabled__.json":
            return None, ["LLM 已显式禁用"]
        prompt = (
            "你是产品负责人，正在输出一份可以直接同步到群里的《全员共识摘要》。\n"
            "请严格输出一个 JSON 对象，不要输出任何解释或 markdown 代码块。\n"
            "JSON 结构如下：\n"
            "{\n"
            '  "purpose": "一句话概括目标",\n'
            '  "scope": ["范围1", "范围2"],\n'
            '  "core_flow": ["步骤1", "步骤2", "步骤3"],\n'
            '  "key_points": ["红线1", "红线2", "待裁决1"]\n'
            "}\n\n"
            "写作要求：\n"
            "1. 直接写人话，不要解释你是怎么总结的。\n"
            "2. 不要漏掉原文里的硬事实，尤其是型号、版本、端、清空/不清空规则。\n"
            "3. 如果原文同时出现设置开关、播控栏开关和自动触发规则，必须明确写出“优先级待裁决/裁决表”。\n"
            "4. 不要写负责人、时间、表头、目录、研发流程。\n"
            "5. scope 写静态范围，core_flow 写动态行为，key_points 写红线与待裁决事项。\n\n"
            f"产品名参考：{product_label}\n"
            f"必须保留的型号：{', '.join(facts.get('models') or []) or '无'}\n"
            f"必须保留的版本：{', '.join(facts.get('versions') or []) or '无'}\n"
            f"必须保留的终端：{', '.join(facts.get('terminals') or []) or '无'}\n"
            f"清空条件：{', '.join(_dedupe_clear_triggers(facts.get('clear_triggers') or [])) or '无'}\n"
            f"不清空例外：{facts.get('keep_exception') or '无'}\n"
            f"存在设置开关：{'是' if facts.get('setting_switch') else '否'}\n"
            f"存在播控栏开关：{'是' if facts.get('control_switch') else '否'}\n"
            f"存在自动规则：{'是' if facts.get('auto_rule') else '否'}\n\n"
            "原文如下：\n"
            f"{raw_text[:2200]}\n\n"
            "如果原文信息不足，就如实保留“不明确/待裁决”，不要编造。"
        )
        try:
            raw_output = call_llm(
                [{"role": "user", "content": prompt}],
                config_path=llm_config_path,
                config_override=llm_config_override,
                stream=False,
                timeout=35,
                max_tokens=600,
            )
        except Exception as e:
            return None, [f"LLM 调用失败：{e}"]
        obj = _extract_first_json_obj(raw_output)
        if not isinstance(obj, dict) or not obj:
            return None, ["LLM 未返回有效 JSON 结构"]
        purpose = _clean_summary_item(obj.get("purpose") or obj.get("summary_paragraph") or "", 220)
        scope_items = _normalize_direct_summary_list(obj.get("scope"), limit=8)
        flow_items = _normalize_direct_summary_list(obj.get("core_flow"), limit=5)
        point_items = _normalize_direct_summary_list(obj.get("key_points"), limit=6)
        if not purpose:
            return None, ["LLM 摘要缺少 purpose"]
        final_slots = _finalize_shared_summary_slots(
            purpose,
            scope_items,
            flow_items,
            point_items,
            facts,
            raw_text,
            product_label,
            llm_first=True,
        )
        ok, failures = _validate_direct_summary(
            {
                "purpose": final_slots.get("purpose", purpose),
                "scope": final_slots.get("scope", []),
                "core_flow": final_slots.get("core_flow", []),
                "key_points": final_slots.get("key_points", []),
            },
            facts,
        )
        if not ok:
            return None, failures
        return {
            "title": f"【{product_label}】全员共识摘要",
            "summary_paragraph": final_slots.get("purpose", purpose),
            "purpose": final_slots.get("purpose", purpose),
            "scope": final_slots.get("scope", []),
            "scope_fact_items": final_slots.get("scope_fact_items", []),
            "scope_capability_items": final_slots.get("scope_capability_items", []),
            "core_flow": final_slots.get("core_flow", []),
            "key_points": final_slots.get("key_points", []),
            "fact_points": final_slots.get("fact_points", final_slots.get("key_points", [])),
            "pending_points": final_slots.get("pending_points", []),
            "dependencies": fallback_summary.get("dependencies", [])[:4],
            "generation_mode": "llm_validated",
            "validation_failures": [],
        }, []

    def _refine_purpose_with_llm(base_text: str, candidates: List[str], source_text: str) -> str:
        cleaned_candidates = []
        for c in candidates:
            s = _normalize_purpose_sentence(c, scope_raw, product_name)
            if s and s not in cleaned_candidates:
                cleaned_candidates.append(s)
        if not cleaned_candidates:
            return base_text
        prompt = (
            "你是需求摘要助手。请只基于下面候选与原文词汇，输出一句自然、简洁、可读的“文档核心目的”。\n"
            "要求：\n"
            "1. 只能基于候选改写，不得引入候选与原文中都没有的新业务名词。\n"
            "2. 禁止输出负责人、时间、目录标题、表格头、背景/目标/需求描述等结构化脏文本。\n"
            "3. 禁止把型号、版本、系统、终端、适配范围写成主目标。\n"
            "4. 保留当前文档里的业务词，不替换成别的词。\n"
            "5. 只输出一句中文，不要解释。\n\n"
            f"候选：\n- " + "\n- ".join(cleaned_candidates[:4]) + "\n\n"
            f"原文词汇参考：\n{source_text[:600]}"
        )
        try:
            refined = call_llm(
                [{"role": "user", "content": prompt}],
                config_path=llm_config_path,
                config_override=llm_config_override,
                stream=False,
                timeout=10,
                max_tokens=120,
            )
        except Exception:
            return base_text
        refined_text = _normalize_purpose_sentence(str(refined or ""), scope_raw, product_name)
        if not refined_text:
            return base_text
        if _looks_like_summary_noise(refined_text):
            return base_text
        if _looks_like_scope_constraint(refined_text):
            return base_text
        if not _summary_term_bigram_guard(refined_text, source_text):
            return base_text
        return refined_text

    raw_summary_text = _collect_summary_source_text()
    product_name = (
        _clean_summary_text(s1.get("product_name"), 200)
        or _guess_product_name(raw_summary_text)
        or "本PRD"
    )
    background = _clean_summary_text(s1.get("background"), 1000)
    goal = _clean_summary_text(s1.get("goal"), 1000)
    if _looks_like_summary_noise(background):
        background = ""
    if _looks_like_summary_noise(goal):
        goal = ""
    modules = [x for x in _ensure_list(s1.get("modules")) if x and x != "【PRD未说明】"][:10]
    features = [x for x in _ensure_list(s1.get("features")) if x and x != "【PRD未说明】"][:10]
    
    # 强制将数组项拼接成人类可读的一句话
    def _humanize_list(arr, limit=8):
        arr = _ensure_list(arr)
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
    fallback_candidates = _extract_fallback_summary_candidates(raw_summary_text)
    
    # scope 也要做连贯处理
    scope_raw = [x for x in (features or modules) if not irrelevant_pattern.search(str(x or ""))]
    if not scope_raw:
        scope_raw = [x for x in fallback_candidates.get("scope", []) if x]
    if not flows:
        flows = _humanize_list(fallback_candidates.get("flow") or [], 8)
    if not rules:
        rules = _humanize_list(fallback_candidates.get("rules") or [], 8)
    scope = _build_scope_items(scope_raw)
    summary_parts: List[str] = []
    goal_for_purpose = "" if _looks_like_scope_constraint(goal) else goal
    background_for_purpose = "" if _looks_like_scope_constraint(background) else background
    if goal_for_purpose:
        summary_parts.append(_normalize_purpose_sentence(goal_for_purpose, scope_raw, product_name))
    elif background_for_purpose:
        summary_parts.append(_normalize_purpose_sentence(background_for_purpose, scope_raw, product_name))
    elif scope_raw:
        summary_parts.append(_build_goal_from_scope(scope_raw, product_name))
    elif goal:
        summary_parts.append(_normalize_purpose_sentence(goal, scope_raw, product_name))
    else:
        summary_parts.append(f"规范 {product_name} 的核心功能与交互逻辑")
    
    # 强制将拼接后的段落合并成单行，消除所有内部换行符
    paragraph = "；".join(summary_parts).strip("；")
    paragraph = " ".join(paragraph.replace("\r", " ").replace("\n", " ").split())
    
    if paragraph and not paragraph.endswith("。"):
        paragraph += "。"
    if not paragraph:
        paragraph = "规范本需求在各类场景下的展示行为与核心逻辑。"
        
    paragraph = _normalize_purpose_sentence(paragraph, scope_raw, product_name)
    if not paragraph or len(paragraph.strip()) < 5 or paragraph.strip() == "版":
        paragraph = "规范本需求在各类场景下的展示行为与核心逻辑。"
    flows = _normalize_summary_items(flows, limit=5, keep_constraints=False)
    rules = _normalize_summary_items(rules, limit=5, keep_constraints=True)
    facts = _extract_summary_facts(raw_summary_text)
    scope = _merge_scope_with_facts(_build_scope_items(scope_raw), facts, limit=8)
    purpose_candidates = [x for x in [goal_for_purpose, background_for_purpose, _build_goal_from_scope(scope_raw, product_name)] if x]
    source_blob = "\n".join(purpose_candidates[:3] + scope[:3] + flows[:2] + rules[:2])
    paragraph = _refine_purpose_with_llm(paragraph, purpose_candidates, source_blob)
    if _looks_like_generic_purpose(paragraph):
        paragraph = _build_specific_purpose_candidate(scope, flows, product_name)
    final_slots = _finalize_shared_summary_slots(
        paragraph,
        scope_raw + fallback_candidates.get("scope", []),
        flows,
        rules,
        facts,
        raw_summary_text,
        product_name,
    )

    fallback_summary = {
        "title": f"【{product_name}】全员共识摘要",
        "summary_paragraph": final_slots.get("purpose", paragraph),
        "purpose": final_slots.get("purpose", paragraph),
        "scope": final_slots.get("scope", scope[:8]),
        "scope_fact_items": final_slots.get("scope_fact_items", []),
        "scope_capability_items": final_slots.get("scope_capability_items", []),
        "core_flow": final_slots.get("core_flow", flows[:5]),
        "key_points": final_slots.get("key_points", rules[:5]),
        "fact_points": final_slots.get("fact_points", final_slots.get("key_points", rules[:5])),
        "pending_points": final_slots.get("pending_points", []),
        "dependencies": dependencies[:4],
        "generation_mode": "rule_fallback",
        "validation_failures": [],
    }
    direct_summary, failures = _build_direct_llm_summary(product_name, raw_summary_text, facts, fallback_summary)
    if direct_summary:
        return direct_summary
    if failures:
        fallback_summary["validation_failures"] = failures[:6]
    return fallback_summary


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
    # 安全类（即便 topic 未细分到 qr_security）也必须给出“权限边界”影响，避免降级成接口口径模板。
    if _build_l2_issue_kind(item) == "security":
        return f"“{feature_obj}”的权限边界不清，容易引发越权访问/误操作与合规风险。"
    topic = _issue_topic(item)
    if topic == "qr_security":
        return f"“{feature_obj}”的权限边界不清，容易引发越权操作和合规风险。"
    if topic == "cloud_degrade":
        return f"“{feature}”在弱网、超时或重启场景下口径不一，开发实现和验收结果会不一致。"
    if topic == "concurrency_control":
        return f"“{feature}”在多人同时触发/重复点击时的互斥、排队与幂等口径不清，结果可能互相覆盖或重复生效，现场难复现难对账。"
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
    # A 车道只收敛“真正需要商业/口径裁决”的条目：
    # - kind=conflict 直接进入 A
    # - 或者出现明确的资损/计费/营收/对账/合规风险等商业约束信号
    # 避免仅因“互斥/冲突”等泛词，把状态恢复/异常类误归到 A 导致致命伤错位。
    if kind == "conflict":
        return "A"
    if re.search(r"资损|营收|计费|对账(?!性)|合规模|合规风险|商业(?!化)|客诉(?!点)", text):
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
    # 安全类兜底（topic 未细分时）
    if _build_l2_issue_kind(item) == "security":
        return f"补齐“{feature_obj}”谁可访问/可操作、凭证有效期、越权与失效提示及审计留痕。"
    if topic == "cloud_degrade":
        return f"补齐“{feature}”在弱网、超时、重启后的状态保留、恢复与重试规则。"
    if topic == "concurrency_control":
        return f"补齐“{feature}”并发互斥/幂等键/排队策略与冲突提示，并落可观测字段。"
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
    prd_content = str((stage3_json or {}).get("prd_content") or "")
    score = s.get("quality_score", 0)
    try:
        score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
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
    
    # L1 计数口径必须与 L3「问题矩阵/缺陷母表」锁死，避免“摘要13条 vs 矩阵20条”。
    # 这里使用与 L3 详细矩阵一致的去重 defects 作为 Ground Truth。
    deduped_defects = _dedupe_defects_for_l3_matrix(defects, limit=120)
    p0 = sum(1 for d in deduped_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in deduped_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in deduped_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P2")
    total_gt = len(deduped_defects)
    gate_title, gate_note = _l1_gate_traffic(p0, p1, score)
    forecast = _l1_risk_forecast_qualitative(p0, p1, p2, score)
    one_liner = str(core.get("one_liner") or "需结合 L3 全量问题与红线条款评估后再排期。")

    # 三个致命伤：按 A/B/C 三车道各取 1 条（优先 P0，否则 P1），避免复读机与错位。
    # pool 优先使用合并问题；不足时用 defects 补位，保证每车道都有“硬核影响”。
    pool: List[Dict[str, Any]] = []
    for it in deduped_merged:
        if isinstance(it, dict):
            pool.append(it)
    if len(pool) < 8:
        for d in deduped_defects[:24]:
            if isinstance(d, dict):
                pool.append(d)
    # 去重（按标题/描述主干），避免 A/B 取到同一类冲突模板
    seen_fp: set = set()
    dedup_pool: List[Dict[str, Any]] = []
    for it in pool:
        if not isinstance(it, dict):
            continue
        fp = (str(it.get("risk_level") or "").upper(), str(it.get("type") or "")[:28], str(it.get("module") or "")[:28], str(it.get("description") or "")[:60])
        if fp in seen_fp:
            continue
        seen_fp.add(fp)
        dedup_pool.append(it)

    def _is_low_signal_template_issue(it: Dict[str, Any]) -> bool:
        """
        过滤“规则库模板洞察”类低信号条目，避免污染 L1 致命伤/决策参考：
        - 典型：状态孤岛/状态死路/不可达/无入口/无出口 等，但缺少可追溯引用句。
        说明：不改 L3 母表，仅影响 L1 选题。
        """
        if not isinstance(it, dict):
            return True
        t = str(it.get("type") or "")
        blob = _issue_blob(it)
        if t in ("状态孤岛", "状态死路"):
            return not _l3_is_usable_quote(_issue_quote(it))
        if re.search(r"(某状态没有(入口|出口)|状态不可达|无法结束)", blob):
            return not _l3_is_usable_quote(_issue_quote(it))
        return False

    dedup_pool = [x for x in dedup_pool if isinstance(x, dict) and not _is_low_signal_template_issue(x)]

    def _pick_lane(letter: str) -> Optional[Dict[str, Any]]:
        cand = _l1_best_in_fatal_lane(dedup_pool, letter)
        if cand:
            return cand
        # 兜底：按风险等级取
        arr = [x for x in dedup_pool if isinstance(x, dict)]
        arr.sort(key=lambda d: (_risk_rank_local(str(d.get("risk_level") or "")), -len(str(d.get("description") or "")) - len(_build_core_issue_title(d))))
        return arr[0] if arr else None

    # 三车道必须互斥：避免 A/B/C 抽到同一议题导致“复读机”
    picked_titles: set = set()

    def _pick_lane_unique(letter: str) -> Optional[Dict[str, Any]]:
        cands = [
            x
            for x in dedup_pool
            if isinstance(x, dict) and _l1_classify_for_fatal_lane(x) == letter
        ]
        cands.sort(
            key=lambda d: (
                _risk_rank_local(str(d.get("risk_level") or "")),
                -len(str(d.get("description") or "")) - len(_build_core_issue_title(d)),
            )
        )
        for it in cands:
            t = _build_core_issue_title(it).strip()
            if not t or t in picked_titles:
                continue
            picked_titles.add(t)
            return it
        return None

    def _pick_any_unique() -> Optional[Dict[str, Any]]:
        arr = [x for x in dedup_pool if isinstance(x, dict)]
        arr.sort(
            key=lambda d: (
                _risk_rank_local(str(d.get("risk_level") or "")),
                -len(str(d.get("description") or "")) - len(_build_core_issue_title(d)),
            )
        )
        for it in arr:
            t = _build_core_issue_title(it).strip()
            if not t or t in picked_titles:
                continue
            picked_titles.add(t)
            return it
        return None

    slot_a = _pick_lane_unique("A") or _pick_any_unique()
    slot_b = _pick_lane_unique("B") or _pick_any_unique()
    slot_c = _pick_lane_unique("C") or _pick_any_unique()

    def _fmt_fatal(prefix: str, it: Optional[Dict[str, Any]]) -> str:
        if not it:
            return f"{prefix}（本轮未抽到可作为致命伤的条目，详见 L3 母表）"
        t = _build_core_issue_title(it)
        lv = str(it.get("risk_level") or "P2").upper()
        impact = _build_l1_issue_impact(it)
        action = _build_l1_issue_action(it)
        # 让“致命伤”更像指令：影响 + 一句话动作
        return f"{prefix} **{t}**（{lv}）：{impact[:200]}（动作：{action[:60]}）"

    # 注意：这里的 A/B/C 文案仅是“阅读车道”，不强绑具体业务域；核心仍以 L3 母表为准。
    fatal3 = [
        _fmt_fatal("【A】业务/合规/资损裁决（互斥、冲突、口径）：", slot_a),
        _fmt_fatal("【B】稳定性/体验（状态、异常、并发、联调）：", slot_b),
        _fmt_fatal("【C】安全/权限/隐私边界（可访问、可操作、可追溯）：", slot_c),
    ]
    lines: List[str] = []
    lines.append("# 第一部分：L1 管理摘要（面向决策层，本地生成）")
    lines.append("> 目的：约 30 秒内了解是否具备全量排期/开工条件；**执行与引用以 L3 为母表**。")
    lines.append("")
    lines.append("## 1. 审计结论")
    lines.append(f"- **当前建议**：{gate_title} — {gate_note}")
    lines.append(f"- **综合质量分**：{round(score, 1)}/10（与 Stage3 七维/缺陷综合一致，口径以本次扫描为准）")
    lines.append(f"- **P0 / P1 / P2 计数**：{p0} / {p1} / {p2}（合计 {total_gt}）；**一句话概览**：{one_liner}")
    lines.append(f"- **核心风险预判（定性）**：{forecast}")
    # 公共版环境提示：默认给通用表述；命中 STB/机顶盒信号才给行业化加强版。
    prd_text_norm = (prd_content or "").lower()
    stb_hit = bool(re.search(r"(机顶盒|stb|断电|关台|重启|弱网)", prd_content or "", re.I))
    if stb_hit:
        lines.append("- **环境防御性提示**：当前 PRD 仍存在“隐含假设”（默认环境稳定、默认用户按理想路径操作）；在 STB 弱网/断电/中断频发的现网下，返工风险偏高。")
    else:
        lines.append("- **环境防御性提示**：当前 PRD 仍存在“隐含假设”（默认环境稳定、默认用户按理想路径操作）；在弱网/中断/依赖不可用等波动环境下，返工风险偏高。")
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
    if deduped_defects:
        lines.append("| 决策ID | 风险议题 | 风险等级 | 指向 | 当前状态 |")
        lines.append("| :-- | :-- | :-- | :-- | :-- |")
        # 决策参考必须与 L3 Ground Truth（deduped_defects）同源，避免出现 “P0=2 但表里 3 个 P0” 的口径打架。
        cand = sorted(
            [d for d in deduped_defects if isinstance(d, dict) and not _is_low_signal_template_issue(d)],
            key=lambda it: (
                _risk_rank_local(str((it or {}).get("risk_level") or "")),
                -len(str((it or {}).get("description") or "")) - len(_build_core_issue_title(it or {})),
            ),
        )
        picked: List[Dict[str, Any]] = []
        seen_t: set = set()
        for it in cand:
            tit0 = _build_core_issue_title(it or {}).replace("|", " ").strip()
            if not tit0 or tit0 in seen_t:
                continue
            seen_t.add(tit0)
            picked.append(it)
            if len(picked) >= 3:
                break
        for i, it in enumerate(picked, start=1):
            tit = _build_core_issue_title(it or {}).replace("|", " ").strip()
            lv = str((it or {}).get("risk_level") or "P2").upper()
            lines.append(f"| L2-D{i} | {tit} | {lv} | 见 L2-D{i} 与 L3 同名问题 | 待 PM 拍板并回写 PRD 锚点 |")
    else:
        lines.append("- 本轮无合并类核心问题，可直接以 L3 缺陷全表为决策依据。")
        lines.append("")
    return "\n".join(lines).strip()


def _build_l2_issue_kind(item: Dict[str, Any]) -> str:
    full_text = _issue_blob(item)
    name = str(item.get("name") or "")
    types_blob = " ".join(_ensure_list(item.get("types")))
    narrow_text = _clean_report_text(
        " ".join(
            [
                name,
                str(item.get("description") or ""),
                str(item.get("reason") or ""),
                str(item.get("suggestion") or ""),
                str(item.get("module") or ""),
                str(item.get("type") or ""),
                types_blob,
            ]
        )
    )
    text = narrow_text or full_text
    topic = _issue_topic(
        {
            "name": name,
            "description": str(item.get("description") or ""),
            "reason": str(item.get("reason") or ""),
            "suggestion": str(item.get("suggestion") or ""),
            "module": str(item.get("module") or ""),
            "type": str(item.get("type") or ""),
            "types": _ensure_list(item.get("types")),
        }
    )
    feature = _derive_feature_name(item)
    head = f"{name} {text} {types_blob}".strip()
    conflict_specific_early = bool(
        re.search(
            r"(开关|总开关|手动|自动|默认|最终是否|谁为准|优先级|生效顺序)",
            head,
        )
        and re.search(
            r"(冲突|矛盾|互斥|打架|不一致|谁为准|优先级未定义|最终是否.{0,12}(决定|控制))",
            head,
        )
    )
    security_specific_early = bool(
        re.search(
            r"(二维码|扫码|外链|短链|深链|小程序|取回|拉取|下载|凭证|授权|鉴权|越权|过期|失效|撤销|重放|谁可访问|谁可见|谁可拉取|权限)",
            head,
        )
    )
    if conflict_specific_early:
        return "conflict"
    if topic == "qr_security" or security_specific_early:
        return "security"
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
    external_access_hit = bool(
        re.search(
            r"(二维码|扫码|外链|短链|深度链接|深链|小程序|分享(?!的)|取回|拉取|下载|查看列表|录音列表|作品列表|访问入口|受控入口|凭证|授权入口)",
            comb_entry,
        )
    )
    security_specific_hit = bool(
        re.search(
            r"(二维码|扫码|外链|短链|深链|凭证|授权|鉴权|越权|过期|失效|撤销|重放|谁可访问|谁可见|谁可拉取|权限)",
            comb_entry,
        )
    )
    if l2_exception_strong and external_access_hit and security_specific_hit:
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
        return f"扫码/取回等受控入口的鉴权与失效规则未定义（{lv}）"
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
    t = _build_l2_issue_title(item).replace("|", " ").strip()
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
            "若**受控入口（扫码/外链/列表取回/下载）**未定义**鉴权边界、凭证有效期、一次性/可重放、越权提示与审计留痕**，"
            "则容易出现“**截图/转发即可取回**”等越权路径，引发**隐私客诉/合规风险**；"
            "同时在资源不存在、过期或越权时若缺少统一错误码与提示，运营与现场难以对账与定责。"
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
    if issue_kind == "security" and any(k in t for k in ("扫码", "二维码", "外链", "取回", "下载", "列表", "凭证", "token", "有效期", "过期", "越权", "鉴权", "权限")):
        return (
            "若“扫码/二维码/外链取回”缺少**身份校验与有效期**，现场会出现“**路人截图也能取走**”的越权路径；"
            "一旦涉及录音/照片/订单等敏感内容，极易形成**隐私客诉与合规风险**，且难以追溯责任。"
        )
    return _l3_scene(sd)


def _clean_l2_problem_desc(desc: str, issue_kind: str) -> str:
    def _clause_matches_kind(clause: str, kind: str) -> bool:
        c = _clean_report_text(clause)
        if not c:
            return False
        patterns = {
            "conflict": r"(开关|总开关|优先级|互斥|冲突|矛盾|谁为准|最终是否|手动|自动|默认|生效顺序)",
            "dispatch": r"(调度|抢占|打断|插播|中断恢复|谁先执行|恢复机制|恢复规则|回到主流程|回到原流程)",
            "exception": r"(失败|超时|弱网|断网|重试|异常|错误|上传|保存|写库|落地|兜底|不可用)",
            "state": r"(状态|终态|回退|重进|退出|切后台|再打开|落点|清屏|状态机|恢复|回到|重开|关台|重启)",
            "security": r"(权限|鉴权|越权|访问边界|授权|凭证|取回|外链|深链|链接|重放|二维码.{0,12}(有效期|失效|过期|权限|授权|访问|重放|次数)|扫码.{0,12}(权限|授权|有效期|失效|过期|重放|访问)|谁可(访问|见|拉取)|一次有效)",
            "concurrency": r"(并发|多个设备|同时|重复操作|重复提交|幂等|排队|限流|覆盖|归属|混淆|会话ID|所属会话|请求竞争)",
            "generic": r".+",
        }
        return bool(re.search(patterns.get(kind, r".+"), c))

    s = _clean_report_text(desc, keep_newlines=True)
    if not s:
        return "【PRD未说明】"
    s = re.sub(r"\s*例如：.*$", "", s, flags=re.DOTALL)
    parts = [p.strip() for p in re.split(r"[；。\n]+", s) if p and p.strip()]
    matched_parts = [p for p in parts if _clause_matches_kind(p, issue_kind)]
    if matched_parts:
        s = "；".join(matched_parts[:3])
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
        if re.search(r"(某状态没有入口|状态不可达|没有入口)", s):
            s = "PRD 未明确中途退出、异常中断或再次进入后的状态落点，导致状态是否可达、是否可恢复不清楚"
        if re.search(r"(会话ID|所属会话|字段|归属|录音列表|数据接口口径)", s) and not re.search(
            r"(退出|重进|状态|回退|切后台|落点|清屏|终态)", s
        ):
            s = "PRD 未明确中途退出、异常中断或再次进入后的状态落点，导致状态是否可达、是否可恢复不清楚"
    if issue_kind == "conflict":
        parts = re.split(r"[；。\n]+", s)
        kept = [p.strip() for p in parts if p.strip() and _clause_matches_kind(p, "conflict")]
        if kept:
            s = "；".join(kept[:2])
        combo = _clean_report_text(desc)
        if re.search(r"(自动(开启|执行|触发|生效)|系统自动|默认开启|默认执行)", combo) and re.search(
            r"(最终是否|根据用户|由用户).{0,12}(开关|设置|手动).{0,8}(决定|控制)",
            combo,
        ):
            s = "PRD 同时存在“系统自动执行”和“由用户开关决定”的规则，但未明确两者冲突时以谁为准"
    if issue_kind == "exception" and re.search(r"(上传失败|保存失败|只描述成功路径|只写成功路径|缺少失败处理)", s):
        s = "PRD 只写了成功路径，未明确失败、超时、弱网或重试时系统怎么处理"
    if issue_kind == "security":
        parts = re.split(r"[；。\n]+", s)
        kept = [p.strip() for p in parts if p.strip() and _clause_matches_kind(p, "security")]
        if kept:
            s = "；".join(kept[:3])
        raw_combo = _clean_report_text(desc)
        if re.search(
            r"(自动(开启|执行|触发|生效)|系统自动|默认开启|默认执行)",
            raw_combo,
        ) and re.search(
            r"(最终是否|根据用户|由用户).{0,12}(开关|设置|手动).{0,8}(决定|控制)",
            raw_combo,
        ):
            s = "PRD 同时存在“系统自动执行”和“由用户开关决定”的规则，但未明确两者冲突时以谁为准"
        if re.search(r"(二维码|扫码|取回|凭证|授权|权限|外链|深链)", raw_combo) and not kept:
            s = "PRD 未明确扫码/取回资源的访问边界、有效期与防重放规则"
    return s.strip("；。 ，,\n") or "【PRD未说明】"


def _l2_issue_fit_penalty(item: Dict[str, Any]) -> int:
    if not isinstance(item, dict):
        return 9
    kind = _build_l2_issue_kind(item)
    desc = _clean_report_text(str(item.get("description") or ""), keep_newlines=True)
    if not desc:
        return 6
    clauses = [p.strip() for p in re.split(r"[；。\n]+", desc) if p and p.strip()]
    if not clauses:
        return 5
    cleaned = _clean_l2_problem_desc(desc, kind)
    penalty = 0
    if cleaned == "【PRD未说明】":
        penalty += 4
    if kind == "state" and re.search(r"(会话ID|所属会话|字段|归属|录音列表|数据接口口径)", desc) and not re.search(
        r"(退出|重进|状态|回退|切后台|落点|清屏|终态)", desc
    ):
        penalty += 3
    if kind == "conflict" and not re.search(r"(开关|优先级|互斥|冲突|矛盾|手动|自动|总开关|最终是否)", desc):
        penalty += 3
    if kind == "security" and re.search(r"(开关|优先级|互斥|冲突|矛盾|手动|自动|总开关|最终是否)", desc) and not re.search(
        r"(二维码|扫码|取回|凭证|授权|鉴权|越权|过期|失效|重放|权限|访问边界)",
        desc,
    ):
        penalty += 4
    if kind == "exception" and not re.search(r"(失败|超时|弱网|断网|重试|上传|保存|异常)", desc):
        penalty += 3
    if len(clauses) >= 2 and "；" not in cleaned:
        penalty += 1
    return penalty


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


def _l2_collect_section_quotes(issue: Dict[str, Any], prd_content: str, limit: int = 2) -> List[str]:
    """
    L2 兜底锚点：当常规 quote 未命中时，优先在“相关章节附近”抽可追溯原文短句。
    仅用于降低“全部未定位”的情况，不参与认知大纲/门禁逻辑。
    """
    text = _clean_report_text(prd_content, keep_newlines=True)
    if not text:
        return []
    kind = _build_l2_issue_kind(issue or {})
    section_pattern_map = {
        "exception": r"(失败|异常|超时|弱网|重试|降级|容错)",
        "state": r"(状态|退出|重进|恢复|切后台|中断|生命周期)",
        "conflict": r"(优先级|裁决|冲突|互斥|总开关|播控栏|自动规则)",
        "dispatch": r"(调度|打断|串行|并行|恢复策略)",
        "concurrency": r"(并发|幂等|重复点击|重复请求|去重)",
        "security": r"(权限|鉴权|越权|有效期|失效|重放|访问边界)",
    }
    section_pat = section_pattern_map.get(kind, r"(规则|口径|接口|状态)")
    keywords = _l3_issue_keywords(issue or {}, [issue] if isinstance(issue, dict) else [])
    if not keywords:
        keywords = []

    windows: List[str] = []
    for m in re.finditer(section_pat, text, re.I):
        st = max(0, m.start() - 48)
        ed = min(len(text), m.start() + 240)
        win = text[st:ed]
        if win and win not in windows:
            windows.append(win)
        if len(windows) >= 24:
            break
    if not windows:
        return []

    scored: List[Tuple[int, str]] = []
    for win in windows:
        for piece in re.split(r"[\r\n]+|(?<=[。！？；])", win):
            s = _clean_report_text(piece)
            if not _l3_is_usable_quote(s):
                continue
            hits = sum(1 for kw in keywords if kw and kw in s)
            if hits < 1 and not re.search(section_pat, s, re.I):
                continue
            score = _l3_quote_score(s, keywords) + (2 if re.search(section_pat, s, re.I) else 0)
            if score < 2:
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
    def _humanize_l2_quote(text: str, limit: int = 200) -> str:
        s = _clean_report_text(text)
        if not s:
            return ""
        s = re.sub(r"^L\d+(?:-L?\d+)?[:：]\s*", "", s, flags=re.I)
        s = re.sub(r"^L\d{3,}(?:-L?\d{3,})?\s*", "", s, flags=re.I)
        s = re.sub(r"（L3/结构体已挂锚点.*$", "", s)
        s = re.sub(r"\(L3/结构体已挂锚点.*$", "", s)
        s = s.strip()
        if re.fullmatch(r"L\d{4}(?:-L\d{4})?", s, re.I):
            return ""
        if re.fullmatch(r"(business_|flows?|features?|states?)[:：].*", s, re.I):
            return ""
        return s[:limit]

    if isinstance(defects, list) and defects:
        try:
            for d in _l3_match_merged_issue_defects(item, defects)[:4]:
                if not isinstance(d, dict):
                    continue
                # Prefer extracted evidence_quotes (sentence-level) if available.
                # This aligns L2 with L3 QUOTE strictness: if Stage3 already extracted usable sentences,
                # L2 should not degrade to "未定位原文".
                eqs = d.get("evidence_quotes") if isinstance(d.get("evidence_quotes"), list) else []
                for eq in eqs[:4]:
                    q_text = _humanize_l2_quote(str(eq))
                    if q_text and _l2_quote_fits_issue(item, q_text) and _l2_quote_in_current_doc(
                        q_text, stage1_snapshot=stage1_snapshot, prd_content=prd_content
                    ):
                        return q_text
                q = _issue_quote(d)
                if not (q and str(q).strip()) or q == "【未定位到可直接引用的 PRD 原文】":
                    anch = str(d.get("anchor") or "").strip()
                    if anch:
                        q = _anchor_quote(anch) or q
                q_text = _humanize_l2_quote(str(q))
                if q_text and _l2_quote_fits_issue(item, q_text) and _l2_quote_in_current_doc(
                    q_text, stage1_snapshot=stage1_snapshot, prd_content=prd_content
                ):
                    return q_text
        except Exception:
            pass
    for anchor in _issue_anchors(item):
        quote = _humanize_l2_quote(_anchor_quote(anchor))
        if _l2_quote_fits_issue(item, quote) and _l2_quote_in_current_doc(quote, stage1_snapshot=stage1_snapshot, prd_content=prd_content):
            return quote[:80]
    stage1_quotes = _l3_collect_stage1_quotes(item, [item] if isinstance(item, dict) else [], stage1_snapshot or {}, limit=1)
    if stage1_quotes:
        for quote in stage1_quotes:
            q_text = _humanize_l2_quote(quote)
            if q_text and _l2_quote_fits_issue(item, q_text) and _l2_quote_in_current_doc(q_text, stage1_snapshot=stage1_snapshot, prd_content=prd_content):
                return q_text[:80]
    prd_quotes = _l2_collect_prd_quotes(item, prd_content, limit=1)
    if prd_quotes:
        for quote in prd_quotes:
            q_text = _humanize_l2_quote(quote)
            if q_text and _l2_quote_fits_issue(item, q_text) and _l2_quote_in_current_doc(q_text, stage1_snapshot=stage1_snapshot, prd_content=prd_content):
                return q_text[:80]
    # 章节兜底：给 L2 一个“弱锚点”引用，避免所有议题都退化为纯规则归纳。
    section_quotes = _l2_collect_section_quotes(item, prd_content, limit=1)
    if section_quotes:
        for quote in section_quotes:
            q_text = _humanize_l2_quote(quote, limit=140)
            if q_text and _l2_quote_fits_issue(item, q_text) and _l2_quote_in_current_doc(
                q_text, stage1_snapshot=stage1_snapshot, prd_content=prd_content
            ):
                return "【弱锚点】" + q_text[:100]
    return "【未定位到可直接引用的 PRD 原文，本条为规则归纳结论】"


def _l2_has_traceable_quote(
    item: Dict[str, Any],
    stage1_snapshot: Optional[Dict[str, Any]] = None,
    prd_content: str = "",
    defects: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    return _build_l2_issue_quote(
        item, stage1_snapshot=stage1_snapshot, prd_content=prd_content, defects=defects
    ) != "【未定位到可直接引用的 PRD 原文，本条为规则归纳结论】"


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


def _l2_role_focus_items(top_kinds: List[str]) -> Dict[str, List[str]]:
    kinds = _l2_expand_kinds(top_kinds, limit=3)
    user_map = {
        "conflict": "会遇到同一动作在不同入口表现不一致，用户会疑惑到底有没有真正生效。",
        "exception": "会遇到失败、超时、弱网时没有明确提示，不知道要不要重试或是否已经成功。",
        "state": "会遇到退出、重进、切后台后页面状态和真实结果对不上，现场容易争议。",
        "security": "会担心谁能看、谁能取回、资源什么时候失效，避免出现越权或误取。",
        "dispatch": "会感知到多个流程同时发生时结果前后不一致，像是系统随机选了一种处理方式。",
        "concurrency": "会遇到重复点击或多人同时操作时结果打架，前后看到的内容不一致。",
    }
    pm_map = {
        "conflict": "需要拍板互斥规则下谁优先、谁有否决权，以及冲突时的产品提示口径。",
        "exception": "需要补齐失败、超时、弱网时的页面提示、重试策略和成功/失败判定。",
        "state": "需要补齐退出、中断、重进后的状态落点、页面展示与终态定义。",
        "security": "需要拍板访问边界、资源有效期、失效方式以及越权时如何拦截。",
        "dispatch": "需要明确多个流程同时触发时的执行顺序、打断关系和恢复策略。",
        "concurrency": "需要明确重复操作、多人同时触发时以哪次结果为准，以及是否排队或幂等。",
    }
    dev_map = {
        "conflict": "实现前必须拿到唯一裁决规则，否则前后端会各自按不同假设落地。",
        "exception": "实现时需要明确错误态、超时阈值、重试策略和可观测字段，避免只做成功路径。",
        "state": "需要补状态机和终态落点，否则退出/重进后的恢复逻辑无法稳定实现。",
        "security": "需要明确鉴权、有效期、资源生命周期和越权拦截，否则接口与前端都难闭环。",
        "dispatch": "需要明确多流程调度与打断恢复机制，否则联调时很难保持一致行为。",
        "concurrency": "需要提前设计幂等、互斥、排队或覆盖规则，避免并发下结果不稳定。",
    }
    qa_map = {
        "conflict": "要重点卡不同入口是否裁决一致，不能出现一端说开、一端说关的情况。",
        "exception": "要重点卡失败、弱网、超时、重试和恢复后的真实结果是否可验收。",
        "state": "要重点卡退出、切后台、重进、异常中断后的页面展示和终态是否一致。",
        "security": "要重点卡谁能访问、何时失效、是否可重放，以及无权限时的提示是否清晰。",
        "dispatch": "要重点卡多个流程同时发生时是否始终按同一顺序执行、被打断后是否可恢复。",
        "concurrency": "要重点卡重复点击、多人同时操作、结果覆盖和端云一致性。",
    }

    def pick(role_map: Dict[str, str]) -> List[str]:
        out: List[str] = []
        for kind in kinds:
            text = role_map.get(kind)
            if text and text not in out:
                out.append(text)
        return out[:2] or ["本轮暂未提取到足够明确的角色关注点，建议结合 L3 继续细化。"]

    return {
        "user": pick(user_map),
        "pm": pick(pm_map),
        "dev": pick(dev_map),
        "qa": pick(qa_map),
    }


def _l2_pick_focus_issues(
    issues: List[Dict[str, Any]],
    stage1_snapshot: Optional[Dict[str, Any]] = None,
    prd_content: str = "",
    limit: int = 3,
    defects: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    ranked: List[Tuple[int, int, int, int, Dict[str, Any]]] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        risk_rank = _risk_rank_local(str(it.get("risk_level") or ""))
        fit_penalty = _l2_issue_fit_penalty(it)
        traceable_rank = (
            0
            if _l2_has_traceable_quote(it, stage1_snapshot=stage1_snapshot, prd_content=prd_content, defects=defects)
            else 1
        )
        detail_rank = -len(str(it.get("description") or ""))
        ranked.append((risk_rank, fit_penalty, traceable_rank, detail_rank, it))
    ranked.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    by_order = [t[4] for t in ranked]
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
    # L2 头部“问题分布”统计口径：必须与 L3 缺陷母表同源（去重 defects），避免与 L1/L3 计数打架。
    # 决策表仍用 merged_issues 做“议题收敛”，但统计只认 Ground Truth。
    deduped_defects = _dedupe_defects_for_l3_matrix(defects, limit=120)
    p0 = sum(1 for d in deduped_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in deduped_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in deduped_defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P2")
    stb_hit = bool(re.search(r"(机顶盒|stb|弱网|断电|关台|重启)", prd_content or "", re.I))
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
    # 议题抽取：先多取一些，再按“kind+标题”去重，避免出现 D2/D4 这种重复议题占位。
    top_raw = _l2_pick_focus_issues(
        deduped_merged, stage1_snapshot=stage1_snapshot, prd_content=prd_content, limit=8, defects=defects
    )
    top3: List[Dict[str, Any]] = []
    seen_focus: set = set()
    for it in top_raw:
        if not isinstance(it, dict):
            continue
        kind0 = _build_l2_issue_kind(it)
        # 去重必须按“最终展示标题”而非 core_title，否则会出现决策表 D1/D4 重复但去重未命中的情况
        tit0 = _build_l2_issue_title(it).strip()
        key = (kind0, tit0)
        if key in seen_focus:
            continue
        seen_focus.add(key)
        top3.append(it)
        if len(top3) >= 4:
            break
    top_kinds = []
    for it in top3:
        if not isinstance(it, dict):
            continue
        kind = _build_l2_issue_kind(it)
        if kind not in top_kinds:
            top_kinds.append(kind)
    def _cell(text: Any, limit: int = 120) -> str:
        s0 = _clean_report_text(str(text or "")).replace("|", " ").strip()
        s0 = re.sub(r"\s+", " ", s0)
        if not s0:
            return "—"
        return (s0[: limit - 3] + "...") if len(s0) > limit else s0

    def _display_risk(issue: Dict[str, Any], kind: str) -> str:
        """
        公共版：默认使用 Stage3 risk_level。
        行业信号命中时允许“展示级别”上调（不改 Stage3 母表），用于让决策表更贴近真实阻塞。
        """
        base_lv = str((issue or {}).get("risk_level") or "P2").upper()
        if not stb_hit:
            return base_lv
        # STB/弱网信号命中：异常退路通常是硬阻塞，安全取回通常是高风险
        if kind == "exception" and base_lv in ("P1", "P2"):
            return "P0"
        if kind == "security" and base_lv in ("P2",):
            return "P1"
        return base_lv

    def _quote_meta(issue: Dict[str, Any]) -> Tuple[str, str, str]:
        quote = _build_l2_issue_quote(
            issue, stage1_snapshot=stage1_snapshot, prd_content=prd_content, defects=defects
        )
        evidence_level = "quote"
        gap = "—"
        if not quote or quote.startswith("【未定位") or quote.startswith("【PRD未说明"):
            evidence_level = "derived"
            gap = "补原文锚点或裁决表"
        elif quote.startswith("【弱锚点】"):
            evidence_level = "quote_soft"
            gap = "—"
        elif "规则归纳结论" in quote:
            evidence_level = "derived"
            gap = "补原文锚点"
        return _cell(quote, 140), evidence_level, gap

    def _decision_when(kind: str) -> str:
        mapping = {
            "conflict": "当同一动作同时受多个入口或规则控制时",
            "dispatch": "当多个流程或任务同时触发、互相打断时",
            "exception": "当主路径失败、超时、弱网或依赖不可用时",
            "concurrency": "当同一能力被重复点击或多人同时触发时",
            "state": "当用户退出、重进、切后台或异常中断时",
            "security": "当用户通过扫码、外链、列表或文件取回资源时",
        }
        return mapping.get(kind, "当关键规则、范围或成功标准未写清时")

    def _decision_options(kind: str) -> Tuple[str, str, str, str]:
        mapping = {
            "conflict": (
                "手动开关优先，自动规则仅在未手动干预时生效",
                "自动规则优先，命中场景时强制覆盖当前开关",
                "按场景分域：总开关控全局，局部开关仅控当前会话",
                "A",
            ),
            "dispatch": (
                "当前主流程优先，被打断项排队恢复",
                "新触发流程抢占，旧流程中止",
                "按流程类型分层：同类串行，异类可并行",
                "A",
            ),
            "exception": (
                "失败即阻断并提示，允许用户显式重试",
                "本地暂存并后台重试，前台展示处理中",
                "静默降级继续主流程，结果异步补偿",
                "A",
            ),
            "concurrency": (
                "首次请求生效，后续重复请求忽略",
                "最后一次请求生效，前序请求撤销",
                "全部进入队列串行执行",
                "C",
            ),
            "state": (
                "中断后回初始态并清理临时状态",
                "中断后保留上下文，重进自动续做",
                "中断后进入恢复确认页，由用户选择继续或放弃",
                "C",
            ),
            "security": (
                "仅资源拥有者可访问，码/链短时且一次有效",
                "登录态同会话可访问，超时后失效",
                "可公开访问，但仅暴露脱敏信息与受限动作",
                "A",
            ),
        }
        return mapping.get(kind, ("严格阻断", "兼容降级", "人工确认", "A"))

    def _qa_when(kind: str) -> str:
        mapping = {
            "conflict": "冲突条件命中时",
            "dispatch": "多流程同时发生或互相打断时",
            "exception": "依赖失败、超时、弱网或重试时",
            "concurrency": "重复点击或多人并发时",
            "state": "退出、重进、切后台或中断恢复时",
            "security": "未授权、已过期、无资源或越权访问时",
        }
        return mapping.get(kind, "关键规则被触发时")

    def _qa_then(kind: str) -> str:
        mapping = {
            "conflict": "所有入口裁决一致，提示口径与最终结果一致，并能看到 effective_rule。",
            "dispatch": "执行顺序、打断结果与恢复动作唯一且可复现。",
            "exception": "在明确时限内给出错误态或处理中提示，可重试路径唯一。",
            "concurrency": "结果只按既定顺序生效一次，不出现重复执行或相互覆盖。",
            "state": "重进后的状态落点唯一，页面外显与真实状态一致。",
            "security": "未授权/过期/无资源时明确拦截并提示，不能越权成功。",
        }
        return mapping.get(kind, "成功、失败与提示口径可量化、可复现。")

    def _qa_observable(kind: str) -> str:
        mapping = {
            "conflict": "effective_rule、decision_source、user_hint",
            "dispatch": "active_flow、preempt_source、recovery_action",
            "exception": "error_code、retry_count、timeout_ms",
            "concurrency": "request_id、idempotency_key、effective_request",
            "state": "state_before、state_after、resume_policy",
            "security": "auth_result、token_status、expire_at",
        }
        return mapping.get(kind, "result_status、error_code")

    def _contract_fields(kind: str) -> str:
        mapping = {
            "conflict": "priority_matrix、effective_switch、decision_source、user_hint",
            "dispatch": "flow_type、preempt_policy、resume_action、target_state",
            "exception": "status_enum、error_code、timeout_ms、retry_policy",
            "concurrency": "request_id、idempotency_key、queue_policy、result_version",
            "state": "state_enum、resume_policy、terminal_state、rollback_action",
            "security": "owner_id、token_status、expire_at、resource_scope、error_code",
        }
        return mapping.get(kind, "status_enum、success_flag、error_code")

    def _contract_state_req(kind: str) -> str:
        mapping = {
            "conflict": "需定义唯一裁决函数，避免多端各算各的。",
            "dispatch": "需定义打断、恢复、放弃三种状态机转移。",
            "exception": "需定义失败终态、重试次数与补偿幂等。",
            "concurrency": "需定义并发互斥、覆盖或排队规则。",
            "state": "需定义退出、重进、切后台后的唯一落点。",
            "security": "需定义鉴权状态、失效状态与重放拦截。",
        }
        return mapping.get(kind, "需定义终态与状态转移口径。")

    def _contract_retry_req(kind: str) -> str:
        mapping = {
            "conflict": "冲突时给出显式提示；不要静默改写结果。",
            "dispatch": "被打断后是立即恢复、延迟恢复还是放弃，需写清。",
            "exception": "需定义超时阈值、重试次数与降级策略。",
            "concurrency": "重复请求返回策略需稳定，超时后不得重复生效。",
            "state": "恢复失败后的回退与兜底提示需明确。",
            "security": "失效、越权、资源不存在时需有统一错误码与提示。",
        }
        return mapping.get(kind, "需定义超时、失败和降级策略。")

    def _contract_lifecycle_req(kind: str) -> str:
        mapping = {
            "conflict": "需明确规则变更后何时生效、是否影响进行中任务。",
            "dispatch": "需明确被打断任务的保留、恢复与清理时机。",
            "exception": "需明确失败后本地缓存、补偿上传与清理规则。",
            "concurrency": "需明确重复结果是否覆盖、保留或去重。",
            "state": "需明确退出前中间态保存、重进后保留与清空规则。",
            "security": "需明确资源生成、访问、失效、撤销与审计保留期。",
        }
        return mapping.get(kind, "需明确保存、保留、清理与对账规则。")

    def _module_label(issue: Dict[str, Any]) -> str:
        modules = [str(x) for x in _issue_modules(issue) if str(x or "").strip()]
        if modules:
            return " / ".join(modules[:2])
        return _cell(issue.get("module") or "跨模块", 40)

    def _overall_status() -> Tuple[str, str]:
        if p0 > 0:
            return "FAIL", "存在 P0 级阻塞项，未补齐前不建议进入开发。"
        if top3:
            blocked_count = 0
            for issue in top3:
                quote, _, gap = _quote_meta(issue)
                if gap != "—" or str(issue.get("risk_level") or "").upper() == "P1":
                    blocked_count += 1
            if blocked_count > 0 or score < 7.0:
                return "BLOCKED", "存在待拍板或待补证据项，建议先完成评审裁决再开工。"
        return "PASS", "当前未发现会直接阻断开工的核心决策项，可按既定口径推进。"

    status, status_reason = _overall_status()
    lines = []
    lines.append("# 第二部分：L2 产品决策文件（面向 PM/QA/Dev，本地生成）")
    lines.append("> 目的：把问题收敛成可拍板、可拆任务、可验收的结构化产物；执行缺陷与逐条锚点仍以 L3 为准。")
    lines.append("")
    lines.append("## 一、总体结论")
    lines.append(f"- 开工判定：**{status}**")
    lines.append(f"- 质量评分：**{round(score, 1)}/10**；问题分布：**P0 {p0} / P1 {p1} / P2 {p2}**")
    lines.append(f"- 主要短板：**{focus_text}**；当前判断：{feel}")
    lines.append(f"- 判定原因：{status_reason}")
    redline_items = [_l2_kind_redline_item(k) for k in _l2_expand_kinds(top_kinds, limit=3)]
    lines.append("- 开工红线 1：" + redline_items[0])
    lines.append("- 开工红线 2：" + redline_items[1])
    lines.append("- 开工红线 3：" + redline_items[2])
    lines.append("")
    
    lines.append("## 二、决策表（Decision Table，Owner=PM）")
    lines.append("")
    lines.append("| decision_id | 冲突/口径点 | 触发条件（When） | 选项A | 选项B | 选项C | 推荐选项 | 用户影响 | 实现影响 | 需要补的证据/原文锚点 | Owner | 状态 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    if not top3:
        lines.append("| D0 | 本轮未抽到需拍板的核心问题 | — | — | — | — | — | — | — | 建议先检查 Stage3 输出或补充锚点 | PM | OPEN |")
    for idx, issue in enumerate(top3, start=1):
        kind = _build_l2_issue_kind(issue)
        # L2 表格展示口径：risk_level 必须与 Stage3 母表一致，避免与“问题分布”统计打架。
        title = _build_l2_issue_title(issue)
        option_a, option_b, option_c, recommended = _decision_options(kind)
        quote, _, gap = _quote_meta(issue)
        decision_status = (
            "BLOCKED"
            if gap != "—" or str(issue.get("risk_level") or "").upper() == "P0"
            else "OPEN"
        )
        risk_text = _cell(_build_l2_risk_analysis(issue, prefix_scope=False), 90)
        try:
            sd = _l3_pick_seed_issue(issue, defects) if defects else issue
            if not isinstance(sd, dict):
                sd = issue
            user_scene = _cell(_l2_pm_on_site(issue, sd, kind), 80)
        except Exception:
            user_scene = "用户在关键路径上会看到结果不一致或不知道下一步怎么做。"
        evidence_cell = quote if gap == "—" else f"{quote}；{gap}"
        lines.append(
            f"| D{idx} | {_cell(title, 48)} | {_cell(_decision_when(kind), 36)} | {_cell(option_a, 48)} | {_cell(option_b, 48)} | {_cell(option_c, 48)} | {recommended} | {_cell(user_scene, 48)} | {risk_text} | {_cell(evidence_cell, 64)} | PM | {decision_status} |"
        )
    lines.append("")

    lines.append("## 三、验收表（AC Table，Owner=QA）")
    lines.append("")
    lines.append("| ac_id | 场景名 | Given | When | Then | 优先级 | 最小观测字段 | 依赖/前置条件 | Owner | 证据等级 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    if not top3:
        lines.append("| AC0 | 本轮无可生成 AC 的核心问题 | — | — | — | P2 | result_status | 补充 Stage3 问题 | QA | derived |")
    for idx, issue in enumerate(top3, start=1):
        kind = _build_l2_issue_kind(issue)
        # 与决策表保持一致：AC 标题与优先级均以 Stage3 risk_level 为准
        title = _build_l2_issue_title(issue)
        quote, evidence_level, _ = _quote_meta(issue)
        priority = str(issue.get("risk_level") or "P2").upper()
        given = "已满足主流程前置条件，且相关配置、入口或依赖可触发该场景。"
        when = _qa_when(kind)
        then = _qa_then(kind)
        depends = quote if evidence_level == "quote" else "需先补原文锚点或明确 PM 裁决结论。"
        lines.append(
            f"| AC{idx} | {_cell(title, 42)} | {_cell(given, 50)} | {_cell(when, 34)} | {_cell(then, 64)} | {priority} | {_cell(_qa_observable(kind), 42)} | {_cell(depends, 52)} | QA | {evidence_level} |"
        )
    lines.append("")
    
    lines.append("## 四、实现契约表（Contract Table，Owner=Dev）")
    lines.append("")
    lines.append("| contract_id | 模块/子系统 | 必须定义的字段/枚举/错误码 | 状态机/幂等/并发裁决要求 | 重试/超时/降级策略 | 数据生命周期（保存/上传/清空/保留） | Owner | 证据缺口 |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    if not top3:
        lines.append("| C0 | 跨模块 | status_enum、error_code | 需补状态机 | 需补降级策略 | 需补生命周期 | Dev | 补 L3 或原文锚点 |")
    for idx, issue in enumerate(top3, start=1):
        kind = _build_l2_issue_kind(issue)
        quote, _, gap = _quote_meta(issue)
        lines.append(
            f"| C{idx} | {_cell(_module_label(issue), 28)} | {_cell(_contract_fields(kind), 48)} | {_cell(_contract_state_req(kind), 48)} | {_cell(_contract_retry_req(kind), 48)} | {_cell(_contract_lifecycle_req(kind), 48)} | Dev | {_cell(gap if gap != '—' else quote, 58)} |"
        )
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
    defects_data = _dedupe_defects_for_l3_matrix(defects_data, limit=120)
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
        # 口径锁死：展示等级以 Ground Truth（缺陷母表）为准，避免 merged_issues 的风险等级导致
        # “风险计数 P0=1，但矩阵出现多个 P0” 的自相矛盾。
        if related_defects:
            best = sorted(
                [d for d in related_defects if isinstance(d, dict)],
                key=lambda x: _risk_rank_local(str(x.get("risk_level") or "P2")),
            )[0]
            level = str(best.get("risk_level") or "P2").upper()
        else:
            level = str(seed.get("risk_level") or issue.get("risk_level") or "P2").upper()
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
    risk_clusters = _l3_build_risk_clusters(summary_cards, limit=6)
    if issue_cards:
        main_problem, one_liner_override, top3_override = _l3_summary_from_cards(summary_cards, defects_data)
    else:
        main_problem = str(summary.get("main_problem", "【PRD未说明】"))
        one_liner_override = str(core_summary.get("one_liner", "【PRD未说明】"))
        top3_override = _ensure_list(core_summary.get("top3"))
    p0_issue_count = sum(1 for d in defects_data if str(d.get("risk_level") or "").upper() == "P0")
    p1_issue_count = sum(1 for d in defects_data if str(d.get("risk_level") or "").upper() == "P1")
    p2_issue_count = sum(1 for d in defects_data if str(d.get("risk_level") or "").upper() == "P2")
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
    lines.append(f"- 风险计数（Ground Truth）：P0 {p0_issue_count} / P1 {p1_issue_count} / P2 {p2_issue_count}（合计 {len(defects_data)}）")
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
    lines.extend(["", "## 二、风险簇矩阵（模块归并）", ""])
    if risk_clusters:
        lines.append("| 风险簇 | 最高风险 | 归并缺陷数 | 代表问题 | 必补动作（可直接回写 PRD） |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for c in risk_clusters:
            topic = str(c.get("topic") or "generic")
            module = str(c.get("module") or "跨模块")
            cluster_name = f"{module} · {topic}"
            title = " / ".join([str(x) for x in (c.get("titles") or [])[:2]]) or "关键规则缺口"
            seed = c.get("seed") if isinstance(c.get("seed"), dict) else {}
            action = _l3_if_then_action(seed)
            lines.append(
                f"| {cluster_name} | {str(c.get('level') or 'P2').upper()} | {int(c.get('count') or 0)} | {title.replace('|', ' ')} | {action.replace('|', ' ')} |"
            )
        lines.append("")
    lines.extend(["", "## 三、核心问题矩阵（合并版）", ""])
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
            actionable = _l3_if_then_action(card["seed"] if isinstance(card.get("seed"), dict) else {})
            suggestion_text = (suggestion_text + "；" + actionable) if actionable else suggestion_text
            lines.append(
                f"| {card['level']} | **{card['title']}** | {evidence} | {card['problem']} | {card['scene']} | {card['risk_reason']} | {suggestion_text.replace('|', ' ')} |"
            )
        lines.append("")
    if defects_data:
        lines.extend(["", "## 四、详细漏洞矩阵（研发/测试，逐条去重）", ""])
        lines.append("> 与「二、核心问题矩阵（合并类）」互补；本表**一行一条缺陷**（经锚点+描述前若干字去重），便于**设计/用例/任务**对齐。必补可来自 `suggestion` 或系统归纳。")
        lines.append("")
        lines.append("| 风险等级 | 问题分类 | 涉及锚点 | 缺陷描述 | 必补动作 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for d in defects_data[:60]:
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
            if_then = _l3_if_then_action(d)
            final_action = f"{sug_}；{if_then}" if if_then else sug_
            lines.append(f"| {lv_} | {typ_} | {anch_} | {desc_} | {str(final_action or '【见 L2/L3 建议列】')[:300]} |")
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
    lines.extend(["", "## 五、待确认清单", ""])
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

    report_shift_left = ""
    test_matrix = {}
    stage4_quality = {}
    diagrams = {}
    stage5_quality = {}
    kg = {}
    outline_engine = {}
    outline_llm = {}
    shift_left = {}
    test_cases = []
    platform_impact = {}
    dependency_analysis = {}
    prd_quality = {}
    test_points = {}
    validation_outline = {}
    risk_prediction = {}
    understanding_cards = {}
    architecture_scan = {}
    release_gate = {}

    # Defaults to keep later stages safe even when skipping downstream assets
    test_matrix, stage4_quality = {}, {}
    diagrams, stage5_quality = {}, {}
    kg = {}
    outline_engine, outline_llm = {}, {}
    shift_left = {}
    test_cases = []

    if PRD_ANALYSIS_ONLY_MODE:
        # 纯 PRD 分析模式也需要“发布门禁”结论（它属于治理/决策输出，不属于测试资产）。
        # 此处用 Stage3 的质量分做一个最小可用的质量信号，避免前端面板显示评分0/P0=0的假象。
        try:
            q10 = 0.0
            if isinstance(stage3_output, dict):
                s = stage3_output.get("summary") if isinstance(stage3_output.get("summary"), dict) else {}
                try:
                    q10 = float(s.get("quality_score") or 0.0)
                except (TypeError, ValueError):
                    q10 = 0.0
            prd_quality_min = {"overall_score": max(0.0, min(100.0, q10 * 10.0))}
            release_gate = run_release_gate(
                stage2_output=stage2_output if isinstance(stage2_output, dict) else {},
                platform_impact=platform_impact if isinstance(platform_impact, dict) else {},
                prd_quality=prd_quality_min,
            )
        except Exception as e:
            logger.warning("analysis-only release gate failed: %s", e)
        yield _json.dumps(
            {"type": "status", "text": "纯PRD分析模式：已跳过测试与扩展资产生成。\n"},
            ensure_ascii=False,
        ) + "\n"
    else:
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
                return {
                    "test_cases": run_test_case_generation(
                        stage1_output if isinstance(stage1_output, dict) else {},
                        defects if isinstance(defects, list) else [],
                        llm_config_path=llm_config_path,
                        timeout=max(timeout, 150),
                        llm_config_override=llm_config_override,
                    )
                }
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

    shared_summary = _build_shared_summary(
        stage1_output if isinstance(stage1_output, dict) else {},
        llm_config_path=llm_config_path,
        llm_config_override=llm_config_override,
        prd_text=content,
    )
    reader_guide = _build_reader_guide(stage1_output if isinstance(stage1_output, dict) else {}, stage3_output if isinstance(stage3_output, dict) else {})

    s3_for_bundle = stage3_output if isinstance(stage3_output, dict) else {}
    bundle_summary = s3_for_bundle.get("summary") if isinstance(s3_for_bundle.get("summary"), dict) else {}
    bundle_defects = s3_for_bundle.get("defects") if isinstance(s3_for_bundle.get("defects"), list) else []
    bundle_scan_meta = s3_for_bundle.get("scan_meta") if isinstance(s3_for_bundle.get("scan_meta"), dict) else {}
    if not bundle_scan_meta and isinstance(stage2_output, dict):
        sm = stage2_output.get("scan_meta")
        bundle_scan_meta = sm if isinstance(sm, dict) else {}

    # -------- 门禁（GATE）最小判定：只盯硬事实 + P0 --------
    # 说明：
    # - shared_summary.generation_mode == llm_validated 表示 “LLM 直写摘要” 且通过最小事实校验
    # - validation_failures 仅用于告诉 PM/测试 “差在哪里”，不做二次改写
    try:
        p0_defects_count = sum(
            1 for d in (bundle_defects or []) if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0"
        )
    except Exception:
        p0_defects_count = 0
    summary_mode = str((shared_summary or {}).get("generation_mode") or "").strip()
    summary_failures = (shared_summary or {}).get("validation_failures") if isinstance(shared_summary, dict) else []
    summary_failures = summary_failures if isinstance(summary_failures, list) else []
    gate_failures: List[str] = []
    if p0_defects_count > 0:
        gate_failures.append(f"存在 P0 风险缺陷：{p0_defects_count} 项")
    if summary_mode != "llm_validated":
        gate_failures.append("全员共识摘要未通过最小事实校验（未使用 llm_validated 直写版本）")
    for f in summary_failures[:6]:
        fs = str(f or "").strip()
        if fs:
            gate_failures.append("摘要校验失败：" + fs)
    # required_evidence：把失败项转成“补文档清单”的粗粒度提示
    required_evidence: List[str] = []
    for f in gate_failures:
        if "型号缺失" in f or "范围收缩" in f:
            required_evidence.append("补齐《目标与范围》：明确盒子型号/版本/端，并确保枚举不被缩窄。")
        if "红线缺失" in f:
            required_evidence.append("补齐《数据生命周期/清空规则》：清空触发条件 + 不清空例外（如转台不清空）。")
        if "冲突缺失" in f or "优先级" in f:
            required_evidence.append("补齐《开关优先级裁决表》：设置总开关/播控栏开关/自动规则的抢占顺序。")
        if "P0 风险缺陷" in f:
            required_evidence.append("逐条关闭 P0：为每条 P0 给出可验收 AC + 最小观测字段 + 锚点证据。")
    # 去重
    dedup_required: List[str] = []
    seen_req = set()
    for x in required_evidence:
        xs = str(x or "").strip()
        if not xs or xs in seen_req:
            continue
        seen_req.add(xs)
        dedup_required.append(xs)
    gate_result = {
        "mode": "GATE",
        "pass": len(gate_failures) == 0,
        "failures": gate_failures[:12],
        "required_evidence": dedup_required[:12],
        "signals": {
            "p0_defects_count": p0_defects_count,
            "summary_generation_mode": summary_mode,
        },
    }

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
        "gate_result": gate_result,
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
        shared_summary = _build_shared_summary(
            stage1_output if isinstance(stage1_output, dict) else {},
            llm_config_path=llm_config_path,
            llm_config_override=llm_config_override,
            prd_text=prd_text,
        )
        reader_guide = _build_reader_guide(
            stage1_output if isinstance(stage1_output, dict) else {},
            stage3_output if isinstance(stage3_output, dict) else {},
        )
        # 供 sync API 直接复用，避免 views 层重复调用（省 token，且保证同一轮结果一致）
        if isinstance(stage3_output, dict):
            stage3_output["shared_summary"] = shared_summary if isinstance(shared_summary, dict) else {}
            stage3_output["reader_guide"] = reader_guide if isinstance(reader_guide, dict) else {}
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
