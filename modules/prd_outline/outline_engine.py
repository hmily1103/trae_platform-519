# -*- coding: utf-8 -*-
from typing import Any, Dict

from .bug_learning import RuleWeightLearner
from .rule_extractor import RuleExtractor
from .system_classifier import PRDClassifier
from .templates import business_flow_template, scheduling_template, state_machine_template
from .utils import to_list


class OutlineEngine:
    def __init__(self):
        self.classifier = PRDClassifier()
        self.extractor = RuleExtractor()

    def generate(self, text: str, stage1: Dict[str, Any] = None, stage2: Dict[str, Any] = None) -> Dict[str, Any]:
        stage1 = stage1 if isinstance(stage1, dict) else {}
        stage2 = stage2 if isinstance(stage2, dict) else {}
        merged_text = str(text or "")
        merged_text += "\n" + "\n".join(to_list(stage1.get("flows")))
        merged_text += "\n" + "\n".join(to_list(stage1.get("business_rules")))
        merged_text += "\n" + "\n".join(to_list(stage1.get("exceptions")))

        cls = self.classifier.classify(merged_text)
        system_type = cls.get("type") or "business_flow"
        model = self.extractor.build_model(merged_text)

        if system_type == "scheduling":
            outline = scheduling_template(model, text=merged_text, stage1=stage1)
            normalized_type = "scheduling_system"
        elif system_type == "state_machine":
            outline = state_machine_template(model, text=merged_text, stage1=stage1)
            normalized_type = "state_machine"
        else:
            outline = business_flow_template(model, text=merged_text, stage1=stage1)
            normalized_type = "business_flow"

        learner = RuleWeightLearner()
        defects = stage2.get("defects") if isinstance(stage2.get("defects"), list) else []
        learner.train(defects)
        outline["key_rules"] = learner.rank_rules(outline.get("key_rules") or [])

        system_model: Dict[str, Any]
        if normalized_type == "scheduling_system":
            system_model = {
                "resource": "screen",
                "actors": model.get("entities") or to_list(stage1.get("modules"))[:8],
                "priority_order": model.get("priority_chain") or [],
                "interrupt_rules": model.get("interrupt_rules") or [],
                "resume_rules": model.get("resume_rules") or [],
            }
        elif normalized_type == "state_machine":
            system_model = {
                "states": to_list(stage1.get("states"))[:12],
                "core_flows": to_list(stage1.get("flows"))[:8],
                "rules": to_list(stage1.get("business_rules"))[:8],
            }
        else:
            system_model = {
                "modules": to_list(stage1.get("modules"))[:12],
                "core_flows": to_list(stage1.get("flows"))[:8],
                "rules": to_list(stage1.get("business_rules"))[:8],
            }

        return {
            "system_type": normalized_type,
            "classifier_score": cls.get("score") or {},
            "classifier_confidence": cls.get("confidence") or 0.0,
            "rule_model": model,
            "system_model": system_model,
            "cognitive_outline": {
                "L0": outline.get("summary") or "",
                "L1": outline.get("core_flow") or [],
                "L2": outline.get("modules") or [],
                "L3": outline.get("key_rules") or [],
            },
            "meta": {"entities": model.get("entities") or []},
        }
