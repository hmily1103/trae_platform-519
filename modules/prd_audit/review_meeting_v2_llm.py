"""
LLM discussion engine for Review Meeting V2.

Isolation rules:
- This module is ONLY used by review_meeting_v2 (V2 workbench).
- Must NOT change static audit outputs or reuse/alter pipeline prompts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from utils.llm_client import call_llm_with_retry


def _clean(s: Any) -> str:
    t = str(s or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _safe_json_loads(text: str) -> Any:
    """
    Try parse JSON object/array from model output.
    Accepts raw JSON or text containing a JSON blob.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    # fast path
    try:
        return json.loads(raw)
    except Exception:
        pass
    # extract first {...} or [...]
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _json_schema_hint(role: str) -> str:
    """
    Keep outputs machine-parseable, but allow human-ish content inside fields.
    To reduce "模板味" and non-JSON failures, we only require the section for current role.
    """
    r = str(role or "").strip()
    if "QA" in r or "测试" in r:
        return """
你必须只输出 JSON（对象），不要输出 markdown、不要输出多余解释。
JSON schema:
{
  "qa": {
    "human_summary": "<=8行的人话摘要（可换行）",
    "chat_lines": ["..."],               // 像群聊发言的短句（3-6条，每条<=60字）
    "questions": ["..."],                // QA 追问（必须可验收/可观测）
    "missing": ["..."],                  // 缺失口径/证据（required_evidence）
    "ac": {"given":"...","when":"...","then":"..."},
    "observability": ["field_a","field_b"]
  },
  "delta": {
    "new_missing": ["..."],              // 本轮新增缺口（用于收敛判断）
    "new_patch": ["..."]
  }
}
""".strip()
    if "Dev" in r or "研发" in r:
        return """
你必须只输出 JSON（对象），不要输出 markdown、不要输出多余解释。
JSON schema:
{
  "dev": {
    "human_summary": "<=8行的人话摘要（可换行）",
    "chat_lines": ["..."],               // 像群聊发言的短句（3-6条，每条<=60字）
    "pushbacks": ["..."],                // 实现风险/约束（要能落字段/状态机/错误码）
    "proposal": ["..."],                 // 最小实现方案（平台通用表达）
    "contract": {"fields":["..."], "errors":["..."], "states":["..."]}
  },
  "delta": {
    "new_missing": ["..."],
    "new_patch": ["..."]
  }
}
""".strip()
    # PM
    return """
你必须只输出 JSON（对象），不要输出 markdown、不要输出多余解释。
JSON schema:
{
  "pm": {
    "human_summary": "<=8行的人话摘要（可换行）",
    "chat_lines": ["..."],               // 像群聊发言的短句（3-6条，每条<=60字）
    "decision_status": "DECIDED|BLOCKED",
    "decision": "一句话裁决（A/B/C/阈值/优先级）",
    "prd_patch": ["..."],                // 可直接回写 PRD 的条款（每条 <=120字）
    "owners": [{"role":"PM|Dev|QA","item":"..."}]
  },
  "delta": {
    "new_missing": ["..."],              // 本轮新增缺口（用于收敛判断）
    "new_patch": ["..."]
  }
}
""".strip()


def build_round_prompt(
    *,
    role: str,
    issue: Dict[str, Any],
    evidence_quotes: List[str],
    prior_round: Optional[Dict[str, Any]] = None,
    audit_constraint: bool = True,
    round_idx: int = 1,
) -> str:
    title = _clean(issue.get("title") or issue.get("type") or issue.get("description") or "未命名议题")
    lv = _clean(issue.get("risk_level") or "P2").upper()
    module = _clean(issue.get("module") or "跨模块")
    anchor = _clean(issue.get("anchor") or "【PRD未说明】")
    quotes = [q for q in (evidence_quotes or []) if _clean(q)]
    quote_block = "\n".join([f"- {q}" for q in quotes[:2]]) if quotes else "- 【无可引用原句：可能为缺失定义类问题】"
    prior = json.dumps(prior_round, ensure_ascii=False) if isinstance(prior_round, dict) else ""
    constraint_text = (
        "静态审计约束=开启：必须优先引用锚点/原文摘录；若 PRD 未写清，必须输出 missing/required_evidence，不得编造具体阈值。\n"
        if audit_constraint
        else "静态审计约束=关闭：允许提出探索性方案，但必须标注 inference 与待补证据，不得把推断当成PRD原文。\n"
    )
    # Human-like guidance: keep it inside JSON fields, not as extra text.
    human_hint = """
human_summary 写法（像开会说话，不要报告腔）：
- 第一行：直接引用一条“原文摘录”里的关键句（如果没有原文，就写“未抽到原文：需要 PRD 补条款”）
- 第二行：一句话说清“现在卡点是什么/为什么不可验收”
- 第三行：一句话说明“最小要补什么/拍板什么”
- 第四行：一句话给“可执行口径/参数（如果 PRD 没写清就明确标注 TBD/待 PM 冻结）”
注意：人话必须写进 human_summary 字段；除此之外禁止输出任何非 JSON 文本。
""".strip()
    generic_hint = """
平台通用性约束（非常重要）：
- 禁止引入 PRD 未出现的业务对象/行业模板（例如：订单/库存/支付单/下单/扣库存 等）。
- 只能使用中性词：资源/实体/对象/会话/请求/任务/屏幕/端/包房 等（若 PRD 原文出现再使用）。
- 错误码优先用通用形态：ERR_TIMEOUT / ERR_NOT_FOUND / ERR_CONFLICT / ERR_UNAUTHORIZED；若 PRD 未给出具体码值，请写 TBD，不要编造 404/408/409 之类 HTTP 细节。
""".strip()
    round_hint = f"""
当前轮次：Round {int(round_idx or 1)}
- Round 1：允许输出完整内容
- Round >=2：禁止复读上一轮的 questions/proposal/prd_patch；只允许补充“新增的缺口/补丁”（写进 delta.new_missing / delta.new_patch），其余列表若无新增请输出空数组 []
- chat_lines 必须是你这个角色“此刻在群里说的话”，禁止照抄其他角色上一轮内容；用第一人称，短句即可。
""".strip()
    return f"""
你在一个“PRD评审群聊”里扮演 {role}。目标：围绕单个议题形成可开测/可验收的结论，并产出可回写PRD的补丁条款。

议题：
- 标题：{title}
- 风险等级：{lv}
- 模块：{module}
- 锚点：{anchor}
- 原文摘录：
{quote_block}

上一轮结果（如有）：
{prior or "（无）"}

{round_hint}

要求：
- QA 必须把问题翻译成可验收的 Given/When/Then + 最小观测字段
- Dev 必须把口径翻译成字段/错误码/状态机或幂等/重试策略（平台通用）
- PM 必须给出裁决（DECIDED/BLOCKED）并输出可回写 PRD 的条款（prd_patch）
- 不要编造 PRD 已明确的细节；缺失则写 missing
 - {constraint_text.strip()}

{human_hint}

{generic_hint}

{_json_schema_hint(role)}
""".strip()


def call_llm_round(
    *,
    llm_config_path: str,
    role: str,
    issue: Dict[str, Any],
    evidence_quotes: List[str],
    prior_round: Optional[Dict[str, Any]] = None,
    audit_constraint: bool = True,
    timeout: int = 45,
    round_idx: int = 1,
) -> Tuple[Optional[Dict[str, Any]], str]:
    prompt = build_round_prompt(
        role=role,
        issue=issue,
        evidence_quotes=evidence_quotes,
        prior_round=prior_round,
        audit_constraint=audit_constraint,
        round_idx=round_idx,
    )
    try:
        # Call with retry to reduce flakiness (rate limit/network).
        txt = call_llm_with_retry(
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            config_path=llm_config_path,
            timeout=timeout,
            max_tokens=4096,
            temperature=0.2,
            max_retries=2,
            retry_delay=2,
        )
        obj = _safe_json_loads(txt)
        if isinstance(obj, dict):
            return obj, ""
        # One more strict retry for "non-json" outputs.
        txt2 = call_llm_with_retry(
            messages=[
                {
                    "role": "user",
                    "content": prompt + "\n\n再次强调：只允许输出 JSON 对象，禁止任何额外字符（包括```代码块）。",
                }
            ],
            config_path=llm_config_path,
            timeout=timeout,
            max_tokens=4096,
            temperature=0.0,
            max_retries=1,
            retry_delay=1,
        )
        obj2 = _safe_json_loads(txt2)
        if isinstance(obj2, dict):
            return obj2, ""
        return None, f"{role}: LLM 输出非JSON"
    except Exception as e:
        return None, f"{role}: {e}"

