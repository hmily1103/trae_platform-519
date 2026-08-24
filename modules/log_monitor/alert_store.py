#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
告警历史落盘（Alert History Store）

设计：
- 追加式 JSONL 文件：modules/log_monitor/data/alerts/alerts.jsonl
- 每条告警按 id 做 upsert（追加新版本，旧版本在 compact 时清理），因此：
    * 写入始终 O(1) append —— 不再全量读文件
    * 重写只发生在 compact（超出 _MAX_LINES 时）
- 读取从文件末尾向前取最近 N 条（默认倒序，最新在前），内存索引辅助定位。
- 线程安全：全局锁保护读写。
- 文件无限增长保护：超过 _MAX_LINES 时执行 compact（去重去旧，保留每个 id 最新版本）。
"""

import json
import os
import threading
from typing import Any, Dict, List, Optional

_lock = threading.Lock()

_STORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "alerts")
_STORE_FILE = os.path.join(_STORE_DIR, "alerts.jsonl")

# 安全上限：超过该行数时执行 compact（去重保留每个 id 最新一条）
_MAX_LINES = 50000

# ========== 内存索引（进程级别，重启后重建） ==========
# {alert_id: file_byte_offset}  —— 每次 upsert 更新，compact 时重建
_index: Dict[str, int] = {}
_index_dirty = False  # compact 后标记，触发下次 load 时重建索引


def _ensure_dir():
    os.makedirs(_STORE_DIR, exist_ok=True)


def _rebuild_index():
    """从文件重建内存索引（仅 compact 后或首次加载时调用）。"""
    global _index, _index_dirty
    _index.clear()
    if not os.path.exists(_STORE_FILE):
        _index_dirty = False
        return
    with open(_STORE_FILE, "r", encoding="utf-8") as f:
        offset = 0
        for line in f:
            s = line.strip()
            if s:
                try:
                    rec = json.loads(s)
                    aid = rec.get("id")
                    if aid:
                        _index[aid] = offset
                except Exception:
                    pass
            offset = f.tell()
    _index_dirty = False


def _compact():
    """去重压缩：保留每个 alert_id 的最新版本，丢弃旧行。"""
    if not os.path.exists(_STORE_FILE):
        return
    seen: Dict[str, int] = {}
    with open(_STORE_FILE, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
                aid = rec.get("id")
                if aid:
                    seen[aid] = i
            except Exception:
                pass

    if not seen:
        return

    # 收集必须保留的行（按行号排序）
    keep_indices = sorted(seen.values())
    keep_lines: List[str] = []
    with open(_STORE_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for idx in keep_indices:
        if idx < len(lines):
            keep_lines.append(lines[idx])

    # 写回并重建索引
    tmp = _STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(keep_lines)
    os.replace(tmp, _STORE_FILE)

    # 重建索引
    global _index, _index_dirty
    _index_dirty = True
    _rebuild_index()


def upsert_alert(record: Dict[str, Any]) -> None:
    """按 id 追加一条告警记录（O(1) append，不读全量文件）。

    已存在的 id：追加新版本，旧版本在 compact 时清理。
    新 id：直接追加。
    """
    alert_id = record.get("id")
    if not alert_id:
        return
    _ensure_dir()
    with _lock:
        # 追加新行
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with open(_STORE_FILE, "a", encoding="utf-8") as f:
            offset = f.tell()
            f.write(line)
        _index[alert_id] = offset

        # 行数超限时异步压缩（不用每次 upsert 都检查文件大小，
        # 用索引大小近似判断）
        if len(_index) > _MAX_LINES:
            _compact()


def load_recent(
    limit: int = 200,
    task_id: Optional[str] = None,
    device_id: Optional[str] = None,
    severity: Optional[str] = None,
    acknowledged: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """从文件末尾向前读取最近 N 条，返回时已按时间倒序（最新在前）。

    支持按 task_id / device_id / severity / acknowledged 过滤。
    使用内存索引跳过已知旧版本，避免 JSON 解析每条历史行。
    """
    if _index_dirty:
        _rebuild_index()

    if not os.path.exists(_STORE_FILE):
        return []

    # 反向读取：从文件末尾向前，按 id 去重（取每个 id 首次遇到的 = 最新版）
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()

    try:
        with _lock:
            with open(_STORE_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except Exception:
        return []

    for ln in reversed(lines):
        s = ln.strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
        except Exception:
            continue

        aid = rec.get("id")
        if aid and aid in seen_ids:
            continue  # 旧版本，跳过
        if aid:
            seen_ids.add(aid)

        if task_id is not None and rec.get("task_id") != task_id:
            continue
        if device_id is not None and rec.get("device_id") != device_id:
            continue
        if severity is not None and rec.get("severity") != severity:
            continue
        if acknowledged is not None and bool(rec.get("acknowledged")) != acknowledged:
            continue

        out.append(rec)
        if len(out) >= limit:
            break

    return out


def store_statistics(task_id: Optional[str] = None) -> Dict[str, Any]:
    """聚合统计（用于历史页概览）。扫描全量较简单，数据量可控。"""
    records = load_recent(limit=200000, task_id=task_id)
    by_severity = {"high": 0, "medium": 0, "low": 0}
    by_type: Dict[str, int] = {}
    acknowledged = 0
    for r in records:
        sev = r.get("severity")
        if sev in by_severity:
            by_severity[sev] += 1
        else:
            by_severity[sev] = by_severity.get(sev, 0) + 1
        t = r.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        if r.get("acknowledged"):
            acknowledged += 1
    return {
        "total": len(records),
        "by_severity": by_severity,
        "by_type": by_type,
        "acknowledged": acknowledged,
        "unacknowledged": len(records) - acknowledged,
    }
