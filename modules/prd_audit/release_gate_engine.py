# -*- coding: utf-8 -*-
import json
import os
from typing import Any, Dict, List


STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))
RELEASE_GATE_CONFIG_FILE = os.path.join(STORAGE_DIR, "release_gate_config.json")
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "p0_block_threshold": 1,
    "platform_risk_block_threshold": 7,
    "quality_review_threshold": 70,
    "p0_penalty": 8,
    "platform_risk_penalty_start": 3,
    "platform_risk_penalty_per_item": 2,
}


def _load_thresholds() -> Dict[str, float]:
    out = dict(DEFAULT_THRESHOLDS)
    try:
        if not os.path.exists(RELEASE_GATE_CONFIG_FILE):
            return out
        with open(RELEASE_GATE_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return out
        for k in out.keys():
            if k in data:
                try:
                    out[k] = float(data[k])
                except (TypeError, ValueError):
                    pass
    except Exception:
        return out
    return out


def _count_p0(defects: List[Dict[str, Any]]) -> int:
    c = 0
    for d in defects or []:
        lv = str((d or {}).get("risk_level") or "").upper()
        if lv == "P0":
            c += 1
    return c


def _collect_platform_risks(platform_impact: Dict[str, Any]) -> int:
    impacts = platform_impact.get("platform_impacts") if isinstance(platform_impact, dict) else []
    if not isinstance(impacts, list):
        return 0
    count = 0
    for it in impacts:
        count += int((it or {}).get("risk_count") or 0)
    return count


def run_release_gate(
    stage2_output: Dict[str, Any],
    platform_impact: Dict[str, Any],
    prd_quality: Dict[str, Any],
) -> Dict[str, Any]:
    thresholds = _load_thresholds()
    s2 = stage2_output if isinstance(stage2_output, dict) else {}
    defects = s2.get("defects") if isinstance(s2.get("defects"), list) else []
    if not isinstance(defects, list):
        defects = []
    p0_count = _count_p0(defects)
    platform_risk_count = _collect_platform_risks(platform_impact if isinstance(platform_impact, dict) else {})
    quality_score = float((prd_quality or {}).get("overall_score") or 0.0)

    reasons: List[str] = []
    must_fix: List[str] = []
    decision = "PASS"

    if p0_count >= int(thresholds.get("p0_block_threshold", 1)):
        decision = "BLOCK"
        reasons.append("存在 P0 风险项 {} 个".format(p0_count))
        must_fix.append("必须清零 P0 风险后再进入开发")
    if platform_risk_count >= int(thresholds.get("platform_risk_block_threshold", 7)):
        decision = "BLOCK"
        reasons.append("主板兼容风险较高（{} 项）".format(platform_risk_count))
        must_fix.append("补充主板差异策略与专项回归")
    if decision != "BLOCK" and quality_score < float(thresholds.get("quality_review_threshold", 70)):
        decision = "REVIEW"
        reasons.append("PRD 质量分 {:.1f} 低于阈值".format(quality_score))
        must_fix.append("补齐流程、状态、异常定义后复审")

    if not reasons:
        reasons.append("关键风险可控，可进入开发阶段")
    if not must_fix:
        must_fix.append("保持关键链路回归用例覆盖")

    p0_penalty = float(thresholds.get("p0_penalty", 8))
    platform_penalty_start = int(thresholds.get("platform_risk_penalty_start", 3))
    platform_penalty_unit = float(thresholds.get("platform_risk_penalty_per_item", 2))
    score = max(0.0, min(100.0, quality_score - p0_count * p0_penalty - max(0, platform_risk_count - platform_penalty_start) * platform_penalty_unit))
    return {
        "score": round(score, 1),
        "decision": decision,
        "reasons": reasons[:4],
        "must_fix": must_fix[:4],
        "thresholds": thresholds,
        "signals": {
            "p0_count": p0_count,
            "platform_risk_count": platform_risk_count,
            "quality_score": round(quality_score, 1),
        },
    }
