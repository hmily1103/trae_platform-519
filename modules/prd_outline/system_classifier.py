# -*- coding: utf-8 -*-
from typing import Dict


class PRDClassifier:
    def __init__(self):
        self.rules = {
            "scheduling": ["优先级", "打断", "恢复", "展示", "切换", "抢占", "并发"],
            "state_machine": ["状态", "流转", "迁移", "状态机", "进入", "退出"],
            "business_flow": ["支付", "订单", "提交", "请求", "审批", "工单"],
        }

    def classify(self, text: str) -> Dict[str, object]:
        merged = str(text or "")
        score = {k: 0 for k in self.rules}
        for system_type, keywords in self.rules.items():
            for kw in keywords:
                if kw in merged:
                    score[system_type] += 2

        if ">" in merged:
            score["scheduling"] += 3
        if "状态" in merged and "->" in merged:
            score["state_machine"] += 3

        best_type = max(score, key=score.get) if score else "business_flow"
        max_score = int(score.get(best_type, 0))
        confidence = min(1.0, max_score / 12.0) if max_score > 0 else 0.0
        return {"type": best_type, "score": score, "confidence": round(confidence, 3)}
