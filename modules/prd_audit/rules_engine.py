# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Set, Tuple


@dataclass
class Rule:
    rule_id: str
    name: str
    description: str
    severity: str
    category: str
    check_type: str
    penalty: int
    check: Callable[[Dict[str, Any]], bool]


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _states(model: Dict[str, Any]) -> Set[str]:
    return {_norm(x) for x in (model.get("states") or []) if _norm(x)}


def _transitions(model: Dict[str, Any]) -> List[Dict[str, str]]:
    out = []
    for t in (model.get("transitions") or []):
        if not isinstance(t, dict):
            continue
        frm = _norm(t.get("from"))
        to = _norm(t.get("to"))
        event = _norm(t.get("event"))
        action = _norm(t.get("action"))
        if frm and to:
            out.append({"from": frm, "to": to, "event": event, "action": action})
    return out


def _priority_relations(model: Dict[str, Any]) -> List[Tuple[str, str]]:
    rel = []
    for r in (model.get("rules") or []):
        if not isinstance(r, dict):
            continue
        p = _norm(r.get("priority"))
        if ">" not in p:
            continue
        parts = [_norm(x) for x in p.split(">") if _norm(x)]
        for i in range(len(parts) - 1):
            rel.append((parts[i], parts[i + 1]))
    return rel


def _priority_cycle(model: Dict[str, Any]) -> bool:
    relations = _priority_relations(model)
    graph: Dict[str, Set[str]] = {}
    for a, b in relations:
        graph.setdefault(a, set()).add(b)
    visited = set()
    stack = set()

    def dfs(node: str) -> bool:
        visited.add(node)
        stack.add(node)
        for nxt in graph.get(node, set()):
            if nxt not in visited:
                if dfs(nxt):
                    return True
            elif nxt in stack:
                return True
        stack.remove(node)
        return False

    for n in graph:
        if n not in visited and dfs(n):
            return True
    return False


def _interrupt_edges(model: Dict[str, Any]) -> List[Tuple[str, str]]:
    edges = []
    for t in _transitions(model):
        action = _norm(t.get("action"))
        if "打断" not in action:
            continue
        edges.append((_norm(t.get("from")), _norm(t.get("to"))))
    return edges


def _resume_exists(model: Dict[str, Any]) -> bool:
    for t in _transitions(model):
        if any(k in _norm(t.get("event")) for k in ["恢复", "回到", "继续"]):
            return True
        if any(k in _norm(t.get("action")) for k in ["恢复", "回到", "继续"]):
            return True
    return False


def _path_exists(start: str, target: str, graph: Dict[str, Set[str]]) -> bool:
    if start == target:
        return True
    q = [start]
    visited = {start}
    while q:
        cur = q.pop(0)
        for nxt in graph.get(cur, set()):
            if nxt == target:
                return True
            if nxt not in visited:
                visited.add(nxt)
                q.append(nxt)
    return False


def check_r01_state_machine_closed(model: Dict[str, Any]) -> bool:
    s = _states(model)
    if not s:
        return False
    reachable = set()
    for t in _transitions(model):
        reachable.add(_norm(t.get("from")))
        reachable.add(_norm(t.get("to")))
    return s.issubset(reachable)


def check_r02_terminal_state(model: Dict[str, Any]) -> bool:
    s = _states(model)
    if not s:
        return False
    from_states = {_norm(t.get("from")) for t in _transitions(model)}
    return len([x for x in s if x not in from_states]) > 0


def check_r03_state_island(model: Dict[str, Any]) -> bool:
    s = _states(model)
    if not s:
        return False
    ins = set()
    outs = set()
    for t in _transitions(model):
        ins.add(_norm(t.get("to")))
        outs.add(_norm(t.get("from")))
    return all((st in ins) or (st in outs) for st in s)


def check_r04_priority_cycle(model: Dict[str, Any]) -> bool:
    return not _priority_cycle(model)


def check_r05_priority_coverage(model: Dict[str, Any]) -> bool:
    actors = {_norm(x) for x in (model.get("actors") or []) if _norm(x)}
    rel = _priority_relations(model)
    if len(actors) <= 1:
        return True
    involved = {a for x in rel for a in x}
    return actors.issubset(involved)


def check_r06_multi_priority_conflict(model: Dict[str, Any]) -> bool:
    ranks: Dict[str, Set[int]] = {}
    for r in (model.get("rules") or []):
        if not isinstance(r, dict):
            continue
        p = _norm(r.get("priority"))
        if ">" not in p:
            continue
        parts = [_norm(x) for x in p.split(">") if _norm(x)]
        for idx, part in enumerate(parts):
            ranks.setdefault(part, set()).add(idx)
    return all(len(v) <= 1 for v in ranks.values())


def check_r07_interrupt_without_resume(model: Dict[str, Any]) -> bool:
    has_interrupt = len(_interrupt_edges(model)) > 0
    if not has_interrupt:
        return True
    return _resume_exists(model)


def check_r08_bidirectional_interrupt(model: Dict[str, Any]) -> bool:
    edges = set(_interrupt_edges(model))
    for a, b in list(edges):
        if (b, a) in edges and a != b:
            return False
    return True


def check_r09_interrupt_chain_closure(model: Dict[str, Any]) -> bool:
    edges = _interrupt_edges(model)
    if len(edges) < 2:
        return True
    graph: Dict[str, Set[str]] = {}
    for t in _transitions(model):
        graph.setdefault(_norm(t.get("from")), set()).add(_norm(t.get("to")))
    for a, b in edges:
        for c, d in edges:
            if b == c and a != d:
                if not _path_exists(d, a, graph):
                    return False
    return True


def check_r10_multi_event_conflict(model: Dict[str, Any]) -> bool:
    transitions = _transitions(model)
    concurrent = [t for t in transitions if any(k in _norm(t.get("event")) for k in ["同时", "并发", "多事件"])]
    if not concurrent:
        return True
    return any(any(k in _norm(t.get("action")) for k in ["优先级", "队列", "互斥", "串行"]) for t in concurrent)


def check_r11_concurrency_undefined(model: Dict[str, Any]) -> bool:
    events = {_norm(x) for x in (model.get("events") or []) if _norm(x)}
    if not any(any(k in e for k in ["并发", "多人", "多端"]) for e in events):
        return True
    return any(any(k in _norm(t.get("action")) for k in ["锁", "互斥", "排队", "幂等"]) for t in _transitions(model))


def check_r12_non_atomic_switch(model: Dict[str, Any]) -> bool:
    transitions = _transitions(model)
    switch_rows = [t for t in transitions if "切换" in _norm(t.get("action"))]
    if not switch_rows:
        return True
    return any(any(k in _norm(t.get("action")) for k in ["原子", "事务", "锁"]) for t in switch_rows)


def check_r13_resume_inconsistent(model: Dict[str, Any]) -> bool:
    transitions = _transitions(model)
    resume_rows = [t for t in transitions if any(k in _norm(t.get("event")) + _norm(t.get("action")) for k in ["恢复", "回到", "继续"])]
    if not resume_rows:
        return True
    states = _states(model)
    return all(_norm(t.get("to")) in states for t in resume_rows)


def check_r14_multi_level_interrupt_order(model: Dict[str, Any]) -> bool:
    rel = _priority_relations(model)
    nodes = {x for r in rel for x in r}
    if len(nodes) < 3:
        return True
    text = " ".join([_norm(t.get("event")) + " " + _norm(t.get("action")) for t in _transitions(model)])
    return any(k in text for k in ["顺序", "栈", "后进先出", "先入后出"])


def check_r15_same_priority_conflict(model: Dict[str, Any]) -> bool:
    raw = " ".join([_norm(r.get("priority")) for r in (model.get("rules") or []) if isinstance(r, dict)])
    has_same = any(k in raw for k in ["同级", "="])
    if not has_same:
        return True
    text = " ".join([_norm(t.get("action")) for t in _transitions(model)])
    return any(k in text for k in ["时间戳", "先到先得", "随机", "队列"])


def check_r16_dynamic_priority_undefined(model: Dict[str, Any]) -> bool:
    raw = " ".join([_norm(e) for e in (model.get("events") or [])])
    has_dynamic = any(k in raw for k in ["动态优先级", "临时优先级"])
    if not has_dynamic:
        return True
    rules_text = " ".join([_norm(r.get("priority")) for r in (model.get("rules") or []) if isinstance(r, dict)])
    return any(k in rules_text for k in ["更新", "时效", "回退"])


def check_r17_resume_order_undefined(model: Dict[str, Any]) -> bool:
    interrupts = _interrupt_edges(model)
    if len(interrupts) < 2:
        return True
    txt = " ".join([_norm(t.get("event")) + " " + _norm(t.get("action")) for t in _transitions(model)])
    return any(k in txt for k in ["恢复顺序", "先恢复", "后恢复", "栈"])


def check_r18_resume_condition_missing(model: Dict[str, Any]) -> bool:
    resume_rows = [t for t in _transitions(model) if any(k in _norm(t.get("event")) + _norm(t.get("action")) for k in ["恢复", "回到", "继续"])]
    if not resume_rows:
        return True
    return any(_norm(t.get("event")) not in {"", "触发", "恢复触发"} for t in resume_rows)


def check_r19_resume_failure_unhandled(model: Dict[str, Any]) -> bool:
    if not _resume_exists(model):
        return True
    txt = " ".join([_norm(t.get("event")) + " " + _norm(t.get("action")) for t in _transitions(model)])
    return any(k in txt for k in ["失败", "超时", "重试", "降级"])


def check_r20_resource_preempt_undefined(model: Dict[str, Any]) -> bool:
    actors = {_norm(x) for x in (model.get("actors") or []) if _norm(x)}
    if len(actors) <= 1:
        return True
    txt = " ".join([_norm(t.get("event")) + " " + _norm(t.get("action")) for t in _transitions(model)])
    return any(k in txt for k in ["抢占", "占用", "互斥", "仲裁"])


def check_r21_resource_release_undefined(model: Dict[str, Any]) -> bool:
    txt = " ".join([_norm(t.get("event")) + " " + _norm(t.get("action")) for t in _transitions(model)])
    if not any(k in txt for k in ["进入", "占用"]):
        return True
    return any(k in txt for k in ["释放", "退出", "回收"])


def check_r22_exception_interrupt_unhandled(model: Dict[str, Any]) -> bool:
    txt = " ".join([_norm(t.get("event")) + " " + _norm(t.get("action")) for t in _transitions(model)])
    has_exception = any(k in txt for k in ["异常", "失败", "弱网", "超时"])
    if not has_exception:
        return True
    return any(k in txt for k in ["回退", "重试", "降级", "空闲"])


def check_r23_middle_state_loss(model: Dict[str, Any]) -> bool:
    states = _states(model)
    mids = [s for s in states if any(k in s for k in ["中间", "处理中", "过渡"])]
    if not mids:
        return True
    ins = {_norm(t.get("to")) for t in _transitions(model)}
    outs = {_norm(t.get("from")) for t in _transitions(model)}
    return all((m in ins and m in outs) for m in mids)


def check_r24_trigger_condition_incomplete(model: Dict[str, Any]) -> bool:
    transitions = _transitions(model)
    if not transitions:
        return False
    bad = [t for t in transitions if len(_norm(t.get("event"))) < 2 or _norm(t.get("event")) in {"触发", "事件"}]
    return len(bad) == 0


def check_r25_duplicate_event_unhandled(model: Dict[str, Any]) -> bool:
    seen: Dict[Tuple[str, str], Set[str]] = {}
    for t in _transitions(model):
        key = (_norm(t.get("from")), _norm(t.get("event")))
        seen.setdefault(key, set()).add(_norm(t.get("to")))
    for _, tos in seen.items():
        if len(tos) > 1:
            return False
    return True


def check_r26_interrupt_frequency_high(model: Dict[str, Any]) -> bool:
    interrupts = _interrupt_edges(model)
    threshold = max(3, len(_states(model)))
    return len(interrupts) <= threshold


def check_r27_resume_experience_incoherent(model: Dict[str, Any]) -> bool:
    if not _resume_exists(model):
        return True
    txt = " ".join([_norm(t.get("action")) for t in _transitions(model)])
    return any(k in txt for k in ["平滑", "继续", "断点", "回到"])


def check_r28_rule_redundancy(model: Dict[str, Any]) -> bool:
    transition_keys = []
    for t in _transitions(model):
        transition_keys.append((_norm(t.get("from")), _norm(t.get("event")), _norm(t.get("to")), _norm(t.get("action"))))
    return len(transition_keys) == len(set(transition_keys))


def check_r29_rule_scattered(model: Dict[str, Any]) -> bool:
    global_rules = [_norm(x) for x in (model.get("global_rules") or []) if _norm(x)]
    module_rules = model.get("module_diff_rules") if isinstance(model.get("module_diff_rules"), list) else []
    if not module_rules:
        return True
    overlap = 0
    for m in module_rules:
        if not isinstance(m, dict):
            continue
        for r in (m.get("rules") or []):
            rr = _norm(r)
            if rr and rr in global_rules:
                overlap += 1
    return overlap == 0


def check_r30_extension_unsupported(model: Dict[str, Any]) -> bool:
    actors = [_norm(x) for x in (model.get("actors") or []) if _norm(x)]
    if len(actors) < 3:
        return True
    txt = " ".join([_norm(t.get("event")) + " " + _norm(t.get("action")) for t in _transitions(model)])
    return any(k in txt for k in ["默认", "其他", "扩展", "新增"])


RULES: List[Rule] = [
    Rule("R01", "状态机未闭环", "存在状态无法到达或无法退出", "P0", "state_machine", "closure", 15, check_r01_state_machine_closed),
    Rule("R02", "无终态", "系统没有结束状态", "P0", "state_machine", "terminal_state", 15, check_r02_terminal_state),
    Rule("R03", "状态孤岛", "存在未被任何路径连接的状态", "P0", "state_machine", "island_state", 15, check_r03_state_island),
    Rule("R04", "优先级循环依赖", "优先级关系存在环", "P0", "priority", "graph_cycle", 15, check_r04_priority_cycle),
    Rule("R05", "优先级未全覆盖", "部分功能未定义优先级", "P0", "priority", "coverage", 15, check_r05_priority_coverage),
    Rule("R06", "多优先级冲突", "同一功能出现多个优先级定义", "P0", "priority", "multi_rank_conflict", 15, check_r06_multi_priority_conflict),
    Rule("R07", "打断无恢复", "被打断后无恢复路径", "P0", "interrupt", "missing_resume", 15, check_r07_interrupt_without_resume),
    Rule("R08", "双向打断死锁", "A打断B同时B打断A", "P0", "interrupt", "bidirectional_deadlock", 15, check_r08_bidirectional_interrupt),
    Rule("R09", "打断链未闭环", "多级打断后无法回到起点", "P0", "interrupt", "chain_closure", 15, check_r09_interrupt_chain_closure),
    Rule("R10", "多事件冲突未定义", "同时触发多事件无仲裁规则", "P0", "concurrency", "multi_event_resolution", 15, check_r10_multi_event_conflict),
    Rule("R11", "并发操作未定义", "多人/多端并发未给出处理", "P0", "concurrency", "concurrency_strategy", 15, check_r11_concurrency_undefined),
    Rule("R12", "状态切换非原子", "状态切换缺少原子性保障", "P0", "state_machine", "atomicity", 15, check_r12_non_atomic_switch),
    Rule("R13", "恢复状态不一致", "恢复后状态与定义集合不一致", "P0", "resume", "state_consistency", 15, check_r13_resume_inconsistent),
    Rule("R14", "多级打断顺序未定义", "A>B>C 但恢复顺序不明确", "P1", "interrupt", "order_missing", 8, check_r14_multi_level_interrupt_order),
    Rule("R15", "同优先级冲突", "同优先级未定义裁决策略", "P1", "priority", "same_level_tie_break", 8, check_r15_same_priority_conflict),
    Rule("R16", "动态优先级未定义", "优先级动态变化策略未说明", "P1", "priority", "dynamic_priority", 8, check_r16_dynamic_priority_undefined),
    Rule("R17", "恢复顺序未定义", "多个恢复目标顺序不明确", "P1", "resume", "resume_order", 8, check_r17_resume_order_undefined),
    Rule("R18", "恢复条件缺失", "恢复触发条件不完整", "P1", "resume", "resume_condition", 8, check_r18_resume_condition_missing),
    Rule("R19", "恢复失败未处理", "恢复失败后的兜底未定义", "P1", "resume", "resume_failure", 8, check_r19_resume_failure_unhandled),
    Rule("R20", "资源抢占未定义", "多功能争抢资源未说明", "P1", "resource", "resource_preempt", 8, check_r20_resource_preempt_undefined),
    Rule("R21", "资源释放未定义", "结束后资源释放策略缺失", "P1", "resource", "resource_release", 8, check_r21_resource_release_undefined),
    Rule("R22", "异常中断未处理", "异常退出后的状态未定义", "P1", "exception", "exception_interrupt", 8, check_r22_exception_interrupt_unhandled),
    Rule("R23", "中间态丢失", "过渡状态无完整进出路径", "P1", "state_machine", "transient_state", 8, check_r23_middle_state_loss),
    Rule("R24", "触发条件不完整", "触发事件缺失或过于模糊", "P1", "state_machine", "trigger_quality", 8, check_r24_trigger_condition_incomplete),
    Rule("R25", "事件重复触发未处理", "同源同事件指向多个目标", "P1", "concurrency", "duplicate_event", 8, check_r25_duplicate_event_unhandled),
    Rule("R26", "打断频率过高", "打断迁移过密影响体验", "P2", "experience", "interrupt_frequency", 3, check_r26_interrupt_frequency_high),
    Rule("R27", "恢复体验不连贯", "恢复动作缺少连贯性表达", "P2", "experience", "resume_experience", 3, check_r27_resume_experience_incoherent),
    Rule("R28", "规则冗余", "重复定义同一规则", "P2", "priority", "rule_redundancy", 3, check_r28_rule_redundancy),
    Rule("R29", "规则分散", "全局规则与模块规则重复散落", "P2", "priority", "rule_scatter", 3, check_r29_rule_scattered),
    Rule("R30", "扩展性不足", "新增功能缺少通用接入策略", "P2", "resource", "extensibility", 3, check_r30_extension_unsupported),
]


def build_rule_engine_model(
    system_model: Dict[str, Any],
    state_machine: Dict[str, Any],
    atomic_rules: List[Dict[str, Any]],
    rule_model: Dict[str, Any],
) -> Dict[str, Any]:
    sm = system_model if isinstance(system_model, dict) else {}
    stm = state_machine if isinstance(state_machine, dict) else {}
    atoms = atomic_rules if isinstance(atomic_rules, list) else []
    rmodel = rule_model if isinstance(rule_model, dict) else {}
    states = [_norm(x) for x in (stm.get("states") or sm.get("actors") or []) if _norm(x)]
    transitions = []
    for t in (stm.get("transitions") or []):
        if not isinstance(t, dict):
            continue
        frm = _norm(t.get("from"))
        to = _norm(t.get("to"))
        event = _norm(t.get("trigger")) or _norm(t.get("event"))
        action = _norm(t.get("action"))
        if frm and to:
            transitions.append({"from": frm, "to": to, "event": event or "触发", "action": action})
    events = {_norm(x.get("event")) for x in transitions if _norm(x.get("event"))}
    for a in atoms:
        if not isinstance(a, dict):
            continue
        cond = _norm(a.get("condition"))
        act = _norm(a.get("action"))
        if cond and cond != "【PRD未说明】":
            events.add(cond)
        if act and act != "【PRD未说明】":
            events.add(act)
    priorities = []
    for x in (sm.get("priority_order") or []):
        p = _norm(x)
        if p:
            priorities.append({"priority": p})
    if not priorities:
        chain = [_norm(x) for x in (rmodel.get("priority_chain") or []) if _norm(x)]
        if len(chain) >= 2:
            priorities.append({"priority": " > ".join(chain)})
    return {
        "states": states,
        "transitions": transitions,
        "rules": priorities,
        "events": sorted(events),
        "actors": [_norm(x) for x in (sm.get("actors") or []) if _norm(x)],
        "global_rules": [_norm(x) for x in (sm.get("global_rules") or []) if _norm(x)],
        "module_diff_rules": sm.get("module_diff_rules") if isinstance(sm.get("module_diff_rules"), list) else [],
    }


def run_rules(model: Dict[str, Any], enabled: Dict[str, bool] = None) -> Dict[str, Any]:
    cfg = enabled if isinstance(enabled, dict) else {}
    defects = []
    checks = []
    score = 100
    for rule in RULES:
        if cfg and not bool(cfg.get(rule.rule_id, True)):
            continue
        try:
            passed = bool(rule.check(model))
        except Exception:
            passed = False
        row = {
            "rule_id": rule.rule_id,
            "name": rule.name,
            "severity": rule.severity,
            "category": rule.category,
            "check_type": rule.check_type,
            "passed": passed,
            "penalty": 0 if passed else rule.penalty,
            "description": rule.description,
        }
        checks.append(row)
        if not passed:
            defects.append({
                "rule_id": rule.rule_id,
                "name": rule.name,
                "severity": rule.severity,
                "category": rule.category,
                "check_type": rule.check_type,
                "description": rule.description,
            })
            score -= int(rule.penalty)
    score = max(score, 0)
    hit_by_rule = {c["rule_id"]: 0 if c["passed"] else 1 for c in checks}
    hit_by_category: Dict[str, int] = {}
    for c in checks:
        if not c["passed"]:
            hit_by_category[c["category"]] = hit_by_category.get(c["category"], 0) + 1
    return {
        "score": score,
        "defects": defects,
        "checks": checks,
        "stats": {
            "total_rules": len(checks),
            "failed_rules": len(defects),
            "passed_rules": max(0, len(checks) - len(defects)),
            "hit_by_rule": hit_by_rule,
            "hit_by_category": hit_by_category,
        },
    }
