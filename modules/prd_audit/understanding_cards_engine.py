# -*- coding: utf-8 -*-
from typing import Any, Dict, List


def _to_list(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _extract_module_names(stage1_output: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    blocks = stage1_output.get("blocks") if isinstance(stage1_output, dict) else []
    if isinstance(blocks, list):
        for b in blocks:
            t = str((b or {}).get("title") or "").strip()
            if t:
                names.append(t)
    if names:
        return names
    mods = _to_list((stage1_output or {}).get("modules"))
    return mods[:10]


def build_understanding_cards(stage1_output: Dict[str, Any], stage2_output: Dict[str, Any]) -> Dict[str, Any]:
    s1 = stage1_output if isinstance(stage1_output, dict) else {}
    s2 = stage2_output if isinstance(stage2_output, dict) else {}
    module_names = _extract_module_names(s1)
    flows = _to_list(s1.get("flows"))
    states = _to_list(s1.get("states"))
    exceptions = _to_list(s1.get("exceptions"))
    defects = s2.get("defects") if isinstance(s2.get("defects"), list) else []
    if not isinstance(defects, list):
        defects = []

    cards: List[Dict[str, Any]] = []
    for idx, m in enumerate(module_names):
        feature_id = "F{:03d}".format(idx + 1)
        risk_points: List[str] = []
        for d in defects:
            title = str((d or {}).get("title") or "")
            if title and (m[:6] in title or m in title):
                risk_points.append(title)
            if len(risk_points) >= 3:
                break
        open_questions: List[str] = []
        if not flows:
            open_questions.append("该功能尚未定义主流程与分支流程。")
        if not states:
            open_questions.append("该功能尚未定义状态机或关键状态。")
        if not exceptions:
            open_questions.append("该功能尚未定义异常处理与恢复策略。")

        cards.append(
            {
                "feature_id": feature_id,
                "feature_name": m,
                "user_goal": "用户希望通过“{}”完成目标任务。".format(m),
                "system_goal": "系统需保证“{}”在关键链路下稳定可用。".format(m),
                "core_flow": flows[:4],
                "key_states": states[:4],
                "risk_points": risk_points,
                "open_questions": open_questions[:3],
            }
        )

    return {
        "cards": cards,
        "card_count": len(cards),
        "source": "stage1_stage2_local_engine",
    }

