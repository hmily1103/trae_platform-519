#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
log_monitor 自愈知识库（RAG 底座）

职责：
- 加载 / 保存自愈案例到模块内私有 JSON（不跨模块依赖 prd_audit 等知识库）
- 提供基于「告警类型 + 关键词重合」的轻量相似检索（纯本地，零新增依赖）

设计原则（不影响平台其他功能）：
- 本文件为纯新增模块，仅被 log_monitor 自愈链路 import。
- 不引入向量服务 / embedding 依赖，先以可解释的关键词匹配跑通 RAG，
  后续若要升级语义检索可在此替换 search_similar 内部实现。
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
_KB_FILE = os.path.join(_KB_DIR, "self_heal_cards.json")

# #27 命中计数低频持久化：两次写盘之间的最小间隔（秒），避免检索高频触发写盘
HIT_SAVE_INTERVAL_SECONDS = float(os.environ.get("LOG_KB_HIT_SAVE_INTERVAL", "60"))

# 进程内读写锁，避免多线程并发写 JSON 损坏
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _extract_keywords(text: str) -> List[str]:
    """从文本提取候选关键词，用于案例索引与检索匹配。

    抽取三类信号：
    1) 异常/错误大写关键字（Exception / Error / ANR / OOM / FATAL ...）
    2) 包名/类路径片段（含两个及以上 '.' 的标识符）
    3) 中文短语（2~6 字）作为粗粒度语义信号
    """
    if not text:
        return []
    kws: set = set()
    for m in re.findall(
        r"\b([A-Z][A-Za-z]*(?:Exception|Error|Timeout|Crash|ANR|FATAL|OOM))\b", text
    ):
        kws.add(m)
    for m in re.findall(r"([a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+){2,})", text):
        kws.add(m)
    for m in re.findall(r"[\u4e00-\u9fa5]{2,6}", text):
        kws.add(m)
    return list(kws)[:40]


class SelfHealKnowledgeBase:
    """自愈案例知识库：沉淀已确认告警，提供相似检索。"""

    def __init__(self, file_path: str = _KB_FILE):
        self.file_path = file_path
        self._cards: Optional[List[Dict[str, Any]]] = None
        # #27 命中计数：内存实时累加 + 低频落盘（脏标记 + 最近写盘时间）
        self._hits_dirty = False
        self._last_hit_save = 0.0

    def _ensure_loaded(self) -> None:
        if self._cards is not None:
            return
        with _lock:
            if self._cards is not None:
                return
            cards: List[Dict[str, Any]] = []
            if os.path.exists(self.file_path):
                try:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        cards = data.get("cards", []) or []
                    elif isinstance(data, list):
                        cards = data
                except Exception as e:  # 损坏则重建，避免整体不可用
                    logger.warning(f"自愈知识库读取失败，将重建: {e}")
                    cards = []
            self._cards = cards

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        with _lock:
            data = {"version": 1, "cards": self._cards}
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.file_path)

    def add_case(
        self,
        alert: Dict[str, Any],
        self_heal: Dict[str, Any],
        resolved: bool = True,
        source: str = "acknowledge",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """沉淀一条自愈案例。

        :param alert: 原始告警 dict（含 rule_name/type/severity 等）
        :param self_heal: 自愈结果 dict（含 root_cause/suggestions/evidence 等）
        :param resolved: 是否标记为已解决
        :param source: 来源标识（acknowledge=人工确认 / auto=自动判定）
        :param extra: #27/#31 结构化补充字段（人工确认时填写）：
            resolution（verified_fixed/not_fixed/partially_fixed/not_reproducible/false_positive/known_issue）、
            final_root_cause（人工确认的最终根因，优先于 AI 根因）、
            fix_action（最终处理方式）、affected_versions、fixed_version、
            owner_module（责任模块）、verification_version（验证版本）、
            reproduced_count（复现次数）、regression_status（是否回归）、
            verifier（验证人）、verification_note（验证备注）
        :return: 写入的知识卡
        """
        self._ensure_loaded()
        extra = extra or {}
        resolution = extra.get("resolution", "") or ""
        final_root_cause = (extra.get("final_root_cause") or "").strip()
        # 关键词索引：把人工最终根因也纳入，检索质量优于纯 AI 根因
        keywords = _extract_keywords(
            (self_heal.get("root_cause", "") + " " + final_root_cause
             + " " + alert.get("rule_name", ""))
        )
        card = {
            "id": "sh_" + datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "alert_type": (self_heal.get("alert_type") or alert.get("type") or "").lower(),
            "rule_name": alert.get("rule_name", ""),
            "severity": self_heal.get("severity") or alert.get("severity", ""),
            "keywords": keywords,
            "root_cause": self_heal.get("root_cause", ""),
            "suggestions": self_heal.get("suggestions", []) or [],
            "suggested_patch": self_heal.get("suggested_patch", "") or "",
            "evidence": self_heal.get("evidence", []) or [],
            "device_id": self_heal.get("device_id", ""),
            "resolved": resolved,
            "source": source,
            "hit_count": 0,
            "created_at": _now_iso(),
            # ---- #27 结构化沉淀字段（人工在环补充，可为空）----
            "resolution": resolution,
            "final_root_cause": final_root_cause,
            "fix_action": (extra.get("fix_action") or "").strip(),
            "affected_versions": (extra.get("affected_versions") or "").strip(),
            "fixed_version": (extra.get("fixed_version") or "").strip(),
            "owner_module": (extra.get("owner_module") or "").strip(),
            "verification_version": (extra.get("verification_version") or "").strip(),
            "reproduced_count": int(extra.get("reproduced_count") or 0),
            "regression_status": (extra.get("regression_status") or "").strip(),
            "verifier": (extra.get("verifier") or "").strip(),
            "verification_note": (extra.get("verification_note") or "").strip(),
            # verified：人工验证通过（verified_fixed）才为 True
            "verified": resolution == "verified_fixed",
        }
        # 误报案例：不作为"已解决"参考，明确降权标记（保留供审计，检索时降权）
        if resolution == "false_positive":
            card["resolved"] = False
        self._cards.append(card)
        self._save()
        # 全量写盘已包含最新命中计数，顺带清脏标记
        self._hits_dirty = False
        self._last_hit_save = time.time()
        logger.info(f"自愈知识库沉淀案例: {card['id']} type={card['alert_type']} resolution={resolution or '-'}")
        return card

    # ---- #27 质量评分 ----
    @staticmethod
    def _quality_weight(card: Dict[str, Any]) -> float:
        """案例质量权重（乘在基础相似分上）：

        - 误报（false_positive）：强降权 0.2 —— 防止污染 RAG 推荐；
        - 人工验证通过（verified）：加权 1.5；
        - 已解决（resolved）：加权 1.2；
        - 重复命中（hit_count）：每次 +5%，封顶 +50% —— 越常被复用越靠前；
        - 信息完整（有 fix_action / final_root_cause）：加权 1.1。
        """
        w = 1.0
        if card.get("resolution") == "false_positive":
            return 0.2
        if card.get("verified"):
            w *= 1.5
        elif card.get("resolved"):
            w *= 1.2
        hits = int(card.get("hit_count", 0) or 0)
        w *= 1.0 + min(hits * 0.05, 0.5)
        if (card.get("fix_action") or "").strip() or (card.get("final_root_cause") or "").strip():
            w *= 1.1
        return w

    def search_similar(
        self, alert_type: str, query_text: str, top_k: int = 3
    ) -> List[Dict[str, Any]]:
        """检索相似历史案例（关键词匹配 × 质量权重，命中计数低频持久化）。

        :param alert_type: 当前告警类型
        :param query_text: 检索文本（触发日志 + 规则名）
        :param top_k: 返回条数
        :return: 命中的知识卡列表（含 _score 综合分与 _base_score 相似分）
        """
        self._ensure_loaded()
        q_kws = set(_extract_keywords(query_text))
        scored: List[tuple] = []
        for card in self._cards:
            type_match = 1.0 if card.get("alert_type") == (alert_type or "").lower() else 0.3
            c_kws = set(card.get("keywords", []))
            overlap = len(q_kws & c_kws)
            # 类型匹配或存在关键词重合才计入
            if type_match >= 1.0 or overlap > 0:
                base = type_match * (1 + overlap)
                scored.append((base * self._quality_weight(card), base, card))
        scored.sort(key=lambda x: x[0], reverse=True)
        top: List[Dict[str, Any]] = []
        for score, base, card in scored[:top_k]:
            # 命中计数：内存实时累加（原卡片对象），低频落盘
            try:
                card["hit_count"] = int(card.get("hit_count", 0) or 0) + 1
                self._hits_dirty = True
            except Exception:
                pass
            c = dict(card)
            c["_score"] = round(score, 3)
            c["_base_score"] = round(base, 3)
            top.append(c)
        self._maybe_save_hits()
        return top

    def _maybe_save_hits(self) -> None:
        """命中计数低频落盘：距上次写盘超过 HIT_SAVE_INTERVAL_SECONDS 才写。"""
        if not self._hits_dirty:
            return
        now = time.time()
        if now - self._last_hit_save < HIT_SAVE_INTERVAL_SECONDS:
            return
        try:
            self._save()
            self._hits_dirty = False
            self._last_hit_save = now
        except Exception as e:
            logger.warning(f"命中计数落盘失败(忽略): {e}")

    def count(self) -> int:
        self._ensure_loaded()
        return len(self._cards)


_kb_instance: Optional[SelfHealKnowledgeBase] = None


def get_knowledge_base() -> SelfHealKnowledgeBase:
    """获取知识库单例。"""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = SelfHealKnowledgeBase()
    return _kb_instance
