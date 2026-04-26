# -*- coding: utf-8 -*-
import re
from datetime import datetime
from typing import Any, Dict, List


def _to_list(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _uniq(seq: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in seq:
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _clean_module_name(s: str) -> str:
    t = str(s or "").strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"^[#\-\*\d\.\)\(一二三四五六七八九十、\s]+", "", t)
    return t[:20]


def _priority_max(a: str, b: str) -> str:
    order = {"P0": 3, "P1": 2, "P2": 1}
    aa = str(a or "P2").upper()
    bb = str(b or "P2").upper()
    return aa if order.get(aa, 0) >= order.get(bb, 0) else bb


def _priority_rank(p: str) -> int:
    return {"P0": 3, "P1": 2, "P2": 1}.get(str(p or "P2").upper(), 1)


def _scenario_type(raw: str) -> str:
    s = str(raw or "")
    if "异常" in s:
        return "abnormal"
    if "边界" in s:
        return "boundary"
    if "并发" in s:
        return "concurrency"
    if "中断" in s or "恢复" in s:
        return "recovery"
    if "权限" in s:
        return "abnormal"
    return "normal"


def _automation_level(priority: str, scenario: str) -> str:
    p = str(priority or "P2").upper()
    if p == "P0":
        return "smoke"
    if p == "P1" or scenario in {"abnormal", "concurrency", "recovery"}:
        return "regression"
    return "full"


def _default_expected(scenario: str) -> str:
    if scenario == "abnormal":
        return "出现异常时有明确错误提示与可执行恢复路径。"
    if scenario == "boundary":
        return "边界输入被正确拦截或处理，系统状态保持一致。"
    if scenario == "concurrency":
        return "并发触发时结果具备幂等与一致性，不出现竞态错乱。"
    if scenario == "recovery":
        return "中断后可恢复到可控状态，关键上下文不丢失。"
    return "主流程可闭环完成，关键输出与状态符合预期。"


def _build_test_point_matrix(
    by_module: List[Dict[str, Any]],
    defects: List[Dict[str, Any]],
) -> Dict[str, Any]:
    defect_ids_by_module: Dict[str, List[str]] = {}
    defect_priority_by_module: Dict[str, str] = {}
    for i, d in enumerate(defects, start=1):
        if not isinstance(d, dict):
            continue
        module = _clean_module_name(d.get("module") or "")
        if not module:
            continue
        did = str(d.get("id") or f"D{i:03d}")
        defect_ids_by_module.setdefault(module, []).append(did)
        defect_priority_by_module[module] = _priority_max(
            defect_priority_by_module.get(module, "P2"),
            str(d.get("risk_level") or "P2").upper(),
        )

    items: List[Dict[str, Any]] = []
    dedup = set()
    p0 = p1 = p2 = 0
    by_module_stats: Dict[str, int] = {}

    for m in by_module:
        module = str(m.get("module") or "").strip()
        points = m.get("points") if isinstance(m.get("points"), list) else []
        for idx, pt in enumerate(points, start=1):
            title = str(pt.get("title") or "").strip()
            ptype = str(pt.get("type") or "")
            scenario = _scenario_type(ptype)
            module_key = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", module)[:12]
            tp_id = str(pt.get("id") or f"TP_{module_key}_{idx:02d}")
            key = (module, scenario, title.lower())
            if key in dedup:
                continue
            dedup.add(key)
            priority = str(pt.get("priority") or defect_priority_by_module.get(module, "P2")).upper()
            if _priority_rank(priority) >= 3:
                p0 += 1
            elif _priority_rank(priority) == 2:
                p1 += 1
            else:
                p2 += 1
            by_module_stats[module] = by_module_stats.get(module, 0) + 1
            evidence = str(pt.get("evidence") or "")
            items.append(
                {
                    "tp_id": tp_id,
                    "module": module,
                    "scenario_type": scenario,
                    "title": title,
                    "preconditions": [f"{module} 已进入可执行状态"],
                    "steps": [f"执行场景：{title}"],
                    "expected": [str(pt.get("expected") or _default_expected(scenario))],
                    "priority": priority,
                    "risk_reason": evidence,
                    "trace": {
                        "defect_ids": defect_ids_by_module.get(module, [])[:8],
                        "rule_ids": [],
                        "anchors": [module, ptype],
                    },
                    "automation": {
                        "level": _automation_level(priority, scenario),
                        "pytest_template_key": scenario,
                    },
                }
            )

    return {
        "version": "v1",
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "stage1+stage2+matrix",
        "items": items,
        "stats": {
            "total": len(items),
            "p0": p0,
            "p1": p1,
            "p2": p2,
            "by_module": by_module_stats,
        },
    }


def _derive_modules(stage1: Dict[str, Any], outline_engine: Dict[str, Any], test_matrix: Dict[str, Any]) -> List[str]:
    modules: List[str] = []
    modules.extend(_to_list((stage1 or {}).get("modules")))
    if not modules and isinstance(outline_engine, dict):
        for it in outline_engine.get("outline") or []:
            if isinstance(it, dict):
                modules.append(str(it.get("title") or ""))
    if not modules and isinstance(test_matrix, dict):
        fm = test_matrix.get("function_matrix")
        if isinstance(fm, list):
            for r in fm:
                if isinstance(r, dict):
                    modules.append(str(r.get("module") or ""))
    modules = [_clean_module_name(x) for x in modules]
    modules = [m for m in modules if m and m not in ["PRD未说明", "【PRD未说明】"]]
    return _uniq(modules)[:12]


def _collect_text(stage1: Dict[str, Any], stage2: Dict[str, Any], prd_text: str) -> str:
    flows = _to_list((stage1 or {}).get("flows"))
    rules = _to_list((stage1 or {}).get("business_rules"))
    exc = _to_list((stage1 or {}).get("exceptions"))
    defects = (stage2 or {}).get("defects") if isinstance(stage2, dict) else []
    if not isinstance(defects, list):
        defects = []
    defect_text = []
    for d in defects:
        if isinstance(d, dict):
            defect_text.append(str(d.get("type") or ""))
            defect_text.append(str(d.get("description") or ""))
    return " ".join([prd_text or ""] + flows + rules + exc + defect_text)


def run_test_points_engine(
    prd_text: str,
    stage1_output: Dict[str, Any],
    stage2_output: Dict[str, Any],
    outline_engine: Dict[str, Any],
    platform_impact: Dict[str, Any],
    dependency_analysis: Dict[str, Any],
    test_matrix: Dict[str, Any],
) -> Dict[str, Any]:
    s1 = stage1_output if isinstance(stage1_output, dict) else {}
    s2 = stage2_output if isinstance(stage2_output, dict) else {}
    outline = outline_engine if isinstance(outline_engine, dict) else {}
    impact = platform_impact if isinstance(platform_impact, dict) else {}
    deps = dependency_analysis if isinstance(dependency_analysis, dict) else {}
    tm = test_matrix if isinstance(test_matrix, dict) else {}

    modules = _derive_modules(s1, outline, tm)
    all_text = _collect_text(s1, s2, prd_text).lower()

    defects = s2.get("defects") if isinstance(s2.get("defects"), list) else []
    defect_by_module: Dict[str, str] = {}
    for d in defects:
        if not isinstance(d, dict):
            continue
        m = _clean_module_name(d.get("module") or "")
        if not m:
            continue
        lv = str(d.get("risk_level") or "P2").upper()
        defect_by_module[m] = _priority_max(defect_by_module.get(m, "P2"), lv)

    impact_risks = []
    for p in impact.get("platform_impacts") or []:
        if isinstance(p, dict):
            for r in p.get("matched_risks") or []:
                if isinstance(r, dict):
                    impact_risks.append(r)

    dep_risks = deps.get("risk_links") if isinstance(deps.get("risk_links"), list) else []

    def point_id(mod: str, idx: int) -> str:
        key = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", mod)[:12]
        return f"TP_{key}_{idx:02d}"

    by_module = []
    total_points = 0
    for mod in modules:
        pts: List[Dict[str, Any]] = []
        base_priority = defect_by_module.get(mod, "P2")
        idx = 1

        pts.append(
            {
                "id": point_id(mod, idx),
                "title": f"{mod}：主流程闭环",
                "type": "正常流程",
                "priority": base_priority,
                "evidence": "来自模块识别/测试矩阵",
            }
        )
        idx += 1

        if ("打断" in all_text) or ("中断" in all_text) or ("恢复" in all_text) or ("广告" in all_text):
            pts.append(
                {
                    "id": point_id(mod, idx),
                    "title": f"{mod}：中断/恢复一致性",
                    "type": "中断恢复",
                    "priority": _priority_max(base_priority, "P1"),
                    "evidence": "PRD出现打断/恢复相关描述",
                }
            )
            idx += 1

        if ("异常" in all_text) or ("失败" in all_text) or ("超时" in all_text) or ("重试" in all_text) or ("弱网" in all_text):
            pts.append(
                {
                    "id": point_id(mod, idx),
                    "title": f"{mod}：异常/重试/提示策略",
                    "type": "异常流程",
                    "priority": _priority_max(base_priority, "P0"),
                    "evidence": "PRD出现异常/重试/弱网相关描述",
                }
            )
            idx += 1

        if ("并发" in all_text) or ("同时" in all_text) or ("抢占" in all_text) or any(mod in (str(r.get("source") or "") + str(r.get("target") or "")) for r in dep_risks if isinstance(r, dict)):
            pts.append(
                {
                    "id": point_id(mod, idx),
                    "title": f"{mod}：并发裁决/幂等性",
                    "type": "并发场景",
                    "priority": _priority_max(base_priority, "P1"),
                    "evidence": "依赖风险链路或PRD并发关键词",
                }
            )
            idx += 1

        if ("权限" in all_text) or ("鉴权" in all_text) or ("管理员" in all_text):
            pts.append(
                {
                    "id": point_id(mod, idx),
                    "title": f"{mod}：权限/越权/角色一致性",
                    "type": "权限安全",
                    "priority": _priority_max(base_priority, "P1"),
                    "evidence": "PRD出现权限/角色相关描述",
                }
            )
            idx += 1

        for r in impact_risks[:6]:
            f = str(r.get("feature") or "").strip()
            if f and (f in mod or mod in f):
                pts.append(
                    {
                        "id": point_id(mod, idx),
                        "title": f"{mod}：平台专项回归（{f}）",
                        "type": "平台兼容",
                        "priority": "P1",
                        "evidence": "平台影响分析命中",
                    }
                )
                idx += 1

        pts = pts[:10]
        total_points += len(pts)
        by_module.append({"module": mod, "points": pts})

    summary = "已生成测试点"
    if not by_module:
        summary = "未识别到模块，暂无可生成测试点"
    test_point_matrix = _build_test_point_matrix(by_module, defects)
    return {
        "summary": summary,
        "modules": by_module,
        "stats": {
            "module_count": len(by_module),
            "point_count": total_points,
            "matrix_total": (test_point_matrix.get("stats") or {}).get("total", 0),
        },
        "test_point_matrix": test_point_matrix,
    }
