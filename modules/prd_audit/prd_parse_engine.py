# -*- coding: utf-8 -*-
"""
PRD 自动解析引擎（Phase1, Rule-first）

目标：
- 将原始 PRD 文本切块（章节/标题层级）
- 对章节做语义分类（背景/目标/角色/流程/状态/规则/数据/异常/依赖/指标）
- 从文本中抽取 Stage1 结构字段的“基础版本”（离线可用）
- 输出 parse_quality、required_elements、conflict_candidates（冲突候选）

设计原则：
- 纯增量，不依赖外部模型
- 尽量使用可解释规则；不追求完美，只追求稳定可用
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple, Optional


SECTION_TYPES = [
    "BACKGROUND",
    "GOAL",
    "ROLE",
    "FEATURE",
    "STATE",
    "FLOW",
    "RULE",
    "DATA",
    "EXCEPTION",
    "DEPENDENCY",
    "METRIC",
    "OTHER",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _split_lines(text: str) -> List[str]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return raw.split("\n")


def _is_heading(line: str) -> Optional[Tuple[int, str]]:
    """
    返回 (level, title) 或 None
    支持：
    - Markdown: # 标题 / ## 标题
    - 中文序号: 一、 / 二、 / 1. / 1、 / 1) / 1.1
    - 常见 PRD 栏目：背景/目标/用户角色/流程/规则/异常/依赖/指标
    """
    s = line.strip()
    if not s:
        return None
    # Markdown heading
    m = re.match(r"^(#{1,6})\s+(.+)$", s)
    if m:
        level = len(m.group(1))
        return level, _norm(m.group(2))

    # Numbered heading
    m = re.match(r"^((?:\d+(?:\.\d+){0,3})|[一二三四五六七八九十]+)[、\.\)]\s*(.+)$", s)
    if m and len(_norm(m.group(2))) <= 60:
        num = m.group(1)
        level = 1 + (num.count("."))
        return level, _norm(m.group(2))

    # Keyword-only heading (short)
    keywords = ["背景", "目标", "用户", "角色", "流程", "规则", "状态", "异常", "边界", "依赖", "指标", "范围", "需求", "功能"]
    if len(s) <= 20 and any(k in s for k in keywords) and not re.search(r"[。；;:：]", s):
        return 2, _norm(s)

    return None


def sectionize(prd_text: str, max_blocks: int = 80) -> Dict[str, Any]:
    """
    输出：
    {
      "blocks": [{level,title,content,range}],
      "full_text": "...",
    }
    """
    lines = _split_lines(prd_text)
    # 标准化为带行号（用于 range）
    numbered: List[Tuple[int, str]] = [(i + 1, lines[i].rstrip()) for i in range(len(lines))]

    blocks: List[Dict[str, Any]] = []
    cur = {"level": 1, "title": "正文", "start": 1, "content_lines": []}  # default block

    def flush(end_line: int):
        nonlocal cur, blocks
        content = "\n".join([x for x in cur["content_lines"] if _norm(x)])
        title = cur["title"] or "正文"
        start = int(cur["start"] or 1)
        rng = f"L{start:04d}-L{end_line:04d}" if end_line >= start else f"L{start:04d}-L{start:04d}"
        blocks.append(
            {
                "level": int(cur["level"] or 1),
                "title": title,
                "content": content.strip(),
                "range": rng,
            }
        )
        cur = {"level": 1, "title": "正文", "start": end_line + 1, "content_lines": []}

    for line_no, raw in numbered:
        h = _is_heading(raw)
        if h and len(blocks) < max_blocks:
            # flush previous
            flush(line_no - 1 if line_no > 1 else 1)
            cur = {"level": h[0], "title": h[1], "start": line_no, "content_lines": []}
        else:
            cur["content_lines"].append(raw)
    flush(len(lines) if lines else 1)

    # 去掉纯空块（但至少保留一个）
    cleaned = [b for b in blocks if (b.get("title") or "").strip() or (b.get("content") or "").strip()]
    if not cleaned:
        cleaned = [{"level": 1, "title": "正文", "content": _norm(prd_text), "range": "L0001-L0001"}]
    return {"blocks": cleaned[:max_blocks], "full_text": prd_text or ""}


def classify_block(title: str, content: str) -> str:
    t = _norm(title)
    c = _norm(content)
    text = (t + " " + c)[:400].lower()

    def has(*words: str) -> bool:
        return any(w.lower() in text for w in words)

    if has("背景", "why", "现状", "痛点"):
        return "BACKGROUND"
    if has("目标", "goals", "成功标准", "范围", "不包含"):
        return "GOAL"
    if has("角色", "用户", "权限", "rbac"):
        return "ROLE"
    if has("状态", "模式", "页面", "state"):
        return "STATE"
    if has("流程", "步骤", "step", "用例", "交互"):
        return "FLOW"
    if has("规则", "必须", "不得", "优先", "打断", "恢复", "口径"):
        return "RULE"
    if has("数据", "字段", "入参", "出参", "对象", "data"):
        return "DATA"
    if has("异常", "失败", "错误", "超时", "重试", "回滚", "边界"):
        return "EXCEPTION"
    if has("依赖", "第三方", "接口", "服务", "sdk"):
        return "DEPENDENCY"
    if has("指标", "sla", "ms", "qps", "成功率", "留存"):
        return "METRIC"
    return "OTHER"


def _extract_bullets(text: str, limit: int = 30) -> List[str]:
    out: List[str] = []
    for line in (text or "").splitlines():
        s = _norm(line)
        if not s:
            continue
        m = re.match(r"^[-*•]\s+(.+)$", s)
        if m:
            out.append(_norm(m.group(1)))
            continue
        m = re.match(r"^\d+[\.\)、]\s*(.+)$", s)
        if m:
            out.append(_norm(m.group(1)))
            continue
    # 去重
    uniq = []
    for x in out:
        if x and x not in uniq:
            uniq.append(x)
        if len(uniq) >= limit:
            break
    return uniq


def _extract_candidates_from_text(text: str, pattern: str, limit: int = 20) -> List[str]:
    s = _norm(text)
    if not s:
        return []
    found = re.findall(pattern, s)
    out = []
    for x in found:
        v = _norm(x)
        if v and v not in out:
            out.append(v)
        if len(out) >= limit:
            break
    return out


def build_stage1_base(sectionized: Dict[str, Any]) -> Dict[str, Any]:
    """
    基于 blocks 生成 Stage1 的基础结构（离线可用）。
    返回：
      stage1_partial: {background, goal, modules, user_roles, flows, states, business_rules, ...}
      source_map_partial: 与数组字段对齐的 range
      parse_quality / required_elements / conflict_candidates
    """
    blocks = sectionized.get("blocks") if isinstance(sectionized, dict) else []
    if not isinstance(blocks, list):
        blocks = []

    # 聚合容器
    product_name = ""
    background = ""
    goal = ""
    modules: List[str] = []
    user_roles: List[str] = []
    flows: List[str] = []
    states: List[str] = []
    business_rules: List[str] = []
    data_structures: List[str] = []
    permissions: List[str] = []
    exceptions: List[str] = []
    edge_cases: List[str] = []
    dependencies: List[str] = []
    nfr: List[str] = []
    success_metrics: List[str] = []

    sm = {
        "product_name": [],
        "modules": [],
        "features": [],
        "user_roles": [],
        "flows": [],
        "states": [],
        "business_rules": [],
        "data_structures": [],
        "permissions": [],
        "exceptions": [],
        "edge_cases": [],
        "dependencies": [],
        "non_functional_requirements": [],
        "success_metrics": [],
    }

    def add_arr(arr: List[str], sm_key: str, value: str, rng: str):
        v = _norm(value)
        if not v:
            return
        if v not in arr:
            arr.append(v)
            sm[sm_key].append(rng)

    for b in blocks:
        title = str(b.get("title") or "")
        content = str(b.get("content") or "")
        rng = str(b.get("range") or "【PRD未说明】")
        t = classify_block(title, content)

        # product/background/goal 取最靠前的非空
        if not product_name:
            # 粗略从文档头部标题中猜测产品名（仅作 hint，不作为强依赖）
            tt = _norm(title)
            if tt and len(tt) <= 40 and any(k in tt for k in ["PRD", "产品", "需求", "方案"]):
                name = re.sub(r"(PRD|产品需求书|需求文档|方案)$", "", tt).strip(" -：:")
                if name and 2 <= len(name) <= 30:
                    product_name = name
                    sm["product_name"].append(rng)
        if t == "BACKGROUND" and not background:
            background = _norm(content)[:600]
        if t == "GOAL" and not goal:
            goal = _norm(content)[:600]

        # roles
        if t == "ROLE":
            for x in _extract_bullets(content, limit=20):
                # 简单过滤：太长的当作描述，不当作角色名
                if len(x) <= 16 and not any(k in x for k in ["可以", "需要", "负责", "权限", "流程", "例如"]):
                    add_arr(user_roles, "user_roles", x, rng)
                if "权限" in x or "可" in x:
                    add_arr(permissions, "permissions", x, rng)

        # flows
        if t == "FLOW":
            # 标题可作为 flow 名称
            if "流程" in title and len(_norm(title)) <= 30:
                add_arr(flows, "flows", f"{_norm(title)}：{_norm(content)[:120]}", rng)
            for x in _extract_bullets(content, limit=25):
                add_arr(flows, "flows", x, rng)

        # states
        if t == "STATE":
            for x in _extract_bullets(content, limit=25):
                # 取短名作为状态候选
                name = x
                # 兼容“状态：xxx”
                if "：" in name:
                    name = name.split("：", 1)[-1]
                name = _norm(name)
                if 1 < len(name) <= 20:
                    add_arr(states, "states", name, rng)
            # 兜底：从正文里找“xxx状态/xxx模式”
            for x in _extract_candidates_from_text(content, r"([\u4e00-\u9fa5A-Za-z0-9_]{2,12})(?:状态|模式|页面)"):
                add_arr(states, "states", x, rng)

        # business rules
        if t == "RULE":
            for x in _extract_bullets(content, limit=30):
                add_arr(business_rules, "business_rules", x, rng)
            # 从正文抓取强约束句式
            for x in re.split(r"[。；;\n]", content or ""):
                s = _norm(x)
                if len(s) < 6:
                    continue
                if any(k in s for k in ["必须", "不得", "优先", "打断", "恢复", "禁止", "允许"]):
                    add_arr(business_rules, "business_rules", s[:120], rng)

        # data
        if t == "DATA":
            for x in _extract_bullets(content, limit=25):
                add_arr(data_structures, "data_structures", x, rng)

        # exception / edge
        if t == "EXCEPTION":
            for x in _extract_bullets(content, limit=30):
                add_arr(exceptions, "exceptions", x, rng)
            # 一些关键词直接塞 edge_cases
            for k in ["超时", "弱网", "断网", "重试", "回滚", "幂等", "重复提交", "失败", "错误码"]:
                if k in content:
                    add_arr(edge_cases, "edge_cases", k, rng)

        # dependency
        if t == "DEPENDENCY":
            for x in _extract_bullets(content, limit=20):
                add_arr(dependencies, "dependencies", x, rng)

        # metric
        if t == "METRIC":
            for x in _extract_bullets(content, limit=20):
                add_arr(success_metrics, "success_metrics", x, rng)

        # modules/features: 从标题里抽取（较保守）
        if t in {"FLOW", "STATE", "RULE", "DATA"}:
            tt = _norm(title)
            if tt and len(tt) <= 20 and not any(k in tt for k in ["背景", "目标", "指标", "异常", "依赖", "角色"]):
                add_arr(modules, "modules", tt.replace("流程", "").strip() or tt, rng)
        if t == "FEATURE":
            tt = _norm(title)
            if tt and len(tt) <= 30:
                add_arr(success_metrics, "success_metrics", tt, rng)
                add_arr(modules, "modules", tt, rng)

        # NFR: 性能/安全/日志等
        if any(k in (title + " " + content) for k in ["性能", "SLA", "QPS", "延迟", "监控", "日志", "安全", "风控"]):
            add_arr(nfr, "non_functional_requirements", _norm(title)[:60] or _norm(content)[:60], rng)

    # conflict candidates（基于 business_rules）
    conflict_candidates = detect_conflicts(business_rules, blocks)

    # required elements
    required_elements = evaluate_required_elements(
        user_roles=user_roles,
        states=states,
        flows=flows,
        exceptions=exceptions,
        dependencies=dependencies,
        metrics=success_metrics,
    )

    parse_quality = evaluate_parse_quality(sectionized, required_elements, conflict_candidates)

    # 兜底填充
    def nz_arr(arr: List[str]) -> List[str]:
        return arr if arr else ["【PRD未说明】"]

    return {
        "stage1": {
            "product_name": product_name or "【PRD未说明】",
            "background": background or "【PRD未说明】",
            "goal": goal or "【PRD未说明】",
            "modules": nz_arr(modules),
            "features": ["【PRD未说明】"],
            "user_roles": nz_arr(user_roles),
            "flows": nz_arr(flows),
            "states": nz_arr(states),
            "business_rules": nz_arr(business_rules),
            "data_structures": nz_arr(data_structures),
            "permissions": nz_arr(permissions),
            "exceptions": nz_arr(exceptions),
            "edge_cases": nz_arr(edge_cases),
            "dependencies": nz_arr(dependencies),
            "non_functional_requirements": nz_arr(nfr),
            "success_metrics": nz_arr(success_metrics),
            "source_map": sm,
        },
        "blocks": blocks,
        "parse_quality": parse_quality,
        "required_elements": required_elements,
        "conflict_candidates": conflict_candidates,
    }


def evaluate_required_elements(
    user_roles: List[str],
    states: List[str],
    flows: List[str],
    exceptions: List[str],
    dependencies: List[str],
    metrics: List[str],
) -> Dict[str, Any]:
    def ok(arr: List[str]) -> bool:
        return bool(arr) and not all(x == "【PRD未说明】" for x in arr)

    items = [
        ("roles", "用户角色", ok(user_roles), len(user_roles)),
        ("states", "状态机/模式", ok(states), len(states)),
        ("flows", "核心流程", ok(flows), len(flows)),
        ("exceptions", "异常与边界", ok(exceptions), len(exceptions)),
        ("dependencies", "外部依赖", ok(dependencies), len(dependencies)),
        ("success_metrics", "成功指标", ok(metrics), len(metrics)),
    ]
    out = {"items": [], "overall": 0.0}
    score = 0.0
    for key, name, present, count in items:
        s = 10.0 if present else 2.0
        if present and count <= 1 and key in {"states", "flows"}:
            s = 6.0
        out["items"].append(
            {
                "key": key,
                "name": name,
                "present": bool(present),
                "count": int(count),
                "score": round(s, 1),
                "impact": "缺失会阻断开发/验收" if not present and key in {"roles", "states", "flows"} else ("建议补充以降低风险" if not present else ""),
            }
        )
        score += s
    out["overall"] = round(score / float(len(items)), 1) if items else 0.0
    return out


def evaluate_parse_quality(sectionized: Dict[str, Any], required_elements: Dict[str, Any], conflicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    blocks = sectionized.get("blocks") if isinstance(sectionized, dict) else []
    blocks_cnt = len(blocks) if isinstance(blocks, list) else 0
    req_overall = float((required_elements or {}).get("overall") or 0.0)
    # 解析质量：章节结构(40%) + 必备要素(50%) + 冲突候选(10%, 有冲突不扣分但提示)
    s_blocks = 10.0 if blocks_cnt >= 6 else (7.0 if blocks_cnt >= 3 else 4.0)
    s_req = req_overall
    s_conf = 8.0 if conflicts else 10.0
    overall = round(0.4 * s_blocks + 0.5 * s_req + 0.1 * s_conf, 1)
    notes = []
    if blocks_cnt < 3:
        notes.append("章节结构不明显，建议使用标题/编号分段。")
    if req_overall < 6.0:
        notes.append("必备要素缺失较多，建议先补齐角色/状态/流程/异常/依赖/指标。")
    if conflicts:
        notes.append(f"发现 {len(conflicts)} 组潜在规则冲突候选，建议优先澄清口径。")
    return {"overall": overall, "blocks": blocks_cnt, "notes": notes[:6]}


def detect_conflicts(business_rules: List[str], blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    冲突候选抽取（Phase1）：用规则/关键词筛选候选对，不做语义判定。
    输出：
      [{a,b,reason,evidence,anchor}]
    """
    rules = [r for r in (business_rules or []) if r and r != "【PRD未说明】"]
    if not rules:
        return []
    cands: List[Dict[str, Any]] = []

    # 典型冲突模板：优先级最高 vs 可被打断/允许打断
    for r in rules:
        if "优先级最高" in r or ("优先" in r and "最高" in r):
            for r2 in rules:
                if r2 == r:
                    continue
                if any(k in r2 for k in ["打断", "可被打断", "允许打断", "可以打断"]) and any(k in r for k in ["广告", "投屏", "游戏", "播放"]):
                    cands.append(
                        {
                            "a": r[:140],
                            "b": r2[:140],
                            "reason": "同一对象可能同时被声明“最高优先级”与“可被打断/可抢占”，需澄清裁决口径。",
                            "evidence": "优先级/打断冲突候选",
                            "anchor": _best_anchor_for_conflict(r, r2, blocks),
                        }
                    )
                    if len(cands) >= 8:
                        return cands

    # 禁止/允许冲突
    for r in rules:
        if "禁止" in r or "不得" in r:
            key = re.sub(r"(禁止|不得)", "", r)
            for r2 in rules:
                if r2 == r:
                    continue
                if ("允许" in r2 or "可以" in r2) and any(w in r2 for w in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,8}", key)[:3]):
                    cands.append(
                        {
                            "a": r[:140],
                            "b": r2[:140],
                            "reason": "同一行为可能出现“禁止/不得”与“允许/可以”的描述，需澄清适用条件。",
                            "evidence": "禁止/允许冲突候选",
                            "anchor": _best_anchor_for_conflict(r, r2, blocks),
                        }
                    )
                    if len(cands) >= 12:
                        return cands
    return cands[:12]


def _best_anchor_for_conflict(a: str, b: str, blocks: List[Dict[str, Any]]) -> str:
    # 在 blocks 中找最相关的标题作为锚点（粗粒度）
    key_terms = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,6}", (a + " " + b)))
    best = None
    for blk in blocks or []:
        title = str(blk.get("title") or "")
        content = str(blk.get("content") or "")
        rng = str(blk.get("range") or "")
        score = 0
        for t in list(key_terms)[:12]:
            if t and (t in title or t in content):
                score += 1
        cand = (score, len(_norm(title)), rng, title)
        if best is None or cand > best:
            best = cand
    if best and best[0] > 0:
        return f"{best[3]}（{best[2]}）"
    return "【PRD未说明】"

