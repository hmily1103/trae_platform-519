# -*- coding: utf-8 -*-
import json
import os
import re
from typing import Any, Dict, List


DEFAULT_SECTION_KEYWORDS: Dict[str, List[str]] = {
    "business_context": ["背景", "概述", "目标", "定位", "愿景", "商业化", "项目简介"],
    "roles": ["角色", "参与方", "分工", "职责", "合作方", "责任方", "商户", "用户"],
    "main_flow": ["主流程", "核心流程", "业务流", "流程", "场景", "开台", "下单", "支付", "释放"],
    "feature_details": ["功能模块", "功能点", "详细设计", "能力", "功能说明"],
    "rules": ["规则", "约束", "限制", "条件", "优先级", "判定"],
    "exceptions": ["异常", "容错", "边界", "错误处理", "超时", "断网", "恢复", "告警", "兜底"],
    "interfaces": ["接口", "API", "交互", "对接", "回调", "token", "鉴权", "校验"],
    "implementation": ["实施", "时间", "计划", "部署", "试点", "推广", "培训", "维护", "上线"],
    "pending": ["待确认", "待定", "TBD", "待与", "未确定", "需确认"],
}

DEFAULT_ROLE_HINTS = [
    "用户", "商户", "平台", "运营", "研发", "测试", "客服", "财务", "管理员",
    "店长", "收银员", "采购", "仓管", "履约", "供应商", "合作方", "甲方", "乙方",
]

ROLE_STOPWORDS = {"点单", "支付", "履约", "库存", "流程", "规则", "异常", "接口", "模块", "系统"}


def _norm(x: Any) -> str:
    s = str(x or "")
    s = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", s)
    s = s.replace("\u0001", "").replace("", "")
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _rule_path() -> str:
    return os.path.join(os.path.dirname(__file__), "prd_scan_rules_v2.json")


def _load_keywords() -> Dict[str, List[str]]:
    kw = {k: list(v) for k, v in DEFAULT_SECTION_KEYWORDS.items()}
    fp = _rule_path()
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            sec = data.get("section_classifier") if isinstance(data, dict) else None
            if isinstance(sec, dict):
                for k, v in sec.items():
                    if isinstance(v, list) and v:
                        kw[k] = [_norm(x) for x in v if _norm(x)]
    except Exception:
        pass
    return kw


def _split_sentences(text: str) -> List[str]:
    rows = re.split(r"[\r\n]+|[。！？!?；;]+", _norm(text))
    out: List[str] = []
    for r in rows:
        t = _norm(r)
        t = re.sub(r"\s+", " ", t)
        t = re.sub(r"[`~^*_=]{2,}", " ", t)
        t = re.sub(r"^[a-zA-Z]\.\s*", "", t)
        t = re.sub(r"^(?:[\d]+[\.\)]|[一二三四五六七八九十]+[、\.\)])\s*", "", t)
        t = t.strip("，,；;。:：- ")
        if len(t) >= 6:
            out.append(t)
    return out[:800]


def _is_noisy_sentence(text: str) -> bool:
    t = _norm(text)
    if not t:
        return True
    if len(t) < 6:
        return True
    if len(t) > 140:
        return True
    if re.search(r"(UI设计|开发周期|负责人|上线时间|目录|版本记录)", t, flags=re.IGNORECASE):
        return True
    if re.search(r"(此处规则需确认|\bTBD\b|待补|版本号|修订记录)", t, flags=re.IGNORECASE):
        return True
    if re.search(r"^[\d\W_]+$", t):
        return True
    return False


def _compress_sentence(text: str, max_len: int = 60) -> str:
    t = _norm(text)
    if not t:
        return t
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(例如|比如|如：|包括但不限于).*$", "", t).strip()
    t = re.sub(r"^(?:[\d]+\.|[ivx]+\.|-|\*)\s*", "", t)
    t = re.sub(r"^[a-zA-Z]\.\s*", "", t)
    t = re.sub(r"^(\d+\.\s*[\u4e00-\u9fa5]+：)", "", t)
    t = re.sub(r"^(\d+\.\s*[\u4e00-\u9fa5]+：)", "", t)
    t = re.sub(r"^(任务|处理|展示)\s*(\d+\.\s*)?", "", t, flags=re.IGNORECASE)
    t = t.replace("⼴", "广").replace("告", "告").replace("⼈", "人").replace("展⽰", "展示").replace("最⾼", "最高")
    t = t.strip("，,；;。:：- ")
    if len(t) <= max_len:
        return t
    return t[:max_len].rstrip("，,；;。:：- ") + "…"


def _is_broken_text(text: str) -> bool:
    t = _norm(text)
    if not t:
        return True
    if len(t) < 6:
        return True
    if re.search(r"(版$|此处规则需确认\)?$|^[a-zA-Z]\.$)", t):
        return True
    return False


def _looks_like_role_name(text: str) -> bool:
    t = _norm(text)
    if not t:
        return False
    if t in DEFAULT_ROLE_HINTS:
        return True
    if re.search(r"(用户|终端|设备|系统|服务|端|平台|后台|前台|运营|研发|测试|管理员|商户|供应商|合作方|客服|财务)$", t):
        return True
    return False


def _is_global_rule_like(text: str) -> bool:
    t = _norm(text)
    return bool(re.search(r"(仅支持|适用于|范围覆盖|定制版|标准版|机顶盒上运行|触摸屏和电视端均有展示)", t))


def _is_state_specific_rule(state_name: str, rule: str) -> bool:
    s = _norm(state_name)
    r = _norm(rule)
    if not s or not r:
        return False
    if _is_global_rule_like(r):
        return False
    mapping = {
        "关闭": ["关闭", "不处理", "禁用", "关闭状态"],
        "开启": ["开启", "自动开启", "打开开关", "开始处理"],
        "处理中": ["处理中", "执行中", "红点", "处理状态", "状态提示"],
        "保存": ["保存", "结果", "持久化", "落盘"],
        "上传": ["上传", "云端", "待上传", "上传成功"],
        "列表": ["列表", "记录", "终端", "扫码", "二维码", "小程序", "获取"],
    }
    for key, kws in mapping.items():
        if key in s:
            return any(k in r for k in kws)
    return any(token in r for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", s))


def _dedup(items: List[str], limit: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for it in items:
        t = _norm(it)
        if not t:
            continue
        key = re.sub(r"\s+", "", t)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _pick(sentences: List[str], kws: List[str], limit: int) -> List[str]:
    scored = []
    for s in sentences:
        if _is_noisy_sentence(s):
            continue
        hit = sum(1 for k in kws if k and (k in s or k.lower() in s.lower()))
        if hit <= 0:
            continue
        action_bonus = 1 if re.search(r"(应|需|将|必须|禁止|支持|进入|退出|触发|回调|校验)", s) else 0
        scored.append((hit + action_bonus, len(s), s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return _dedup([x[2] for x in scored], limit)


def _extract_exception_rows(sentences: List[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    patterns = [
        r"(?:当|如果|若)(.+?)(?:时|，)\s*(?:系统|应|将|需)(.+)",
        r"异常(?:场景)?[：:]\s*(.+?)[，,。；;]\s*(?:处理|系统行为|动作)[：:]\s*(.+)",
    ]
    for s in sentences:
        if _is_noisy_sentence(s):
            continue
        for p in patterns:
            m = re.search(p, s)
            if not m:
                continue
            scene = _norm(m.group(1))
            behavior = _norm(m.group(2))
            if not scene or not behavior:
                continue
            trig = scene
            level = "P2"
            ss = s.lower()
            if any(x in ss for x in ["中断", "崩溃", "失败", "不可用", "断网"]):
                level = "P1"
            if any(x in ss for x in ["资金", "扣费", "结算", "支付", "鉴权失败"]):
                level = "P0"
            out.append({
                "scene": scene,
                "trigger": trig,
                "behavior": behavior,
                "level": level,
            })
            break
    return out[:20]


def _build_alignment_digest(sec: Dict[str, List[str]], module_summaries: List[Dict[str, str]]) -> List[str]:
    out: List[str] = []
    biz = sec.get("business_context") or []
    roles = sec.get("roles") or []
    flow = sec.get("main_flow") or []
    rules = sec.get("rules") or []
    exps = sec.get("exceptions") or []
    impl = sec.get("implementation") or []
    if biz:
        out.append("目标与定位：" + _compress_sentence(biz[0], 48))
    if roles:
        out.append("关键参与方：" + "；".join([_compress_sentence(x, 28) for x in roles[:2]]))
    if flow:
        out.append("主流程主线：" + _compress_sentence(flow[0], 52))
    if rules:
        out.append("关键约束：" + _compress_sentence(rules[0], 48))
    if exps:
        out.append("异常处理关注：" + _compress_sentence(exps[0], 48))
    if impl:
        out.append("落地节奏：" + _compress_sentence(impl[0], 48))
    if len(out) < 4:
        mods = [str((m or {}).get("name") or "") for m in module_summaries if str((m or {}).get("name") or "")]
        if mods:
            out.append("核心模块：" + "、".join(mods[:4]))
    return _dedup(out, 6)


def _extract_role_owner(sentence: str) -> str:
    s = _norm(sentence)
    s = re.sub(r"^(角色|参与方|职责|分工)[：:]\s*", "", s)
    s = re.sub(r"^(由)\s*", "", s)
    if re.search(r"^(在|当|若|如果|用户点击|用户使用|用户通过|任务最终是否|此处|其中)", s):
        return ""
    if "负责" in s:
        pre = _norm(s.split("负责")[0])
        if 1 < len(pre) <= 16 and _looks_like_role_name(pre):
            return pre
    m = re.search(r"(我方|甲方|乙方|平台|运营|研发|测试|财务|客服|管理员|用户|商户|门店店长|收银员|供应商|合作方)", s)
    if m:
        return _norm(m.group(1))
    seg = re.split(r"[：:，,；; ]", s)[0]
    seg = _compress_sentence(seg or "", 14)
    if not _looks_like_role_name(seg):
        return ""
    return seg


def _extract_role_duty(sentence: str) -> str:
    s = _norm(sentence)
    m = re.search(r"(负责|用于|执行|处理|校验|审批|同步|维护|运营|管理)(.+)", s)
    if m:
        return _compress_sentence(_norm(m.group(1) + m.group(2)), 48)
    return _compress_sentence(s, 48)


def _collect_role_candidates(sec: Dict[str, List[str]], stage1: Dict[str, Any], sentences: List[str]) -> List[str]:
    cand: List[str] = []
    cand.extend(sec.get("roles") or [])
    cand.extend([_norm(x) for x in (stage1.get("roles") or []) if _norm(x)])
    for x in (stage1.get("modules") or []):
        t = _norm(x)
        if not t:
            continue
        if re.search(r"(员|方|者|长|经理|主管|客服|财务|运营|研发|测试|管理员)$", t):
            cand.append(t)
    for s in sentences[:120]:
        m = re.search(r"([\u4e00-\u9fffA-Za-z]{2,12})(?:负责|参与|审批|维护|运营|校验|处理|接单|制作|执行)", s)
        if m:
            cand.append(_norm(m.group(1)))
    cand.extend(DEFAULT_ROLE_HINTS)
    out: List[str] = []
    seen = set()
    for x in cand:
        raw = _norm(x)
        raw = re.sub(r"^(角色|参与方|职责|分工)[：:]\s*", "", raw)
        if "负责" in raw:
            raw = _norm(raw.split("负责")[0])
        raw = re.split(r"[：:，,；; ]", raw)[0]
        t = _compress_sentence(raw, 14)
        if not t:
            continue
        if t in ROLE_STOPWORDS:
            continue
        if re.search(r"(主流程|流程|规则|异常|接口|系统)", t):
            continue
        if not _looks_like_role_name(t):
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[:20]


def _match_owner_from_candidates(sentence: str, role_candidates: List[str]) -> str:
    s = _norm(sentence)
    for r in (role_candidates or []):
        if r and r in s:
            return r
    m = re.search(r"(我方|甲方|乙方|平台|运营|研发|测试|财务|客服|管理员|用户|商户|门店店长|收银员|供应商|合作方)", s)
    if m:
        return _norm(m.group(1))
    return ""


def _build_role_duty_table(sec: Dict[str, List[str]], stage1: Dict[str, Any], sentences: List[str]) -> List[Dict[str, str]]:
    roles = [
        s for s in (sec.get("roles") or [])
        if not re.search(r"(主流程|流程：|系统校验|接单制作)", _norm(s))
    ]
    role_candidates = _collect_role_candidates(sec, stage1, sentences)
    role_sentences = roles + [
        s for s in sentences
        if _match_owner_from_candidates(s, role_candidates)
        and re.search(r"(负责|职责|分工|参与方|角色)", s)
    ]
    out: List[Dict[str, str]] = []
    seen = set()
    for s in role_sentences[:24]:
        owner = _match_owner_from_candidates(s, role_candidates) or _extract_role_owner(s)
        owner = _compress_sentence(owner, 14)
        if owner in ROLE_STOPWORDS:
            continue
        owner = owner.replace("负责", "")
        if not _looks_like_role_name(owner):
            continue
        duty = _extract_role_duty(s)
        key = owner + "|" + duty
        if not owner or key in seen:
            continue
        seen.add(key)
        out.append({"role": owner, "duty": duty})
    return out[:10]


def _split_flow_steps(flow_sentence: str) -> List[str]:
    s = _norm(flow_sentence)
    if not s:
        return []
    if _is_noisy_sentence(s):
        return []
    s = s.replace("->", "→").replace("-->", "→").replace("=>", "→")
    parts = []
    if "→" in s:
        parts = [p for p in s.split("→") if _norm(p)]
    else:
        parts = re.split(r"[，,；;]", s)
    steps = [_compress_sentence(_norm(x), 32) for x in parts if _norm(x)]
    out: List[str] = []
    for st in steps:
        if not st:
            continue
        if _is_noisy_sentence(st):
            continue
        if re.search(r"(此处规则需确认|待确认|转台不清空|盒子重启|关台或重开台)", st):
            continue
        if re.search(r"^[\u4e00-\u9fffA-Za-z0-9]+(?: [\u4e00-\u9fffA-Za-z0-9]+){1,4}$", st):
            if not re.search(r"(系统|用户|提交|校验|确认|接单|制作|同步|回调|通知|恢复|重试|释放|切换|进入|退出|审核|处理|登录|扫码)", st):
                continue
        if re.search(r"(扫码|登录|输入|选择|提交|下单|支付|校验|确认|接单|制作|发货|审核|通知|同步|回调|恢复|重试|释放|切换|进入|退出|展示|点击|取消)", st):
            out.append(st)
            continue
        if len(st) <= 8 and re.search(r"^[\u4e00-\u9fffA-Za-z0-9 /]+$", st):
            continue
        out.append(st)
    return out


def _infer_step_io(step: str) -> Dict[str, str]:
    s = _norm(step)
    in_hint = "触发事件"
    out_hint = "状态变更"
    if re.search(r"(发起|打开|进入|扫码|登录|输入|选择|提交|下单)", s):
        in_hint = "用户操作"
    elif re.search(r"(回调|同步|推送|通知|校验)", s):
        in_hint = "外部接口"
    if re.search(r"(支付|扣费|结算|下单|创建)", s):
        out_hint = "订单/资金状态更新"
    elif re.search(r"(提示|告警|通知)", s):
        out_hint = "反馈通知"
    elif re.search(r"(进入|退出|切换|恢复|重试|释放)", s):
        out_hint = "流程状态切换"
    return {"input": in_hint, "output": out_hint}


def _infer_step_owner(step: str, role_duty_table: List[Dict[str, str]], role_candidates: List[str]) -> str:
    s = _norm(step)
    for r in (role_candidates or []):
        if r and r in s:
            return r
    for row in (role_duty_table or []):
        role = _norm((row or {}).get("role"))
        duty = _norm((row or {}).get("duty"))
        if role and role in s:
            return role
        if duty and any(tok in duty for tok in ["支付", "收银"]) and re.search(r"(支付|扣费|结算|退款)", s):
            return role or "收银/财务"
        if duty and any(tok in duty for tok in ["下单", "扫码", "登录", "点单"]) and re.search(r"(扫码|下单|登录|点单|选择)", s):
            return role or "用户"
    if re.search(r"(扫码|下单|登录|点单|选择)", s):
        return "用户"
    if re.search(r"(支付|扣费|结算|退款)", s):
        return "收银/财务"
    if re.search(r"(校验|同步|回调|推送|通知)", s):
        return "平台系统"
    if re.search(r"(制作|接单|发货|履约)", s):
        return "履约/执行方"
    return "平台系统"


def _build_flow_step_table(sec: Dict[str, List[str]], role_duty_table: List[Dict[str, str]], role_candidates: List[str]) -> List[Dict[str, str]]:
    flows = sec.get("main_flow") or []
    flat_steps: List[str] = []
    for f in flows[:4]:
        flat_steps.extend(_split_flow_steps(f))
    dedup_steps = _dedup(flat_steps, 12)
    out: List[Dict[str, str]] = []
    for i, st in enumerate(dedup_steps, start=1):
        io = _infer_step_io(st)
        owner = _infer_step_owner(st, role_duty_table, role_candidates)
        out.append({"step": f"S{i:02d}", "owner": owner, "action": st, "input": io["input"], "output": io["output"]})
    return out[:12]


def generate_explicit_outline(content: str, stage1: Dict[str, Any]) -> Dict[str, Any]:
    kw = _load_keywords()
    text = _norm(content)
    merged = " ".join(
        [text]
        + [_norm(x) for x in (stage1.get("modules") or [])]
        + [_norm(x) for x in (stage1.get("flows") or [])]
        + [_norm(x) for x in (stage1.get("business_rules") or [])]
        + [_norm(x) for x in (stage1.get("exceptions") or [])]
    )
    sentences = _split_sentences(merged)
    noisy_count = len([s for s in sentences if _is_noisy_sentence(s)])
    clean_sentences = [s for s in sentences if not _is_noisy_sentence(s)]
    sec: Dict[str, List[str]] = {}
    for key, kws in kw.items():
        sec[key] = _pick(clean_sentences, kws, 8 if key == "main_flow" else 6)

    if not sec.get("main_flow"):
        sec["main_flow"] = _dedup([_norm(x) for x in (stage1.get("flows") or [])], 8)
    if not sec.get("roles"):
        sec["roles"] = _dedup([_norm(x) for x in (stage1.get("modules") or [])], 6)
    if not sec.get("rules"):
        sec["rules"] = _dedup([_norm(x) for x in (stage1.get("business_rules") or [])], 8)
    if not sec.get("exceptions"):
        sec["exceptions"] = _dedup([_norm(x) for x in (stage1.get("exceptions") or [])], 8)

    sec["business_context"] = [x for x in sec.get("business_context", []) if not _is_broken_text(x)]
    sec["roles"] = [x for x in sec.get("roles", []) if _looks_like_role_name(_extract_role_owner(x) or x)]
    sec["main_flow"] = [
        x for x in sec.get("main_flow", [])
        if not _is_global_rule_like(x)
        and not re.search(r"(清空|规则需确认|转台不清空|盒子重启|关台或重开台)", _norm(x))
    ]
    sec["rules"] = [x for x in sec.get("rules", []) if not _is_broken_text(x)]
    sec["exceptions"] = [x for x in sec.get("exceptions", []) if not _is_broken_text(x)]

    exception_rows = _extract_exception_rows(sec.get("exceptions", []) + _pick(clean_sentences, kw.get("exceptions", []), 12))
    module_summaries = [{"name": m, "summary": ""} for m in _dedup([_norm(x) for x in (stage1.get("modules") or [])], 12)]
    for m in module_summaries:
        name = _norm(m.get("name"))
        related = [s for s in clean_sentences if name and name in s][:2]
        m["summary"] = "；".join(related) if related else "未提取到明确摘要"

    groups = [
        ("business_context", sec.get("business_context")),
        ("roles", sec.get("roles")),
        ("main_flow", sec.get("main_flow")),
        ("rules", sec.get("rules")),
        ("exceptions", sec.get("exceptions")),
        ("interfaces", sec.get("interfaces")),
        ("implementation", sec.get("implementation")),
        ("pending", sec.get("pending")),
    ]
    hit = sum(1 for _, v in groups if v)
    total = len(groups)
    ratio = round(hit / float(total), 3) if total else 0.0
    level = "low"
    if ratio >= 0.75:
        level = "high"
    elif ratio >= 0.45:
        level = "medium"
    missing_sections = [k for k, v in groups if not v]
    clean_ratio = round((len(clean_sentences) / float(len(sentences))), 3) if sentences else 0.0
    readability_score = round(min(1.0, ratio * 0.75 + clean_ratio * 0.25), 3)
    alignment_digest = _build_alignment_digest(sec, module_summaries)
    role_candidates = _collect_role_candidates(sec, stage1 if isinstance(stage1, dict) else {}, clean_sentences)
    role_duty_table = _build_role_duty_table(sec, stage1 if isinstance(stage1, dict) else {}, clean_sentences)
    flow_step_table = _build_flow_step_table(sec, role_duty_table, role_candidates)
    return {
        "business_summary": sec.get("business_context", [])[:4],
        "roles": sec.get("roles", [])[:8],
        "main_flow": sec.get("main_flow", [])[:10],
        "rules_summary": sec.get("rules", [])[:12],
        "exceptions_summary": sec.get("exceptions", [])[:10],
        "exception_table": exception_rows,
        "interfaces": sec.get("interfaces", [])[:8],
        "implementation": sec.get("implementation", [])[:8],
        "pending_list": sec.get("pending", [])[:8],
        "modules": module_summaries[:12],
        "alignment_digest": alignment_digest,
        "role_duty_table": role_duty_table,
        "flow_step_table": flow_step_table,
        "missing_sections": missing_sections,
        "quality_signals": {
            "sentence_count": len(sentences),
            "clean_sentence_count": len(clean_sentences),
            "noisy_sentence_count": noisy_count,
            "clean_ratio": clean_ratio,
            "readability_score": readability_score,
        },
        "coverage": {"hit": hit, "total": total, "ratio": ratio, "level": level},
        "rule_version": "section_classifier_v1",
        "mode": "rule_only",
    }
