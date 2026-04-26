# -*- coding: utf-8 -*-
import re
from typing import Dict, List

from .utils import clean_text, split_lines


class RuleExtractor:
    def extract_priority_chain(self, text: str) -> List[str]:
        pattern = r"([\u4e00-\u9fa5A-Za-z0-9_]+(\s*>\s*[\u4e00-\u9fa5A-Za-z0-9_]+)+)"
        match = re.search(pattern, str(text or ""))
        if not match:
            return []
        chain = [clean_text(x) for x in match.group(1).split(">")]
        return [x for x in chain if x]

    def extract_interrupt_rules(self, text: str) -> List[str]:
        out = []
        for line in split_lines(text):
            if "打断" in line or "切断" in line or "抢占" in line:
                out.append(clean_text(line))
        return list(dict.fromkeys(out))[:12]

    def extract_resume_rules(self, text: str) -> List[str]:
        out = []
        for line in split_lines(text):
            if "恢复" in line or "继续" in line:
                out.append(clean_text(line))
        return list(dict.fromkeys(out))[:12]

    def extract_entities(self, text: str) -> List[str]:
        candidates = [
            "投屏", "游戏", "广告", "AI数字人", "明星墙", "点歌",
            "语音", "直播", "弹窗", "来电", "播放", "屏保",
        ]
        found = [c for c in candidates if c in str(text or "")]
        return list(dict.fromkeys(found))[:12]

    def build_model(self, text: str) -> Dict[str, object]:
        return {
            "priority_chain": self.extract_priority_chain(text),
            "interrupt_rules": self.extract_interrupt_rules(text),
            "resume_rules": self.extract_resume_rules(text),
            "entities": self.extract_entities(text),
        }
