# -*- coding: utf-8 -*-
import re
from typing import Any, Dict, List, Tuple


def _as_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def _as_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _clamp(n: int, lo: int, hi: int) -> int:
    return lo if n < lo else (hi if n > hi else n)


def _heading_count(md: str) -> int:
    lines = (md or "").splitlines()
    return sum(1 for ln in lines if ln.lstrip().startswith("## "))


def _contains_any(text: str, needles: List[str]) -> bool:
    hay = (text or "").lower()
    for n in needles:
        if (n or "").lower() in hay:
            return True
    return False


def _detect_prompt_injection(text: str) -> List[str]:
    s = (text or "").lower()
    patterns = [
        "忽略以上",
        "忽略之前",
        "ignore previous",
        "system prompt",
        "你是chatgpt",
        "你是一个大模型",
        "必须输出",
        "只输出",
        "不要输出",
        "泄露",
        "api key",
        "密钥",
        "token",
        "hacked",
    ]
    hits = []
    for p in patterns:
        if p in s:
            hits.append(p)
    return hits[:12]


def _defect_missing_fields(defects: List[Dict[str, Any]]) -> Tuple[int, int]:
    missing = 0
    total = 0
    for d in defects or []:
        if not isinstance(d, dict):
            continue
        total += 1
        if not str(d.get("type") or "").strip():
            missing += 1
            continue
        if not str(d.get("description") or "").strip():
            missing += 1
            continue
        if not str(d.get("suggestion") or "").strip():
            missing += 1
            continue
    return missing, total


def evaluate_guardrail(
    prd_text: str,
    stage1_output: Dict[str, Any],
    stage2_output: Dict[str, Any],
    report_md: str,
    test_cases: List[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stage1 = _as_dict(stage1_output)
    stage2 = _as_dict(stage2_output)
    defects = _as_list(stage2.get("defects"))
    cases = _as_list(test_cases)

    checks: List[Dict[str, Any]] = []
    score = 100

    md = (report_md or "").strip()
    if not md:
        checks.append({"id": "report_empty", "ok": False, "severity": "P0", "message": "报告为空", "hint": "检查 Stage3 生成链路或 LLM 配置"})
        score -= 80
    else:
        hcnt = _heading_count(md)
        ok = hcnt >= 5
        checks.append({"id": "report_structure", "ok": ok, "severity": "P1" if not ok else "P2", "message": f"二级标题数量：{hcnt}", "hint": "建议报告至少包含 5 个以上的 ## 小节，避免输出过于碎片化"})
        if not ok:
            score -= 12

        len_ok = len(md) >= 800
        checks.append({"id": "report_length", "ok": len_ok, "severity": "P2" if not len_ok else "P3", "message": f"报告长度：{len(md)} 字符", "hint": "建议补充更多具体结论、缺陷与测试建议"})
        if not len_ok:
            score -= 8

    prd_len_ok = len((prd_text or "").strip()) >= 200
    checks.append({"id": "input_prd_length", "ok": prd_len_ok, "severity": "P2" if not prd_len_ok else "P3", "message": f"PRD 输入长度：{len((prd_text or '').strip())} 字符", "hint": "PRD 过短会导致结构化与审计结果偏弱"})
    if not prd_len_ok:
        score -= 6

    inj_hits = _detect_prompt_injection(prd_text or "")
    if inj_hits:
        checks.append({"id": "prompt_injection_signal", "ok": False, "severity": "P1", "message": f"检测到潜在提示注入信号：{', '.join(inj_hits)}", "hint": "建议对输入进行指令隔离、转义或启用护栏策略（禁止泄露、禁止偏离任务）"})
        score -= 12
        if _contains_any(md, ["hacked", "系统提示词", "api key", "密钥", "token"]):
            checks.append({"id": "prompt_injection_followed", "ok": False, "severity": "P0", "message": "报告疑似被提示注入影响（出现敏感/固定输出）", "hint": "建议启用更严格的输出校验与安全策略"})
            score -= 40

    missing, total = _defect_missing_fields(defects)
    ok = (total == 0) or (missing == 0)
    checks.append({"id": "defect_fields", "ok": ok, "severity": "P1" if not ok else "P3", "message": f"缺陷字段完整性：{total - missing}/{total}", "hint": "建议确保每条缺陷包含 type/description/suggestion"})
    if total > 0 and missing > 0:
        score -= _clamp(5 + missing * 2, 6, 20)

    p0 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0")
    p1 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P1")
    p2 = sum(1 for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P2")
    checks.append({"id": "risk_distribution", "ok": True, "severity": "P3", "message": f"风险分布：P0={p0}, P1={p1}, P2={p2}", "hint": ""})

    if p0 > 0:
        ok = _contains_any(md, ["p0", "致命", "高风险", "阻断"]) or any(
            _contains_any(md, [str((d or {}).get("type") or "")]) for d in defects if isinstance(d, dict) and str(d.get("risk_level") or "").upper() == "P0"
        )
        checks.append({"id": "p0_ack", "ok": ok, "severity": "P1" if not ok else "P3", "message": "报告是否显式覆盖 P0 风险", "hint": "建议在报告中明确列出 P0 风险与回归范围调整"})
        if not ok:
            score -= 10

    if cases:
        bad = 0
        for c in cases:
            if not isinstance(c, dict):
                bad += 1
                continue
            if not str(c.get("steps") or "").strip():
                bad += 1
                continue
            if not str(c.get("expected") or "").strip():
                bad += 1
                continue
        ok = bad == 0
        checks.append({"id": "test_case_quality", "ok": ok, "severity": "P2" if not ok else "P3", "message": f"测试用例完整性：{len(cases) - bad}/{len(cases)}", "hint": "建议每条用例至少包含 steps/expected"})
        if not ok:
            score -= _clamp(5 + bad * 2, 6, 18)

    parse_quality = _as_dict(stage1.get("parse_quality"))
    pq_score = parse_quality.get("score")
    if isinstance(pq_score, (int, float)):
        ok = float(pq_score) >= 0.6
        checks.append({"id": "parse_quality", "ok": ok, "severity": "P2" if not ok else "P3", "message": f"结构化解析质量分：{pq_score}", "hint": "建议补充 PRD 中的模块/流程/状态机/异常分支等要素"})
        if not ok:
            score -= 8

    if md:
        has_tests = re.search(r"(测试用例|测试点|回归|验证)", md) is not None
        checks.append({"id": "has_test_guidance", "ok": bool(has_tests), "severity": "P2" if not has_tests else "P3", "message": "报告是否包含测试指导内容", "hint": "建议补充回归范围、测试重点与新增用例建议"})
        if not has_tests:
            score -= 10

    score = _clamp(score, 0, 100)
    grade = "A" if score >= 90 else ("B" if score >= 75 else ("C" if score >= 60 else "D"))
    summary = f"Guardrail Score: {score}/100 ({grade})"
    return {"score": score, "grade": grade, "summary": summary, "checks": checks}
