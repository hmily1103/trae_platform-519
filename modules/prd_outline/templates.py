# -*- coding: utf-8 -*-
import re
from typing import Any, Dict, List


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _split_sentences(text: str) -> List[str]:
    # 按照完整的句子进行切割，而不是遇到标点就切得太碎
    rows = re.split(r"[\r\n]+|[。！？!?]+", _norm(text))
    out: List[str] = []
    for r in rows:
        t = _norm(r)
        t = re.sub(r"\s+", " ", t).strip("，,；;。:：- ")
        if 12 <= len(t) <= 200:  # 提高最短长度，保留长句的完整语义
            out.append(t)
    return out[:500]


def _dedup(items: List[str], limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in items:
        t = _norm(x)
        if not t:
            continue
        k = re.sub(r"\s+", "", t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _pick(sentences: List[str], kws: List[str], limit: int) -> List[str]:
    scored = []
    for s in sentences:
        hit = sum(1 for k in kws if k and k in s)
        if hit <= 0:
            continue
        scored.append((hit, len(s), s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return _dedup([x[2] for x in scored], limit)


def _compress(text: str, max_len: int = 80) -> str:
    t = _norm(text)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(例如|比如|如：|包括但不限于).*$", "", t).strip()
    t = re.sub(r"^(?:[\d]+\.|[ivx]+\.|-|\*)\s*", "", t)
    t = re.sub(r"^(\d+\.\s*[\u4e00-\u9fa5]+：)", "", t)
    t = re.sub(r"^【[^】]+】\s*", "", t)
    t = re.sub(r"^(ai数字人|数字人|投屏|游戏|广告)\s*(\d+\.\s*)?", "", t, flags=re.IGNORECASE)
    t = t.replace("⼴", "广").replace("告", "告").replace("⼈", "人").replace("展⽰", "展示").replace("最⾼", "最高")
    t = t.strip("，,；;。:：- ")
    if len(t) <= max_len:
        return t
    # 不要随便从冒号逗号截断，尽量保留完整逻辑，只做硬截断
    return t[:max_len].rstrip("，,；;。:：- ") + "…"


def scheduling_template(model: Dict[str, object], text: str = "", stage1: Dict[str, Any] = None) -> Dict[str, object]:
    stage1 = stage1 if isinstance(stage1, dict) else {}
    chain = model.get("priority_chain") if isinstance(model.get("priority_chain"), list) else []
    priority = " > ".join([str(x) for x in chain if str(x).strip()]) if chain else "未明确"
    interrupt_rules = model.get("interrupt_rules") if isinstance(model.get("interrupt_rules"), list) else []
    resume_rules = model.get("resume_rules") if isinstance(model.get("resume_rules"), list) else []
    sentences = _split_sentences(text)
    modules = [str(x) for x in (stage1.get("modules") or []) if _norm(x)]
    entities = [str(x) for x in (model.get("entities") or []) if _norm(x)]
    actors = _dedup(modules + entities, 8)
    l0_candidates = _pick(sentences, ["优先级", "打断", "恢复", "抢占", "调度", "切换"], 3)
    if l0_candidates:
        summary = _compress(l0_candidates[0], 60)
    elif actors:
        summary = "该PRD定义" + "、".join(actors[:4]) + "在同一资源下的优先级调度与恢复规则。"
    else:
        summary = "该PRD定义同一资源上的优先级调度与恢复规则。"
    flow_from_text = _pick(sentences, ["触发", "检测", "判断", "切换", "打断", "恢复", "回退"], 6)
    core_flow = _dedup([_compress(x, 36) for x in flow_from_text], 5)
    if not core_flow:
        core_flow = [
            "识别触发条件并定位当前占用方",
            "按优先级与限制条件执行裁决",
            "必要时中断低优先级内容",
            "高优先级结束后恢复目标内容",
        ]
    module_lines = _pick(sentences, ["模块", "功能", "能力", "负责", "处理", "控制"], 6)
    module_items = _dedup([_compress(x, 20) for x in modules + entities + module_lines], 6)
    key_rules: List[str] = [f"优先级顺序：{priority}", "高优先级可打断低优先级", "高优先级结束后恢复原内容"]
    key_rules.extend([str(x) for x in interrupt_rules[:3]])
    key_rules.extend([str(x) for x in resume_rules[:3]])
    key_rules = list(dict.fromkeys([x for x in key_rules if str(x).strip()]))[:12]
    return {
        "summary": summary,
        "core_flow": core_flow,
        "modules": module_items,
        "key_rules": key_rules,
    }


def business_flow_template(model: Dict[str, object], text: str = "", stage1: Dict[str, Any] = None) -> Dict[str, object]:
    stage1 = stage1 if isinstance(stage1, dict) else {}
    sentences = _split_sentences(text)
    flows = [str(x) for x in (stage1.get("flows") or []) if _norm(x)]
    modules = [str(x) for x in (stage1.get("modules") or []) if _norm(x)]
    summary_candidates = _pick(sentences, ["目标", "用于", "场景", "业务", "价值", "用户"], 3)
    summary = _compress(summary_candidates[0], 60) if summary_candidates else "该PRD描述业务流程闭环与规则边界。"
    core = _dedup(flows + _pick(sentences, ["发起", "提交", "校验", "处理", "返回", "通知"], 8), 5)
    core = [_compress(x, 36) for x in core][:5]
    if not core:
        core = ["用户发起业务请求", "系统按规则完成处理并反馈结果"]
    module_items = _dedup(modules + _pick(sentences, ["模块", "功能", "能力", "组件"], 8), 6)
    module_items = [_compress(x, 20) for x in module_items][:6]
    return {
        "summary": summary,
        "core_flow": core,
        "modules": module_items,
        "key_rules": [],
    }


def state_machine_template(model: Dict[str, object], text: str = "", stage1: Dict[str, Any] = None) -> Dict[str, object]:
    stage1 = stage1 if isinstance(stage1, dict) else {}
    sentences = _split_sentences(text)
    states = [str(x) for x in (stage1.get("states") or []) if _norm(x)]
    rules = [str(x) for x in (stage1.get("business_rules") or []) if _norm(x)]
    summary_candidates = _pick(sentences, ["状态", "流转", "迁移", "回退", "异常"], 3)
    summary = _compress(summary_candidates[0], 60) if summary_candidates else "该PRD描述状态流转规则与异常回退策略。"
    core = _dedup(_pick(sentences, ["触发", "状态", "迁移", "回退", "恢复", "进入", "退出"], 8), 5)
    core = [_compress(x, 36) for x in core][:5]
    if not core:
        core = ["识别状态变更事件", "校验迁移条件并执行转移", "异常时执行回退或恢复"]
    modules = _dedup(states + _pick(sentences, ["状态", "事件", "动作", "转移"], 8), 6)
    modules = [_compress(x, 20) for x in modules][:6]
    key_rules = _dedup(rules + _pick(sentences, ["禁止", "必须", "仅", "不可", "条件"], 8), 8)
    key_rules = [_compress(x, 50) for x in key_rules][:8]
    return {
        "summary": summary,
        "core_flow": core,
        "modules": modules,
        "key_rules": key_rules,
    }
