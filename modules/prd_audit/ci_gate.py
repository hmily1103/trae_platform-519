# -*- coding: utf-8 -*-
import argparse
import json
import os
import sys
from typing import Any, Dict

from .outline_engine import run_outline_engine


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _config() -> Dict[str, Any]:
    fp = os.path.join(os.path.dirname(__file__), "gate_config.json")
    cfg = _load_json(fp)
    return {
        "min_score": int(cfg.get("min_score", 80)),
        "block_on_p0": bool(cfg.get("block_on_p0", True)),
        "max_failed_rules": int(cfg.get("max_failed_rules", 12)),
    }


def _extract_text(args: argparse.Namespace) -> str:
    if _norm(args.prd_text):
        return _norm(args.prd_text)
    if _norm(args.prd_file):
        try:
            with open(_norm(args.prd_file), "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    if _norm(args.analysis_json):
        data = _load_json(_norm(args.analysis_json))
        return _norm(data.get("prd_text")) or _norm(data.get("content"))
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="PRD Audit CI Gate")
    parser.add_argument("--prd-file", help="PRD文本文件路径", default="")
    parser.add_argument("--prd-text", help="PRD文本内容", default="")
    parser.add_argument("--analysis-json", help="包含prd_text的json文件路径", default="")
    args = parser.parse_args()

    text = _extract_text(args)
    if not text:
        print("GATE_FAIL: 未提供可分析的PRD文本")
        return 2

    result = run_outline_engine(text, {}, {})
    dr = result.get("deterministic_rules") if isinstance(result.get("deterministic_rules"), dict) else {}
    score = int(dr.get("score", 0))
    defects = dr.get("defects") if isinstance(dr.get("defects"), list) else []
    failed_rules = int((dr.get("stats") or {}).get("failed_rules", len(defects)))
    has_p0 = any(_norm(d.get("severity")).upper() == "P0" for d in defects if isinstance(d, dict))
    cfg = _config()

    blocked = False
    reasons = []
    if score < cfg["min_score"]:
        blocked = True
        reasons.append(f"score<{cfg['min_score']} (actual={score})")
    if cfg["block_on_p0"] and has_p0:
        blocked = True
        reasons.append("has_P0_defect")
    if failed_rules > cfg["max_failed_rules"]:
        blocked = True
        reasons.append(f"failed_rules>{cfg['max_failed_rules']} (actual={failed_rules})")

    plugin = result.get("rule_plugin") if isinstance(result.get("rule_plugin"), dict) else {}
    msg = {
        "score": score,
        "failed_rules": failed_rules,
        "has_p0": has_p0,
        "plugin": _norm(plugin.get("plugin_id")) or "unknown",
        "blocked": blocked,
        "reasons": reasons,
    }
    print(json.dumps(msg, ensure_ascii=False))
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
