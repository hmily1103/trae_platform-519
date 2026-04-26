# -*- coding: utf-8 -*-
import json
import os
from typing import Any, Dict, List

from .rules_engine import RULES


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _profile_file_path() -> str:
    return os.path.join(os.path.dirname(__file__), "rule_plugins.json")


def _knowledge_cards_path() -> str:
    return os.path.join(os.path.dirname(__file__), "knowledge_cards.json")


def _usage_history_path() -> str:
    return os.path.join(os.path.dirname(__file__), "plugin_usage_history.json")


def _default_profiles() -> List[Dict[str, Any]]:
    return [
        {
            "plugin_id": "generic_universal",
            "name": "通用规则集",
            "description": "适用于大多数通用PRD场景的基础规则组合",
            "match": {"system_types": ["general", "state_machine"], "keywords": [], "modules": []},
            "keyword_pack": "generic",
            "enabled_rule_ids": [],
            "disabled_rule_ids": [],
        },
        {
            "plugin_id": "scheduling_strict",
            "name": "调度规则集",
            "description": "面向状态调度、优先级抢占、打断恢复类PRD的增强规则组合",
            "match": {
                "system_types": ["scheduling_system"],
                "keywords": ["投屏", "广告", "打断", "恢复", "优先级", "并发", "抢占"],
                "modules": ["投屏", "游戏", "广告"],
            },
            "keyword_pack": "scheduling",
            "enabled_rule_ids": [],
            "disabled_rule_ids": [],
        },
    ]


def load_rule_plugin_profiles() -> List[Dict[str, Any]]:
    fp = _profile_file_path()
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            profiles = data.get("profiles") if isinstance(data, dict) else []
            if isinstance(profiles, list) and profiles:
                return profiles
    except Exception:
        pass
    return _default_profiles()


def save_rule_plugin_profiles(profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = [p for p in (profiles or []) if isinstance(p, dict) and _norm(p.get("plugin_id"))]
    if not normalized:
        normalized = _default_profiles()
    fp = _profile_file_path()
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"profiles": normalized}, f, ensure_ascii=False, indent=2)
    return normalized


def _load_knowledge_cards() -> List[Dict[str, Any]]:
    fp = _knowledge_cards_path()
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else []
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
    except Exception:
        pass
    return []


def _profile_match_score(profile: Dict[str, Any], content: str, stage1: Dict[str, Any], system_type: str) -> Dict[str, Any]:
    p = profile if isinstance(profile, dict) else {}
    m = p.get("match") if isinstance(p.get("match"), dict) else {}
    score = 0
    matched: List[str] = []

    system_types = [_norm(x) for x in (m.get("system_types") or []) if _norm(x)]
    if not system_types or _norm(system_type) in system_types:
        score += 3
        matched.append(f"system_type:{_norm(system_type) or 'general'}")

    text = _norm(content)
    keywords = [_norm(x) for x in (m.get("keywords") or []) if _norm(x)]
    for k in keywords:
        if k and k in text:
            score += 1
            matched.append(f"keyword:{k}")

    modules = {_norm(x) for x in (stage1.get("modules") or []) if _norm(x)}
    expected_modules = [_norm(x) for x in (m.get("modules") or []) if _norm(x)]
    for em in expected_modules:
        if em in modules:
            score += 1
            matched.append(f"module:{em}")
    return {"score": score, "matched_terms": matched[:12]}


def resolve_rule_plugin(content: str, stage1: Dict[str, Any], system_type: str) -> Dict[str, Any]:
    profiles = load_rule_plugin_profiles()
    all_rule_ids = [str(r.rule_id) for r in RULES]
    cards = _load_knowledge_cards()
    card_hits = []
    text = _norm(content)
    for c in cards[:200]:
        trigger = _norm(c.get("trigger"))
        name = _norm(c.get("name")) or _norm(c.get("capability_id"))
        domain = _norm(c.get("domain"))
        if trigger and trigger in text:
            card_hits.append({"name": name, "domain": domain, "trigger": trigger})
    best = None
    best_score = -1
    for p in profiles:
        m = _profile_match_score(p, content, stage1 if isinstance(stage1, dict) else {}, system_type)
        profile_id = _norm((p or {}).get("plugin_id"))
        if profile_id == "scheduling_strict":
            m["score"] += min(3, len([x for x in card_hits if "ktv" in _norm(x.get("domain")).lower() or "点歌" in _norm(x.get("domain"))]))
        if profile_id == "generic_universal" and not card_hits:
            m["score"] += 1
        if m["score"] > best_score:
            best_score = m["score"]
            best = {"profile": p, "score": m["score"], "matched_terms": m["matched_terms"]}
    chosen = (best or {}).get("profile") if isinstance(best, dict) else None
    if not isinstance(chosen, dict):
        chosen = _default_profiles()[0]
        best = {"score": 0, "matched_terms": []}

    enabled_rule_ids = [_norm(x) for x in (chosen.get("enabled_rule_ids") or []) if _norm(x)]
    disabled_rule_ids = {_norm(x) for x in (chosen.get("disabled_rule_ids") or []) if _norm(x)}
    if enabled_rule_ids:
        final_rule_ids = [rid for rid in enabled_rule_ids if rid in all_rule_ids and rid not in disabled_rule_ids]
    else:
        final_rule_ids = [rid for rid in all_rule_ids if rid not in disabled_rule_ids]
    enabled_map = {rid: (rid in final_rule_ids) for rid in all_rule_ids}
    return {
        "plugin_id": _norm(chosen.get("plugin_id")) or "generic_universal",
        "name": _norm(chosen.get("name")) or "通用规则集",
        "description": _norm(chosen.get("description")) or "",
        "keyword_pack": _norm(chosen.get("keyword_pack")) or "",
        "match_score": int((best or {}).get("score") or 0),
        "matched_terms": (best or {}).get("matched_terms") or [],
        "enabled_rule_ids": final_rule_ids,
        "enabled_rule_count": len(final_rule_ids),
        "total_rule_count": len(all_rule_ids),
        "knowledge_hits": card_hits[:8],
        "enabled_map": enabled_map,
    }


def append_plugin_usage(record: Dict[str, Any]) -> None:
    fp = _usage_history_path()
    payload = {"items": []}
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and isinstance(old.get("items"), list):
                payload = old
    except Exception:
        payload = {"items": []}
    item = record if isinstance(record, dict) else {}
    payload["items"].append(item)
    payload["items"] = payload["items"][-2000:]
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def get_plugin_usage_stats(limit: int = 500) -> Dict[str, Any]:
    fp = _usage_history_path()
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
    by_plugin: Dict[str, int] = {}
    for it in items:
        pid = _norm(it.get("plugin_id")) or "unknown"
        by_plugin[pid] = by_plugin.get(pid, 0) + 1
    top = sorted(by_plugin.items(), key=lambda x: x[1], reverse=True)
    return {
        "total_runs": len(items),
        "plugin_hits": [{"plugin_id": k, "count": v} for k, v in top],
        "sample": items[-20:],
    }
