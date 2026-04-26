# -*- coding: utf-8 -*-
import math
import re
from typing import Any, Dict, List


def _tokenize(text: str) -> List[str]:
    s = str(text or "").lower()
    chunks = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", s)
    out: List[str] = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        out.append(c)
        if re.match(r"^[\u4e00-\u9fff]+$", c):
            if len(c) >= 2:
                for i in range(len(c) - 1):
                    out.append(c[i : i + 2])
            if len(c) >= 3:
                for i in range(len(c) - 2):
                    out.append(c[i : i + 3])
    return out


def _tf(tokens: List[str]) -> Dict[str, float]:
    d: Dict[str, float] = {}
    for t in tokens:
        d[t] = d.get(t, 0.0) + 1.0
    n = float(len(tokens) or 1.0)
    for k in list(d.keys()):
        d[k] = d[k] / n
    return d


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    keys = set(a.keys()) & set(b.keys())
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def build_platform_docs(kb: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    for item in kb or []:
        platform = str(item.get("platform") or "")
        caps = item.get("capabilities") if isinstance(item.get("capabilities"), list) else []
        risks = item.get("risks") if isinstance(item.get("risks"), list) else []
        for r in risks:
            feature = str(r.get("feature") or "")
            risk = str(r.get("risk") or "")
            severity = str(r.get("severity") or "P2")
            text = " ".join([platform, feature, risk] + [str(x) for x in caps])
            docs.append(
                {
                    "platform": platform,
                    "feature": feature,
                    "risk": risk,
                    "severity": severity,
                    "text": text,
                    "vec": _tf(_tokenize(text)),
                }
            )
    return docs


def retrieve_platform_risks(query_text: str, docs: List[Dict[str, Any]], top_k: int = 20) -> List[Dict[str, Any]]:
    q_tokens = _tokenize(query_text)
    q_vec = _tf(q_tokens)
    res: List[Dict[str, Any]] = []
    q_set = set(q_tokens)
    for d in docs or []:
        score = _cosine(q_vec, d.get("vec") or {})
        if score <= 0.0:
            continue
        d_tokens = set(_tokenize(d.get("text") or ""))
        common = list(q_set & d_tokens)[:8]
        row = dict(d)
        row["retrieval_score"] = round(float(score), 4)
        row["evidence_terms"] = common
        res.append(row)
    res.sort(key=lambda x: x.get("retrieval_score", 0.0), reverse=True)
    return res[: max(1, int(top_k))]

