# -*- coding: utf-8 -*-
import re
from typing import Any, Dict, List, Tuple


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


def _clean_node(s: str) -> str:
    t = str(s or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:24]


def _contains_pair(text: str, a: str, b: str) -> bool:
    t = str(text or "")
    return (a in t) and (b in t)


def _base_edges() -> List[Tuple[str, str, str]]:
    return [
        ("广告", "播放器", "播放依赖"),
        ("投屏", "播放器", "播放依赖"),
        ("点歌", "队列", "调度依赖"),
        ("切歌", "队列", "调度依赖"),
        ("播放器", "硬件解码", "硬件依赖"),
        ("广告", "硬件解码", "解码竞争"),
        ("投屏", "硬件解码", "解码竞争"),
        ("权限", "账号系统", "鉴权依赖"),
    ]


def run_dependency_analysis(
    content: str,
    stage1_output: Dict[str, Any],
    outline_engine: Dict[str, Any],
    platform_impact: Dict[str, Any],
) -> Dict[str, Any]:
    modules = _to_list((stage1_output or {}).get("modules"))
    flows = _to_list((stage1_output or {}).get("flows"))
    rules = _to_list((stage1_output or {}).get("business_rules"))
    outline_nodes = (outline_engine or {}).get("nodes") if isinstance(outline_engine, dict) else []
    if isinstance(outline_nodes, list):
        for n in outline_nodes[:30]:
            if isinstance(n, dict) and int(n.get("level") or 1) <= 2:
                title = str(n.get("title") or "").strip()
                if title:
                    modules.append(title)
    modules = _uniq([_clean_node(x) for x in modules if x])[:30]

    context = " ".join([str(content or "")] + flows + rules)
    impacts = (platform_impact or {}).get("platform_impacts") if isinstance(platform_impact, dict) else []
    impact_features: List[str] = []
    if isinstance(impacts, list):
        for p in impacts:
            if isinstance(p, dict):
                for r in p.get("matched_risks") or []:
                    if isinstance(r, dict):
                        f = str(r.get("feature") or "").strip()
                        if f:
                            impact_features.append(f)

    edges: List[Dict[str, Any]] = []
    for s, t, reason in _base_edges():
        if (_contains_pair(context, s, t) or s in modules or t in modules or s in impact_features or t in impact_features):
            edges.append({"source": s, "target": t, "reason": reason, "strength": 0.75})

    for i in range(len(modules)):
        for j in range(i + 1, len(modules)):
            a, b = modules[i], modules[j]
            if len(a) < 2 or len(b) < 2:
                continue
            if _contains_pair(context, a, b):
                edges.append({"source": a, "target": b, "reason": "同段共现", "strength": 0.55})

    dedup = {}
    for e in edges:
        k = (e["source"], e["target"])
        old = dedup.get(k)
        if not old or float(e.get("strength") or 0) > float(old.get("strength") or 0):
            dedup[k] = e
    edge_list = list(dedup.values())

    risk_links = [e for e in edge_list if str(e.get("reason") or "") in ["解码竞争", "同段共现"]]
    risk_links = sorted(risk_links, key=lambda x: float(x.get("strength") or 0), reverse=True)[:8]

    lines = ["flowchart LR"]
    for e in edge_list[:60]:
        a = _clean_node(e.get("source") or "")
        b = _clean_node(e.get("target") or "")
        if not a or not b:
            continue
        reason = str(e.get("reason") or "")
        lines.append(f'  {a} -->|{reason}| {b}')
    mermaid = "\n".join(lines)

    summary = "未识别到明显依赖冲突"
    if risk_links:
        s = risk_links[0]
        summary = f"重点关注 {s.get('source')} → {s.get('target')}（{s.get('reason')}）"

    return {
        "summary": summary,
        "modules": modules,
        "edges": edge_list,
        "risk_links": risk_links,
        "dependency_graph_mermaid": mermaid,
    }

