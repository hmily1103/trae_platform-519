# -*- coding: utf-8 -*-
from collections import defaultdict
from typing import Dict, List


class RuleWeightLearner:
    def __init__(self):
        self.rule_weights = defaultdict(int)
        self.keyword_to_rule_type = {
            "并发": "并发问题",
            "竞态": "并发问题",
            "优先级": "优先级错误",
            "打断": "优先级错误",
            "恢复": "恢复逻辑缺失",
            "继续": "恢复逻辑缺失",
            "异常": "异常处理缺失",
            "超时": "异常处理缺失",
            "重试": "异常处理缺失",
        }

    def train(self, bug_list: List[Dict[str, object]]):
        for bug in bug_list or []:
            if not isinstance(bug, dict):
                continue
            bt = str(bug.get("type") or bug.get("category") or "").strip()
            desc = str(bug.get("description") or bug.get("bug_desc") or "").strip()
            if bt:
                self.rule_weights[bt] += 1
            text = bt + " " + desc
            for kw, rt in self.keyword_to_rule_type.items():
                if kw in text:
                    self.rule_weights[rt] += 1

    def get_weight(self, rule_text: str) -> int:
        s = str(rule_text or "")
        w = 1
        for kw, rt in self.keyword_to_rule_type.items():
            if kw in s:
                w += int(self.rule_weights.get(rt, 0))
        return w

    def rank_rules(self, rules: List[str]) -> List[str]:
        arr = [str(x).strip() for x in (rules or []) if str(x).strip()]
        return sorted(arr, key=lambda r: self.get_weight(r), reverse=True)
