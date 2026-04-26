# -*- coding: utf-8 -*-
from typing import Any, Dict, List


def _norm(x: Any) -> str:
    return str(x or "").strip()


CATEGORY_MAP = {
    "state_machine": ("状态机问题", "状态路径缺陷"),
    "priority": ("优先级问题", "优先级冲突"),
    "interrupt": ("并发问题", "打断冲突"),
    "resume": ("恢复机制问题", "恢复链路缺失"),
    "concurrency": ("并发问题", "并发仲裁不足"),
    "resource": ("资源管理问题", "资源占用与释放不完整"),
    "exception": ("规则缺失问题", "异常兜底不足"),
    "experience": ("规则缺失问题", "体验规则缺失"),
}


def _strategy_pack(category: str, root_type: str) -> Dict[str, Any]:
    c = _norm(category)
    r = _norm(root_type)
    if c == "优先级问题":
        return {
            "strategies": ["建立单向优先级链并禁止闭环", "引入优先级唯一源配置中心", "增加恢复阶段优先级回溯规则"],
            "architecture": {
                "module": "Scheduler",
                "components": ["PriorityManager", "StateController", "InterruptHandler", "ResumeManager"],
            },
            "tasks": ["实现优先级配置中心", "实现统一状态切换入口", "增加冲突兜底回退策略"],
            "test_focus": ["多级打断恢复顺序", "同级优先级冲突裁决", "优先级环路回归校验"],
            "risk_benefit": {
                "benefit": ["降低状态震荡风险", "提升调度确定性", "减少线上冲突缺陷"],
                "risk": ["涉及核心调度链路", "需要全链路回归验证"],
            },
        }
    if c == "状态机问题":
        return {
            "strategies": ["补齐状态闭环与终态定义", "新增不可达与死胡同状态拦截", "统一状态迁移前置条件"],
            "architecture": {
                "module": "StateController",
                "components": ["TransitionGuard", "StateRouter", "FallbackHandler"],
            },
            "tasks": ["补全状态与转移矩阵", "实现状态迁移守卫", "增加状态异常回退路径"],
            "test_focus": ["不可达状态检测", "无出口状态检测", "状态迁移一致性"],
            "risk_benefit": {
                "benefit": ["避免状态错乱", "提高流程可解释性"],
                "risk": ["状态定义需同步业务全模块"],
            },
        }
    if c == "并发问题":
        return {
            "strategies": ["定义并发事件仲裁规则", "引入排队或互斥机制", "补充重复触发幂等处理"],
            "architecture": {
                "module": "ConcurrencyCoordinator",
                "components": ["EventQueue", "ConflictResolver", "IdempotencyGuard"],
            },
            "tasks": ["实现并发事件队列", "实现互斥裁决器", "增加重复事件幂等保护"],
            "test_focus": ["多端并发触发稳定性", "重复事件幂等", "冲突仲裁一致性"],
            "risk_benefit": {
                "benefit": ["降低并发竞争故障", "提升系统稳定性"],
                "risk": ["并发改造影响吞吐与时延"],
            },
        }
    if c == "恢复机制问题":
        return {
            "strategies": ["建立恢复顺序与条件模型", "增加恢复失败兜底", "引入恢复队列与状态回溯"],
            "architecture": {
                "module": "ResumeManager",
                "components": ["ResumeQueue", "ResumePolicy", "RecoveryFallback"],
            },
            "tasks": ["实现恢复队列机制", "定义恢复触发条件", "实现恢复失败降级策略"],
            "test_focus": ["恢复顺序正确性", "恢复失败回退", "打断-恢复闭环"],
            "risk_benefit": {
                "benefit": ["减少打断后状态丢失", "提升恢复可靠性"],
                "risk": ["恢复链路复杂度上升"],
            },
        }
    if c == "资源管理问题":
        return {
            "strategies": ["定义资源抢占与释放策略", "统一资源生命周期", "增加资源冲突监控"],
            "architecture": {
                "module": "ResourceArbiter",
                "components": ["ResourceRegistry", "LeaseManager", "ReleaseTracker"],
            },
            "tasks": ["实现资源占用注册", "实现资源释放回收", "实现抢占冲突告警"],
            "test_focus": ["资源抢占顺序", "资源释放完整性", "异常资源泄漏"],
            "risk_benefit": {
                "benefit": ["降低资源冲突", "避免泄漏与锁死"],
                "risk": ["需要补充资源埋点与监控"],
            },
        }
    return {
        "strategies": [f"围绕“{r or '规则缺失'}”补齐规则定义", "建立统一调度入口", "增加兜底与监控告警"],
        "architecture": {
            "module": "Scheduler",
            "components": ["PolicyCenter", "StateController", "FallbackHandler"],
        },
        "tasks": ["补齐规则文档与配置", "实现统一策略分发", "增加关键路径告警"],
        "test_focus": ["关键规则命中", "异常路径兜底", "回归稳定性"],
        "risk_benefit": {
            "benefit": ["减少决策分歧", "提升可维护性"],
            "risk": ["规则改造需同步跨团队口径"],
        },
    }


def build_strategy_report(
    deterministic_rules: Dict[str, Any],
    explainable_report: Dict[str, Any],
    state_machine: Dict[str, Any],
) -> Dict[str, Any]:
    dr = deterministic_rules if isinstance(deterministic_rules, dict) else {}
    er = explainable_report if isinstance(explainable_report, dict) else {}
    sm = state_machine if isinstance(state_machine, dict) else {}
    defects = dr.get("defects") if isinstance(dr.get("defects"), list) else []
    explain_items = er.get("items") if isinstance(er.get("items"), list) else []

    plans: List[Dict[str, Any]] = []
    seen = set()
    for d in defects[:12]:
        if not isinstance(d, dict):
            continue
        cat_key = _norm(d.get("category"))
        category, root_type = CATEGORY_MAP.get(cat_key, ("规则缺失问题", _norm(d.get("check_type")) or "规则缺失"))
        key = (category, root_type)
        if key in seen:
            continue
        seen.add(key)
        base = _strategy_pack(category, root_type)
        plans.append({
            "problem_type": category,
            "root_type": root_type,
            "severity": _norm(d.get("severity")) or "P1",
            "related_rules": [_norm(d.get("rule_id"))] if _norm(d.get("rule_id")) else [],
            "strategies": base["strategies"],
            "architecture": base["architecture"],
            "tasks": base["tasks"],
            "test_focus": base["test_focus"],
            "risk_benefit": base["risk_benefit"],
        })

    for it in explain_items[:6]:
        if not isinstance(it, dict):
            continue
        t = _norm(it.get("type"))
        if not t:
            continue
        merged = False
        for p in plans:
            if t in _norm(p.get("root_type")) or _norm(p.get("problem_type")) in t:
                merged = True
                break
        if not merged:
            base = _strategy_pack("规则缺失问题", t)
            plans.append({
                "problem_type": "规则缺失问题",
                "root_type": t,
                "severity": _norm(it.get("severity")) or "P1",
                "related_rules": [_norm(x) for x in (it.get("related_rules") or []) if _norm(x)],
                "strategies": base["strategies"],
                "architecture": base["architecture"],
                "tasks": base["tasks"],
                "test_focus": base["test_focus"],
                "risk_benefit": base["risk_benefit"],
            })

    states = sm.get("states") if isinstance(sm.get("states"), list) else []
    transitions = sm.get("transitions") if isinstance(sm.get("transitions"), list) else []
    summary = {
        "plan_count": len(plans),
        "state_count": len(states),
        "transition_count": len(transitions),
        "failed_rule_count": len(defects),
    }
    if any(_norm(p.get("severity")).upper() == "P0" for p in plans):
        summary["priority"] = "P0"
    elif plans:
        summary["priority"] = "P1"
    else:
        summary["priority"] = "P2"
    return {
        "summary": summary,
        "plans": plans[:8],
    }
