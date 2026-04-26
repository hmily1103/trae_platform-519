# -*- coding: utf-8 -*-
from typing import Any, Dict, List


def _to_num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _risk_level(prob: float) -> str:
    if prob >= 0.75:
        return "高"
    if prob >= 0.5:
        return "中"
    return "低"


def _top_platform(platform_impact: Dict[str, Any]) -> Dict[str, Any]:
    arr = platform_impact.get("platform_impacts") if isinstance(platform_impact, dict) else []
    if not isinstance(arr, list) or not arr:
        return {}
    arr2 = sorted(arr, key=lambda x: (_to_num((x or {}).get("retrieval_score"), 0.0), int((x or {}).get("risk_count") or 0)), reverse=True)
    return arr2[0] if arr2 else {}


def _build_test_points_index(test_points: Dict[str, Any]) -> Dict[str, List[str]]:
    idx: Dict[str, List[str]] = {}
    modules = test_points.get("modules") if isinstance(test_points, dict) else []
    if not isinstance(modules, list):
        return idx
    for m in modules:
        if not isinstance(m, dict):
            continue
        module = str(m.get("module") or "").strip()
        if not module:
            continue
        points = m.get("points") if isinstance(m.get("points"), list) else []
        ids: List[str] = []
        for p in points:
            if isinstance(p, dict):
                pid = str(p.get("id") or "").strip()
                if pid:
                    ids.append(pid)
        idx[module] = ids[:6]
    return idx


def _find_path_for_module(module: str, dep_risk_links: List[Dict[str, Any]]) -> str:
    m = str(module or "").strip()
    if not m:
        return ""
    for r in dep_risk_links or []:
        if not isinstance(r, dict):
            continue
        s = str(r.get("source") or "")
        t = str(r.get("target") or "")
        reason = str(r.get("reason") or "")
        if m in s or m in t:
            return f"{s}→{t}（{reason}）"
    return ""


def run_risk_prediction_engine(
    stage2_output: Dict[str, Any],
    prd_quality: Dict[str, Any],
    platform_impact: Dict[str, Any],
    dependency_analysis: Dict[str, Any],
    test_points: Dict[str, Any],
) -> Dict[str, Any]:
    s2 = stage2_output if isinstance(stage2_output, dict) else {}
    q = prd_quality if isinstance(prd_quality, dict) else {}
    p = platform_impact if isinstance(platform_impact, dict) else {}
    d = dependency_analysis if isinstance(dependency_analysis, dict) else {}
    t = test_points if isinstance(test_points, dict) else {}

    defects = s2.get("defects") if isinstance(s2.get("defects"), list) else []
    p0 = sum(1 for x in defects if isinstance(x, dict) and str(x.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for x in defects if isinstance(x, dict) and str(x.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for x in defects if isinstance(x, dict) and str(x.get("risk_level") or "").upper() == "P2")

    score = _to_num(q.get("overall_score"), 0.0)
    dep_risk_links = d.get("risk_links") if isinstance(d.get("risk_links"), list) else []
    dep_count = len(dep_risk_links)
    top_p = _top_platform(p)
    plat_score = _to_num((top_p or {}).get("retrieval_score"), 0.0)
    plat_risk_count = int((top_p or {}).get("risk_count") or 0)

    tp_stats = t.get("stats") if isinstance(t.get("stats"), dict) else {}
    tp_index = _build_test_points_index(t)
    point_count = int(tp_stats.get("point_count") or 0)
    module_count = int(tp_stats.get("module_count") or 0)
    coverage_ratio = 0.0
    if module_count > 0:
        coverage_ratio = min(1.0, point_count / float(module_count * 4))

    prob = 0.18
    prob += p0 * 0.22 + p1 * 0.08 + p2 * 0.02
    prob += dep_count * 0.03
    prob += max(0.0, plat_score - 0.05) * 0.9 + min(0.16, plat_risk_count * 0.02)
    prob += max(0.0, (70.0 - score) / 200.0)
    prob -= coverage_ratio * 0.12
    if prob < 0.01:
        prob = 0.01
    if prob > 0.99:
        prob = 0.99

    evidence = []
    if p0 > 0:
        evidence.append(f"存在 {p0} 个 P0 漏洞")
    if p1 > 0:
        evidence.append(f"存在 {p1} 个 P1 漏洞")
    if dep_count > 0:
        evidence.append(f"依赖风险链路 {dep_count} 条")
    if top_p:
        evidence.append(f"平台风险最高: {top_p.get('platform') or '-'}")
    if score > 0:
        evidence.append(f"PRD质量总分 {score}")
    if coverage_ratio > 0:
        evidence.append(f"测试点覆盖比 {round(coverage_ratio*100,1)}%")
    if not evidence:
        evidence.append("当前证据较少，建议补充结构化输入")

    key_risks: List[Dict[str, Any]] = []
    for dft in defects[:8]:
        if not isinstance(dft, dict):
            continue
        lv = str(dft.get("risk_level") or "P2").upper()
        base = 0.35 if lv == "P0" else (0.2 if lv == "P1" else 0.1)
        rprob = min(0.99, max(0.05, prob * 0.7 + base))
        key_risks.append(
            {
                "title": str(dft.get("type") or "风险项"),
                "module": str(dft.get("module") or "全局"),
                "risk_level": lv,
                "probability": round(rprob, 3),
                "reason": str(dft.get("description") or ""),
                "impact_path": _find_path_for_module(str(dft.get("module") or ""), dep_risk_links),
                "related_test_points": tp_index.get(str(dft.get("module") or "").strip(), [])[:4],
            }
        )

    if not key_risks and dep_risk_links:
        for r in dep_risk_links[:5]:
            if not isinstance(r, dict):
                continue
            rr = _to_num(r.get("strength"), 0.5)
            key_risks.append(
                {
                    "title": f"{r.get('source') or '-'}→{r.get('target') or '-'}",
                    "module": str(r.get("source") or "全局"),
                    "risk_level": "P1",
                    "probability": round(min(0.95, 0.45 + rr * 0.4), 3),
                    "reason": str(r.get("reason") or "依赖冲突"),
                    "impact_path": f"{r.get('source') or '-'}→{r.get('target') or '-'}",
                    "related_test_points": tp_index.get(str(r.get("source") or "").strip(), [])[:4],
                }
            )

    return {
        "overall_probability": round(prob, 3),
        "overall_level": _risk_level(prob),
        "evidence": evidence,
        "key_risks": key_risks[:12],
        "signals": {
            "p0": p0,
            "p1": p1,
            "p2": p2,
            "dependency_risk_count": dep_count,
            "platform_top_score": round(plat_score, 3),
            "quality_score": round(score, 1),
            "test_coverage_ratio": round(coverage_ratio, 3),
        },
    }
