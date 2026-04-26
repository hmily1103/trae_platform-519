# -*- coding: utf-8 -*-
import re
from typing import Any, Dict, List, Tuple


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _extract_nodes_from_text(text: str) -> List[str]:
    t = _norm(text)
    if not t:
        return []
    parts = re.split(r"[>\-—→,，、\s]+", t)
    out = []
    for p in parts:
        pp = _norm(p)
        if not pp:
            continue
        if len(pp) <= 1:
            continue
        if any(k in pp for k in ["优先级", "冲突", "规则", "存在", "循环", "依赖"]):
            continue
        if pp not in out:
            out.append(pp)
    return out[:8]


def _build_graph(transitions: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    graph: Dict[str, List[str]] = {}
    for t in transitions:
        if not isinstance(t, dict):
            continue
        a = _norm(t.get("from"))
        b = _norm(t.get("to"))
        if not a or not b:
            continue
        graph.setdefault(a, []).append(b)
    return graph


def _find_path(transitions: List[Dict[str, Any]], start: str, target: str) -> List[Dict[str, str]]:
    graph = _build_graph(transitions)
    q: List[Tuple[str, List[str]]] = [(start, [start])]
    visited = {start}
    while q:
        node, path = q.pop(0)
        if node == target:
            seq = []
            for i in range(len(path) - 1):
                frm = path[i]
                to = path[i + 1]
                edge = next((x for x in transitions if _norm(x.get("from")) == frm and _norm(x.get("to")) == to), None)
                seq.append({
                    "from": frm,
                    "to": to,
                    "event": _norm((edge or {}).get("trigger")) or _norm((edge or {}).get("event")) or "触发",
                    "action": _norm((edge or {}).get("action")) or "状态切换",
                })
            return seq
        for nxt in graph.get(node, []):
            if nxt not in visited:
                visited.add(nxt)
                q.append((nxt, path + [nxt]))
    return []


def _path_to_mermaid(path: List[Dict[str, str]]) -> str:
    if not path:
        return ""
    lines = ["stateDiagram-v2"]
    for p in path[:12]:
        frm = _norm(p.get("from")) or "未知状态"
        to = _norm(p.get("to")) or "未知状态"
        event = _norm(p.get("event")) or "触发"
        lines.append(f"  {frm} --> {to} : {event}")
    return "\n".join(lines)


def _explain_for_conflict(conflict: Dict[str, Any], state_machine: Dict[str, Any], idx: int) -> Dict[str, Any]:
    c = conflict if isinstance(conflict, dict) else {}
    ctype = _norm(c.get("type"))
    evidence = _norm(c.get("evidence"))
    message = _norm(c.get("message")) or "存在规则冲突"
    transitions = state_machine.get("transitions") if isinstance(state_machine, dict) and isinstance(state_machine.get("transitions"), list) else []
    nodes = _extract_nodes_from_text(evidence) or _extract_nodes_from_text(message)
    if not nodes:
        nodes = [_norm(x) for x in (state_machine.get("states") or [])[:3] if _norm(x)]
    path = []
    if len(nodes) >= 2:
        for i in range(len(nodes) - 1):
            path.extend(_find_path(transitions, nodes[i], nodes[i + 1]))
    if not path and transitions:
        for t in transitions[:3]:
            path.append({
                "from": _norm(t.get("from")),
                "to": _norm(t.get("to")),
                "event": _norm(t.get("trigger")) or _norm(t.get("event")) or "触发",
                "action": _norm(t.get("action")) or "状态切换",
            })
    title_map = {
        "priority_cycle": "优先级循环冲突",
        "priority_conflict": "优先级互斥冲突",
        "mode_constraint_conflict": "模式约束冲突",
        "low_confidence_rules": "规则可解释性不足",
    }
    root_map = {
        "priority_cycle": "优先级关系形成闭环，恢复阶段无法确定唯一目标状态。",
        "priority_conflict": "同一组功能存在互相压制定义，裁决逻辑不具备单调性。",
        "mode_constraint_conflict": "模式规则与限制条件同时要求互斥方向，导致执行条件冲突。",
        "low_confidence_rules": "规则文本条件与动作不完整，导致状态转移推理不稳定。",
    }
    impact_map = {
        "priority_cycle": ["UI频繁切换", "状态不一致", "可能进入循环抖动"],
        "priority_conflict": ["展示结果不可预测", "恢复目标错乱", "调度确定性下降"],
        "mode_constraint_conflict": ["横竖屏切换异常", "条件误命中", "回退策略频繁触发"],
        "low_confidence_rules": ["实现口径不一致", "测试边界遗漏", "线上回归成本上升"],
    }
    suggestion_map = {
        "priority_cycle": ["重排为唯一优先级链并禁止环路", "定义冲突兜底状态", "补充恢复优先级策略"],
        "priority_conflict": ["同级功能补充裁决策略", "删除互斥重复规则", "引入统一调度入口"],
        "mode_constraint_conflict": ["拆分模式适用前置条件", "补充例外分支", "增加模式冲突保护"],
        "low_confidence_rules": ["补全触发条件", "补全动作结果", "统一规则句式"],
    }
    related_rules = {
        "priority_cycle": ["R04"],
        "priority_conflict": ["R06", "R08"],
        "mode_constraint_conflict": ["R24"],
        "low_confidence_rules": ["R28"],
    }.get(ctype, [])
    return {
        "conflict_id": f"C{idx:02d}",
        "type": title_map.get(ctype, "规则冲突"),
        "severity": _norm(c.get("severity")) or "P1",
        "related_rules": related_rules,
        "involved_nodes": nodes[:8],
        "path": path[:10],
        "mermaid": _path_to_mermaid(path[:10]),
        "root_cause": root_map.get(ctype, message),
        "impact": impact_map.get(ctype, ["状态机行为不稳定", "实现与测试口径不一致"]),
        "suggestion": suggestion_map.get(ctype, ["补充冲突规则定义", "补充恢复兜底路径"]),
    }


def build_explainable_report(
    rule_diagnostics: Dict[str, Any],
    deterministic_rules: Dict[str, Any],
    state_machine: Dict[str, Any],
) -> Dict[str, Any]:
    di = rule_diagnostics if isinstance(rule_diagnostics, dict) else {}
    dr = deterministic_rules if isinstance(deterministic_rules, dict) else {}
    sm = state_machine if isinstance(state_machine, dict) else {}
    conflicts = di.get("conflicts") if isinstance(di.get("conflicts"), list) else []
    explain_items: List[Dict[str, Any]] = []
    for i, c in enumerate(conflicts[:8], start=1):
        explain_items.append(_explain_for_conflict(c, sm, i))
    if not explain_items:
        defects = dr.get("defects") if isinstance(dr.get("defects"), list) else []
        for i, d in enumerate(defects[:5], start=1):
            c = {
                "type": _norm(d.get("check_type")) or _norm(d.get("rule_id")),
                "message": _norm(d.get("description")) or "规则校验失败",
                "severity": _norm(d.get("severity")) or "P1",
            }
            explain_items.append(_explain_for_conflict(c, sm, i))
    risk_level = "low"
    if any(_norm(x.get("severity")).upper() == "P0" for x in explain_items):
        risk_level = "high"
    elif explain_items:
        risk_level = "medium"
    return {
        "summary": {
            "conflict_count": len(explain_items),
            "risk_level": risk_level,
        },
        "items": explain_items,
    }
