# -*- coding: utf-8 -*-
"""Agent 2.0 Synthesizer 综合裁决器（C1）。

职责：
- 合并各 Agent 的 AgentFinding 进统一证据链（扩展 build_evidence_chain）
- 四维置信度升级为多源打分（源码证据挂堆栈维，LLM 复判确认可给满分）
- 自动关单口径不放宽（等级判定逻辑与 1.0 完全一致）
- 返回与 1.0 handle_alert 兼容的 dict，views.py 入口可无缝切换

设计要点：
1. 纯函数：synthesize() 接收 PlanExecution + alert + context，返回 dict，无副作用
2. 降级安全：all_degraded 时返回 None，调用方退回 1.0 单链路
3. 兼容：返回 dict 的 key 与 SelfHealAgent.handle_alert 完全一致
"""
import logging
import os
from typing import Any, Dict, List, Optional

from .base import AgentFinding, STATUS_OK
from .executor import PlanExecution
from .planner import DiagnosisPlan, AGENT_LOG, AGENT_PROBE, AGENT_HISTORY, AGENT_SOURCE

logger = logging.getLogger(__name__)

# Agent 2.0 开关（默认关闭，安全上线后再开启）
AGENT_V2_ENABLED = os.environ.get("LOG_AGENT_V2_ENABLED", "false").lower() in (
    "1", "true", "yes", "on"
)

# 置信度常量（与 selfheal._assess_confidence 保持一致）
AUTO_CLOSE_MIN_OVERLAP = float(os.environ.get("LOG_SELF_HEAL_MIN_OVERLAP", "3"))


def _extract_log_analysis(usable: List[AgentFinding]) -> Dict[str, Any]:
    """从日志分析 Agent 的 finding 中提取根因/建议/定位等字段。"""
    for f in usable:
        if f.agent_name == AGENT_LOG and f.status == STATUS_OK:
            arts = f.artifacts or {}
            return {
                "root_cause": arts.get("root_cause", ""),
                "suggestions": arts.get("suggestions", []) or [],
                "problem_location": arts.get("problem_location", ""),
                "impact": arts.get("impact", ""),
                "investigation_path": arts.get("investigation_path", []) or [],
                "suggested_patch": arts.get("suggested_patch", ""),
                "summary": arts.get("summary", ""),
            }
    return {}


def _extract_history(usable: List[AgentFinding]) -> List[Dict[str, Any]]:
    """从历史求证 Agent 的 finding 中提取案例列表。"""
    for f in usable:
        if f.agent_name == AGENT_HISTORY and f.status == STATUS_OK:
            return (f.artifacts or {}).get("cases", []) or []
    return []


def _extract_source(usable: List[AgentFinding]) -> Dict[str, Any]:
    """从源码关联 Agent 的 finding 中提取源码片段 + LLM 复判。"""
    for f in usable:
        if f.agent_name == AGENT_SOURCE and f.status == STATUS_OK:
            return {
                "snippets": (f.artifacts or {}).get("snippets", []) or [],
                "review": (f.artifacts or {}).get("review"),
                "evidence": f.evidence or [],
            }
    return {"snippets": [], "review": None, "evidence": []}


def _extract_probe(usable: List[AgentFinding]) -> Dict[str, Any]:
    """从设备取证 Agent 的 finding 中提取产物。"""
    for f in usable:
        if f.agent_name == AGENT_PROBE and f.status == STATUS_OK:
            return (f.artifacts or {}).get("action_result", {}) or {}
    return {}


def _merge_evidence_chain(
    base_chain: Dict[str, List],
    source_evidence: List[Dict[str, Any]],
    agent_findings: List[AgentFinding],
) -> Dict[str, List]:
    """在 build_evidence_chain 基础上追加多 Agent 证据。"""
    chain = {
        "direct": list(base_chain.get("direct", [])),
        "inferred": list(base_chain.get("inferred", [])),
        "references": list(base_chain.get("references", [])),
    }
    # 源码证据挂入 direct（源码是客观事实，不是模型推断）
    for ev in source_evidence:
        chain["direct"].append({
            "kind": ev.get("kind", "source_code"),
            "label": ev.get("desc", "源码片段"),
            "detail": ev.get("detail", ""),
            "source": "源码只读关联 Agent",
        })
    # 各 Agent 的降级信息挂入 inferred（留痕可审计）
    for f in agent_findings:
        if f.status not in (STATUS_OK, "skipped"):
            chain["inferred"].append({
                "kind": "agent_degraded",
                "label": "Agent 降级(%s)" % f.agent_name,
                "detail": "状态=%s, 原因=%s" % (f.status, f.degrade_reason or f.error or ""),
                "source": "Agent 2.0 执行器",
            })
    return chain


def _assess_confidence_v2(
    root_cause: str,
    historical: List[Dict[str, Any]],
    rule_name: str,
    problem_location: str,
    log_lines: List[str],
    context_meta: Optional[Dict[str, Any]],
    probe_evidence: List[str],
    source_info: Dict[str, Any],
) -> tuple:
    """置信度分项打分（多源升级版）。

    与 1.0 _assess_confidence 的差异：
    - 堆栈维：源码片段佐证时，从 50→75（模型定位 + 源码确认）；
      LLM 复判 confirmed=True 时，→100（源码 + LLM 双重佐证）
    - 其他三维不变，自动关单口径不放宽
    """
    import re
    _STACK_AT_RE = re.compile(r'\bat\s+[\w.$]+\([\w$.]+\.(java|kt):\d+\)')

    STACK_W, HISTORY_W, CONTEXT_W, PROBE_W = 30, 40, 15, 15
    log_lines = log_lines or []
    probe_evidence = probe_evidence or []

    # 维度① 堆栈（多源升级）
    stack_hit = bool(problem_location) and any(
        _STACK_AT_RE.search(l or "") for l in log_lines
    )
    source_snippets = source_info.get("snippets", [])
    source_review = source_info.get("review")

    if stack_hit:
        stack_score, stack_note = 100, "命中堆栈帧（已定位到具体 文件:行号）"
    elif problem_location and source_snippets:
        # 模型给了定位 + 源码确认 → 从 50 提升
        if source_review and source_review.get("confirmed"):
            stack_score, stack_note = 100, "模型定位 + 源码片段佐证 + LLM 复判确认"
        else:
            stack_score, stack_note = 75, "模型定位 + 源码片段佐证（无 LLM 复判或复判未确认）"
    elif problem_location:
        stack_score, stack_note = 50, "模型给出问题定位但日志无堆栈佐证（未核实）"
    else:
        stack_score, stack_note = 0, "未命中堆栈（无法定位到具体代码行）"

    # 维度② 历史案例（与 1.0 完全一致）
    history_hit = False
    if not historical:
        history_score, history_note = 0, "无历史相似案例"
    else:
        top = historical[0]
        score = top.get("_score", 0)
        same_rule = bool(top.get("rule_name")) and top.get("rule_name") == rule_name
        if not top.get("resolved", False):
            history_score, history_note = 0, "历史案例未标记已解决"
        elif score >= 1 + AUTO_CLOSE_MIN_OVERLAP or same_rule:
            history_hit = True
            history_score, history_note = 100, (
                f"命中已知已解决案例 {top.get('id', '')}"
                f"（重合度 {score}，同规则={same_rule}）"
            )
        else:
            history_score, history_note = 60, (
                f"历史案例部分重合（重合度 {score} < {1 + AUTO_CLOSE_MIN_OVERLAP}，"
                f"同规则={same_rule}），未达自动关单阈值"
            )

    # 维度③ 上下文充足度（与 1.0 一致）
    ctx_lines = len(log_lines)
    if context_meta and isinstance(context_meta.get("lines"), int):
        ctx_lines = max(ctx_lines, context_meta.get("lines", 0))
    if ctx_lines >= 10:
        ctx_score, ctx_note = 100, f"上下文充足（{ctx_lines}行）"
    elif ctx_lines >= 5:
        ctx_score, ctx_note = 60, f"上下文偏少（{ctx_lines}行）"
    else:
        ctx_score, ctx_note = 0, f"上下文不足（{ctx_lines}行）"

    # 维度④ 只读探针（与 1.0 一致）
    if not probe_evidence:
        probe_score, probe_note = 0, "未启用只读探针采集（非 collect/assist 模式）"
    else:
        useful = [
            e for e in probe_evidence
            if e and not e.strip().endswith("(无输出)") and "采集失败" not in e
        ]
        if useful:
            probe_score, probe_note = 100, f"只读探针采集到有效输出（{len(useful)}/{len(probe_evidence)} 条）"
        else:
            probe_score, probe_note = 40, "只读探针已执行但未采集到有效输出"

    # 加权综合分
    total = round(
        stack_score * STACK_W / 100
        + history_score * HISTORY_W / 100
        + ctx_score * CONTEXT_W / 100
        + probe_score * PROBE_W / 100
    )

    breakdown = {
        "score": total,
        "stack": stack_score,
        "history": history_score,
        "context": ctx_score,
        "probe": probe_score,
        "weights": {"stack": STACK_W, "history": HISTORY_W, "context": CONTEXT_W, "probe": PROBE_W},
        "notes": {
            "stack": stack_note, "history": history_note,
            "context": ctx_note, "probe": probe_note,
        },
    }

    # 等级判定（与 1.0 完全一致，自动关单口径不放宽）
    reason = (
        f"命中堆栈={'是' if stack_hit else '否'}；"
        f"历史案例：{history_note}；"
        f"上下文={'充足' if ctx_lines >= 10 else '不足'}（{ctx_lines}行）"
    )
    if not (root_cause and not root_cause.startswith("分析失败")):
        for _d in ("stack", "history", "context", "probe"):
            breakdown[_d] = 0
            breakdown["notes"][_d] = "根因无效，分项评分作废"
        breakdown["score"] = 0
        return "low", "本次根因无效；" + reason, breakdown
    if history_hit:
        return "high", reason, breakdown
    if stack_hit and ctx_lines >= 10:
        return "medium", reason, breakdown
    # 多源升级：源码+LLM 双重佐证也可达 medium
    if source_snippets and source_review and source_review.get("confirmed") and ctx_lines >= 10:
        return "medium", reason + "（源码+LLM复判佐证）", breakdown
    return "low", reason, breakdown


def synthesize(
    execution: PlanExecution,
    alert: Dict[str, Any],
    context: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """综合裁决：合并多 Agent 结果 → 兼容 1.0 格式的诊断 dict。

    返回 None 表示降级（all_degraded 或无日志分析结果），调用方应退回 1.0。
    """
    usable = execution.usable_findings
    if not usable or execution.all_degraded:
        logger.info("[synthesizer] 全部 Agent 降级，返回 None 供调用方退回 1.0")
        return None

    # 1) 从各 Agent 提取结构化字段
    log_data = _extract_log_analysis(usable)
    if not log_data:
        # 日志分析 Agent 没出结果 → 无法裁决
        logger.warning("[synthesizer] 日志分析 Agent 无可用结果，返回 None")
        return None

    root_cause = log_data.get("root_cause", "")
    suggestions = log_data.get("suggestions", [])
    problem_location = log_data.get("problem_location", "")
    impact = log_data.get("impact", "")
    investigation_path = log_data.get("investigation_path", [])
    suggested_patch = log_data.get("suggested_patch", "")

    historical = _extract_history(usable)
    source_info = _extract_source(usable)
    probe_result = _extract_probe(usable)

    # 2) 构建证据链（复用 build_evidence_chain + 追加多 Agent 证据）
    from ..selfheal import build_evidence_chain

    log_lines: List[str] = list(context.get("log_lines") or [])
    trigger_line = str(alert.get("message") or alert.get("log_line") or "")
    context_meta = context.get("context_meta")
    device_context = context.get("device_context")
    action_result = probe_result or context.get("action_result")

    base_chain = build_evidence_chain(
        root_cause=root_cause,
        problem_location=problem_location,
        impact=impact,
        trigger_line=trigger_line,
        log_lines=log_lines,
        context_meta=context_meta,
        device_context=device_context,
        probe_evidence=[],  # 探针证据由 device_probe Agent 的 evidence 体现
        historical=historical,
        action_result=action_result,
    )

    # 追加多 Agent 证据
    all_findings = execution.findings
    evidence_chain = _merge_evidence_chain(base_chain, source_info["evidence"], all_findings)

    # 3) 置信度评估（多源升级版）
    rule_name = str(alert.get("rule_name") or "")
    severity = str(alert.get("severity") or "")

    # 探针证据：从 device_probe finding 的 evidence 转换
    probe_evidence_strs: List[str] = []
    for f in usable:
        if f.agent_name == AGENT_PROBE and f.status == STATUS_OK:
            for ev in (f.evidence or []):
                probe_evidence_strs.append(ev.get("detail", ""))

    confidence, confidence_reason, conf_breakdown = _assess_confidence_v2(
        root_cause, historical, rule_name,
        problem_location=problem_location,
        log_lines=log_lines,
        context_meta=context_meta,
        probe_evidence=probe_evidence_strs,
        source_info=source_info,
    )

    # 4) 状态判定（与 1.0 完全一致）
    auto_close_enabled = os.environ.get(
        "LOG_SELF_HEAL_AUTO_CLOSE", "false"
    ).lower() in ("1", "true", "yes", "on")

    if severity == "high" or not root_cause or root_cause.startswith("分析失败"):
        status = "NEEDS_HUMAN"
        auto_closeable = False
    elif confidence == "high":
        status = "AUTO_RESOLVED"
        auto_closeable = auto_close_enabled
    else:
        status = "ANALYZED"
        auto_closeable = False

    # 5) 如果 LLM 复判给出了修复建议，追加到 suggested_patch（仅展示不落地）
    source_review = source_info.get("review")
    if source_review and source_review.get("suggested_fix"):
        if suggested_patch:
            suggested_patch += "\n\n--- LLM 源码复判建议 ---\n" + source_review["suggested_fix"]
        else:
            suggested_patch = source_review["suggested_fix"]

    # 6) 组装兼容 dict
    return {
        "device_id": str(context.get("device_id") or ""),
        "alert_type": str(alert.get("type") or ""),
        "severity": severity,
        "root_cause": root_cause,
        "suggestions": suggestions,
        "problem_location": problem_location,
        "impact": impact,
        "investigation_path": investigation_path,
        "suggested_patch": suggested_patch,
        "evidence": probe_evidence_strs,
        "evidence_chain": evidence_chain,
        "context_meta": context_meta,
        "historical_cases": historical,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "confidence_score": conf_breakdown.get("score", 0),
        "confidence_breakdown": conf_breakdown,
        "auto_closeable": auto_closeable,
        "status": status,
        "needs_human": status in ("NEEDS_HUMAN", "ANALYZED"),
        "mode": str(context.get("mode") or "observe"),
        "alert_id": alert.get("id") if isinstance(alert, dict) else None,
        "agent_name": "Agent 2.0",
        "stage": "多 Agent 协作诊断",
        "roadmap": [],  # Agent 2.0 不再用单链路 roadmap
        "device_context": device_context or {},
        # Agent 2.0 专属字段
        "agent_v2": True,
        "plan": execution.plan.to_dict() if execution.plan else {},
        "execution": execution.to_dict(),
    }
