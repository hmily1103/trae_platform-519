# -*- coding: utf-8 -*-
import argparse
import json
import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from modules.prd_audit.audit_learning import build_rule_draft_from_snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description="从 PRD 审计历史样本生成规则候选与 v2 草案")
    parser.add_argument("--min-count", type=int, default=2, help="候选规则最小命中次数")
    parser.add_argument("--max-new-rules", type=int, default=30, help="草案最多追加自动候选规则数")
    args = parser.parse_args()
    result = build_rule_draft_from_snapshots(min_count=args.min_count, max_new_rules=args.max_new_rules)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
