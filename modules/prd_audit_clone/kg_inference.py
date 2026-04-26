# -*- coding: utf-8 -*-
"""
Knowledge Graph Inference (MVP)

目标：
- 输入：Stage2 defects（带稳定 rule_id/缺陷 id）
- 输出：根因候选 root_causes、风险传播链 risk_chains、系统级风险 system_risks

设计原则：
- 纯增量，不影响既有 Stage1/2/3/4/5
- 无需外部数据库，使用 JSON edges + 图遍历
- 可解释：输出 explain 文案，便于写入报告
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Any, List, Set, Tuple, Optional


STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))
KG_EDGES_FILE = os.path.join(STORAGE_DIR, "kg_edges.json")


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    relation: str
    weight: float


def _safe_float(v: Any, default: float = 0.5) -> float:
    try:
        x = float(v)
        if x < 0:
            return 0.0
        if x > 1:
            return 1.0
        return x
    except Exception:
        return default


def _extract_first_json_object(text: str) -> Dict[str, Any]:
    """本地 JSON 文件容错：只取第一段完整对象，与 LLM 返回解析分离。"""
    raw = (text or "").strip()
    if not raw:
        return {}
    start = raw.find("{")
    if start == -1:
        return {}
    depth = 0
    in_s = None
    esc = False
    i = start
    while i < len(raw):
        c = raw[i]
        if esc:
            esc = False
            i += 1
            continue
        if c == "\\" and in_s:
            esc = True
            i += 1
            continue
        if in_s:
            if c == in_s:
                in_s = None
            i += 1
            continue
        if c in ("'", '"'):
            in_s = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    break
        i += 1
    end = raw.rfind("}")
    if end != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {}


def _load_edges() -> List[Edge]:
    """仅加载本地 kg_edges.json，与大模型用 JSON 分离。"""
    if not os.path.exists(KG_EDGES_FILE):
        return []
    with open(KG_EDGES_FILE, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = _extract_first_json_object(raw)
    edges = data.get("edges") if isinstance(data, dict) else []
    out: List[Edge] = []
    if isinstance(edges, list):
        for e in edges:
            if not isinstance(e, dict):
                continue
            src = str(e.get("from") or "").strip()
            dst = str(e.get("to") or "").strip()
            rel = str(e.get("relation") or "CAUSE").strip().upper()
            w = _safe_float(e.get("weight"), 0.6)
            if src and dst:
                out.append(Edge(src=src, dst=dst, relation=rel, weight=w))
    return out


def _build_adj(edges: List[Edge]) -> Dict[str, List[Edge]]:
    adj: Dict[str, List[Edge]] = {}
    for e in edges:
        adj.setdefault(e.src, []).append(e)
    return adj


def _normalize_hit_ids(defects: List[Dict[str, Any]]) -> Set[str]:
    hits: Set[str] = set()
    for d in defects or []:
        if not isinstance(d, dict):
            continue
        # 优先使用稳定的规则/缺陷ID（如 STATE_001 / FLOW_003 / R001）
        rid = str(d.get("id") or "").strip()
        if rid:
            hits.add(rid)
    return hits


def _path_strength(path_edges: List[Edge]) -> float:
    if not path_edges:
        return 0.0
    s = 1.0
    for e in path_edges:
        # CAUSE 更强，AMPLIFY 次之，DEPEND 最弱（可调）
        rel_boost = 1.0
        if e.relation == "CAUSE":
            rel_boost = 1.0
        elif e.relation == "AMPLIFY":
            rel_boost = 0.9
        elif e.relation == "DEPEND":
            rel_boost = 0.8
        s *= (e.weight * rel_boost)
    return round(s, 3)


def _explain_path(nodes: List[str], edges: List[Edge]) -> str:
    # 简洁中文说明，适合写入 L3/L1
    parts = []
    for i in range(len(edges)):
        a = nodes[i]
        b = nodes[i + 1]
        rel = edges[i].relation
        arrow = "→"
        if rel == "AMPLIFY":
            arrow = "⇢(放大)→"
        elif rel == "DEPEND":
            arrow = "→(依赖)→"
        parts.append(f"{a} {arrow} {b}")
    return "；".join(parts) if parts else ""


def _bfs_paths(
    adj: Dict[str, List[Edge]],
    start: str,
    max_depth: int = 4,
    limit: int = 50,
) -> List[Tuple[List[str], List[Edge]]]:
    """
    生成从 start 出发的路径（节点序列、边序列），限制深度与总量。
    """
    results: List[Tuple[List[str], List[Edge]]] = []
    queue: List[Tuple[List[str], List[Edge]]] = [([start], [])]
    while queue and len(results) < limit:
        nodes, edges = queue.pop(0)
        if len(edges) >= max_depth:
            continue
        last = nodes[-1]
        for e in adj.get(last, []):
            if e.dst in nodes:
                continue  # avoid cycles
            new_nodes = nodes + [e.dst]
            new_edges = edges + [e]
            results.append((new_nodes, new_edges))
            queue.append((new_nodes, new_edges))
            if len(results) >= limit:
                break
    return results


def infer_kg(defects: List[Dict[str, Any]], max_root_causes: int = 2, max_chains: int = 3) -> Dict[str, Any]:
    """
    基于命中的缺陷ID推理根因与风险传播。
    输出字段：
      - root_causes: [{id, score, why}]
      - risk_chains: [{path, strength, explain}]
      - system_risks: [{name, from}]
      - meta: {hits, edges_used}
    """
    edges = _load_edges()
    adj = _build_adj(edges)
    hits = _normalize_hit_ids(defects or [])
    if not hits or not edges:
        return {
            "root_causes": [],
            "risk_chains": [],
            "system_risks": [],
            "meta": {"hits": sorted(list(hits))[:50], "edges_used": 0},
        }

    # 根因：命中节点中“可到达的命中节点越多、路径越短、强度越高”得分越高
    root_scores: Dict[str, float] = {}
    hit_list = sorted(list(hits))
    for h in hit_list:
        paths = _bfs_paths(adj, h, max_depth=4, limit=80)
        score = 0.0
        reached: Set[str] = set()
        for nodes, pedges in paths:
            end = nodes[-1]
            if end in hits and end != h:
                reached.add(end)
                strength = _path_strength(pedges)
                # 深度惩罚：越短越重要
                depth_penalty = 1.0 / float(len(pedges) + 0.5)
                score += strength * depth_penalty
        # 自身命中也算基础分
        score += 0.3
        # reached 越多加成
        score += min(2.0, len(reached) * 0.25)
        root_scores[h] = round(score, 3)

    top_roots = sorted(root_scores.items(), key=lambda x: x[1], reverse=True)[: max_root_causes]
    root_causes = []
    for rid, sc in top_roots:
        root_causes.append(
            {
                "id": rid,
                "score": round(min(10.0, sc * 3.0), 1),  # 映射到 0-10 便于展示
                "why": "作为上游原因节点，触发多条因果/放大链路" if sc > 0.9 else "可能为上游缺口，建议优先澄清",
            }
        )

    # 风险传播链：从 root 出发，挑 strongest 的 2-4 节点路径（优先到达已命中节点）
    chains: List[Dict[str, Any]] = []
    for rid, _ in top_roots:
        paths = _bfs_paths(adj, rid, max_depth=4, limit=120)
        candidates: List[Tuple[float, List[str], List[Edge]]] = []
        for nodes, pedges in paths:
            if len(nodes) < 2:
                continue
            end = nodes[-1]
            # 终点若命中则优先；否则只取较强路径
            strength = _path_strength(pedges)
            hit_bonus = 1.2 if end in hits else 1.0
            candidates.append((strength * hit_bonus, nodes, pedges))
        candidates.sort(key=lambda x: x[0], reverse=True)
        for val, nodes, pedges in candidates[: max_chains]:
            chains.append(
                {
                    "path": nodes,
                    "strength": round(val, 3),
                    "explain": _explain_path(nodes, pedges),
                }
            )

    # 去重：相同 path
    seen_paths = set()
    uniq_chains = []
    for c in sorted(chains, key=lambda x: x["strength"], reverse=True):
        key = "->".join(c.get("path") or [])
        if not key or key in seen_paths:
            continue
        seen_paths.add(key)
        uniq_chains.append(c)
        if len(uniq_chains) >= max_chains:
            break

    # 系统级风险：把传播链的尾部聚合成风险主题（简单映射）
    def _risk_bucket(node_id: str) -> Optional[str]:
        if node_id.startswith("EXC_"):
            return "异常与恢复风险"
        if node_id.startswith("CONC_"):
            return "并发与裁决风险"
        if node_id.startswith("DATA_"):
            return "数据一致性风险"
        if node_id.startswith("PERM_") or node_id.startswith("TECH_"):
            return "权限与安全风险"
        if node_id.startswith("TEST_"):
            return "测试覆盖不足风险"
        if node_id.startswith("FLOW_"):
            return "流程闭环风险"
        if node_id.startswith("STATE_"):
            return "状态机设计风险"
        return None

    bucket_to_from: Dict[str, Set[str]] = {}
    for c in uniq_chains:
        path = c.get("path") or []
        if not path:
            continue
        tail = path[-1]
        b = _risk_bucket(tail)
        if not b:
            continue
        bucket_to_from.setdefault(b, set()).update([x for x in path if x in hits])

    system_risks = []
    for name, from_set in bucket_to_from.items():
        system_risks.append({"name": name, "from": sorted(list(from_set))[:10]})
    system_risks.sort(key=lambda x: len(x.get("from") or []), reverse=True)

    return {
        "root_causes": root_causes,
        "risk_chains": uniq_chains,
        "system_risks": system_risks[:5],
        "meta": {"hits": sorted(list(hits))[:50], "edges_used": len(edges)},
    }

