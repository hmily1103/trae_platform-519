# -*- coding: utf-8 -*-
import json
import os
from typing import Any, Dict, List


STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BOARD_CAPABILITY_FILE = os.path.join(STORAGE_DIR, "board_capability_model.json")


def _default_model() -> Dict[str, Any]:
    return {"boards": []}


def get_board_capability_model() -> Dict[str, Any]:
    try:
        if not os.path.exists(BOARD_CAPABILITY_FILE):
            return _default_model()
        with open(BOARD_CAPABILITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_model()
        boards = data.get("boards")
        if not isinstance(boards, list):
            data["boards"] = []
        return data
    except Exception:
        return _default_model()


def iter_board_rules() -> List[Dict[str, Any]]:
    model = get_board_capability_model()
    out: List[Dict[str, Any]] = []
    boards = model.get("boards") if isinstance(model, dict) else []
    if not isinstance(boards, list):
        return out
    for b in boards:
        if not isinstance(b, dict):
            continue
        name = str(b.get("board") or "").strip()
        limits = b.get("limits") if isinstance(b.get("limits"), dict) else {}
        feature_rules = b.get("feature_rules") if isinstance(b.get("feature_rules"), list) else []
        for fr in feature_rules:
            if not isinstance(fr, dict):
                continue
            out.append(
                {
                    "board": name,
                    "limits": limits,
                    "feature": str(fr.get("feature") or "").strip(),
                    "keywords": fr.get("keywords") if isinstance(fr.get("keywords"), list) else [],
                    "supported": bool(fr.get("supported", True)),
                    "risk": str(fr.get("risk") or "").strip(),
                    "severity": str(fr.get("severity") or "P2").strip().upper() or "P2",
                }
            )
    return out

