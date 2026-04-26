# -*- coding: utf-8 -*-
from typing import Any, Dict, List

from .platform_knowledge import get_platform_knowledge_base
from .platform_retriever import build_platform_docs, retrieve_platform_risks
from .board_capability_model import iter_board_rules


def _norm_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _collect_signals(content: str, stage1_output: Dict[str, Any], defects: List[Dict[str, Any]], outline_engine: Dict[str, Any]) -> str:
    words: List[str] = []
    words.extend(_norm_list((stage1_output or {}).get("modules")))
    words.extend(_norm_list((stage1_output or {}).get("flows")))
    words.extend(_norm_list((stage1_output or {}).get("business_rules")))
    for d in defects or []:
        if isinstance(d, dict):
            words.append(str(d.get("type") or ""))
            words.append(str(d.get("description") or ""))
    for n in (outline_engine or {}).get("nodes") or []:
        if isinstance(n, dict):
            words.append(str(n.get("title") or ""))
    words.append(str(content or ""))
    return " ".join(words).lower()


def run_platform_impact_analysis(
    content: str,
    stage1_output: Dict[str, Any],
    stage2_output: Dict[str, Any],
    outline_engine: Dict[str, Any],
) -> Dict[str, Any]:
    kb = get_platform_knowledge_base()
    defects = (stage2_output or {}).get("defects") if isinstance(stage2_output, dict) else []
    if not isinstance(defects, list):
        defects = []
    signal_text = _collect_signals(content, stage1_output if isinstance(stage1_output, dict) else {}, defects, outline_engine if isinstance(outline_engine, dict) else {})

    docs = build_platform_docs(kb)
    retrieved = retrieve_platform_risks(signal_text, docs, top_k=30)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for hit in retrieved:
        p = str(hit.get("platform") or "")
        grouped.setdefault(p, []).append(hit)

    board_rules = iter_board_rules()
    for rule in board_rules:
        platform = str(rule.get("board") or "").strip()
        if not platform:
            continue
        if bool(rule.get("supported", True)):
            continue
        kws = rule.get("keywords") if isinstance(rule.get("keywords"), list) else []
        kws = [str(x).strip().lower() for x in kws if str(x).strip()]
        if not kws:
            continue
        if not any(k in signal_text for k in kws):
            continue
        grouped.setdefault(platform, []).append(
            {
                "platform": platform,
                "feature": str(rule.get("feature") or "平台能力项"),
                "risk": str(rule.get("risk") or "主板能力不支持"),
                "severity": str(rule.get("severity") or "P1"),
                "retrieval_score": 0.2,
                "evidence_terms": kws[:4],
                "source": "board_capability_model",
            }
        )

    platform_impacts: List[Dict[str, Any]] = []
    merged_platforms: List[Dict[str, Any]] = []
    seen = set()
    for item in kb:
        p = str((item or {}).get("platform") or "")
        merged_platforms.append(item)
        if p:
            seen.add(p)
    for p in grouped.keys():
        if p not in seen:
            merged_platforms.append({"platform": p, "capabilities": [], "risks": []})
            seen.add(p)

    for item in merged_platforms:
        platform = str(item.get("platform") or "")
        matched = []
        for r in grouped.get(platform, []):
            if float(r.get("retrieval_score") or 0.0) < 0.06:
                continue
            matched.append(
                {
                    "feature": str(r.get("feature") or ""),
                    "risk": str(r.get("risk") or ""),
                    "severity": str(r.get("severity") or "P2"),
                    "retrieval_score": float(r.get("retrieval_score") or 0.0),
                    "evidence_terms": r.get("evidence_terms") or [],
                    "source": str(r.get("source") or "retrieval"),
                    "suggestion": "建议增加专项回归与平台兼容验证用例",
                }
            )
        platform_score = 0.0
        if matched:
            platform_score = max([float(x.get("retrieval_score") or 0.0) for x in matched])
        platform_impacts.append(
            {
                "platform": platform,
                "matched_risks": matched,
                "risk_count": len(matched),
                "retrieval_score": round(platform_score, 4),
                "compatibility": "⚠" if platform_score >= 0.08 else ("△" if matched else "✓"),
            }
        )

    top = sorted(platform_impacts, key=lambda x: (x.get("retrieval_score", 0.0), x.get("risk_count", 0)), reverse=True)
    summary = "平台影响较低"
    if top and float(top[0].get("retrieval_score", 0.0)) >= 0.12:
        summary = f"{top[0].get('platform')} 风险最高，建议优先做兼容性验证"
    elif top and float(top[0].get("retrieval_score", 0.0)) >= 0.08:
        summary = f"{top[0].get('platform')} 存在中等风险，建议补充回归"

    matrix = []
    for pi in platform_impacts:
        features = []
        for r in pi.get("matched_risks") or []:
            features.append(str(r.get("feature") or ""))
        matrix.append({"platform": pi.get("platform"), "compatibility": pi.get("compatibility"), "features": features, "score": pi.get("retrieval_score", 0.0)})

    return {
        "summary": summary,
        "retrieval_backend": "local_tfidf_cosine+board_capability_model",
        "platform_impacts": platform_impacts,
        "compatibility_matrix": matrix,
    }
