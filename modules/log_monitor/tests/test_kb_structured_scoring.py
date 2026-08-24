#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#27 历史案例结构化沉淀 + 质量评分 单元测试

覆盖：
1. add_case 结构化字段落库（final_root_cause/fix_action/versions/owner_module/verified）
2. 误报案例自动 resolved=False
3. 质量权重：verified 加权 / false_positive 强降权 / hit_count 加权封顶
4. search_similar 排序受质量权重影响（误报案例排在已验证案例之后）
5. 命中计数：内存实时累加 + 低频落盘（间隔内不写盘、超间隔写盘）
6. 向后兼容：旧卡片（无新字段）检索不报错
"""
import json
import os
import shutil
import tempfile
import time
import unittest

from modules.log_monitor.knowledge_base import (
    SelfHealKnowledgeBase,
)
import modules.log_monitor.knowledge_base as kb_mod


def _mk_kb(tmpdir):
    return SelfHealKnowledgeBase(file_path=os.path.join(tmpdir, "cards.json"))


ALERT = {"rule_name": "NullPointerException检测", "type": "crash", "severity": "high"}
SH = {
    "alert_type": "crash",
    "severity": "high",
    "root_cause": "PlayerManager 空指针导致崩溃 NullPointerException",
    "suggestions": ["onDestroy 增加判空保护", "复测切歌场景"],
    "suggested_patch": "if (mPlayer != null) { mPlayer.release(); }",
    "evidence": [],
    "device_id": "192.168.1.140:8787",
}


class TestStructuredAddCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kb = _mk_kb(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_case_with_extra_fields(self):
        card = self.kb.add_case(ALERT, SH, resolved=True, source="acknowledge", extra={
            "resolution": "verified_fixed",
            "final_root_cause": "切歌时 PlayerManager 未判空",
            "fix_action": "onDestroy 判空 + 5.1.2.2002 修复",
            "affected_versions": "5.1.2.2001",
            "fixed_version": "5.1.2.2002",
            "owner_module": "播放器",
        })
        self.assertTrue(card["verified"])
        self.assertEqual(card["final_root_cause"], "切歌时 PlayerManager 未判空")
        self.assertEqual(card["fixed_version"], "5.1.2.2002")
        self.assertEqual(card["owner_module"], "播放器")
        self.assertTrue(card["resolved"])
        # 落盘验证
        with open(self.kb.file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["cards"]), 1)
        self.assertEqual(data["cards"][0]["fix_action"], "onDestroy 判空 + 5.1.2.2002 修复")

    def test_false_positive_marked_unresolved(self):
        card = self.kb.add_case(ALERT, SH, resolved=True, extra={"resolution": "false_positive"})
        self.assertFalse(card["resolved"])
        self.assertFalse(card["verified"])
        self.assertEqual(card["resolution"], "false_positive")

    def test_add_case_without_extra_backward_compatible(self):
        """不传 extra（老调用方式）不报错，新字段为空。"""
        card = self.kb.add_case(ALERT, SH)
        self.assertEqual(card["final_root_cause"], "")
        self.assertFalse(card["verified"])
        self.assertTrue(card["resolved"])

    def test_final_root_cause_indexed_into_keywords(self):
        """人工最终根因参与关键词索引，可被检索命中。"""
        self.kb.add_case(ALERT, dict(SH, root_cause="x"), extra={
            "resolution": "verified_fixed",
            "final_root_cause": "AudioTrack 缓冲区溢出导致爆音",
        })
        hits = self.kb.search_similar("crash", "爆音 AudioTrack 缓冲区")
        self.assertEqual(len(hits), 1)


class TestQualityWeight(unittest.TestCase):
    def test_false_positive_strong_downweight(self):
        w = SelfHealKnowledgeBase._quality_weight({"resolution": "false_positive", "verified": False})
        self.assertAlmostEqual(w, 0.2)

    def test_verified_weight_higher_than_resolved(self):
        w_verified = SelfHealKnowledgeBase._quality_weight({"verified": True, "resolved": True, "hit_count": 0})
        w_resolved = SelfHealKnowledgeBase._quality_weight({"verified": False, "resolved": True, "hit_count": 0})
        self.assertGreater(w_verified, w_resolved)

    def test_hit_count_weight_capped(self):
        w_100 = SelfHealKnowledgeBase._quality_weight({"hit_count": 100})
        w_10 = SelfHealKnowledgeBase._quality_weight({"hit_count": 10})
        self.assertEqual(w_100, w_10)  # 都到 +50% 封顶

    def test_structured_info_bonus(self):
        w_full = SelfHealKnowledgeBase._quality_weight({"resolved": True, "fix_action": "已修复"})
        w_bare = SelfHealKnowledgeBase._quality_weight({"resolved": True})
        self.assertGreater(w_full, w_bare)


class TestSearchRanking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kb = _mk_kb(self.tmp)
        # 同类型同关键词的两条案例：一条误报、一条人工验证
        self.kb.add_case(ALERT, SH, extra={"resolution": "false_positive"})
        self.kb.add_case(ALERT, SH, extra={
            "resolution": "verified_fixed",
            "final_root_cause": "PlayerManager 判空缺失",
            "fix_action": "已在 2002 版本修复",
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verified_ranks_above_false_positive(self):
        hits = self.kb.search_similar("crash", "NullPointerException PlayerManager 崩溃", top_k=3)
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(hits[0]["resolution"], "verified_fixed")
        self.assertEqual(hits[-1]["resolution"], "false_positive")
        # 综合分与基础分都返回
        self.assertIn("_score", hits[0])
        self.assertIn("_base_score", hits[0])
        self.assertGreater(hits[0]["_score"], hits[-1]["_score"])


class TestHitCountPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kb = _mk_kb(self.tmp)
        self.kb.add_case(ALERT, SH, extra={"resolution": "verified_fixed"})
        self._old_interval = kb_mod.HIT_SAVE_INTERVAL_SECONDS

    def tearDown(self):
        kb_mod.HIT_SAVE_INTERVAL_SECONDS = self._old_interval
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hit_count_increments_in_memory(self):
        for _ in range(3):
            self.kb.search_similar("crash", "NullPointerException PlayerManager")
        hits = self.kb.search_similar("crash", "NullPointerException PlayerManager")
        # 第4次检索时，前3次的命中已累加
        self.assertGreaterEqual(hits[0]["hit_count"], 3)

    def test_hit_count_not_saved_within_interval(self):
        """间隔内不写盘：add_case 刚写过盘，紧接着检索不应触发写盘。"""
        kb_mod.HIT_SAVE_INTERVAL_SECONDS = 3600
        self.kb.search_similar("crash", "NullPointerException PlayerManager")
        with open(self.kb.file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["cards"][0]["hit_count"], 0)  # 盘上还是 0

    def test_hit_count_saved_after_interval(self):
        kb_mod.HIT_SAVE_INTERVAL_SECONDS = 0.0  # 立即落盘
        self.kb._last_hit_save = 0.0
        self.kb.search_similar("crash", "NullPointerException PlayerManager")
        with open(self.kb.file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["cards"][0]["hit_count"], 1)


class TestBackwardCompatOldCards(unittest.TestCase):
    def test_old_card_without_new_fields(self):
        """旧格式卡片（无 resolution/verified/hit_count 等）检索不报错且正常加权。"""
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "cards.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"cards": [{
                    "id": "sh_old", "alert_type": "crash",
                    "rule_name": "NullPointerException检测",
                    "keywords": ["NullPointerException"],
                    "root_cause": "老案例根因描述足够长",
                    "suggestions": ["老修复建议"], "resolved": True,
                }]}, f, ensure_ascii=False)
            kb = SelfHealKnowledgeBase(file_path=path)
            hits = kb.search_similar("crash", "NullPointerException")
            self.assertEqual(len(hits), 1)
            self.assertGreater(hits[0]["_score"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
