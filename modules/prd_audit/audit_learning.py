# -*- coding: utf-8 -*-
import json
import os
import time
import uuid
import tempfile
from typing import Any, Dict, List, Tuple


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEARNING_DIR = os.path.join(BASE_DIR, "learning_repo")
SNAPSHOT_DIR = os.path.join(LEARNING_DIR, "snapshots")
INDEX_FILE = os.path.join(LEARNING_DIR, "index.json")
RULE_CANDIDATES_FILE = os.path.join(LEARNING_DIR, "rule_candidates.json")
RULE_DRAFT_FILE = os.path.join(LEARNING_DIR, "prd_scan_rules_v2.draft.json")
RULE_APPLIED_FILE = os.path.join(LEARNING_DIR, "prd_scan_rules_v2.applied.json")
RULE_V2_FILE = os.path.join(BASE_DIR, "prd_scan_rules_v2.json")
ACTION_LOG_FILE = os.path.join(LEARNING_DIR, "learning_actions.json")
OWNER_CORRECTION_FILE = os.path.join(LEARNING_DIR, "owner_corrections.json")
ADVERSARIAL_CASES_FILE = os.path.join(LEARNING_DIR, "adversarial_cases.json")
INCIDENT_SAMPLES_FILE = os.path.join(LEARNING_DIR, "incident_samples.jsonl")
REGRESSION_RUNS_FILE = os.path.join(LEARNING_DIR, "regression_runs.json")

import threading

_INCIDENT_LOCK = threading.Lock()
_REGRESSION_LOCK = threading.Lock()


def _ensure_dirs() -> None:
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def _now() -> int:
    return int(time.time())


def _read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data: Any) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def load_adversarial_cases() -> Dict[str, Any]:
    _ensure_dirs()
    data = _read_json(ADVERSARIAL_CASES_FILE, {"items": []})
    if not isinstance(data, dict):
        data = {"items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def save_adversarial_cases(data: Dict[str, Any]) -> None:
    _ensure_dirs()
    if not isinstance(data, dict):
        data = {"items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    _write_json(ADVERSARIAL_CASES_FILE, data)


def append_incident_sample(sample: Dict[str, Any]) -> None:
    _ensure_dirs()
    if not isinstance(sample, dict):
        return
    line = json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
    os.makedirs(os.path.dirname(INCIDENT_SAMPLES_FILE), exist_ok=True)
    with _INCIDENT_LOCK:
        with open(INCIDENT_SAMPLES_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_incident_samples(limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_dirs()
    if not os.path.exists(INCIDENT_SAMPLES_FILE):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(INCIDENT_SAMPLES_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for raw in lines[-max(1, min(int(limit), 2000)) :]:
            s = (raw or "").strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
    except Exception:
        return []
    return out


def append_regression_run(run: Dict[str, Any]) -> None:
    _ensure_dirs()
    if not isinstance(run, dict):
        return
    os.makedirs(os.path.dirname(REGRESSION_RUNS_FILE), exist_ok=True)
    with _REGRESSION_LOCK:
        data = _read_json(REGRESSION_RUNS_FILE, {"runs": []})
        if not isinstance(data, dict):
            data = {"runs": []}
        runs = data.get("runs")
        if not isinstance(runs, list):
            runs = []
        runs.append(run)
        data["runs"] = runs[-5000:]
        _write_json(REGRESSION_RUNS_FILE, data)


def load_latest_regression_run() -> Dict[str, Any]:
    _ensure_dirs()
    data = _read_json(REGRESSION_RUNS_FILE, {"runs": []})
    runs = data.get("runs") if isinstance(data, dict) else []
    runs = runs if isinstance(runs, list) else []
    if not runs:
        return {}
    last = runs[-1]
    return last if isinstance(last, dict) else {}


def _risk_rank(level: str) -> int:
    lv = (level or "").upper()
    if lv == "P0":
        return 0
    if lv == "P1":
        return 1
    if lv == "P2":
        return 2
    return 9


def _category_from_defect(defect: Dict[str, Any]) -> str:
    text = " ".join([
        str(defect.get("type") or ""),
        str(defect.get("module") or ""),
        str(defect.get("description") or ""),
        str(defect.get("reason") or ""),
    ])
    if any(k in text for k in ["状态", "状态机", "跳转", "回滚", "恢复"]):
        return "STATE_MACHINE"
    if any(k in text for k in ["流程", "闭环", "打断", "优先级"]):
        return "FLOW"
    if any(k in text for k in ["权限", "越权", "安全", "风控", "隐私", "串房", "鉴权"]):
        return "PERMISSION"
    if any(k in text for k in ["并发", "冲突", "幂等", "重试", "抢占"]):
        return "CONCURRENCY"
    if any(k in text for k in ["异常", "失败", "超时", "降级", "边界", "断网"]):
        return "EXCEPTION"
    if any(k in text for k in ["字段", "数据", "一致性", "契约", "模型"]):
        return "DATA"
    if any(k in text for k in ["测试", "验收", "可测试", "口径"]):
        return "TEST"
    if any(k in text for k in ["量化", "主观", "验收标准", "可测试性缺失", "体验词汇", "性能指标缺失"]):
        return "TEST_VERIFIABILITY"
    return "TECH"


def _is_learning_noise_defect(defect: Dict[str, Any]) -> bool:
    if not isinstance(defect, dict):
        return True
    dtype = str(defect.get("type") or "").strip()
    module = str(defect.get("module") or "").strip()
    desc = str(defect.get("description") or "").strip()
    reason = str(defect.get("reason") or "").strip()
    source = str(defect.get("source") or "").strip().lower()
    if module == "扫描引擎" and dtype == "扫描异常":
        return True
    if source == "llm" and ("__llm_disabled__.json" in reason or "LLM 配置不存在" in reason):
        return True
    if source == "llm" and desc == "漏洞扫描阶段执行失败":
        return True
    return False


def _defect_source(defect: Dict[str, Any]) -> str:
    src = str((defect or {}).get("source") or "").strip().lower()
    if src in ("rule", "llm"):
        return src
    return "other"


def _lane_by_source(rule_count: int, llm_count: int) -> str:
    if rule_count > 0 and llm_count > 0:
        return "hybrid"
    if rule_count > 0:
        return "local_only"
    if llm_count > 0:
        return "llm_only"
    return "none"


def _extract_learning_meta(defects: List[Dict[str, Any]]) -> Dict[str, Any]:
    raw = {"rule": 0, "llm": 0, "other": 0}
    learning = {"rule": 0, "llm": 0, "other": 0}
    for d in defects:
        if not isinstance(d, dict):
            continue
        src = _defect_source(d)
        raw[src] = int(raw.get(src, 0)) + 1
        if _is_learning_noise_defect(d):
            continue
        learning[src] = int(learning.get(src, 0)) + 1
    lane = _lane_by_source(int(learning.get("rule", 0)), int(learning.get("llm", 0)))
    return {
        "source_raw": raw,
        "source_learning": learning,
        "lane": lane,
    }


def _has_meaningful_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float, bool)):
        return True
    if isinstance(value, list):
        return any(_has_meaningful_content(x) for x in value)
    if isinstance(value, dict):
        return any(_has_meaningful_content(x) for x in value.values())
    return bool(value)


def build_snapshot_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    stage1 = payload.get("stage1_output") if isinstance(payload.get("stage1_output"), dict) else {}
    stage2 = payload.get("stage2_output") if isinstance(payload.get("stage2_output"), dict) else {}
    extras = payload.get("extras") if isinstance(payload.get("extras"), dict) else {}
    reports = payload.get("reports") if isinstance(payload.get("reports"), dict) else {}
    learning_meta = payload.get("learning_meta") if isinstance(payload.get("learning_meta"), dict) else {}
    defects = stage2.get("defects") if isinstance(stage2.get("defects"), list) else []
    summary = extras.get("summary") if isinstance(extras.get("summary"), dict) else {}
    prd_quality = extras.get("prd_quality") if isinstance(extras.get("prd_quality"), dict) else {}
    scan_meta = stage2.get("scan_meta") if isinstance(stage2.get("scan_meta"), dict) else {}

    p0 = sum(1 for d in defects if str((d or {}).get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in defects if str((d or {}).get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in defects if str((d or {}).get("risk_level") or "").upper() == "P2")

    product_name = str(stage1.get("product_name") or "").strip()
    top_modules: List[str] = []
    for d in defects:
        if not isinstance(d, dict):
            continue
        mod = str(d.get("module") or "").strip()
        if not mod or mod == "【PRD未说明】" or mod in top_modules:
            continue
        top_modules.append(mod)
        if len(top_modules) >= 3:
            break

    first_defect = defects[0] if defects and isinstance(defects[0], dict) else {}
    first_problem = str(
        summary.get("main_problem")
        or first_defect.get("type")
        or first_defect.get("description")
        or ""
    ).strip()

    quality_score = summary.get("quality_score")
    if quality_score in (None, ""):
        quality_score = prd_quality.get("overall_score")
    quality_grade = str(prd_quality.get("grade") or "").strip()

    llm_outline = extras.get("outline_llm") if isinstance(extras.get("outline_llm"), dict) else {}
    understanding_cards = extras.get("understanding_cards") if isinstance(extras.get("understanding_cards"), dict) else {}
    card_count = understanding_cards.get("card_count")
    try:
        card_count = int(card_count) if card_count is not None else 0
    except (TypeError, ValueError):
        card_count = 0

    return {
        "product_name": product_name,
        "main_problem": first_problem,
        "quality_score": quality_score,
        "quality_grade": quality_grade,
        "top_modules": top_modules,
        "has_llm_outline": bool(llm_outline.get("ok")),
        "has_test_matrix": _has_meaningful_content(extras.get("test_matrix")) or _has_meaningful_content(extras.get("test_point_matrix")),
        "has_kg": _has_meaningful_content(extras.get("kg")),
        "has_dependency_analysis": _has_meaningful_content(extras.get("dependency_analysis")),
        "has_reader_guide": _has_meaningful_content(extras.get("reader_guide")),
        "has_risk_prediction": _has_meaningful_content(extras.get("risk_prediction")),
        "has_understanding_cards": card_count > 0 or _has_meaningful_content(understanding_cards),
        "understanding_card_count": card_count,
        "report_levels": {
            "L1": bool(str(reports.get("L1") or "").strip()),
            "L2": bool(str(reports.get("L2") or "").strip()),
            "L3": bool(str(reports.get("L3") or "").strip()),
        },
        "llm_scan_ok": scan_meta.get("llm_scan_ok"),
        "lane": learning_meta.get("lane") or "none",
        "source_rule_count": int((learning_meta.get("source_learning") or {}).get("rule", 0)),
        "source_llm_count": int((learning_meta.get("source_learning") or {}).get("llm", 0)),
        "defects_count": len(defects),
        "p0_count": p0,
        "p1_count": p1,
        "p2_count": p2,
    }


def build_snapshot_index_entry(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    preview = build_snapshot_preview(payload)
    return {
        "snapshot_id": payload.get("snapshot_id"),
        "created_at": payload.get("created_at") or 0,
        "created_at_str": payload.get("created_at_str") or "",
        "offline_mode": bool(payload.get("offline_mode")),
        "defects_count": preview.get("defects_count", 0),
        "p0_count": preview.get("p0_count", 0),
        "p1_count": preview.get("p1_count", 0),
        "p2_count": preview.get("p2_count", 0),
        "lane": preview.get("lane") or "none",
        "source_rule_count": preview.get("source_rule_count", 0),
        "source_llm_count": preview.get("source_llm_count", 0),
        "preview": preview,
    }


def _append_action_log(action: str, payload: Dict[str, Any]) -> None:
    current = _read_json(ACTION_LOG_FILE, {"items": []})
    if not isinstance(current, dict):
        current = {"items": []}
    items = current.get("items")
    if not isinstance(items, list):
        items = []
    items.append({
        "id": f"act_{_now()}_{uuid.uuid4().hex[:8]}",
        "action": str(action or "").strip() or "unknown",
        "created_at": _now(),
        "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "payload": payload if isinstance(payload, dict) else {},
    })
    current["items"] = items[-5000:]
    _write_json(ACTION_LOG_FILE, current)


def save_outline_owner_correction(
    prd_text: str,
    flow_rows: List[Dict[str, Any]],
    role_rows: List[Dict[str, Any]] = None,
    meta: Dict[str, Any] = None,
) -> Dict[str, Any]:
    _ensure_dirs()
    rows = flow_rows if isinstance(flow_rows, list) else []
    roles = role_rows if isinstance(role_rows, list) else []
    norm_rows: List[Dict[str, Any]] = []
    for r in rows[:200]:
        if not isinstance(r, dict):
            continue
        step = str(r.get("step") or "").strip()
        action = str(r.get("action") or "").strip()
        owner = str(r.get("owner") or "").strip()
        input_hint = str(r.get("input") or "").strip()
        output_hint = str(r.get("output") or "").strip()
        if not step and not action:
            continue
        norm_rows.append({
            "step": step,
            "owner": owner,
            "action": action,
            "input": input_hint,
            "output": output_hint,
        })
    norm_roles: List[Dict[str, Any]] = []
    for r in roles[:100]:
        if not isinstance(r, dict):
            continue
        role = str(r.get("role") or "").strip()
        duty = str(r.get("duty") or "").strip()
        if not role and not duty:
            continue
        norm_roles.append({"role": role, "duty": duty})
    correction_id = f"owner_fix_{_now()}_{uuid.uuid4().hex[:8]}"
    item = {
        "correction_id": correction_id,
        "created_at": _now(),
        "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "prd_text": str(prd_text or "")[:50000],
        "flow_rows": norm_rows,
        "role_rows": norm_roles,
        "meta": meta if isinstance(meta, dict) else {},
    }
    current = _read_json(OWNER_CORRECTION_FILE, {"items": []})
    if not isinstance(current, dict):
        current = {"items": []}
    items = current.get("items")
    if not isinstance(items, list):
        items = []
    items.append(item)
    current["items"] = items[-5000:]
    _write_json(OWNER_CORRECTION_FILE, current)
    _append_action_log("outline_owner_correction", {
        "correction_id": correction_id,
        "flow_row_count": len(norm_rows),
        "role_row_count": len(norm_roles),
        "meta": item.get("meta") or {},
    })
    return {
        "correction_id": correction_id,
        "flow_row_count": len(norm_rows),
        "role_row_count": len(norm_roles),
        "saved_at": item["created_at_str"],
    }


def save_audit_snapshot(
    prd_text: str,
    stage1_output: Dict[str, Any],
    stage2_output: Dict[str, Any],
    report_l3: str,
    report_l1: str = "",
    report_l2: str = "",
    extras: Dict[str, Any] = None,
    offline_mode: bool = False,
) -> str:
    _ensure_dirs()
    snapshot_id = f"audit_{_now()}_{uuid.uuid4().hex[:8]}"
    defects = stage2_output.get("defects") if isinstance(stage2_output, dict) else []
    defects = defects if isinstance(defects, list) else []
    learning_meta = _extract_learning_meta(defects)
    p0 = sum(1 for d in defects if str((d or {}).get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in defects if str((d or {}).get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in defects if str((d or {}).get("risk_level") or "").upper() == "P2")
    payload = {
        "snapshot_id": snapshot_id,
        "created_at": _now(),
        "created_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "offline_mode": bool(offline_mode),
        "prd_text": (prd_text or "")[:200000],
        "stage1_output": stage1_output or {},
        "stage2_output": stage2_output or {},
        "reports": {
            "L1": report_l1 or "",
            "L2": report_l2 or "",
            "L3": report_l3 or "",
        },
        "extras": extras or {},
        "learning_meta": learning_meta,
    }
    payload["preview"] = build_snapshot_preview(payload)
    _write_json(os.path.join(SNAPSHOT_DIR, f"{snapshot_id}.json"), payload)
    idx = _read_json(INDEX_FILE, {"snapshots": []})
    if not isinstance(idx, dict):
        idx = {"snapshots": []}
    snaps = idx.get("snapshots")
    if not isinstance(snaps, list):
        snaps = []
    snaps.append(build_snapshot_index_entry(payload))
    idx["snapshots"] = snaps[-5000:]
    _write_json(INDEX_FILE, idx)
    return snapshot_id


def load_all_snapshots(limit: int = 2000) -> List[Dict[str, Any]]:
    _ensure_dirs()
    out: List[Dict[str, Any]] = []
    files = [x for x in os.listdir(SNAPSHOT_DIR) if x.endswith(".json")]
    files.sort(reverse=True)
    for name in files[: max(0, int(limit))]:
        data = _read_json(os.path.join(SNAPSHOT_DIR, name), None)
        if isinstance(data, dict):
            out.append(data)
    return out


def _aggregate_candidates(snapshots: List[Dict[str, Any]], min_count: int = 2) -> List[Dict[str, Any]]:
    bucket: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for snap in snapshots:
        stage2 = snap.get("stage2_output") if isinstance(snap, dict) else {}
        defects = stage2.get("defects") if isinstance(stage2, dict) else []
        if not isinstance(defects, list):
            continue
        for d in defects:
            if not isinstance(d, dict):
                continue
            if _is_learning_noise_defect(d):
                continue
            dtype = str(d.get("type") or "").strip() or "规则命中"
            category = _category_from_defect(d)
            key = (category, dtype)
            item = bucket.setdefault(key, {
                "category": category,
                "rule_name": dtype,
                "count": 0,
                "risk_levels": [],
                "modules": {},
                "descriptions": [],
                "reasons": [],
                "suggestions": [],
            })
            item["count"] += 1
            lv = str(d.get("risk_level") or "P2").upper()
            item["risk_levels"].append(lv)
            mod = str(d.get("module") or "全局")
            item["modules"][mod] = int(item["modules"].get(mod, 0)) + 1
            desc = str(d.get("description") or "").strip()
            reason = str(d.get("reason") or "").strip()
            sug = str(d.get("suggestion") or "").strip()
            if desc and desc not in item["descriptions"]:
                item["descriptions"].append(desc)
            if reason and reason not in item["reasons"]:
                item["reasons"].append(reason)
            if sug and sug not in item["suggestions"]:
                item["suggestions"].append(sug)

    candidates: List[Dict[str, Any]] = []
    for v in bucket.values():
        if int(v.get("count", 0)) < int(min_count):
            continue
        risk_levels = list(v.get("risk_levels") or [])
        risk_levels.sort(key=_risk_rank)
        top_module = "全局"
        modules = v.get("modules") or {}
        if isinstance(modules, dict) and modules:
            top_module = sorted(modules.items(), key=lambda x: x[1], reverse=True)[0][0]
        severity = risk_levels[0] if risk_levels else "P2"
        weight = 10 if severity == "P0" else (7 if severity == "P1" else 5)
        candidates.append({
            "category": v.get("category") or "TECH",
            "rule_name": v.get("rule_name") or "自动候选规则",
            "severity": severity,
            "weight": weight,
            "description": (v.get("descriptions") or ["历史审计中反复出现的问题"])[0],
            "detection_logic": f"历史样本命中次数={v.get('count', 0)}，主要模块={top_module}",
            "risk_reason": (v.get("reasons") or ["该问题在历史样本中重复出现，建议上升为规则检查项"])[0],
            "suggestion": (v.get("suggestions") or ["补充可执行规则与验收标准"])[0],
            "example_fix": (v.get("suggestions") or ["补齐关键约束并给出示例流程"])[0],
            "enabled": False,
            "source_stats": {
                "count": v.get("count", 0),
                "top_module": top_module,
            },
        })
    candidates.sort(key=lambda x: (_risk_rank(str(x.get("severity") or "P2")), -int((x.get("source_stats") or {}).get("count", 0))))
    return candidates


def build_rule_draft_from_snapshots(min_count: int = 2, max_new_rules: int = 30) -> Dict[str, Any]:
    min_count = max(1, int(min_count or 1))
    max_new_rules = max(1, int(max_new_rules or 1))
    snapshots = load_all_snapshots(limit=5000)
    candidates = _aggregate_candidates(snapshots, min_count=min_count)
    _write_json(RULE_CANDIDATES_FILE, {
        "created_at": _now(),
        "snapshot_count": len(snapshots),
        "candidate_count": len(candidates),
        "candidates": candidates,
    })

    base = _read_json(RULE_V2_FILE, {"version": "2.0", "description": "draft", "categories": [], "rules": []})
    if not isinstance(base, dict):
        base = {"version": "2.0", "description": "draft", "categories": [], "rules": []}
    rules = base.get("rules")
    rules = rules if isinstance(rules, list) else []

    existing_names = set()
    for r in rules:
        if isinstance(r, dict):
            existing_names.add(f"{str(r.get('category') or '')}::{str(r.get('rule_name') or '')}".lower())

    auto_rules: List[Dict[str, Any]] = []
    idx = 1
    for c in candidates:
        k = f"{str(c.get('category') or '')}::{str(c.get('rule_name') or '')}".lower()
        if k in existing_names:
            continue
        auto_rules.append({
            "rule_id": f"AUTO_{idx:03d}",
            "category": c.get("category") or "TECH",
            "rule_name": c.get("rule_name") or "自动候选规则",
            "severity": c.get("severity") or "P2",
            "weight": int(c.get("weight") or 5),
            "description": c.get("description") or "历史样本高频问题",
            "detector": "",
            "detection_logic": c.get("detection_logic") or "基于历史样本统计生成",
            "llm_check_prompt": f"检查是否存在：{c.get('rule_name') or '该问题'}。",
            "risk_reason": c.get("risk_reason") or "历史样本中重复出现",
            "suggestion": c.get("suggestion") or "补齐规则与验收标准",
            "example_fix": c.get("example_fix") or "补充示例修复方案",
            "enabled": False,
            "auto_generated": True,
            "source_stats": c.get("source_stats") or {},
        })
        idx += 1
        if len(auto_rules) >= int(max_new_rules):
            break

    draft = dict(base)
    draft["description"] = f"{str(base.get('description') or '')}（AUTO_DRAFT, snapshot={len(snapshots)}）"
    draft["rules"] = rules + auto_rules
    _write_json(RULE_DRAFT_FILE, draft)
    _append_action_log("build_rule_draft", {
        "snapshot_count": len(snapshots),
        "candidate_count": len(candidates),
        "new_rules_count": len(auto_rules),
        "min_count": min_count,
        "max_new_rules": max_new_rules,
    })
    return {
        "snapshot_count": len(snapshots),
        "candidate_count": len(candidates),
        "new_rules_count": len(auto_rules),
        "candidates_file": RULE_CANDIDATES_FILE,
        "draft_file": RULE_DRAFT_FILE,
    }


def get_learning_status() -> Dict[str, Any]:
    idx = _read_json(INDEX_FILE, {"snapshots": []})
    snaps = idx.get("snapshots") if isinstance(idx, dict) else []
    snaps = snaps if isinstance(snaps, list) else []
    total = len(snaps)
    p0 = sum(int(s.get("p0_count", 0)) for s in snaps if isinstance(s, dict))
    p1 = sum(int(s.get("p1_count", 0)) for s in snaps if isinstance(s, dict))
    p2 = sum(int(s.get("p2_count", 0)) for s in snaps if isinstance(s, dict))
    latest = snaps[-1] if snaps else None
    return {
        "snapshot_count": total,
        "risk_count": {"p0": p0, "p1": p1, "p2": p2},
        "latest_snapshot": latest if isinstance(latest, dict) else {},
        "index_file": INDEX_FILE,
        "candidates_file": RULE_CANDIDATES_FILE,
        "draft_file": RULE_DRAFT_FILE,
        "applied_file": RULE_APPLIED_FILE,
    }


def load_rule_candidates() -> Dict[str, Any]:
    data = _read_json(RULE_CANDIDATES_FILE, {"candidates": []})
    if not isinstance(data, dict):
        data = {"candidates": []}
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        candidates = []
    data["candidates"] = candidates
    return data


def apply_selected_candidates(selected_names: List[str], max_new_rules: int = 100) -> Dict[str, Any]:
    max_new_rules = max(1, int(max_new_rules or 1))
    selected_set = set((str(x).strip() for x in (selected_names or []) if str(x).strip()))
    candidates_payload = load_rule_candidates()
    candidates = candidates_payload.get("candidates") if isinstance(candidates_payload, dict) else []
    candidates = candidates if isinstance(candidates, list) else []
    base = _read_json(RULE_V2_FILE, {"version": "2.0", "description": "applied", "categories": [], "rules": []})
    if not isinstance(base, dict):
        base = {"version": "2.0", "description": "applied", "categories": [], "rules": []}
    rules = base.get("rules")
    rules = rules if isinstance(rules, list) else []
    existing_names = set()
    for r in rules:
        if isinstance(r, dict):
            existing_names.add(f"{str(r.get('category') or '')}::{str(r.get('rule_name') or '')}".lower())

    new_rules: List[Dict[str, Any]] = []
    idx = 1
    for c in candidates:
        if not isinstance(c, dict):
            continue
        rn = str(c.get("rule_name") or "").strip()
        if not rn:
            continue
        if selected_set and rn not in selected_set:
            continue
        key = f"{str(c.get('category') or '')}::{rn}".lower()
        if key in existing_names:
            continue
        new_rules.append({
            "rule_id": f"AUTO_APPLY_{idx:03d}",
            "category": c.get("category") or "TECH",
            "rule_name": rn,
            "severity": c.get("severity") or "P2",
            "weight": int(c.get("weight") or 5),
            "description": c.get("description") or "历史样本高频问题",
            "detector": "",
            "detection_logic": c.get("detection_logic") or "基于历史样本统计生成",
            "llm_check_prompt": f"检查是否存在：{rn}。",
            "risk_reason": c.get("risk_reason") or "历史样本中重复出现",
            "suggestion": c.get("suggestion") or "补齐规则与验收标准",
            "example_fix": c.get("example_fix") or "补充示例修复方案",
            "enabled": False,
            "auto_generated": True,
            "source_stats": c.get("source_stats") or {},
        })
        idx += 1
        if len(new_rules) >= int(max_new_rules):
            break

    applied = dict(base)
    applied["description"] = f"{str(base.get('description') or '')}（AUTO_APPLIED, selected={len(selected_set) if selected_set else 'all'}）"
    applied["rules"] = rules + new_rules
    _write_json(RULE_APPLIED_FILE, applied)
    _append_action_log("apply_candidates", {
        "selected_count": len(selected_set),
        "applied_new_rules": len(new_rules),
        "max_new_rules": max_new_rules,
    })
    return {
        "selected_count": len(selected_set),
        "applied_new_rules": len(new_rules),
        "applied_file": RULE_APPLIED_FILE,
    }


def publish_applied_rules(create_backup: bool = True) -> Dict[str, Any]:
    if not os.path.exists(RULE_APPLIED_FILE):
        raise FileNotFoundError("可应用规则文件不存在，请先执行 apply_candidates")
    applied = _read_json(RULE_APPLIED_FILE, None)
    if not isinstance(applied, dict):
        raise ValueError("可应用规则文件格式不正确")
    rules = applied.get("rules")
    if not isinstance(rules, list):
        raise ValueError("可应用规则文件缺少 rules 列表")
    backup_file = ""
    old_count = 0
    if os.path.exists(RULE_V2_FILE):
        current = _read_json(RULE_V2_FILE, {})
        current_rules = current.get("rules") if isinstance(current, dict) else []
        if isinstance(current_rules, list):
            old_count = len(current_rules)
        if create_backup:
            backup_file = os.path.join(LEARNING_DIR, f"prd_scan_rules_v2.backup.{_now()}.json")
            _write_json(backup_file, current if isinstance(current, dict) else {})
    _write_json(RULE_V2_FILE, applied)
    _append_action_log("publish_applied", {
        "create_backup": bool(create_backup),
        "backup_file": backup_file,
        "old_rule_count": old_count,
        "new_rule_count": len(rules),
    })
    return {
        "published_file": RULE_V2_FILE,
        "backup_file": backup_file,
        "old_rule_count": old_count,
        "new_rule_count": len(rules),
    }


def list_rule_backups(limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_dirs()
    files = [x for x in os.listdir(LEARNING_DIR) if x.startswith("prd_scan_rules_v2.backup.") and x.endswith(".json")]
    files.sort(reverse=True)
    out: List[Dict[str, Any]] = []
    for name in files:
        path = os.path.join(LEARNING_DIR, name)
        data = _read_json(path, {})
        rules = data.get("rules") if isinstance(data, dict) else []
        if not isinstance(rules, list):
            continue
        out.append({
            "file_name": name,
            "file_path": path,
            "rule_count": len(rules),
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


def rollback_rules_from_backup(backup_file_name: str, create_backup: bool = True) -> Dict[str, Any]:
    name = str(backup_file_name or "").strip()
    if not name or name.find("..") >= 0 or name.find("/") >= 0 or name.find("\\") >= 0:
        raise ValueError("backup_file_name 非法")
    backup_path = os.path.join(LEARNING_DIR, name)
    if not os.path.exists(backup_path):
        raise FileNotFoundError("备份文件不存在")
    backup_data = _read_json(backup_path, None)
    if not isinstance(backup_data, dict):
        raise ValueError("备份文件格式不正确")
    backup_rules = backup_data.get("rules")
    if not isinstance(backup_rules, list):
        raise ValueError("备份文件缺少 rules 列表")
    current = _read_json(RULE_V2_FILE, {"rules": []})
    current_rules = current.get("rules") if isinstance(current, dict) else []
    old_count = len(current_rules) if isinstance(current_rules, list) else 0
    current_backup_file = ""
    if create_backup and os.path.exists(RULE_V2_FILE):
        current_backup_file = os.path.join(LEARNING_DIR, f"prd_scan_rules_v2.backup.rollback.{_now()}.json")
        _write_json(current_backup_file, current if isinstance(current, dict) else {})
    _write_json(RULE_V2_FILE, backup_data)
    _append_action_log("rollback_backup", {
        "backup_file_name": name,
        "create_backup": bool(create_backup),
        "current_backup_file": current_backup_file,
        "old_rule_count": old_count,
        "new_rule_count": len(backup_rules),
    })
    return {
        "rolled_back_from": backup_path,
        "current_backup_file": current_backup_file,
        "old_rule_count": old_count,
        "new_rule_count": len(backup_rules),
        "published_file": RULE_V2_FILE,
    }


def get_learning_lane_stats(limit: int = 5000) -> Dict[str, Any]:
    snapshots = load_all_snapshots(limit=max(1, int(limit or 1)))
    lanes = {
        "local_only": {"count": 0, "p0": 0, "p1": 0, "p2": 0},
        "llm_only": {"count": 0, "p0": 0, "p1": 0, "p2": 0},
        "hybrid": {"count": 0, "p0": 0, "p1": 0, "p2": 0},
        "none": {"count": 0, "p0": 0, "p1": 0, "p2": 0},
    }
    source_totals = {"rule": 0, "llm": 0, "other": 0}
    offline_count = 0
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        if bool(snap.get("offline_mode")):
            offline_count += 1
        stage2 = snap.get("stage2_output") if isinstance(snap.get("stage2_output"), dict) else {}
        defects = stage2.get("defects") if isinstance(stage2, dict) else []
        defects = defects if isinstance(defects, list) else []
        meta = snap.get("learning_meta") if isinstance(snap.get("learning_meta"), dict) else _extract_learning_meta(defects)
        lane = str(meta.get("lane") or "none")
        if lane not in lanes:
            lane = "none"
        lane_info = lanes[lane]
        lane_info["count"] = int(lane_info.get("count", 0)) + 1
        p0 = 0
        p1 = 0
        p2 = 0
        for d in defects:
            if not isinstance(d, dict):
                continue
            if _is_learning_noise_defect(d):
                continue
            lv = str(d.get("risk_level") or "").upper()
            if lv == "P0":
                p0 += 1
            elif lv == "P1":
                p1 += 1
            elif lv == "P2":
                p2 += 1
        lane_info["p0"] = int(lane_info.get("p0", 0)) + p0
        lane_info["p1"] = int(lane_info.get("p1", 0)) + p1
        lane_info["p2"] = int(lane_info.get("p2", 0)) + p2
        src = meta.get("source_learning") if isinstance(meta.get("source_learning"), dict) else {}
        source_totals["rule"] += int(src.get("rule", 0) or 0)
        source_totals["llm"] += int(src.get("llm", 0) or 0)
        source_totals["other"] += int(src.get("other", 0) or 0)
    return {
        "snapshot_count": len(snapshots),
        "offline_count": offline_count,
        "lanes": lanes,
        "source_totals": source_totals,
    }


def get_learning_quality_dashboard(limit: int = 5000) -> Dict[str, Any]:
    lane_stats = get_learning_lane_stats(limit=limit)
    snapshots = load_all_snapshots(limit=max(1, int(limit or 1)))
    candidates_payload = load_rule_candidates()
    candidates = candidates_payload.get("candidates") if isinstance(candidates_payload, dict) else []
    candidates = candidates if isinstance(candidates, list) else []
    applied_data = _read_json(RULE_APPLIED_FILE, {"rules": []})
    applied_rules = applied_data.get("rules") if isinstance(applied_data, dict) else []
    applied_rules = applied_rules if isinstance(applied_rules, list) else []
    published_data = _read_json(RULE_V2_FILE, {"rules": []})
    published_rules = published_data.get("rules") if isinstance(published_data, dict) else []
    published_rules = published_rules if isinstance(published_rules, list) else []
    action_data = _read_json(ACTION_LOG_FILE, {"items": []})
    actions = action_data.get("items") if isinstance(action_data, dict) else []
    actions = actions if isinstance(actions, list) else []
    op = {"build_rule_draft": 0, "apply_candidates": 0, "publish_applied": 0, "rollback_backup": 0}
    for a in actions:
        if not isinstance(a, dict):
            continue
        k = str(a.get("action") or "")
        if k in op:
            op[k] += 1
    applied_auto_count = sum(1 for r in applied_rules if isinstance(r, dict) and bool(r.get("auto_generated")))
    published_auto_count = sum(1 for r in published_rules if isinstance(r, dict) and bool(r.get("auto_generated")))
    candidate_count = len(candidates)
    backups = list_rule_backups(limit=10000)
    defect_bucket: Dict[str, Dict[str, Any]] = {}
    for snap in snapshots:
        stage2 = snap.get("stage2_output") if isinstance(snap, dict) else {}
        defects = stage2.get("defects") if isinstance(stage2, dict) else []
        if not isinstance(defects, list):
            continue
        for d in defects:
            if not isinstance(d, dict):
                continue
            if _is_learning_noise_defect(d):
                continue
            dtype = str(d.get("type") or "").strip() or "规则命中"
            src = _defect_source(d)
            item = defect_bucket.setdefault(dtype, {"type": dtype, "total": 0, "rule": 0, "llm": 0, "other": 0})
            item["total"] = int(item.get("total", 0)) + 1
            item[src] = int(item.get(src, 0)) + 1
    top_defects = sorted(defect_bucket.values(), key=lambda x: int(x.get("total", 0)), reverse=True)[:10]
    adoption_rate = 0.0 if candidate_count <= 0 else round(float(published_auto_count) / float(candidate_count), 4)
    apply_rate = 0.0 if candidate_count <= 0 else round(float(applied_auto_count) / float(candidate_count), 4)
    publish_rate = 0.0 if applied_auto_count <= 0 else round(float(published_auto_count) / float(applied_auto_count), 4)
    rollback_rate = 0.0 if op["publish_applied"] <= 0 else round(float(op["rollback_backup"]) / float(op["publish_applied"]), 4)
    return {
        "lane_stats": lane_stats,
        "kpis": {
            "snapshot_count": lane_stats.get("snapshot_count", 0),
            "candidate_count": candidate_count,
            "applied_auto_rule_count": applied_auto_count,
            "published_auto_rule_count": published_auto_count,
            "backup_count": len(backups),
            "build_count": op["build_rule_draft"],
            "apply_count": op["apply_candidates"],
            "publish_count": op["publish_applied"],
            "rollback_count": op["rollback_backup"],
            "apply_rate": apply_rate,
            "adoption_rate": adoption_rate,
            "publish_rate": publish_rate,
            "rollback_rate": rollback_rate,
        },
        "top_defect_types": top_defects,
        "recent_actions": actions[-20:],
        "files": {
            "action_log_file": ACTION_LOG_FILE,
            "candidates_file": RULE_CANDIDATES_FILE,
            "applied_file": RULE_APPLIED_FILE,
            "published_file": RULE_V2_FILE,
        },
    }
