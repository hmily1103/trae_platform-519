# -*- coding: utf-8 -*-
import json
import os
from typing import Any, Dict, List


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _path() -> str:
    return os.path.join(os.path.dirname(__file__), "prompt_center.json")


def _history_path() -> str:
    return os.path.join(os.path.dirname(__file__), "prompt_eval_history.json")


def _default_data() -> Dict[str, Any]:
    return {
        "default_profile": "prd_audit_default",
        "profiles": [{
            "profile_id": "prd_audit_default",
            "name": "默认审计提示词集",
            "version": "v1.0.0",
            "stages": {},
            "ab_variants": [{"variant_id": "A", "weight": 100, "notes": "default"}],
            "evaluation": {"metrics": ["coverage"], "target": {"coverage": 0.9}},
        }],
    }


def load_prompt_center() -> Dict[str, Any]:
    fp = _path()
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return _default_data()


def save_prompt_center(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = data if isinstance(data, dict) else {}
    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
    profiles = [p for p in profiles if isinstance(p, dict) and _norm(p.get("profile_id"))]
    if not profiles:
        payload = _default_data()
    else:
        payload = {
            "default_profile": _norm(payload.get("default_profile")) or _norm((profiles[0] or {}).get("profile_id")),
            "profiles": profiles,
        }
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def select_prompt_profile(system_type: str, plugin_id: str = "") -> Dict[str, Any]:
    data = load_prompt_center()
    profiles = data.get("profiles") if isinstance(data.get("profiles"), list) else []
    default_id = _norm(data.get("default_profile")) or "prd_audit_default"
    chosen = next((p for p in profiles if _norm((p or {}).get("profile_id")) == default_id), None)
    if not isinstance(chosen, dict) and profiles:
        chosen = profiles[0]
    chosen = chosen if isinstance(chosen, dict) else _default_data()["profiles"][0]

    ab = chosen.get("ab_variants") if isinstance(chosen.get("ab_variants"), list) else []
    variant = "A"
    if len(ab) >= 2:
        key = f"{_norm(system_type)}|{_norm(plugin_id)}"
        variant = "A" if (sum(ord(c) for c in key) % 100) < int((ab[0] or {}).get("weight") or 50) else "B"
    return {
        "profile_id": _norm(chosen.get("profile_id")) or "prd_audit_default",
        "name": _norm(chosen.get("name")) or "默认审计提示词集",
        "version": _norm(chosen.get("version")) or "v1.0.0",
        "variant": variant,
        "stages": chosen.get("stages") if isinstance(chosen.get("stages"), dict) else {},
        "evaluation_target": (chosen.get("evaluation") or {}).get("target") if isinstance(chosen.get("evaluation"), dict) else {},
    }


def evaluate_prompt_outcome(result: Dict[str, Any], prompt_profile: Dict[str, Any]) -> Dict[str, Any]:
    r = result if isinstance(result, dict) else {}
    pp = prompt_profile if isinstance(prompt_profile, dict) else {}
    score = 0.0
    deterministic = r.get("deterministic_rules") if isinstance(r.get("deterministic_rules"), dict) else {}
    explainable = r.get("explainable_report") if isinstance(r.get("explainable_report"), dict) else {}
    strategy = r.get("strategy_report") if isinstance(r.get("strategy_report"), dict) else {}
    if deterministic.get("checks"):
        score += 0.35
    if (explainable.get("summary") or {}).get("conflict_count", 0) >= 0:
        score += 0.25
    if strategy.get("plans"):
        score += 0.25
    if r.get("state_machine"):
        score += 0.15
    return {
        "profile_id": _norm(pp.get("profile_id")),
        "variant": _norm(pp.get("variant")) or "A",
        "quality_estimate": round(score, 3),
        "passed": score >= 0.75,
    }


def append_prompt_evaluation(record: Dict[str, Any]) -> None:
    fp = _history_path()
    payload = {"items": []}
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and isinstance(old.get("items"), list):
                payload = old
    except Exception:
        payload = {"items": []}
    payload["items"].append(record if isinstance(record, dict) else {})
    payload["items"] = payload["items"][-2000:]
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def get_prompt_evaluation_stats(limit: int = 500) -> Dict[str, Any]:
    fp = _history_path()
    items: List[Dict[str, Any]] = []
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("items") if isinstance(data, dict) else []
            if isinstance(raw, list):
                items = [x for x in raw if isinstance(x, dict)]
    except Exception:
        items = []
    items = items[-max(1, min(5000, int(limit or 500))):]
    total = len(items)
    pass_count = len([x for x in items if bool(x.get("passed"))])
    avg = 0.0
    if total:
        avg = round(sum(float(x.get("quality_estimate", 0) or 0) for x in items) / total, 3)
    by_variant: Dict[str, int] = {}
    for it in items:
        v = _norm(it.get("variant")) or "A"
        by_variant[v] = by_variant.get(v, 0) + 1
    return {
        "total_runs": total,
        "pass_rate": round((pass_count / total), 3) if total else 0.0,
        "avg_quality_estimate": avg,
        "variant_hits": by_variant,
        "sample": items[-20:],
    }
