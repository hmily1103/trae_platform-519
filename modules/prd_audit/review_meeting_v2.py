"""
Review Meeting V2 (QA-led, vertical workbench).

Hard constraint:
- Do NOT change static audit outputs. V2 only CONSUMES snapshot/stage outputs and produces meeting artifacts.
- All V2 code is isolated in this module + its template + minimal route registration.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Callable

from . import review_meeting_v2_llm as rmv2llm
from utils.llm_client import call_llm_with_retry


def _short_hash(s: str) -> str:
    try:
        return hex(abs(hash(s)) % (16**8))[2:]
    except Exception:
        return uuid.uuid4().hex[:8]


def _now_ts() -> int:
    return int(time.time())


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_now_ts()))


def _risk_rank(lv: str) -> int:
    s = (lv or "").upper()
    if s == "P0":
        return 0
    if s == "P1":
        return 1
    if s == "P2":
        return 2
    return 9


def _clean_text(s: Any) -> str:
    t = str(s or "").strip()
    # Normalize unicode to reduce "looks same but not equal" issues in PRD text
    # (e.g., x9、x7、派2 with special-width chars).
    try:
        t = unicodedata.normalize("NFKC", t)
    except Exception:
        pass
    t = re.sub(r"\s+", " ", t)
    return t


def _ensure_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    return []


def _dedupe_str(items: List[Any], limit: int = 50) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for it in items or []:
        s = _clean_text(it)
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= int(limit or 50):
            break
    return out


def _strip_field_prefix(s: Any) -> str:
    """
    V2 UI/exports should not show schema-ish prefixes like 'required_evidence:' / 'missing:'.
    Keep platform-generic; only remove obvious prefixes at start of string.
    """
    t = _clean_text(s)
    if not t:
        return ""
    t2 = re.sub(r"^(required_evidence|missing)\s*[:：]\s*", "", t, flags=re.I).strip()
    return t2 or t


def _infer_required_evidence(defect: Dict[str, Any]) -> List[str]:
    """
    QA-led: convert "missing spec" style defects into concrete evidence requests.
    Keep platform-generic; do not invent business-specific values.
    """
    text = " ".join(
        [
            str(defect.get("type") or ""),
            str(defect.get("description") or ""),
            str(defect.get("reason") or ""),
            str(defect.get("suggestion") or ""),
        ]
    )
    text = _clean_text(text)
    req: List[str] = []
    if re.search(r"(超时|timeout|弱网|断网|失败|错误码|重试|降级|补偿)", text, re.I):
        req.append("补齐失败/超时/弱网的错误态、提示口径、重试次数/间隔/终止条件与降级/补偿策略（含错误码）。")
    if re.search(r"(退出|中断|重进|切后台|回滚|恢复|清理|状态机|终态)", text):
        req.append("补齐中断/退出/重进后的唯一状态落点、状态机转移（含终态）与资源清理/保留清单。")
    if re.search(r"(优先级|裁决|互斥|冲突|按.*为准|开关|手动|自动)", text):
        req.append("补齐冲突裁决：优先级矩阵/裁决函数（手动vs自动/全局vs局部/多入口）与可验收提示口径。")
    if re.search(r"(权限|鉴权|越权|二维码|扫码|有效期|失效|重放|审计)", text):
        req.append("补齐访问控制：谁可访问/可操作、有效期/一次性/重放拦截、越权提示与审计留痕字段。")
    if re.search(r"(验收|成功标准|可测试|日志|埋点|观测|指标)", text):
        req.append("补齐验收口径与最小观测字段（日志/埋点/错误码/状态枚举），确保可复现可对账。")
    if not req:
        req.append("补齐该规则的可验收口径（触发条件、终态、错误码/提示、最小观测字段）并提供可引用锚点。")
    return _dedupe_str(req, limit=6)


def _infer_observability_fields(defect: Dict[str, Any]) -> List[str]:
    text = " ".join([str(defect.get("type") or ""), str(defect.get("description") or "")])
    text = _clean_text(text)
    fields: List[str] = ["result_status", "error_code"]
    if re.search(r"(重试|超时|弱网|断网|失败|降级|补偿)", text):
        fields += ["timeout_ms", "retry_count", "retry_policy"]
    if re.search(r"(退出|中断|重进|切后台|恢复|回滚|清理|状态)", text):
        fields += ["state_before", "state_after", "resume_policy"]
    if re.search(r"(优先级|裁决|互斥|冲突|开关|手动|自动)", text):
        fields += ["effective_rule", "decision_source"]
    if re.search(r"(并发|幂等|重复|多人)", text):
        fields += ["request_id", "idempotency_key"]
    if re.search(r"(权限|鉴权|越权|二维码|扫码|有效期|失效|重放)", text):
        fields += ["auth_result", "token_status", "expire_at"]
    return _dedupe_str(fields, limit=24)


def _infer_ac(defect: Dict[str, Any]) -> Dict[str, str]:
    """
    Produce a minimal Given/When/Then in QA language.
    """
    subject = _clean_text(defect.get("module") or defect.get("type") or "该功能")
    dtype = _clean_text(defect.get("type") or "")
    desc = _clean_text(defect.get("description") or "")
    given = "已满足主流程前置条件，且相关配置/入口/依赖可触发该场景。"
    when = "触发关键规则/异常场景时"
    then = "系统行为与提示口径唯一可验收，并能通过最小观测字段对账。"
    if re.search(r"(失败|超时|弱网|断网|重试|降级)", dtype + " " + desc):
        when = "依赖失败/超时/弱网或重试时"
        then = "在明确时限内给出错误态/处理中提示；重试路径唯一；最终结果可对账。"
    if re.search(r"(退出|中断|重进|切后台|回滚|恢复|清理)", dtype + " " + desc):
        when = "退出/切后台/中断后重进或恢复时"
        then = "状态落点唯一；页面外显与真实状态一致；资源清理/保留符合口径。"
    if re.search(r"(优先级|裁决|互斥|冲突|开关|手动|自动)", dtype + " " + desc):
        when = "多规则/多入口同时命中时"
        then = "裁决顺序唯一；提示口径一致；可观测 effective_rule/decision_source。"
    if re.search(r"(权限|鉴权|越权|二维码|扫码|有效期|失效|重放)", dtype + " " + desc):
        when = "未授权/过期/越权/无资源访问时"
        then = "统一拦截并提示；不能越权成功；留审计字段可追溯。"
    return {"given": given, "when": when, "then": then, "scene": f"{subject} - {dtype or '待补口径'}"}


def _evidence_class(defect: Dict[str, Any]) -> str:
    """
    V2 only consumes Stage3 defect data; do not attempt heavy quote parsing.
    Prefer explicit evidence_quotes, then anchor, otherwise MISSING_SPEC when description indicates missing.
    """
    quotes = defect.get("evidence_quotes")
    if isinstance(quotes, list) and any(_clean_text(x) for x in quotes):
        return "QUOTE"
    anch = _clean_text(defect.get("anchor") or "")
    if anch and not anch.startswith("功能："):
        return "DERIVED"
    blob = " ".join([str(defect.get("type") or ""), str(defect.get("description") or ""), str(defect.get("reason") or "")])
    if re.search(r"(未说明|未定义|缺失|不明确|未给出|只描述成功路径|PRD未说明)", blob):
        return "MISSING_SPEC"
    return "DERIVED"


def _build_discussion_round(blocker: Dict[str, Any], round_no: int = 1) -> List[Dict[str, Any]]:
    """
    Build a bounded, QA-led discussion transcript for a single blocker.
    This is intentionally deterministic/template-based (MVP) to ensure:
    - Always shows a "process"
    - No unbounded loops
    - No dependence on LLM availability
    """
    title = _clean_text(blocker.get("title") or blocker.get("type") or "未命名议题")
    lv = str(blocker.get("risk_level") or "P2").upper()
    evidence_class = _clean_text(blocker.get("evidence_class") or "DERIVED")
    req = blocker.get("required_evidence") if isinstance(blocker.get("required_evidence"), list) else []
    req = _dedupe_str(req, limit=6)
    ac = blocker.get("ac") if isinstance(blocker.get("ac"), dict) else {}
    obs = blocker.get("observability") if isinstance(blocker.get("observability"), list) else []
    obs = [str(x) for x in obs[:10]]

    qa_lines: List[str] = []
    qa_lines.append(f"议题：{title}（{lv}）")
    qa_lines.append(f"证据等级：{evidence_class}")
    if req:
        qa_lines.append("不可验收点/必补证据：")
        qa_lines.extend([f"- {x}" for x in req[:4]])
    else:
        qa_lines.append("不可验收点：当前未发现明确缺口，但需确认验收口径与观测字段。")
    if ac:
        qa_lines.append("AC（草案）：")
        qa_lines.append(f"- Given：{_clean_text(ac.get('given'))}")
        qa_lines.append(f"- When：{_clean_text(ac.get('when'))}")
        qa_lines.append(f"- Then：{_clean_text(ac.get('then'))}")

    dev_lines: List[str] = []
    dev_lines.append("最小实现回应：")
    if "effective_rule" in obs or "decision_source" in obs:
        dev_lines.append("- 需要统一裁决函数与可观测字段（effective_rule/decision_source），避免多端各算各的。")
    if "state_before" in obs or "state_after" in obs:
        dev_lines.append("- 需要状态机/终态定义与中断恢复落点（state_before/state_after/resume_policy），否则无法稳定复现。")
    if "timeout_ms" in obs or "retry_count" in obs:
        dev_lines.append("- 需要明确 timeout_ms/retry_policy，保证失败/重试不重复生效且可对账。")
    if "auth_result" in obs:
        dev_lines.append("- 需要鉴权边界/有效期/重放拦截与统一错误码，且审计字段可追溯。")
    if len(dev_lines) == 1:
        dev_lines.append("- 需要把 PRD 的口径落成字段/枚举/错误码与验收断言，避免联调期返工。")

    pm_lines: List[str] = []
    if req:
        pm_lines.append("裁决：BLOCKED（待补证据/口径后再冻结）")
        pm_lines.append("PM 回写动作：")
        pm_lines.extend([f"- {x}" for x in req[:4]])
    else:
        pm_lines.append("裁决：DECIDED（可进入拆任务与验收）")
        pm_lines.append("回写建议：将 AC 与最小观测字段写入 PRD 附录/验收口径。")

    return [
        {
            "role": "assistant",
            "speaker": "测试（QA）",
            "ts": _now_str(),
            "round": int(round_no),
            "issue_title": title,
            "text": "\n".join(qa_lines),
        },
        {
            "role": "assistant",
            "speaker": "研发（Dev）",
            "ts": _now_str(),
            "round": int(round_no),
            "issue_title": title,
            "text": "\n".join(dev_lines),
        },
        {
            "role": "assistant",
            "speaker": "产品（PM）",
            "ts": _now_str(),
            "round": int(round_no),
            "issue_title": title,
            "text": "\n".join(pm_lines),
        },
    ]


def _extract_quotes_from_anchor(anchor: str, prd_content: str, limit: int = 2) -> List[str]:
    """
    V2-local quote extraction to strengthen PRD binding.
    Supports anchors like L0004-L0011 and extracts usable short lines from prd_content.
    """
    anch = _clean_text(anchor)
    if not anch or not prd_content:
        return []
    mm = re.search(r"(L\d{3,5})\s*-\s*(L?\d{3,5})", anch, flags=re.I)
    if not mm:
        return []
    try:
        s1 = int(re.sub(r"\D", "", mm.group(1) or "0") or 0)
        s2 = int(re.sub(r"\D", "", mm.group(2) or "0") or 0)
    except Exception:
        return []
    if s1 <= 0 or s2 <= 0:
        return []
    start = min(s1, s2)
    end = max(s1, s2)
    lines = (prd_content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Anchor points beyond PRD line count → cannot extract by line range.
    if start > len(lines):
        return []
    out: List[str] = []
    for i in range(start, min(end, len(lines)) + 1):
        raw = lines[i - 1].strip().strip("•-—*")
        q = _clean_text(raw)
        if not q:
            continue
        # avoid extremely long dump lines; keep copyable
        if len(q) < 6 or len(q) > 220:
            continue
        if q not in out:
            out.append(q)
        if len(out) >= max(1, int(limit or 1)):
            break
    return out[: max(1, int(limit or 1))]


def _blocker_keywords(defect: Dict[str, Any]) -> List[str]:
    """
    Build lightweight keywords from a defect to locate PRD lines when anchor is unusable.
    Keep platform-generic (no business constants).
    """
    title = _clean_text(defect.get("title") or defect.get("name") or defect.get("type") or "")
    module = _clean_text(defect.get("module") or "")
    desc = _clean_text(defect.get("description") or "")
    # Prefer short tokens
    raw = " ".join([title, module, desc[:120]])
    parts = [p for p in re.split(r"[，,。.;；、\s:/\\|]+", raw) if _clean_text(p)]
    # Keep a few informative tokens
    kws = []
    for p in parts:
        p2 = _clean_text(p)
        if not p2 or len(p2) < 2:
            continue
        if p2 not in kws:
            kws.append(p2)
        if len(kws) >= 8:
            break
    return kws


def _find_quote_by_keywords(prd_content: str, keywords: List[str], *, limit: int = 2) -> List[str]:
    """
    Lightweight quote finder: scan PRD lines and return copyable evidence lines.
    Tries to find lines containing multiple keywords first, then any keyword.
    """
    text = (prd_content or "").replace("\r\n", "\n").replace("\r", "\n")
    try:
        text = unicodedata.normalize("NFKC", text)
    except Exception:
        pass
    if not text.strip():
        return []
    kws = [_clean_text(k) for k in (keywords or []) if _clean_text(k)]
    if not kws:
        return []
    lines = text.split("\n")
    out: List[str] = []

    def _clip_long_line(ln: str, kws: List[str], *, max_len: int = 220) -> str:
        """
        If PRD is pasted as one giant line, keep a readable snippet around first keyword hit.
        We must not fabricate; snippet must be a substring of original PRD text.
        """
        raw0 = (ln or "").strip().strip("•-—*")
        if not raw0:
            return ""
        s0 = _clean_text(raw0)
        if not s0:
            return ""
        if len(s0) <= max_len:
            return s0
        # Find first hit position using normalized string
        try:
            hit_pos = -1
            hit_kw = ""
            for k in kws:
                if not k:
                    continue
                p = s0.find(k)
                if p >= 0 and (hit_pos < 0 or p < hit_pos):
                    hit_pos = p
                    hit_kw = k
            if hit_pos < 0:
                return ""
            # window around hit keyword
            half = max(40, int(max_len / 2))
            start = max(0, hit_pos - half)
            end = min(len(s0), hit_pos + len(hit_kw) + half)
            snippet = s0[start:end].strip()
            if start > 0:
                snippet = "…" + snippet
            if end < len(s0):
                snippet = snippet + "…"
            # Ensure bounded and cleaned
            snippet = _clean_text(snippet)
            if len(snippet) > max_len:
                snippet = snippet[: max_len - 1].rstrip() + "…"
            return snippet
        except Exception:
            return ""

    for ln in lines:
        s = _clean_text(ln.strip().strip("•-—*"))
        if not s or len(s) < 6:
            continue
        if len(s) > 220:
            # For giant lines, clip a local snippet around keywords.
            clipped = _clip_long_line(ln, kws, max_len=220)
            if clipped:
                s = clipped
            else:
                continue
        hit = sum(1 for k in kws if k and (k in s))
        if hit >= min(2, len(kws)):
            out.append(s)
        if len(out) >= max(1, int(limit or 1)):
            break
    if out:
        return _dedupe_str(out, limit=limit)
    for ln in lines:
        s = _clean_text(ln.strip().strip("•-—*"))
        if not s or len(s) < 6:
            continue
        if len(s) > 220:
            clipped = _clip_long_line(ln, kws, max_len=220)
            if clipped:
                s = clipped
            else:
                continue
        if any(k in s for k in kws):
            out.append(s)
        if len(out) >= max(1, int(limit or 1)):
            break
    return _dedupe_str(out, limit=limit)


def _expand_claim_keywords(claim: str) -> List[str]:
    """
    Expand keywords for common "scope/range" claims so Claim-Check can match PRD wording.
    Keep platform-generic (no business-specific constants), just synonyms.
    """
    c = _clean_text(claim)
    kws: List[str] = []
    # Base tokens: split by punctuation incl. Chinese '、'
    parts = [p for p in re.split(r"[，,。.;；、\s:/\\|]+", c) if _clean_text(p)]
    kws.extend(parts[:10])

    if re.search(r"(型号范围|盒子型号|机顶盒型号|设备型号|型号)", c):
        kws.extend(["盒子型号", "机顶盒型号", "设备型号", "型号"])
    if re.search(r"(版本范围|版本|定制版|标准版)", c):
        kws.extend(["版本", "录音范围", "定制版", "标准版"])
    if re.search(r"(终端范围|终端|展示|触摸屏|电视端|手机端|小程序)", c):
        kws.extend(["展示", "终端", "触摸屏", "电视端", "手机端", "小程序"])

    # Also extract compact alnum tokens (x9/x7/派2/60秒/50MB)
    kws.extend(re.findall(r"[A-Za-z]+\d+|\d+MB|\d+秒|x\d+|派\d+", c, flags=re.I))
    return _dedupe_str(kws, limit=18)


def _claim_importance(claim: str) -> str:
    c = _clean_text(claim)
    if re.search(r"(范围|适用|支持|不支持|型号|版本|端|红线|必须|禁止|优先级|裁决|互斥|权限|鉴权|越权)", c):
        return "P0"
    return "P1"


def build_claim_checks(
    *,
    stage3_output: Dict[str, Any],
    prd_content: str,
    decisions: List[Dict[str, Any]],
    blockers: List[Dict[str, Any]],
    limit: int = 14,
) -> List[Dict[str, Any]]:
    """
    Claim-Check: each claim must be supported by PRD quotes; otherwise UNSUPPORTED and lists required_evidence.
    V2-only: does not modify static audit pipeline.
    """
    candidates: List[str] = []

    shared = stage3_output.get("shared_summary") if isinstance(stage3_output, dict) else None
    if isinstance(shared, dict):
        for k in ("scope", "scope_in", "scope_out", "device_models", "model_range", "redlines", "priorities", "conflicts"):
            v = shared.get(k)
            if isinstance(v, str) and _clean_text(v):
                candidates.append(_clean_text(v))
            elif isinstance(v, list):
                candidates.extend([_clean_text(x) for x in v if _clean_text(x)])

    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        t = _clean_text(d.get("decision") or "")
        if t and len(t) <= 120:
            candidates.append(t)

    # pull scope-ish lines from PRD itself (fast + very traceable)
    prd_lines = (prd_content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for ln in prd_lines:
        s = _clean_text(ln.strip().strip("•-—*"))
        if not s or len(s) < 10 or len(s) > 180:
            continue
        if re.search(r"(适用范围|范围|支持|不支持|型号|版本|红线|禁止|必须|优先级|裁决|互斥|权限|鉴权)", s):
            candidates.append(s)
        if len(candidates) >= 80:
            break

    cand = _dedupe_str(candidates, limit=80)

    out: List[Dict[str, Any]] = []
    for c in cand:
        importance = _claim_importance(c)
        tokens = _expand_claim_keywords(c)
        quotes = _find_quote_by_keywords(prd_content, tokens[:6], limit=2)
        status = "SUPPORTED" if quotes else "UNSUPPORTED"
        required = []
        if status == "UNSUPPORTED":
            required.append("请在 PRD 补充/明确该结论的原文条款并提供可引用锚点（建议放在《目标与范围/裁决规则/异常口径/权限边界》）。")
        out.append(
            {
                "id": "clm_" + _short_hash(c),
                "importance": importance,
                "claim": c,
                "status": status,
                "evidence_quotes": quotes,
                "required_evidence": _dedupe_str(required, limit=3),
            }
        )
        if len(out) >= max(6, int(limit or 12)):
            break

    if not out and blockers:
        for b in blockers[:6]:
            title = _clean_text(b.get("title") or "")
            if not title:
                continue
            claim = f"PRD 必须明确：{title} 的验收口径/异常路径/边界限制（否则不可验收）"
            out.append(
                {
                    "id": "clm_" + _short_hash(claim),
                    "importance": "P0",
                    "claim": claim,
                    "status": "UNSUPPORTED",
                    "evidence_quotes": [],
                    "required_evidence": ["请补齐该议题的原文条款与锚点，至少包含：触发条件、终态、错误码/提示、观测字段。"],
                }
            )
            if len(out) >= 8:
                break

    return out


def build_blocker_item(defect: Dict[str, Any], *, prd_content: str = "") -> Dict[str, Any]:
    lv = str(defect.get("risk_level") or "P2").upper()
    ac = _infer_ac(defect)
    evidence_class = _evidence_class(defect)
    required = _infer_required_evidence(defect) if evidence_class in ("MISSING_SPEC", "DERIVED") else []
    quotes = _ensure_list(defect.get("evidence_quotes"))[:2]
    anch0 = str(defect.get("anchor") or "")
    if (not quotes) and prd_content:
        quotes = _extract_quotes_from_anchor(anch0, prd_content, limit=2)
    if (not quotes) and prd_content:
        # Fallback: find PRD lines by keywords when anchor is not a real PRD line range.
        kws = _blocker_keywords(defect)
        quotes = _find_quote_by_keywords(prd_content, kws[:6], limit=2)
    # Make anchor more readable if it is an out-of-range line anchor.
    anchor_txt = _clean_text(anch0)
    if anchor_txt and prd_content:
        try:
            ln_cnt = len((prd_content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"))
            mm = re.search(r"(L\d{3,5})\s*-\s*(L?\d{3,5})", anchor_txt, flags=re.I)
            if mm:
                s1 = int(re.sub(r"\D", "", mm.group(1) or "0") or 0)
                s2 = int(re.sub(r"\D", "", mm.group(2) or "0") or 0)
                if min(s1, s2) > ln_cnt and ln_cnt > 0:
                    anchor_txt = f"{anchor_txt}（PRD共{ln_cnt}行，锚点超出范围）"
        except Exception:
            pass
    item = {
        "id": str(defect.get("id") or ""),
        "risk_level": lv,
        "title": _clean_text(defect.get("title") or defect.get("name") or defect.get("type") or defect.get("description") or "未命名问题"),
        "type": _clean_text(defect.get("type") or ""),
        "module": _clean_text(defect.get("module") or "跨模块"),
        "anchor": anchor_txt,
        "evidence_class": evidence_class,
        "evidence_quotes": quotes,
        "ac": ac,
        "observability": _infer_observability_fields(defect),
        "required_evidence": required,
        "owner": "PM",
        "status": "BLOCKED" if required else "OPEN",
    }
    return item


def _build_patch_from_blockers(blockers: List[Dict[str, Any]]) -> str:
    """
    Always produce a minimal PRD patch (even without LLM) so QA can copy back.
    Platform-generic; uses required_evidence + AC + observability.
    """
    if not blockers:
        return ""
    lines: List[str] = []
    lines.append("### V2 评审补丁（建议回写 PRD 附录）")
    lines.append("")
    for b in blockers[:12]:
        lv = str(b.get("risk_level") or "P2").upper()
        if lv not in ("P0", "P1"):
            continue
        title = _clean_text(b.get("title") or "未命名议题")
        anchor = _clean_text(b.get("anchor") or "【PRD未说明】")
        lines.append(f"#### {title}（{lv}）")
        lines.append(f"- 关联锚点：{anchor}")
        req = b.get("required_evidence") if isinstance(b.get("required_evidence"), list) else []
        if req:
            lines.append("- 必补条款：")
            for x in req[:6]:
                lines.append(f"  - {str(x)}")
        ac = b.get("ac") if isinstance(b.get("ac"), dict) else {}
        if ac:
            lines.append("- 验收口径（AC）：")
            lines.append(f"  - Given：{_clean_text(ac.get('given'))}")
            lines.append(f"  - When：{_clean_text(ac.get('when'))}")
            lines.append(f"  - Then：{_clean_text(ac.get('then'))}")
        obs = b.get("observability") if isinstance(b.get("observability"), list) else []
        if obs:
            lines.append("- 最小观测字段：")
            lines.append("  - " + ", ".join([_clean_text(x) for x in obs[:18] if _clean_text(x)]))
        lines.append("")
    return "\n".join(lines).strip()


def build_meeting_gate(blockers: List[Dict[str, Any]], claims: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    blockers = blockers if isinstance(blockers, list) else []
    p0 = [b for b in blockers if isinstance(b, dict) and str(b.get("risk_level") or "").upper() == "P0"]
    blocked = [b for b in blockers if isinstance(b, dict) and str(b.get("status") or "").upper() == "BLOCKED"]
    required_all: List[str] = []
    # Map each required_evidence -> example quote/anchor/title (for fast reading)
    req_sources: Dict[str, List[Dict[str, str]]] = {}
    for b in blocked:
        if not isinstance(b, dict):
            continue
        b_title = _clean_text(b.get("title") or b.get("type") or "")
        b_anchor = _clean_text(b.get("anchor") or "")
        qs = b.get("evidence_quotes") if isinstance(b.get("evidence_quotes"), list) else []
        b_quote = _clean_text(qs[0]) if qs else ""
        for x in _ensure_list(b.get("required_evidence")):
            sx = _strip_field_prefix(x)
            if not sx:
                continue
            required_all.append(sx)
            req_sources.setdefault(sx, []).append(
                {
                    "title": b_title,
                    "anchor": b_anchor,
                    "quote": b_quote,
                }
            )
    required_all = _dedupe_str(required_all, limit=80)

    claims_list = claims if isinstance(claims, list) else []
    unsupported_p0_claims = [
        c
        for c in claims_list
        if isinstance(c, dict)
        and str(c.get("status") or "").upper() == "UNSUPPORTED"
        and str(c.get("importance") or "").upper() == "P0"
    ]

    # Formal gate: "正式冻结可开工PRD" (evidence + freeze required)
    if p0 and blocked:
        formal_gate = "FAIL"
        formal_reason = "存在 P0 且关键证据/口径缺口未补齐，暂不可视为正式冻结PRD（不可作为最终开测依据）。"
    elif unsupported_p0_claims:
        formal_gate = "BLOCKED"
        formal_reason = "存在关键结论未被 PRD 原文支持（Claim-Check 未通过），需补齐原文条款与锚点后才能冻结。"
    elif p0:
        formal_gate = "BLOCKED"
        formal_reason = "存在 P0，需先完成裁决与验收口径冻结后才能视为正式冻结PRD。"
    elif blocked:
        formal_gate = "BLOCKED"
        formal_reason = "存在待补证据项，建议先补齐 required_evidence 再推进冻结。"
    else:
        formal_gate = "PASS"
        formal_reason = "未发现阻断冻结的核心缺口，可视为正式可开工PRD（以 AC 与观测字段为准）。"

    # Work-start gate: "临时可开工过渡版" (allows AUTO_PROPOSED defaults, platform-generic)
    def _is_security_like(b: Dict[str, Any]) -> bool:
        t = str(b.get("topic") or "").lower()
        title = _clean_text(b.get("title") or "")
        return (
            t in ("security", "privacy", "auth", "permission")
            or bool(re.search(r"(安全|隐私|越权|鉴权|权限|token|签名|风控|合规)", title, re.I))
        )

    security_p0 = [b for b in p0 if isinstance(b, dict) and _is_security_like(b)]
    if formal_gate == "PASS":
        work_gate = "PASS"
        work_reason = "正式门禁已通过，可直接开工/开测。"
    elif security_p0:
        work_gate = "BLOCKED"
        work_reason = "存在安全/权限类 P0（平台通用红线），不允许以默认口径临时开工；必须先补齐证据并冻结规则。"
    else:
        work_gate = "TEMP_PASS"
        work_reason = "可临时开工：允许使用 AUTO_PROPOSED 平台通用默认口径推进实现/用例，但需在补齐证据后收敛为正式冻结规则。"

    return {
        # Backward compatible: UI/exports that read gate/reason still see formal gate.
        "gate": formal_gate,
        "reason": formal_reason,
        "formal_gate": formal_gate,
        "formal_reason": formal_reason,
        "work_gate": work_gate,
        "work_reason": work_reason,
        "metrics": {
            "p0_count": len(p0),
            "blocked_count": len(blocked),
            "required_evidence_count": len(required_all),
            "unsupported_claims_p0": len(unsupported_p0_claims),
            "security_p0_count": len(security_p0),
        },
        # Human-friendly: include example quotes/anchors when available.
        "required_evidence_top": [
            (
                f"{txt}"
                + (
                    f"（来源：{_clean_text((req_sources.get(txt) or [{}])[0].get('title'))}"
                    + (
                        f"；原文：“{_clean_text((req_sources.get(txt) or [{}])[0].get('quote'))}”"
                        if _clean_text((req_sources.get(txt) or [{}])[0].get('quote'))
                        else "；原文：PRD原文未覆盖该点"
                    )
                    + "）"
                )
                if req_sources.get(txt)
                else txt
            )
            for txt in required_all[:10]
        ],
    }


def _llm_summarize_gate(
    *,
    llm_config_path: str,
    gate: Dict[str, Any],
    blockers: List[Dict[str, Any]],
    timeout: int = 35,
) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Produce LLM-first summary for Gate + next steps.
    Returns (summary, required_evidence_top_llm, error).
    Never mentions "人话" or internal template names.
    """
    try:
        g = gate if isinstance(gate, dict) else {}
        m = g.get("metrics") if isinstance(g.get("metrics"), dict) else {}
        formal_gate = _clean_text(g.get("formal_gate") or g.get("gate") or "—").upper()
        work_gate = _clean_text(g.get("work_gate") or "—").upper()
        # Build compact input for LLM (avoid huge text)
        blocked = [b for b in (blockers or []) if isinstance(b, dict) and str(b.get("status") or "").upper() == "BLOCKED"]
        items: List[str] = []
        for i, b in enumerate(blocked[:6], start=1):
            title = _clean_text(b.get("title") or b.get("type") or "未命名")
            req = _ensure_list(b.get("required_evidence"))
            req_txt = "；".join([_strip_field_prefix(x) for x in req[:4] if _strip_field_prefix(x)])
            items.append(f"- B{i} {title}: {req_txt}" if req_txt else f"- B{i} {title}")
        req_top = g.get("required_evidence_top") if isinstance(g.get("required_evidence_top"), list) else []
        req_top_txt = "\n".join([f"- {str(x)}" for x in req_top[:8] if str(x).strip()])

        prompt = f"""你是产品评审秘书。请将“评审结论 + 必补项”改写成可直接发给产品/研发/测试群的结论摘要。

硬约束：
1) 不要新增事实/数值/字段名/错误码：只能基于输入里已有内容改写与归并。
2) 不要使用“模板/人话/大模型/规则”等内部词。
3) 输出必须简洁、可执行、可转发。
4) 统一命名：Formal Gate 改称「发布门禁」，Work-start Gate 改称「开工门禁」。输出里不要出现 Formal/Work-start 字样。

输入：
- 发布门禁: {formal_gate}
- 开工门禁: {work_gate}
- 指标: P0={m.get('p0_count',0)} / BLOCKED={m.get('blocked_count',0)} / required_evidence={m.get('required_evidence_count',0)}
- BLOCKED 条目（节选）：
{chr(10).join(items) if items else '(无)'}
- 必补证据Top（原始清单）：
{req_top_txt if req_top_txt else '(无)'}

输出（直接输出Markdown，不要解释）：
## 评审结论摘要
- 用 2-4 条说明当前为什么卡住/能不能开工（同时说明「发布门禁/开工门禁」的含义）

## 下一步（按优先级）
- 将必补项归并成 3-6 条“行动项”，每条以动词开头（补齐/冻结/明确/统一…），不要重复同一含义
"""

        txt = call_llm_with_retry(
            messages=[{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            timeout=int(timeout or 35),
            max_retries=1,
            retry_delay=2,
        )
        summary = str(txt or "").strip()
        if not summary:
            return None, None, "LLM 返回为空"

        # Derive a compact next-step list for UI "Top" from the same output.
        # (We keep it lightweight: extract bullet lines under '下一步' section if present.)
        lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
        tops: List[str] = []
        in_next = False
        for ln in lines:
            if ln.startswith("##"):
                in_next = ("下一步" in ln)
                continue
            if in_next and (ln.startswith("- ") or ln.startswith("• ")):
                tops.append(re.sub(r"^[-•]\s+", "", ln).strip())
        tops = _dedupe_str([t for t in tops if t], limit=8)
        return summary, tops, None
    except Exception as e:
        return None, None, str(e)


def _extract_first_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    start = raw.find("{")
    if start == -1:
        return {}
    depth = 0
    in_string: Optional[str] = None
    escape = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
                continue
            if c == in_string:
                in_string = None
            continue
        if c in ('"', "'"):
            in_string = c
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start : i + 1])
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    return {}
    return {}


def _llm_polish_blockers_and_decisions(
    *,
    llm_config_path: str,
    blockers: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
    timeout: int = 35,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    LLM-first polish: rewrite summaries into product-readable, forwardable sentences.
    Updates are applied by title matching (stable enough for MVP).
    Returns (changed_titles, error).
    """
    try:
        bs = []
        for b in (blockers or [])[:6]:
            if not isinstance(b, dict):
                continue
            bs.append(
                {
                    "title": _clean_text(b.get("title") or b.get("type") or "未命名"),
                    "status": _clean_text(b.get("status") or "OPEN").upper(),
                    "risk_level": _clean_text(b.get("risk_level") or "P2").upper(),
                    "required_evidence": _dedupe_str(_ensure_list(b.get("required_evidence")), limit=6),
                    "human_summary": _clean_text(b.get("human_summary") or ""),
                }
            )
        ds = []
        for d in (decisions or [])[:12]:
            if not isinstance(d, dict):
                continue
            ds.append(
                {
                    "title": _clean_text(d.get("title") or d.get("description") or "未命名"),
                    "status": _clean_text(d.get("status") or "BLOCKED").upper(),
                    "risk_level": _clean_text(d.get("risk_level") or "P2").upper(),
                    "owner": _clean_text(d.get("owner") or "PM"),
                    "decision": _clean_text(d.get("decision") or d.get("conclusion") or ""),
                    "required_evidence": _dedupe_str(_ensure_list(d.get("required_evidence")), limit=6),
                    "human_summary": _clean_text(d.get("human_summary") or ""),
                }
            )

        prompt = f"""你是产品评审秘书。请把输入里的 Blockers 与 Decisions 的“摘要(human_summary)”改写成产品/研发/测试都能直接转发理解的句式。

硬约束：
1) 不要新增事实/数值/字段名/错误码：只能重写表达与归并同义项。
2) 不要使用“模板/人话/大模型/规则”等内部词。
3) 每条摘要最多 2-3 句，必须包含：问题/结论 + 影响/范围 + 下一步（若是 BLOCKED 则指出要补什么）。
4) 输出必须是 JSON（不要 Markdown，不要解释）。

输入 JSON：
{json.dumps({'blockers': bs, 'decisions': ds}, ensure_ascii=False)}

输出 JSON schema：
{{
  "blockers": [{{"title": "...", "human_summary": "..."}}],
  "decisions": [{{"title": "...", "human_summary": "..."}}]
}}
"""

        txt = call_llm_with_retry(
            messages=[{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            timeout=int(timeout or 35),
            max_retries=1,
            retry_delay=2,
        )
        obj = _extract_first_json_object(str(txt or ""))
        if not obj:
            return None, "LLM 输出非JSON"

        changed: List[str] = []
        bmap: Dict[str, str] = {}
        for it in obj.get("blockers") if isinstance(obj.get("blockers"), list) else []:
            if isinstance(it, dict) and _clean_text(it.get("title")) and _clean_text(it.get("human_summary")):
                bmap[_clean_text(it.get("title"))] = _clean_text(it.get("human_summary"))
        dmap: Dict[str, str] = {}
        for it in obj.get("decisions") if isinstance(obj.get("decisions"), list) else []:
            if isinstance(it, dict) and _clean_text(it.get("title")) and _clean_text(it.get("human_summary")):
                dmap[_clean_text(it.get("title"))] = _clean_text(it.get("human_summary"))

        for b in blockers or []:
            t = _clean_text(b.get("title") or b.get("type") or "")
            if t and t in bmap:
                b["human_summary"] = bmap[t]
                changed.append(t)
        for d in decisions or []:
            t = _clean_text(d.get("title") or d.get("description") or "")
            if t and t in dmap:
                d["human_summary"] = dmap[t]
                changed.append(t)
        return _dedupe_str(changed, limit=40), None
    except Exception as e:
        return None, str(e)


def export_meeting_markdown(meeting: Dict[str, Any]) -> str:
    gate = meeting.get("gate") if isinstance(meeting.get("gate"), dict) else {}
    blockers = meeting.get("blockers") if isinstance(meeting.get("blockers"), list) else []
    claims = meeting.get("claims") if isinstance(meeting.get("claims"), list) else []
    meta = meeting.get("meta") if isinstance(meeting.get("meta"), dict) else {}
    lines: List[str] = []
    lines.append("# 多Agent评审结论文档（QA主导）")
    lines.append("")
    lines.append(f"- 生成时间：{_now_str()}")
    if meta.get("snapshot_id"):
        lines.append(f"- 输入快照：{meta.get('snapshot_id')}")
    if meta.get("mode"):
        lines.append(f"- 会议模式：{meta.get('mode')}")
    lines.append("")
    lines.append("## 1. QA Gate")
    lines.append("")
    lines.append(f"- 结论：**{gate.get('gate', '—')}**")
    lines.append(f"- 原因：{gate.get('reason', '—')}")
    m = gate.get("metrics") if isinstance(gate.get("metrics"), dict) else {}
    lines.append(f"- 指标：P0={m.get('p0_count', 0)} / BLOCKED={m.get('blocked_count', 0)} / required_evidence={m.get('required_evidence_count', 0)}")
    req = gate.get("required_evidence_top") if isinstance(gate.get("required_evidence_top"), list) else []
    if req:
        lines.append("- 必补证据Top：")
        for x in req[:10]:
            lines.append(f"  - {str(x)}")
    lines.append("")
    if claims:
        lines.append("## 2. Claim-Check（关键结论校验：必须可回溯到PRD原文）")
        lines.append("")
        sup = sum(1 for c in claims if isinstance(c, dict) and str(c.get("status") or "").upper() == "SUPPORTED")
        uns = sum(1 for c in claims if isinstance(c, dict) and str(c.get("status") or "").upper() == "UNSUPPORTED")
        lines.append(f"- 汇总：SUPPORTED={sup} / UNSUPPORTED={uns}")
        lines.append("")
        for c in claims[:16]:
            if not isinstance(c, dict):
                continue
            lines.append(f"### {c.get('claim','—')}")
            lines.append(f"- 状态：{c.get('status','—')}（重要性：{c.get('importance','P1')}）")
            qs = c.get("evidence_quotes") if isinstance(c.get("evidence_quotes"), list) else []
            if qs:
                lines.append("- 原文证据：")
                for q in qs[:2]:
                    lines.append(f"  - “{q}”")
            reqc = c.get("required_evidence") if isinstance(c.get("required_evidence"), list) else []
            if reqc:
                lines.append("- 必补证据：")
                for x in reqc[:3]:
                    lines.append(f"  - {str(x)}")
            lines.append("")
    lines.append("## 3. P0 Blockers（含必补证据/AC/观测字段）")
    lines.append("")
    if not blockers:
        lines.append("- （无）")
    for b in blockers:
        lv = str(b.get("risk_level") or "P2").upper()
        if lv != "P0":
            continue
        lines.append(f"### {b.get('title') or '未命名'}（{lv}）")
        lines.append(f"- 模块：{b.get('module')}")
        lines.append(f"- 锚点：{b.get('anchor') or '【PRD未说明】'}")
        lines.append(f"- 证据等级：{b.get('evidence_class')}")
        quotes = b.get("evidence_quotes") if isinstance(b.get("evidence_quotes"), list) else []
        if quotes:
            lines.append("- 原文摘录：")
            for q in quotes[:2]:
                lines.append(f"  - “{q}”")
        ac = b.get("ac") if isinstance(b.get("ac"), dict) else {}
        lines.append("- AC：")
        lines.append(f"  - Given：{ac.get('given', '')}")
        lines.append(f"  - When：{ac.get('when', '')}")
        lines.append(f"  - Then：{ac.get('then', '')}")
        obs = b.get("observability") if isinstance(b.get("observability"), list) else []
        if obs:
            lines.append(f"- 最小观测字段：{', '.join([str(x) for x in obs[:18]])}")
        req2 = b.get("required_evidence") if isinstance(b.get("required_evidence"), list) else []
        if req2:
            lines.append("- 必补证据：")
            for x in req2[:8]:
                lines.append(f"  - {str(x)}")
        lines.append("")
    patch = meeting.get("prd_patch") if isinstance(meeting.get("prd_patch"), str) else ""
    if patch.strip():
        lines.append("## 4. PRD Patch（可回写附录）")
        lines.append("")
        lines.append(patch.strip())
        lines.append("")
    msgs = meeting.get("messages") if isinstance(meeting.get("messages"), list) else []
    if msgs:
        lines.append("## 5. 讨论过程（按Round）")
        lines.append("")
        for m0 in msgs[:200]:
            if not isinstance(m0, dict):
                continue
            rnd = m0.get("round", 1)
            sp = m0.get("speaker", "—")
            it = m0.get("issue_title", "—")
            txt = m0.get("text", "")
            lines.append(f"- Round {rnd} · {it} · {sp}")
            for ln in str(txt).splitlines()[:16]:
                lines.append(f"  {ln}")
            lines.append("")
    return "\n".join(lines).strip()


def export_prd_v2_markdown(meeting: Dict[str, Any]) -> str:
    """
    PRD v2.0 (Executable Version) generated from V2 meeting outputs.
    V2-only: generate a merged, executable spec (not just a review/patch report).
    """
    meta = meeting.get("meta") if isinstance(meeting.get("meta"), dict) else {}
    gate = meeting.get("gate") if isinstance(meeting.get("gate"), dict) else {}
    blockers = meeting.get("blockers") if isinstance(meeting.get("blockers"), list) else []
    decisions = meeting.get("decisions") if isinstance(meeting.get("decisions"), list) else []
    claims = meeting.get("claims") if isinstance(meeting.get("claims"), list) else []
    patch = meeting.get("prd_patch") if isinstance(meeting.get("prd_patch"), str) else ""
    prd_source = meeting.get("prd_source") if isinstance(meeting.get("prd_source"), str) else ""

    def _extract_prd_sections(src: str) -> Dict[str, List[str]]:
        """
        Heuristic PRD reflow: split PRD into coarse sections so v2.0 can embed original content
        into the executable structure (A mode: allow TBD).
        """
        s = (src or "").replace("\r\n", "\n").replace("\r", "\n")
        lines0 = [ln.strip() for ln in s.split("\n")]
        # Keep short, meaningful lines (avoid huge dumps)
        lines0 = [_clean_text(x) for x in lines0 if _clean_text(x) and len(_clean_text(x)) <= 240]
        buckets: Dict[str, List[str]] = {"background": [], "goals": [], "scope": [], "requirements": [], "other": []}
        cur = "other"
        for ln in lines0:
            if re.search(r"(背景|现状|痛点|why)", ln, re.I):
                cur = "background"
            elif re.search(r"(目标|目的|success|验收目标)", ln, re.I):
                cur = "goals"
            elif re.search(r"(范围|适用|型号|版本|终端|设备|不支持|支持)", ln, re.I):
                cur = "scope"
            elif re.search(r"(需求描述|功能|规则|交互|流程|接口|字段|状态|异常|错误码|权限)", ln, re.I):
                cur = "requirements"
            buckets.setdefault(cur, [])
            buckets[cur].append(ln)
        # de-dupe
        for k in list(buckets.keys()):
            buckets[k] = _dedupe_str(buckets[k], limit=60)
        return buckets

    # --- Enterprise-ish PRD template (close to user-provided) ---
    lines: List[str] = []
    formal_gate = str(gate.get("formal_gate") or gate.get("gate") or "—").upper()
    if formal_gate == "PASS":
        doc_kind = "正式冻结版（合格PRD）"
    else:
        doc_kind = "临时开工过渡版（非正式冻结PRD）"
    lines.append(f"# 企业标准·完整可落地PRD（PRD v2.0 / {doc_kind}）")
    lines.append("")
    lines.append("## 文档基础信息")
    lines.append(f"- 文档名称：{_clean_text(meta.get('doc_title') or 'TBD')}")
    lines.append(f"- 版本号：{_clean_text(meta.get('doc_version') or 'V2.0')}")
    lines.append(f"- 生成日期：{_now_str()}")
    lines.append(f"- 业务模块：{_clean_text(meta.get('biz_module') or 'TBD')}")
    lines.append(f"- 关联终端：{_clean_text(meta.get('terminals') or 'TBD')}")
    lines.append(f"- 正式冻结门禁（Formal Gate）：{gate.get('formal_gate', gate.get('gate','—'))}（{gate.get('formal_reason', gate.get('reason','—'))}）")
    lines.append(f"- 临时开工门禁（Work-start Gate）：{gate.get('work_gate', '—')}（{gate.get('work_reason', '—')}）")
    if formal_gate != "PASS":
        lines.append("- 重要提示：**本版本不合格**（Formal Gate 未通过）。仅允许按 AUTO_PROPOSED 临时开工推进拆任务/写用例，禁止作为最终验收/发布依据。")
    lines.append(f"- 干系人：{_clean_text(meta.get('stakeholders') or '产品/UI/客户端/服务端/测试/硬件（TBD）')}")
    if meta.get("snapshot_id"):
        lines.append(f"- 关联输入：{meta.get('snapshot_id')}")
    lines.append("")

    lines.append("## 0. 变更摘要 & 版本履历")
    lines.append("")
    lines.append("### 0.1 版本迭代")
    lines.append("")
    lines.append("| 版本 | 日期 | 更新人 | 更新内容 |")
    lines.append("|------|------|--------|----------|")
    lines.append("| V1.0 | TBD  | TBD    | 原始需求初稿 |")
    lines.append("| V2.0 | TBD  | AI     | 新增规范、验收口径、问题补齐（含 AUTO_PROPOSED 默认口径） |")
    lines.append("")
    lines.append("### 0.2 本次补丁重点")
    lines.append("")
    if patch.strip():
        p_lines = [ln.strip() for ln in patch.splitlines() if ln.strip()]
        if p_lines and p_lines[0].startswith("###"):
            p_lines = p_lines[1:]
        for ln in p_lines[:12]:
            lines.append("- " + ln.lstrip("- ").strip()[:200])
    else:
        lines.append("- （本次未生成补丁条款）")
    lines.append("")

    lines.append("## 1. 背景说明 & 名词定义")
    lines.append("")
    lines.append("### 1.1 业务背景")
    if prd_source.strip():
        prd_sec = _extract_prd_sections(prd_source)
        for ln in (prd_sec.get("background") or [])[:8]:
            lines.append(f"- {ln}")
    else:
        lines.append("- TBD")
    lines.append("")
    lines.append("### 1.2 名词释义")
    lines.append("- （TBD：可在此补充名词表）")
    lines.append("")

    lines.append("## 2. 整体范围 & 约束（必补QA锚点）")
    lines.append("")
    lines.append("### 2.1 功能范围")
    lines.append("- TBD")
    lines.append("")
    lines.append("### 2.2 终端范围【关键补项】")
    lines.append("| 终端 | 是否支持 | 证据/依据 | 备注 |")
    lines.append("|------|----------|-----------|------|")
    if not claims:
        lines.append("| TBD | TBD | 未生成 Claim-Check | - |")
    for c in (claims or [])[:18]:
        if not isinstance(c, dict):
            continue
        claim = _clean_text(c.get("claim") or "")
        if not claim or ("终端" not in claim and "展示" not in claim):
            continue
        st = str(c.get("status") or "UNSUPPORTED").upper()
        qs = c.get("evidence_quotes") if isinstance(c.get("evidence_quotes"), list) else []
        ev = _clean_text(qs[0]) if qs else "（无原文证据）"
        lines.append(f"| {claim} | {'SUPPORTED' if st=='SUPPORTED' else 'TBD'} | {ev} | {st} |")
    lines.append("")
    lines.append("### 2.3 业务约束&限制条件")
    lines.append("- TBD")
    lines.append("")

    lines.append("## 3. 整体业务规则 & 基础优先级")
    lines.append("")
    lines.append("### 3.1 全局展示优先级（固化为正式规则）")
    lines.append("- TBD")
    lines.append("")
    lines.append("### 3.2 多模式基础运行规则")
    lines.append("- TBD")
    lines.append("")

    lines.append("## 4. 核心功能详细需求（分模块写）")
    lines.append("")
    if prd_source.strip():
        for ln in (prd_sec.get("requirements") or [])[:14]:
            lines.append(f"- {ln}")
    else:
        lines.append("- TBD：按模块补齐（前置条件/正常流程/入口/展示/联动/退出）")
    lines.append("")

    lines.append("## 5. 并发冲突裁决矩阵【P0必补】")
    lines.append("")
    lines.append("### 5.1 全局优先级顺序（正式冻结）")
    lines.append("- 若本次会议已冻结项目口径，以《裁决矩阵》与《评审决议》为准；否则使用下列默认口径。")
    lines.append("")
    lines.append("### 5.2 多入口并发冲突规则")
    lines.append("- AUTO_PROPOSED：单用户维度 FIFO 队列 + 最大并发 N=3（超出排队）。")
    lines.append("- AUTO_PROPOSED：重复提交/重复开始 → 返回当前状态（幂等）。")
    lines.append("")
    lines.append("### 5.3 冲突弹窗&提示口径")
    lines.append("- AUTO_PROPOSED：排队提示“正在处理，请稍候”；幂等提示“操作已生效”。")
    lines.append("")

    lines.append("## 6. 完整状态机设计")
    lines.append("")
    lines.append("### 6.1 状态枚举定义")
    lines.append("| 状态 | 状态说明 |")
    lines.append("|------|----------|")
    lines.append("| IDLE | 空闲初始态 |")
    lines.append("| RECORDING | 对应功能运行中（示例） |")
    lines.append("| STOPPED | 完成态 |")
    lines.append("| ERROR | 异常态 |")
    lines.append("")
    lines.append("### 6.2 状态流转图（Mermaid）")
    lines.append("```mermaid")
    lines.append("stateDiagram-v2")
    lines.append("  [*] --> IDLE")
    lines.append("  IDLE --> RECORDING: start")
    lines.append("  RECORDING --> STOPPED: stop")
    lines.append("  RECORDING --> ERROR: failure/timeout/permission_denied")
    lines.append("  ERROR --> IDLE: recover (TBD)")
    lines.append("  STOPPED --> IDLE: done")
    lines.append("```")
    lines.append("")
    lines.append("### 6.3 中断/退出/切后台/杀掉重进 状态落点规则")
    lines.append("- AUTO_PROPOSED：客户端本地持久化（SQLite/本地KV）保存 state_before/state_after/request_id/error_code/updated_at。")
    lines.append("")
    lines.append("### 6.4 资源清理&资源保留清单")
    lines.append("- AUTO_PROPOSED：成功终态清理临时文件；失败终态保留以便重试；退出/切后台写入状态并释放临时句柄。")
    lines.append("")

    lines.append("## 7. 异常流程 & 容错策略【P0必补】")
    lines.append("")
    lines.append("### 7.1 异常场景覆盖")
    lines.append("- 接口失败 / 网络超时 / 弱网断网")
    lines.append("- 权限拒绝 / 资源不存在")
    lines.append("- 高并发挤压、请求拥堵")
    lines.append("")
    lines.append("### 7.2 重试策略（若未冻结则使用默认）")
    lines.append("- 若本次会议已冻结项目口径，以《裁决矩阵》与《评审决议》为准；否则按默认策略执行。")
    lines.append("")
    lines.append("### 7.3 降级方案 & 兜底策略")
    lines.append("- AUTO_PROPOSED：失败后保留草稿/缓存/任务记录，支持手动继续或取消。")
    lines.append("")

    lines.append("## 8. 错误码 & 前端提示文案字典")
    lines.append("")
    lines.append("| 错误码error_code | 异常场景 | 前端用户提示文案 | 是否可重试 | 处理方式 |")
    lines.append("|------------------|----------|------------------|------------|----------|")
    lines.append("| ERR_NETWORK | 弱网/断网/网络不可达 | 网络异常，请检查网络后重试 | 是 | 重试/保留草稿 |")
    lines.append("| ERR_TIMEOUT | 超时 | 请求超时，请稍后重试 | 是 | 重试/终止进入ERROR |")
    lines.append("| ERR_PERMISSION | 权限拒绝 | 权限不足，请开启权限后重试 | 否 | 引导授权 |")
    lines.append("| ERR_STORAGE_FULL | 存储满 | 存储空间不足，请清理后重试 | 否 | 释放空间 |")
    lines.append("| ERR_UNKNOWN | 兜底 | 操作失败，请稍后重试 | 视情况 | 兜底 |")
    lines.append("")

    lines.append("## 9. 权限设计 & 安全策略")
    lines.append("")
    lines.append("### 9.1 访问&操作权限范围")
    lines.append("- TBD（可按角色/终端/资源粒度补齐）")
    lines.append("")
    lines.append("### 9.2 身份&Token规则（有效期/一次性）")
    lines.append("- TBD")
    lines.append("")
    lines.append("### 9.3 越权拦截规则 & 提示")
    lines.append("- AUTO_PROPOSED：统一错误码 ERR_PERMISSION，提示“权限不足，请开启权限后重试”。")
    lines.append("")
    lines.append("### 9.4 防重放、防重复提交")
    lines.append("- AUTO_PROPOSED：request_id + idempotency_key 去重；重复请求返回当前状态。")
    lines.append("")
    lines.append("### 9.5 审计留痕字段")
    lines.append("- AUTO_PROPOSED：request_id / user_id / token_status / error_code / state_before/state_after / updated_at")
    lines.append("")

    lines.append("## 10. 验收标准AC（Given-When-Then 标准格式）")
    lines.append("")
    rows = [b for b in blockers if isinstance(b, dict) and str(b.get("risk_level") or "").upper() in ("P0", "P1")]
    lines.append("| 优先级 | 场景 | Given前置条件 | When触发动作 | Then预期结果 | 可观测字段 |")
    lines.append("|--------|------|---------------|--------------|--------------|------------|")
    if not rows:
        lines.append("| TBD | TBD | TBD | TBD | TBD | TBD |")
    else:
        for b in rows[:24]:
            ac = b.get("ac") if isinstance(b.get("ac"), dict) else {}
            obs = b.get("observability") if isinstance(b.get("observability"), list) else []
            lines.append(
                f"| {str(b.get('risk_level') or 'P2').upper()} | {_clean_text(ac.get('scene') or b.get('title') or '')} |"
                f" {_clean_text(ac.get('given') or '')} | {_clean_text(ac.get('when') or '')} | {_clean_text(ac.get('then') or '')} |"
                f" {', '.join([_clean_text(x) for x in obs[:10] if _clean_text(x)])} |"
            )
    lines.append("")

    lines.append("## 11. 埋点 & 最小观测字段（对账/日志/线上排查）")
    lines.append("")
    lines.append("- 通用基础字段：request_id, user_id, error_code, result_status, updated_at")
    lines.append("- 业务专属字段：TBD")
    lines.append("- 追踪字段：idempotency_key, token_status, state_before, state_after")
    lines.append("")

    # Formal PASS should not ship with a "TBD 待跟进清单" section.
    if formal_gate != "PASS":
        lines.append("## 12. 未解决问题 & TBD待跟进清单")
        lines.append("")
        lines.append("| 优先级 | 问题描述 | 影响范围 | 计划补齐时间 |")
        lines.append("|--------|----------|----------|--------------|")
        todo: List[str] = []
        for b in blockers:
            if not isinstance(b, dict):
                continue
            lv = str(b.get("risk_level") or "").upper()
            req = b.get("required_evidence") if isinstance(b.get("required_evidence"), list) else []
            for r in req[:3]:
                if _clean_text(r):
                    todo.append(f"[{lv}] {str(b.get('title') or '')}: {str(r)}")
        for c in claims:
            if isinstance(c, dict) and str(c.get("status") or "").upper() == "UNSUPPORTED":
                imp = str(c.get("importance") or "P1").upper()
                todo.append(f"[{imp}] Claim: {str(c.get('claim') or '')}（补原文条款/锚点）")
        todo = _dedupe_str(todo, limit=24)
        if not todo:
            lines.append("| — | （无） | — | — |")
        else:
            for x in todo[:18]:
                lines.append(f"| {x.split(']')[0].lstrip('[')} | {x.split(']',1)[-1].strip()} | TBD | TBD |")
        lines.append("")
    lines.append("")
    lines.append("| 优先级 | 问题描述 | 影响范围 | 计划补齐时间 |")
    lines.append("|--------|----------|----------|--------------|")
    todo: List[str] = []
    for b in blockers:
        if not isinstance(b, dict):
            continue
        lv = str(b.get("risk_level") or "").upper()
        req = b.get("required_evidence") if isinstance(b.get("required_evidence"), list) else []
        for r in req[:3]:
            if _clean_text(r):
                todo.append(f"[{lv}] {str(b.get('title') or '')}: {str(r)}")
    for c in claims:
        if isinstance(c, dict) and str(c.get("status") or "").upper() == "UNSUPPORTED":
            imp = str(c.get("importance") or "P1").upper()
            todo.append(f"[{imp}] Claim: {str(c.get('claim') or '')}（补原文条款/锚点）")
    todo = _dedupe_str(todo, limit=24)
    if not todo:
        lines.append("| — | （无） | — | — |")
    else:
        for x in todo[:18]:
            lines.append(f"| {x.split(']')[0].lstrip('[')} | {x.split(']',1)[-1].strip()} | TBD | TBD |")
    lines.append("")

    lines.append("## 13. 可追溯矩阵 & 锚点引用")
    lines.append("")
    lines.append("- Claim-Check：")
    for i, c in enumerate((claims or [])[:16], start=1):
        if not isinstance(c, dict):
            continue
        st = str(c.get("status") or "UNSUPPORTED").upper()
        imp = str(c.get("importance") or "P1").upper()
        lines.append(f"  - Claim#{i} [{imp}/{st}] {str(c.get('claim') or '')[:160]}")
    lines.append("")

    lines.append("## 14. 附录")
    lines.append("")
    lines.append("### 14.1 变更附录（Patch，来源于评审讨论）")
    lines.append(patch.strip() if patch.strip() else "- （无）")
    lines.append("")
    lines.append("### 14.2 原始 PRD（输入原文，便于对照）")
    if prd_source.strip():
        lines.append("```")
        lines.append(prd_source[:20000])
        lines.append("```")
    else:
        lines.append("- （未保存原文）")
    lines.append("")

    # Embed original PRD content into the executable structure (A mode)
    prd_sec = _extract_prd_sections(prd_source)
    if any(prd_sec.get(k) for k in ("background", "goals", "scope", "requirements")):
        lines.append("## 2.1 原始 PRD 内容重排（原文摘录，已按执行结构归位）")
        lines.append("")
        if prd_sec.get("background"):
            lines.append("### 背景（原文摘录）")
            lines.append("")
            for ln in prd_sec["background"][:10]:
                lines.append(f"- {ln}")
            lines.append("")
        if prd_sec.get("goals"):
            lines.append("### 目标（原文摘录）")
            lines.append("")
            for ln in prd_sec["goals"][:10]:
                lines.append(f"- {ln}")
            lines.append("")
        if prd_sec.get("scope"):
            lines.append("### 范围/约束（原文摘录）")
            lines.append("")
            for ln in prd_sec["scope"][:16]:
                lines.append(f"- {ln}")
            lines.append("")
        if prd_sec.get("requirements"):
            lines.append("### 需求描述/规则（原文摘录）")
            lines.append("")
            for ln in prd_sec["requirements"][:24]:
                lines.append(f"- {ln}")
            lines.append("")

    lines.append("## 3. 状态机规范（State Machine Spec）")
    lines.append("")
    lines.append("- 目标：让多端实现口径一致（UI/日志/接口）。")
    lines.append("")
    lines.append("| state | meaning | entry_condition | exit_condition | ui | observability |")
    lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
    lines.append("| IDLE | 初始/未录制 | 页面进入/功能关闭 | start_recording | 按钮可点击 | state_before/state_after |")
    lines.append("| RECORDING | 录制中 | start_recording | stop_recording 或 error | 显示录制中+计时 | duration_s, error_code |")
    lines.append("| STOPPED | 已完成 | stop_recording | done | 显示已保存/入口 | file_id, file_size |")
    lines.append("| ERROR | 异常 | error | recover(TBD) | 统一错误提示 | error_code, error_message |")
    lines.append("")
    lines.append("```mermaid")
    lines.append("stateDiagram-v2")
    lines.append("  [*] --> IDLE")
    lines.append("  IDLE --> RECORDING: start_recording")
    lines.append("  RECORDING --> STOPPED: stop_recording")
    lines.append("  RECORDING --> ERROR: failure/timeout/permission_denied")
    lines.append("  ERROR --> IDLE: recover (TBD)")
    lines.append("  STOPPED --> IDLE: done")
    lines.append("```")
    lines.append("")

    lines.append("## 4. 错误码与提示文案字典（Exception Dictionary）")
    lines.append("")
    blob = "\n".join([patch or ""] + [_clean_text(d.get("decision") or "") for d in decisions if isinstance(d, dict)])
    codes = _dedupe_str(re.findall(r"\b\d{3,5}\b|ERR_[A-Z_]+|E_[A-Z_]+|RECORD_[A-Z_]+", blob), limit=18)
    lines.append("| error_code | user_message | retryable | handling | source |")
    lines.append("| :--- | :--- | :--- | :--- | :--- |")
    if not codes:
        lines.append("| TBD | TBD | TBD | TBD | 本次产物未抽取到明确错误码 |")
    for c in codes[:18]:
        hint = ""
        for ln in (patch or "").splitlines():
            if str(c) in ln:
                hint = _clean_text(ln)
                break
        lines.append(f"| {c} | （待定：补齐具体文案） | TBD | {hint or '按异常流程处理'} | Patch/裁决抽取 |")
    lines.append("")

    lines.append("## 5. 验收口径（AC）与最小观测字段（开测必需）")
    lines.append("")
    rows = [b for b in blockers if isinstance(b, dict) and str(b.get("risk_level") or "").upper() in ("P0", "P1")]
    if not rows:
        lines.append("- （无）")
    else:
        lines.append("| priority | scene | Given | When | Then | observability | trace |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for idx, b in enumerate(rows[:24], start=1):
            ac = b.get("ac") if isinstance(b.get("ac"), dict) else {}
            obs = b.get("observability") if isinstance(b.get("observability"), list) else []
            trace = f"Blocker#{idx} {b.get('anchor') or ''}".strip()
            lines.append(
                f"| {str(b.get('risk_level') or 'P2').upper()} | {_clean_text(ac.get('scene') or b.get('title') or '')} |"
                f" {_clean_text(ac.get('given') or '')} | {_clean_text(ac.get('when') or '')} | {_clean_text(ac.get('then') or '')} |"
                f" {', '.join([_clean_text(x) for x in obs[:10] if _clean_text(x)])} | {trace} |"
            )
    lines.append("")

    # 5.1 TBD fill-in checklist (A mode: tell PM exactly what to fill)
    lines.append("## 5.1 TBD 待补清单（按可开工优先级）")
    lines.append("")
    todo: List[str] = []
    for b in blockers:
        if not isinstance(b, dict):
            continue
        lv = str(b.get("risk_level") or "").upper()
        req = b.get("required_evidence") if isinstance(b.get("required_evidence"), list) else []
        for r in req[:6]:
            if _clean_text(r):
                todo.append(f"[{lv}] {str(b.get('title') or '')}: {str(r)}")
    # also include unsupported claims as TBD
    for c in claims:
        if not isinstance(c, dict):
            continue
        if str(c.get("status") or "").upper() != "UNSUPPORTED":
            continue
        imp = str(c.get("importance") or "P1").upper()
        todo.append(f"[{imp}] Claim: {str(c.get('claim') or '')}（补原文条款/锚点）")
    todo = _dedupe_str(todo, limit=40)
    if not todo:
        lines.append("- （无 TBD：当前已具备开工条件）")
    else:
        for x in todo[:24]:
            lines.append(f"- {x}")
        lines.append("")
        lines.append("TBD 填写模板建议：")
        lines.append("- **裁决矩阵**：补齐优先级/裁决规则（谁覆盖谁、冲突时按谁为准）。")
        lines.append("- **状态机**：补齐 ERROR 恢复策略（自动重试/手动触发/重启恢复）与终态定义。")
        lines.append("- **错误码字典**：补齐每个 error_code 的 user_message（可操作、可定位）。")
        lines.append("- **AC**：补齐可复现前置条件与可观测字段（日志/埋点/字段枚举）。")
    lines.append("")

    # 5.2 Auto-proposed executable defaults (platform-generic) to unblock implementation
    # This is an "assistant fill-in" layer: concrete values, clearly labeled as AUTO_PROPOSED.
    if todo:
        lines.append("## 5.2 AUTO_PROPOSED：可执行默认口径（用于先开工，后补证据再收敛）")
        lines.append("")
        lines.append("> 说明：以下为平台通用默认口径，用于把 TBD 变成“可执行实现”。它不是 PRD 原文；请 PM/业务在补齐证据后替换为最终口径。")
        lines.append("")
        lines.append("### 5.2.1 并发/冲突裁决（P0 默认）")
        lines.append("")
        lines.append("- 并发模型：**单用户维度 FIFO 队列 + 最大并发 N=3**（超出进入排队，不丢弃）。")
        lines.append("- 冲突裁决函数（Decision Function）：")
        lines.append("  - 若存在 **手动操作** 与 **自动规则** 同时命中：**手动 > 自动**。")
        lines.append("  - 若多个入口同时触发同一动作：以 `request_id` 时间顺序裁决，后到请求进入队列等待。")
        lines.append("  - 若发生互斥（例如重复提交/重复开始）：要求幂等，重复请求返回当前状态，不重复生效。")
        lines.append("- 提示口径：统一提示“正在处理，请稍候”（排队） / “操作已生效”（幂等命中）。")
        lines.append("")
        lines.append("### 5.2.2 失败/超时/弱网：重试与降级（P0 默认）")
        lines.append("")
        lines.append("- 自动重试：仅对**幂等**操作自动重试，指数退避：10s / 30s / 60s，最多 3 次。")
        lines.append("- 非幂等操作：不自动重试，提示用户手动重试，并展示上次失败错误码。")
        lines.append("- 超时：默认 `timeout_ms=15000`（可配置）；超过即失败进入 ERROR。")
        lines.append("- 降级/补偿：失败后保留任务/草稿/本地缓存，支持用户手动继续。")
        lines.append("")
        lines.append("### 5.2.3 状态落点/恢复策略（P0 默认）")
        lines.append("")
        lines.append("- 状态落点：客户端本地持久化（SQLite/本地KV）保存 `state_before/state_after/request_id/error_code/updated_at`。")
        lines.append("- 重进恢复：应用重启/重进后读取持久化状态，若处于 RECORDING/UPLOADING 等进行态：恢复为可解释的中间态并允许用户继续/取消。")
        lines.append("- 资源清理：退出/切后台时按策略写入状态并释放临时句柄；成功终态清理临时文件，失败终态保留以便重试。")
        lines.append("")
        lines.append("### 5.2.4 错误码与文案（P0 默认字典）")
        lines.append("")
        lines.append("| error_code | user_message | retryable | notes |")
        lines.append("| :--- | :--- | :--- | :--- |")
        lines.append("| ERR_NETWORK | 网络异常，请检查网络后重试 | yes | 弱网/断网/网络不可达 |")
        lines.append("| ERR_TIMEOUT | 请求超时，请稍后重试 | yes | 超时 |")
        lines.append("| ERR_PERMISSION | 权限不足，请开启权限后重试 | no | 权限拒绝 |")
        lines.append("| ERR_STORAGE_FULL | 存储空间不足，请清理后重试 | no | 存储满 |")
        lines.append("| ERR_UNKNOWN | 操作失败，请稍后重试 | maybe | 兜底 |")
        lines.append("")
        lines.append("### 5.2.5 终端范围（若 Claim-Check UNSUPPORTED 的默认处理）")
        lines.append("")
        lines.append("- 若 PRD 未明确终端范围：默认按“当前 PRD 提到的端”为准；未提到的端标记为 **NOT_SUPPORTED**，避免隐性扩散。")
        lines.append("- 建议 PM 在《范围总表》补充：端列表 + 每端差异（UI/权限/上传能力）。")
        lines.append("")

    lines.append("## 6. 变更附录（Patch，来源于评审讨论）")
    lines.append("")
    lines.append(patch.strip() if patch.strip() else "- （无）")
    lines.append("")

    lines.append("## 7. 原始 PRD（输入原文，便于对照）")
    lines.append("")
    if prd_source.strip():
        lines.append("```")
        lines.append(prd_source[:20000])
        lines.append("```")
    else:
        lines.append("- （未保存原文）")
    lines.append("")

    lines.append("## 8. Traceability（可追溯矩阵）")
    lines.append("")
    lines.append("- 规则：每条结论/条款都应能追溯到 Claim-Check（证据）或 required_evidence（缺口）。")
    if claims:
        lines.append("- Claim-Check 索引：")
        for i, c in enumerate(claims[:16], start=1):
            st = str(c.get("status") or "UNSUPPORTED").upper()
            imp = str(c.get("importance") or "P1").upper()
            lines.append(f"  - Claim#{i} [{imp}/{st}] {str(c.get('claim') or '')[:160]}")
    lines.append("")
    return "\n".join(lines).strip()


@dataclass
class MeetingV2:
    meeting_id: str
    meta: Dict[str, Any]
    prd_source: str
    agenda: List[Dict[str, Any]]
    decisions: List[Dict[str, Any]]
    blockers: List[Dict[str, Any]]
    claims: List[Dict[str, Any]]
    gate: Dict[str, Any]
    messages: List[Dict[str, Any]]
    prd_patch: str
    prd_v2: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "meta": self.meta,
            "prd_source": self.prd_source,
            "agenda": self.agenda,
            "decisions": self.decisions,
            "blockers": self.blockers,
            "claims": self.claims,
            "gate": self.gate,
            "messages": self.messages,
            "prd_patch": self.prd_patch,
            "prd_v2": self.prd_v2,
            "created_at": self.created_at,
        }


# In-memory meetings (MVP). Persisted snapshots are stored separately.
_MEETINGS_V2: Dict[str, MeetingV2] = {}


def create_meeting_from_stage3(
    stage3_output: Dict[str, Any],
    *,
    snapshot_id: str,
    mode: str = "snapshot",
    prd_content: str = "",
) -> MeetingV2:
    defects = stage3_output.get("defects") if isinstance(stage3_output, dict) else []
    defects = defects if isinstance(defects, list) else []
    # Only take P0 + key P1 for QA-led workbench (bounded).
    sorted_defects = sorted(
        [d for d in defects if isinstance(d, dict)],
        key=lambda d: (_risk_rank(str(d.get("risk_level") or "")), -len(str(d.get("description") or ""))),
    )
    picked: List[Dict[str, Any]] = []
    for d in sorted_defects:
        lv = str(d.get("risk_level") or "").upper()
        if lv not in ("P0", "P1"):
            continue
        picked.append(d)
        if len(picked) >= 12:
            break
    blockers = [build_blocker_item(d, prd_content=prd_content) for d in picked]
    # Promote P0 blockers to top
    blockers.sort(key=lambda b: (_risk_rank(str(b.get("risk_level") or "")), str(b.get("title") or "")))
    # QA kickoff agenda + bounded deterministic discussion (MVP).
    agenda: List[Dict[str, Any]] = []
    for idx, b in enumerate(blockers[:8], start=1):
        agenda.append(
            {
                "order": idx,
                "risk_level": str(b.get("risk_level") or "P2").upper(),
                "title": _clean_text(b.get("title") or ""),
                "focus": _dedupe_str(_ensure_list(b.get("required_evidence")), limit=3),
            }
        )
    decisions: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    # Kickoff message (WeChat-like): QA initiates agenda
    kickoff_lines = ["本次会议议程（QA 发起，按顺序收敛）："]
    if agenda:
        for a in agenda[:8]:
            focus = "；".join(a.get("focus") or []) or "（待补口径/证据）"
            kickoff_lines.append(f"- {a.get('order')}. [{a.get('risk_level')}] {a.get('title')}：{focus}")
    else:
        kickoff_lines.append("- （未抽到可讨论议题）")
    messages.append(
        {
            "role": "assistant",
            "speaker": "测试（QA）",
            "ts": _now_str(),
            "round": 0,
            "issue_title": "会议议程",
            "text": "\n".join(kickoff_lines),
        }
    )
    for b in blockers[:8]:
        messages.extend(_build_discussion_round(b, round_no=1))
        decisions.append(
            {
                "title": _clean_text(b.get("title") or ""),
                "risk_level": str(b.get("risk_level") or "P2").upper(),
                "status": str(b.get("status") or "OPEN").upper(),
                "owner": str(b.get("owner") or "PM"),
                "required_evidence": _dedupe_str(_ensure_list(b.get("required_evidence")), limit=4),
                "decision": "待补证据/口径后再冻结" if str(b.get("status") or "").upper() == "BLOCKED" else "可进入拆任务与验收",
            }
        )
    claims = build_claim_checks(
        stage3_output=stage3_output if isinstance(stage3_output, dict) else {},
        prd_content=prd_content or "",
        decisions=decisions,
        blockers=blockers,
        limit=14,
    )
    meeting_id = f"rmv2_{uuid.uuid4().hex[:10]}"
    meta = {
        "snapshot_id": snapshot_id,
        "mode": mode,
        "source": "stage3_output",
    }
    m = MeetingV2(
        meeting_id=meeting_id,
        meta=meta,
        prd_source=_clean_text(prd_content) if prd_content else "",
        agenda=agenda,
        decisions=decisions,
        blockers=blockers,
        claims=claims,
        gate=build_meeting_gate(blockers, claims),
        messages=messages,
        prd_patch=_build_patch_from_blockers(blockers),
        prd_v2="",  # computed below
        created_at=_now_str(),
    )
    # 必出产物：PRD v2.0（Executable Version）
    try:
        m.prd_v2 = export_prd_v2_markdown(m.to_dict())
    except Exception:
        m.prd_v2 = ""
    _MEETINGS_V2[meeting_id] = m
    return m


def quick_scan_for_meeting(*, prd_text: str, limit: int = 6) -> List[Dict[str, Any]]:
    """
    Lightweight scan for V2 meeting: fast, platform-generic, no dependency on 6-stage audit pipeline.
    Produces a small list of "defect-like" dicts that can be fed into build_blocker_item().
    """
    text = (prd_text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _clean_text(text)
    if not t:
        return []

    def _has(rx: str) -> bool:
        try:
            return bool(re.search(rx, t, flags=re.I))
        except Exception:
            return False

    # Heuristic: only create blockers when PRD likely misses the spec.
    # Keep small, stable, and platform-generic.
    candidates: List[Dict[str, Any]] = []

    # P0: concurrency / conflict arbitration missing
    if _has(r"(并发|同时|抢占|互斥|冲突|顺序|排队|队列)") and (not _has(r"(裁决|优先级矩阵|互斥规则|幂等|去重|锁|队列)")):
        # Prefer PRD-native wording (often no literal "并发" word)
        kws = ["互不干涉", "三种模式", "点歌", "投屏", "游戏", "优先级"]
        quotes = _find_quote_by_keywords(text, kws, limit=1)
        candidates.append(
            {
                "id": "qk_concurrency",
                "risk_level": "P0",
                "module": "业务流程",
                "type": "并发操作未定义",
                "title": "并发操作未定义",
                "description": "PRD 提到并发/顺序/互斥，但未给出冲突裁决函数、幂等/去重、排队策略与错误码/提示口径。",
                "anchor": "",
                "evidence_quotes": quotes,
            }
        )

    # P0: exception/timeout/retry/resume missing
    if _has(r"(弱网|失败|异常|超时|重试|降级|恢复|回退|中断|退出|重进)") and (not _has(r"(timeout_ms|超时\\s*\\d+|重试\\s*\\d+|退避|终止条件|错误码|补偿|恢复策略|状态机)")):
        # Prefer PRD-native lines (进入/退出/提示/切断等)
        kws = ["退出", "进入", "提示", "切断", "超时", "失败"]
        quotes = _find_quote_by_keywords(text, kws, limit=1)
        candidates.append(
            {
                "id": "qk_exception",
                "risk_level": "P0",
                "module": "业务流程",
                "type": "异常流程缺失",
                "title": "异常流程缺失",
                "description": "PRD 未明确失败/超时/弱网时的用户提示、重试次数/间隔/终止条件、降级/补偿策略，以及中断/退出/重进的状态落点与恢复策略。",
                "anchor": "",
                "evidence_quotes": quotes,
            }
        )

    # P1: logic conflict / priority arbitration not executable
    if _has(r"(优先级|>|\u003e)") and (not _has(r"(冲突时|裁决|互斥|覆盖|同屏|并发)")):
        kws = ["优先级", "投屏", "游戏", "广告"]
        quotes = _find_quote_by_keywords(text, kws, limit=1)
        candidates.append(
            {
                "id": "qk_priority",
                "risk_level": "P1",
                "module": "业务流程",
                "type": "逻辑矛盾",
                "title": "逻辑矛盾",
                "description": "PRD 有优先级/切换描述，但缺少冲突裁决与可验收口径（例如同时触发、多入口、重入时最终展示/状态应唯一）。",
                "anchor": "",
                "evidence_quotes": quotes,
            }
        )

    # If PRD is very short or unstructured, still output a minimal P0 set to make meeting useful.
    if not candidates:
        candidates.extend(
            [
                {
                    "id": "qk_concurrency",
                    "risk_level": "P0",
                    "module": "业务流程",
                    "type": "并发操作未定义",
                    "title": "并发操作未定义",
                    "description": "未看到可执行的并发/互斥/裁决规则（谁覆盖谁、重复提交是否幂等、排队/拒绝策略、冲突错误码/提示）。",
                    "anchor": "",
                    "evidence_quotes": [],
                },
                {
                    "id": "qk_exception",
                    "risk_level": "P0",
                    "module": "业务流程",
                    "type": "异常流程缺失",
                    "title": "异常流程缺失",
                    "description": "未看到失败/超时/弱网/重试/降级/恢复的唯一口径（含错误码/提示与状态落点）。",
                    "anchor": "",
                    "evidence_quotes": [],
                },
            ]
        )

    return candidates[: max(1, int(limit or 6))]


def create_meeting_from_quick_scan(*, prd_text: str, snapshot_id: str, mode: str = "prd") -> MeetingV2:
    """
    Create a V2 meeting from PRD text using quick_scan_for_meeting().
    This is V2-only and MUST NOT affect static audit pipeline.
    """
    prd = prd_text or ""
    defects = quick_scan_for_meeting(prd_text=prd, limit=6)
    blockers = [build_blocker_item(d, prd_content=prd) for d in defects if isinstance(d, dict)]
    # Minimal stage3-like container for claim-check builder (it also reads PRD lines directly)
    stage3_like = {"shared_summary": {}}
    decisions: List[Dict[str, Any]] = []
    claims = build_claim_checks(stage3_output=stage3_like, prd_content=prd, decisions=decisions, blockers=blockers, limit=14)
    meeting_id = f"rmv2_{uuid.uuid4().hex[:10]}"
    meta = {
        "snapshot_id": snapshot_id,
        "mode": mode,
        "source": "quick_scan",
        "scan_level": "quick",
    }
    m = MeetingV2(
        meeting_id=meeting_id,
        meta=meta,
        prd_source=_clean_text(prd) if prd else "",
        agenda=[],
        decisions=decisions,
        blockers=blockers,
        claims=claims,
        gate=build_meeting_gate(blockers, claims),
        messages=[],
        prd_patch=_build_patch_from_blockers(blockers),
        prd_v2="",
        created_at=_now_str(),
    )
    try:
        m.prd_v2 = export_prd_v2_markdown(m.to_dict())
    except Exception:
        m.prd_v2 = ""
    _MEETINGS_V2[meeting_id] = m
    return m


def run_llm_discussion_for_meeting(
    *,
    meeting: MeetingV2,
    llm_config_path: str,
    max_rounds: int = 2,
    audit_constraint: bool = True,
    on_update: Optional[Callable[[MeetingV2, str], None]] = None,
) -> Tuple[MeetingV2, str]:
    """
    Upgrade template discussion to LLM discussion (bounded).
    Returns (meeting, llm_error). On error, keeps template messages.
    """
    llm_error = ""
    try:
        def _norm_tokens(lines: Any) -> set:
            if not isinstance(lines, list):
                return set()
            blob = " ".join([_clean_text(x) for x in lines if _clean_text(x)])[:1200]
            toks = [t for t in re.split(r"[，,。.;；、\s:/\\|]+", blob) if _clean_text(t)]
            return set([t.lower() for t in toks if len(t) >= 2])

        def _jaccard(a: set, b: set) -> float:
            if not a and not b:
                return 1.0
            if not a or not b:
                return 0.0
            inter = len(a & b)
            uni = len(a | b)
            return float(inter) / float(max(1, uni))

        def _dedupe_repeat(curr: List[Any], prev: List[Any], *, thr: float = 0.78) -> List[Any]:
            sa = _norm_tokens(curr)
            sb = _norm_tokens(prev)
            return [] if (_jaccard(sa, sb) >= float(thr)) else curr

        msgs: List[Dict[str, Any]] = []
        patch_lines: List[str] = []
        decisions: List[Dict[str, Any]] = []
        agenda: List[Dict[str, Any]] = []
        meeting.meta = meeting.meta or {}
        meeting.meta["llm_ok"] = False
        meeting.meta["llm_running"] = True
        # Only discuss top blockers (bounded)
        for idx, b in enumerate((meeting.blockers or [])[:6], start=1):
            issue = dict(b)
            quotes = b.get("evidence_quotes") if isinstance(b.get("evidence_quotes"), list) else []
            prior: Optional[Dict[str, Any]] = None
            prior_qa_q: List[Any] = []
            prior_dev_prop: List[Any] = []
            prior_pm_patch: List[Any] = []
            agenda.append(
                {
                    "order": idx,
                    "risk_level": str(issue.get("risk_level") or "P2").upper(),
                    "title": _clean_text(issue.get("title") or ""),
                    "focus": _dedupe_str(_ensure_list(issue.get("required_evidence")), limit=3),
                }
            )
            for rnd in range(1, max(1, int(max_rounds)) + 1):
                qa_obj, qa_err = rmv2llm.call_llm_round(
                    llm_config_path=llm_config_path,
                    role="测试（QA）",
                    issue=issue,
                    evidence_quotes=quotes,
                    prior_round=prior,
                    audit_constraint=audit_constraint,
                    timeout=45,
                    round_idx=rnd,
                )
                dev_obj, dev_err = rmv2llm.call_llm_round(
                    llm_config_path=llm_config_path,
                    role="研发（Dev）",
                    issue=issue,
                    evidence_quotes=quotes,
                    prior_round=qa_obj or prior,
                    audit_constraint=audit_constraint,
                    timeout=45,
                    round_idx=rnd,
                )
                pm_obj, pm_err = rmv2llm.call_llm_round(
                    llm_config_path=llm_config_path,
                    role="产品（PM）",
                    issue=issue,
                    evidence_quotes=quotes,
                    prior_round=dev_obj or qa_obj or prior,
                    audit_constraint=audit_constraint,
                    timeout=45,
                    round_idx=rnd,
                )
                if not (qa_obj and dev_obj and pm_obj):
                    llm_error = llm_error or (qa_err or dev_err or pm_err or "LLM 讨论失败")
                    break
                # Render as chat messages (WeChat-like UI will display)
                title = str(issue.get("title") or issue.get("type") or "—")
                qa_part = qa_obj.get("qa") if isinstance(qa_obj.get("qa"), dict) else {}
                dev_part = dev_obj.get("dev") if isinstance(dev_obj.get("dev"), dict) else {}
                pm_part = pm_obj.get("pm") if isinstance(pm_obj.get("pm"), dict) else {}

                # Backend guard: Round>=2 often repeats. If it is basically the same, drop it.
                try:
                    if rnd >= 2:
                        if isinstance(qa_part.get("questions"), list):
                            qa_part["questions"] = _dedupe_repeat(qa_part.get("questions"), prior_qa_q)
                        if isinstance(dev_part.get("proposal"), list):
                            dev_part["proposal"] = _dedupe_repeat(dev_part.get("proposal"), prior_dev_prop)
                        if isinstance(pm_part.get("prd_patch"), list):
                            pm_part["prd_patch"] = _dedupe_repeat(pm_part.get("prd_patch"), prior_pm_patch)
                except Exception:
                    pass

                if isinstance(qa_part.get("missing"), list) and qa_part.get("missing"):
                    issue["required_evidence"] = _dedupe_str([_strip_field_prefix(x) for x in qa_part.get("missing")], limit=10)
                    issue["status"] = "BLOCKED"
                if isinstance(qa_part.get("observability"), list) and qa_part.get("observability"):
                    issue["observability"] = _dedupe_str(qa_part.get("observability"), limit=24)
                if isinstance(qa_part.get("ac"), dict) and qa_part.get("ac"):
                    ac0 = qa_part.get("ac")
                    if isinstance(ac0, dict):
                        issue["ac"] = {
                            "given": _clean_text(ac0.get("given")),
                            "when": _clean_text(ac0.get("when")),
                            "then": _clean_text(ac0.get("then")),
                            "scene": _clean_text((issue.get("module") or "场景") + " - " + (issue.get("type") or issue.get("title") or ""))[:80],
                        }

                # Human-friendly summary (only when LLM succeeded this round)
                try:
                    # Prefer LLM-provided human_summary if present, otherwise fallback to stitched summary.
                    hs0 = ""
                    if isinstance(qa_part.get("human_summary"), str):
                        hs0 = _clean_text(qa_part.get("human_summary"))
                    if not hs0 and isinstance(dev_part.get("human_summary"), str):
                        hs0 = _clean_text(dev_part.get("human_summary"))
                    if not hs0 and isinstance(pm_part.get("human_summary"), str):
                        hs0 = _clean_text(pm_part.get("human_summary"))
                    if hs0:
                        issue["human_summary"] = hs0
                    else:
                        q0 = ""
                        if isinstance(quotes, list) and quotes:
                            q0 = _clean_text(quotes[0])
                        miss = qa_part.get("missing") if isinstance(qa_part.get("missing"), list) else []
                        miss = [_clean_text(x) for x in miss[:3] if _clean_text(x)]
                        prop = dev_part.get("proposal") if isinstance(dev_part.get("proposal"), list) else []
                        prop = [_clean_text(x) for x in prop[:2] if _clean_text(x)]
                        decision_status = str(pm_part.get("decision_status") or "BLOCKED").upper()
                        decision_text = _clean_text(pm_part.get("decision") or "")
                        hs: List[str] = []
                        if q0:
                            hs.append(f"触发原文：{q0}")
                        if miss:
                            hs.append("缺口（必须拍板）：" + "；".join(miss))
                        if prop:
                            hs.append("最小方案（Dev 建议）：" + "；".join(prop))
                        if decision_status:
                            hs.append("当前裁决：" + decision_status + (f"（{decision_text}）" if decision_text else ""))
                        issue["human_summary"] = "\n".join(hs).strip()
                except Exception:
                    pass

                # Apply round outputs back to meeting blocker AFTER all fields are updated,
                # so partial streaming UI can show latest human_summary/AC/required_evidence.
                try:
                    meeting.blockers[idx - 1] = issue
                except Exception:
                    pass

                qa_lines: List[str] = [f"议题：{title}（Round {rnd}）"]
                qs = qa_part.get("questions") if isinstance(qa_part.get("questions"), list) else []
                miss = qa_part.get("missing") if isinstance(qa_part.get("missing"), list) else []
                chat = qa_part.get("chat_lines") if isinstance(qa_part.get("chat_lines"), list) else []
                if chat:
                    qa_lines.extend([f"- {str(x)[:160]}" for x in chat[:8] if str(x or "").strip()])
                if qs:
                    qa_lines.append("（补充追问）")
                    qa_lines.extend([f"- {str(x)[:140]}" for x in qs[:5] if str(x or "").strip()])
                if miss:
                    qa_lines.append("（还缺这些）")
                    qa_lines.extend([f"- {str(x)[:160]}" for x in miss[:5] if str(x or "").strip()])
                acp = qa_part.get("ac") if isinstance(qa_part.get("ac"), dict) else {}
                if acp:
                    qa_lines.append("AC（草案）：")
                    qa_lines.append(f"- Given：{_clean_text(acp.get('given'))}")
                    qa_lines.append(f"- When：{_clean_text(acp.get('when'))}")
                    qa_lines.append(f"- Then：{_clean_text(acp.get('then'))}")
                obs = qa_part.get("observability") if isinstance(qa_part.get("observability"), list) else []
                if obs:
                    qa_lines.append("最小观测字段：")
                    qa_lines.append("- " + ", ".join([_clean_text(x) for x in obs[:12] if _clean_text(x)]))

                dev_lines: List[str] = ["最小实现回应："]
                push = dev_part.get("pushbacks") if isinstance(dev_part.get("pushbacks"), list) else []
                prop = dev_part.get("proposal") if isinstance(dev_part.get("proposal"), list) else []
                dchat = dev_part.get("chat_lines") if isinstance(dev_part.get("chat_lines"), list) else []
                if dchat:
                    dev_lines.extend([f"- {str(x)[:160]}" for x in dchat[:8] if str(x or "").strip()])
                if push:
                    dev_lines.append("（实现约束）")
                    dev_lines.extend([f"- {str(x)[:160]}" for x in push[:4] if str(x or "").strip()])
                if prop:
                    dev_lines.append("（最小方案）")
                    dev_lines.extend([f"- {str(x)[:160]}" for x in prop[:4] if str(x or "").strip()])
                contract = dev_part.get("contract") if isinstance(dev_part.get("contract"), dict) else {}
                if contract:
                    fs = contract.get("fields") if isinstance(contract.get("fields"), list) else []
                    es = contract.get("errors") if isinstance(contract.get("errors"), list) else []
                    if fs:
                        dev_lines.append("字段/契约： " + ", ".join([_clean_text(x) for x in fs[:12] if _clean_text(x)]))
                    if es:
                        dev_lines.append("错误码： " + ", ".join([_clean_text(x) for x in es[:8] if _clean_text(x)]))

                pm_lines: List[str] = []
                decision_status = str(pm_part.get("decision_status") or "BLOCKED").upper()
                decision_text = _clean_text(pm_part.get("decision") or "")
                pchat = pm_part.get("chat_lines") if isinstance(pm_part.get("chat_lines"), list) else []
                if pchat:
                    pm_lines.extend([f"- {str(x)[:160]}" for x in pchat[:8] if str(x or "").strip()])
                pm_lines.append(f"裁决：{decision_status}" + (f" — {decision_text}" if decision_text else ""))
                prd_patch = pm_part.get("prd_patch") if isinstance(pm_part.get("prd_patch"), list) else []
                if prd_patch:
                    pm_lines.append("（回写条款）")
                    pm_lines.extend([f"- {str(x)[:160]}" for x in prd_patch[:6] if str(x or "").strip()])
                owners = pm_part.get("owners") if isinstance(pm_part.get("owners"), list) else []
                if owners:
                    pm_lines.append("Owner/行动项：")
                    for o in owners[:6]:
                        if isinstance(o, dict):
                            pm_lines.append(f"- {str(o.get('role') or 'PM')}: {str(o.get('item') or '')[:140]}")

                msgs.append({"role": "assistant", "speaker": "测试（QA）", "ts": _now_str(), "round": rnd, "issue_title": title, "text": "\n".join(qa_lines)})
                msgs.append({"role": "assistant", "speaker": "研发（Dev）", "ts": _now_str(), "round": rnd, "issue_title": title, "text": "\n".join(dev_lines)})
                msgs.append({"role": "assistant", "speaker": "产品（PM）", "ts": _now_str(), "round": rnd, "issue_title": title, "text": "\n".join(pm_lines)})
                # Incremental meeting stream for "real meeting feel"
                try:
                    kickoff = ["本次会议议程（QA 发起，按顺序收敛）："]
                    for a in agenda[:8]:
                        focus = "；".join(a.get("focus") or []) or "（待补口径/证据）"
                        kickoff.append(f"- {a.get('order')}. [{a.get('risk_level')}] {a.get('title')}：{focus}")
                    meeting.messages = [
                        {
                            "role": "assistant",
                            "speaker": "测试（QA）",
                            "ts": _now_str(),
                            "round": 0,
                            "issue_title": "会议议程",
                            "text": "\n".join(kickoff),
                        }
                    ] + msgs
                    meeting.agenda = agenda
                    if on_update:
                        on_update(meeting, f"{title} · Round {rnd} · QA/Dev/PM 已输出")
                except Exception:
                    pass
                # Collect patch snippets
                for ln in prd_patch[:8]:
                    s = _clean_text(ln)
                    # De-templatize: remove leading bracket labels like 【并发策略】
                    s = re.sub(r"^【[^】]{1,12}】\s*", "", s).strip()
                    if s and s not in patch_lines:
                        patch_lines.append(s)
                # Record decision summary for top area
                decisions.append(
                    {
                        "title": title,
                        "risk_level": str(issue.get("risk_level") or "P2").upper(),
                        "status": decision_status,
                        "owner": "PM",
                        "decision": decision_text or ("待补证据/口径" if decision_status == "BLOCKED" else "已冻结口径"),
                        "required_evidence": _dedupe_str(miss, limit=4),
                        "human_summary": _clean_text(issue.get("human_summary") or ""),
                    }
                )
                # Convergence: if no new delta, stop early
                delta = pm_obj.get("delta") if isinstance(pm_obj.get("delta"), dict) else {}
                new_missing = delta.get("new_missing") if isinstance(delta.get("new_missing"), list) else []
                new_patch = delta.get("new_patch") if isinstance(delta.get("new_patch"), list) else []
                if rnd >= 2 and (not new_missing and not new_patch):
                    break
                prior = {"qa": qa_obj.get("qa"), "dev": dev_obj.get("dev"), "pm": pm_obj.get("pm")}
                # Store previous lists for repeat detection next round
                prior_qa_q = qa_part.get("questions") if isinstance(qa_part.get("questions"), list) else prior_qa_q
                prior_dev_prop = dev_part.get("proposal") if isinstance(dev_part.get("proposal"), list) else prior_dev_prop
                prior_pm_patch = pm_part.get("prd_patch") if isinstance(pm_part.get("prd_patch"), list) else prior_pm_patch
        if msgs:
            # Prepend QA kickoff agenda message
            kickoff = ["本次会议议程（QA 发起，按顺序收敛）："]
            for a in agenda[:8]:
                focus = "；".join(a.get("focus") or []) or "（待补口径/证据）"
                kickoff.append(f"- {a.get('order')}. [{a.get('risk_level')}] {a.get('title')}：{focus}")
            meeting.messages = [
                {
                    "role": "assistant",
                    "speaker": "测试（QA）",
                    "ts": _now_str(),
                    "round": 0,
                    "issue_title": "会议议程",
                    "text": "\n".join(kickoff),
                }
            ] + msgs
            meeting.agenda = agenda
            # Deduplicate decisions by title (keep the latest)
            dedup: Dict[str, Dict[str, Any]] = {}
            for d in decisions:
                if not isinstance(d, dict):
                    continue
                k = str(d.get("title") or "").strip() or str(uuid.uuid4())
                dedup[k] = d
            meeting.decisions = list(dedup.values())[:12]
        if patch_lines:
            meeting.prd_patch = "### V2 评审补丁（建议回写 PRD 附录）\n" + "\n".join([f"- {x}" for x in patch_lines[:30]])
        # 必出产物：PRD v2.0（Executable Version）
        try:
            meeting.prd_v2 = export_prd_v2_markdown(meeting.to_dict())
        except Exception:
            pass
        # Recompute gate after discussion (status/required_evidence may change)
        try:
            meeting.gate = build_meeting_gate(meeting.blockers or [], meeting.claims or [])
        except Exception:
            pass
        # LLM-first Gate summary (for readability). Falls back silently if LLM fails.
        try:
            if llm_config_path and isinstance(meeting.gate, dict):
                s, tops, err = _llm_summarize_gate(
                    llm_config_path=llm_config_path,
                    gate=meeting.gate,
                    blockers=meeting.blockers or [],
                    timeout=35,
                )
                if s:
                    meeting.gate["summary"] = s
                if tops:
                    meeting.gate["required_evidence_top_llm"] = tops
                if err:
                    meeting.gate["summary_error"] = str(err)
        except Exception:
            pass
        # LLM-first polish: make Blockers/Decisions summaries forwardable.
        try:
            if llm_config_path:
                _changed, _err = _llm_polish_blockers_and_decisions(
                    llm_config_path=llm_config_path,
                    blockers=meeting.blockers or [],
                    decisions=meeting.decisions or [],
                    timeout=35,
                )
                if isinstance(meeting.meta, dict) and _err:
                    meeting.meta["polish_error"] = str(_err)
        except Exception:
            pass
        try:
            if on_update:
                on_update(meeting, "LLM 讨论已完成，已生成最终 Gate/PRD v2.0")
        except Exception:
            pass
        meeting.meta = meeting.meta or {}
        meeting.meta["llm_ok"] = not bool(llm_error)
        meeting.meta["llm_running"] = False
        return meeting, llm_error
    except Exception as e:
        try:
            meeting.meta = meeting.meta or {}
            meeting.meta["llm_ok"] = False
            meeting.meta["llm_running"] = False
        except Exception:
            pass
        return meeting, str(e)


def get_meeting(meeting_id: str) -> Optional[MeetingV2]:
    return _MEETINGS_V2.get(meeting_id)


def apply_freeze_rules(*, meeting: MeetingV2, freeze_rules: Dict[str, Any]) -> MeetingV2:
    """
    Apply project-specific, concrete decisions (numbers/rules) provided by PM/QA.
    This is platform-generic: it never injects business names, only stores explicit rules given by user.
    It recomputes blockers/decisions status, gate, and PRD v2.0.
    """
    fr = freeze_rules if isinstance(freeze_rules, dict) else {}

    def _txt(v: Any) -> str:
        return _clean_text(v)

    def _set_decision(title: str, decision: str, risk_level: str = "P0", status: str = "DECIDED") -> None:
        if not title or not decision:
            return
        # replace or append by title
        found = False
        for d in meeting.decisions or []:
            if isinstance(d, dict) and _clean_text(d.get("title")) == title:
                d["decision"] = decision
                d["risk_level"] = str(d.get("risk_level") or risk_level).upper()
                d["status"] = status
                d["owner"] = d.get("owner") or "PM"
                d["required_evidence"] = []
                found = True
                break
        if not found:
            meeting.decisions = (meeting.decisions or []) + [
                {
                    "title": title,
                    "risk_level": str(risk_level).upper(),
                    "status": status,
                    "owner": "PM",
                    "required_evidence": [],
                    "decision": decision,
                }
            ]

    def _mark_blocker_done(keys: List[str]) -> None:
        if not isinstance(meeting.blockers, list):
            return
        for b in meeting.blockers:
            if not isinstance(b, dict):
                continue
            ttl = _clean_text(b.get("title") or "")
            if any(k and k in ttl for k in keys):
                b["status"] = "DECIDED"
                b["required_evidence"] = []

    # 1) Concurrency / arbitration
    term = fr.get("terminal_scope") if isinstance(fr.get("terminal_scope"), dict) else {}
    if term:
        terminals = _txt(term.get("terminals") or "")
        quote = _txt(term.get("evidence_quote") or "")
        if terminals:
            # Store into meta so exporter can show it as project-specific, not TBD
            try:
                meeting.meta = meeting.meta or {}
                meeting.meta["terminals"] = terminals
            except Exception:
                pass
            _set_decision("终端范围（项目冻结口径）", f"终端范围（项目冻结口径）：{terminals}", risk_level="P0", status="DECIDED")
            # If user provides an actual PRD quote, mark related claims as SUPPORTED
            if quote and isinstance(meeting.claims, list):
                for c in meeting.claims:
                    if not isinstance(c, dict):
                        continue
                    cl = _clean_text(c.get("claim") or "")
                    if "终端" in cl or "展示" in cl:
                        c["status"] = "SUPPORTED"
                        c["evidence_quotes"] = [quote]
                        c["required_evidence"] = []

    conc = fr.get("concurrency") if isinstance(fr.get("concurrency"), dict) else {}
    if conc:
        n = conc.get("max_concurrency_n")
        arbitration = _txt(conc.get("arbitration") or "")
        queue = _txt(conc.get("queue") or "")
        decision = "并发/冲突裁决（项目冻结口径）：\n"
        if n is not None and str(n).strip():
            decision += f"- 最大并发：N={n}\n"
        if arbitration:
            decision += f"- 裁决函数：{arbitration}\n"
        if queue:
            decision += f"- 排队/互斥：{queue}\n"
        _set_decision("并发/冲突裁决（项目冻结口径）", decision.strip(), risk_level="P0", status="DECIDED")
        _mark_blocker_done(["并发", "冲突"])

    # 2) Retry / timeout / degrade
    retry = fr.get("retry") if isinstance(fr.get("retry"), dict) else {}
    if retry:
        timeout_ms = retry.get("timeout_ms")
        times = retry.get("retry_times")
        backoff = _txt(retry.get("backoff") or "")
        stop = _txt(retry.get("stop_condition") or "")
        fail = _txt(retry.get("final_fail") or "")
        decision = "异常流程（项目冻结口径）：\n"
        if timeout_ms is not None and str(timeout_ms).strip():
            decision += f"- 超时：{timeout_ms}ms\n"
        if times is not None and str(times).strip():
            decision += f"- 重试次数：{times}\n"
        if backoff:
            decision += f"- 重试间隔：{backoff}\n"
        if stop:
            decision += f"- 终止条件：{stop}\n"
        if fail:
            decision += f"- 最终失败：{fail}\n"
        _set_decision("异常/超时/弱网（项目冻结口径）", decision.strip(), risk_level="P0", status="DECIDED")
        _mark_blocker_done(["异常", "超时", "弱网", "失败"])

    # 3) Resume / exit / restart
    resume = fr.get("resume") if isinstance(fr.get("resume"), dict) else {}
    if resume:
        storage = _txt(resume.get("state_storage") or "")
        strategy = _txt(resume.get("strategy") or "")
        decision = "中断/退出/重进恢复（项目冻结口径）：\n"
        if storage:
            decision += f"- 状态存储：{storage}\n"
        if strategy:
            decision += f"- 恢复策略：{strategy}\n"
        _set_decision("中断/退出/重进恢复（项目冻结口径）", decision.strip(), risk_level="P0", status="DECIDED")
        _mark_blocker_done(["中断", "退出", "重进", "恢复", "状态落点"])

    # 4) Auth / permission / tenancy
    auth = fr.get("auth") if isinstance(fr.get("auth"), dict) else {}
    if auth:
        who = _txt(auth.get("who_can") or "")
        token = _txt(auth.get("token") or "")
        ttl = auth.get("token_ttl_s")
        cross = _txt(auth.get("cross_scope_block") or "")
        deny = _txt(auth.get("deny_error") or "")
        decision = "权限/鉴权（项目冻结口径）：\n"
        if who:
            decision += f"- 谁可操作：{who}\n"
        if token:
            decision += f"- Token/凭证：{token}\n"
        if ttl is not None and str(ttl).strip():
            decision += f"- Token有效期：{ttl}s\n"
        if cross:
            decision += f"- 跨域/跨房间拦截：{cross}\n"
        if deny:
            decision += f"- 越权错误码/提示：{deny}\n"
        _set_decision("权限/鉴权（项目冻结口径）", decision.strip(), risk_level="P0", status="DECIDED")
        _mark_blocker_done(["权限", "安全", "鉴权", "越权"])

    # Recompute gate/prd_v2 (do NOT force PASS)
    try:
        meeting.gate = build_meeting_gate(meeting.blockers or [], meeting.claims or [])
    except Exception:
        pass
    try:
        meeting.prd_v2 = export_prd_v2_markdown(meeting.to_dict())
    except Exception:
        pass
    # Try to auto-close Claim-Check against PRD v2.0 (newly generated executable spec),
    # so "freeze decisions" become "white-paper text" and can support PASS.
    try:
        prd2 = str(meeting.prd_v2 or "")
        if prd2 and isinstance(meeting.claims, list):
            for c in meeting.claims:
                if not isinstance(c, dict):
                    continue
                if str(c.get("status") or "").upper() == "SUPPORTED":
                    continue
                claim = _clean_text(c.get("claim") or "")
                if not claim:
                    continue
                tokens = _expand_claim_keywords(claim)
                quotes = _find_quote_by_keywords(prd2, tokens[:6], limit=1)
                if quotes:
                    c["status"] = "SUPPORTED"
                    c["evidence_quotes"] = quotes
                    c["required_evidence"] = []
    except Exception:
        pass
    try:
        meeting.gate = build_meeting_gate(meeting.blockers or [], meeting.claims or [])
    except Exception:
        pass
    return meeting


def ensure_meeting_snapshot_dir(base_dir: str) -> str:
    """
    Store meeting snapshots under prd_audit/learning_repo/meeting_snapshots (isolated from static audit snapshots).
    """
    meeting_dir = os.path.join(base_dir, "learning_repo", "meeting_snapshots")
    os.makedirs(meeting_dir, exist_ok=True)
    return meeting_dir


def save_meeting_snapshot(base_dir: str, meeting: MeetingV2) -> Dict[str, Any]:
    meeting_dir = ensure_meeting_snapshot_dir(base_dir)
    sid = f"msnap_{uuid.uuid4().hex[:10]}"
    payload = {
        "meeting_snapshot_id": sid,
        "created_at": _now_str(),
        "meeting": meeting.to_dict(),
    }
    path = os.path.join(meeting_dir, f"{sid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"meeting_snapshot_id": sid, "path": path}

