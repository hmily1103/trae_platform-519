"""
PRD Rewrite Engine — generates full PRD 2.0 document from original PRD + V2 meeting artifacts.

Modes:
  - llm:   LLM-powered chapter-level rewrite (requires valid API key).
  - rule:  template-based stitching without LLM (always available).

V2 isolation contract:
  - This module does NOT modify meeting objects, blockers, or any V2 state.
  - It only READS meeting data and original PRD, and produces a new PRD 2.0 text.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from utils.llm_client import call_llm_with_retry


def _clean(s: Any) -> str:
    t = str(s or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _ensure_list(val: Any) -> List[Any]:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    return []


def rewrite_prd_rule(
    *,
    original_prd: str,
    blockers: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
    prd_patch: str,
) -> str:
    """
    Stitch a PRD 2.0 using template rules + meeting artifacts.
    Returns a full markdown PRD document.
    """
    lines: List[str] = []
    lines.append("# PRD 文档 2.0（评审修订版）")
    lines.append("")
    lines.append("> 本文档由 V2 多Agent评审会议生成，基于原始 PRD 经由 QA/Dev/PM 三方收敛后输出。")
    lines.append("> 说明：若 LLM 不可用，本引擎会以规则拼接方式生成可交付文档结构。")
    lines.append("")

    if decisions:
        lines.append("## 一、评审决议（冻结口径）")
        lines.append("")
        for d in decisions[:24]:
            title = _clean(d.get("title") or d.get("description") or "未命名")
            decision = _clean(d.get("decision") or d.get("conclusion") or "—")
            owner = _clean(d.get("owner") or d.get("resolved_by") or "PM")
            lines.append(f"- **{title}**：{decision}（Owner：{owner}）")
        lines.append("")

    blocked_items = [b for b in blockers if str((b or {}).get("status") or "").upper() == "BLOCKED"]
    open_items = [b for b in blockers if str((b or {}).get("status") or "").upper() != "BLOCKED"]

    lines.append("## 二、阻断项（BLOCKED：补齐证据后才能冻结）")
    lines.append("")
    if not blocked_items:
        lines.append("- （无）")
    for b in blocked_items[:24]:
        lv = str(b.get("risk_level") or "P2").upper()
        title = _clean(b.get("title") or "未命名")
        anchor = _clean(b.get("anchor") or "—")
        req = _ensure_list(b.get("required_evidence"))
        lines.append(f"### {title}（{lv}）")
        lines.append(f"- 关联锚点：{anchor}")
        if req:
            lines.append("- 必补条款：")
            for r in req[:8]:
                if _clean(r):
                    lines.append(f"  - {str(r)}")
        lines.append("")
    lines.append("")

    lines.append("## 三、可执行验收口径（AC + 最小观测字段）")
    lines.append("")
    any_ac = False
    for b in open_items[:40]:
        ac = b.get("ac") if isinstance(b.get("ac"), dict) else {}
        if not ac:
            continue
        any_ac = True
        lv = str(b.get("risk_level") or "P2").upper()
        title = _clean(b.get("title") or "未命名")
        anchor = _clean(b.get("anchor") or "—")
        obs = _ensure_list(b.get("observability"))
        lines.append(f"### {title}（{lv}）")
        lines.append(f"- 关联锚点：{anchor}")
        lines.append(f"- Given：{_clean(ac.get('given'))}")
        lines.append(f"- When：{_clean(ac.get('when'))}")
        lines.append(f"- Then：{_clean(ac.get('then'))}")
        if obs:
            lines.append(f"- 最小观测字段：{', '.join([_clean(x) for x in obs[:16] if _clean(x)])}")
        lines.append("")
    if not any_ac:
        lines.append("- （暂无结构化 AC）")
        lines.append("")

    if prd_patch:
        lines.append("## 四、评审补丁条款（附录，供回写对照）")
        lines.append("")
        patch_text = prd_patch.strip()
        patch_text = re.sub(r"^### V2 评审补丁（建议回写 PRD 附录）\n?", "", patch_text)
        lines.append(patch_text)
        lines.append("")

    if original_prd.strip():
        lines.append("---")
        lines.append("")
        lines.append("## 五、原始 PRD（输入原文，便于对照）")
        lines.append("")
        lines.append(original_prd.strip())
        lines.append("")

    return "\n".join(lines).strip()


def _build_rewrite_prompt(
    *,
    original_prd: str,
    blockers: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
    prd_patch: str,
    gate: Dict[str, Any],
    export_kind: str,
    now_str: str,
) -> str:
    blockers_md: List[str] = []
    for idx, b in enumerate(blockers[:18], start=1):
        if not isinstance(b, dict):
            continue
        lv = str(b.get("risk_level") or "P2").upper()
        title = _clean(b.get("title") or "未命名")
        anchor = _clean(b.get("anchor") or "【PRD未说明】")
        ac = b.get("ac") if isinstance(b.get("ac"), dict) else {}
        req = _ensure_list(b.get("required_evidence"))
        obs = _ensure_list(b.get("observability"))
        status = _clean(b.get("status") or "OPEN").upper()
        blockers_md.append(
            f"## B{idx} · {title}（{lv}/{status}）\n"
            f"- 锚点：{anchor}\n"
            + (f"- Given：{_clean(ac.get('given'))}\n- When：{_clean(ac.get('when'))}\n- Then：{_clean(ac.get('then'))}\n" if ac else "")
            + (f"- 必补条款：{'；'.join([str(x) for x in req[:6]])}\n" if req else "")
            + (f"- 最小观测字段：{', '.join([_clean(x) for x in obs[:12]])}\n" if obs else "")
        )

    decisions_md: List[str] = []
    for idx, d in enumerate(decisions[:24], start=1):
        if not isinstance(d, dict):
            continue
        title = _clean(d.get("title") or d.get("description") or "未命名")
        decision = _clean(d.get("decision") or d.get("conclusion") or "—")
        owner = _clean(d.get("owner") or "PM")
        status = _clean(d.get("status") or "").upper()
        st = status if status else "—"
        decisions_md.append(f"- **D{idx} · {title}**：{decision}（{owner}；status={st}）")

    blockers_section = "\n---\n".join(blockers_md) if blockers_md else "(无)"
    decisions_section = "\n".join(decisions_md) if decisions_md else "(无)"
    patch_section = (prd_patch or "").strip()
    patch_section = patch_section[:3000] if patch_section else "(无)"

    gate_state = _clean((gate or {}).get("gate") or "—").upper()
    gate_reason = _clean((gate or {}).get("reason") or "")
    export_kind = (export_kind or "draft").strip().lower()
    kind_name = "正式冻结版" if export_kind == "frozen" else "可开工草案版"
    now_line = _clean(now_str or "")
    if not now_line:
        # Best-effort fallback; real value should be passed from caller.
        import time as _time
        now_line = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime())

    style_hint = ""
    if export_kind == "frozen":
        style_hint = """写作风格要求（正式冻结版）：
- 输出必须是“干净的正式版 PRD”，直接可下发研发/测试；不要在正文出现“过渡/临时/模板/规则引擎/自动生成”等措辞。
- 删除所有 TBD/AUTO_PROPOSED/待定项；如仍存在缺口，应拒绝输出正式冻结口径（因为 frozen 只允许在 Gate=PASS 时导出）。
- 把已定结论（尤其是 P0 解决方案）直接写回正文对应章节（例如：并发冲突处理、异常流程与重试策略、错误码字典、状态机）。
- 文档结构参考企业 PRD：文档基础信息、背景、目标、范围、核心规则（表格优先）、异常与错误码（表格）、状态机（含 mermaid）、验收标准 AC、埋点/观测字段。
"""
    else:
        style_hint = """写作风格要求（可开工草案版）：
- 输出必须可直接用于拆任务/写用例；允许存在未冻结项，但必须集中在《开工前置条件清单》里。
- 正文只写“已冻结且可执行”的条款；凡是原文未写清、或仅来自评审倾向但尚未冻结的内容，一律放进《开工前置条件清单》或“候选口径（待冻结）”，不要写成正式规则。
- 评审结论里如果出现具体数值/字段名/错误码，但【原始PRD原文】未出现对应事实，则必须标注为“候选口径（待冻结）”，并同时在《开工前置条件清单》保留该缺口，避免出现“GAP 说未定义，但正文写死了数值”的矛盾。
- 两层表达（重要）：正文默认面向产品/业务，优先“能决策的问题清单”；工程实现细节统一放到文末《工程附录（折叠）》里，不要污染正文可读性。
"""

    return f"""你是一个资深产品经理，负责将原始 PRD 文档结合评审结论，重写为可直接开工的 PRD 2.0 版本。

交付类型：{kind_name}
当前门禁结论：{gate_state}{('；原因：' + gate_reason) if gate_reason else ''}
生成时间：{now_line}

{style_hint}

硬约束（必须遵守）：
1) 不得新增事实：不得凭空发明字段名/错误码/重试次数与间隔/状态枚举/端能力/时长阈值等细节。任何“数值/枚举/字段名/常量名/流程规则”必须来自【原始PRD原文】或【评审事实包】里的 D#/B# 或补丁条款原文。
2) 强制可追溯：所有关键结论必须在句末追加来源引用之一：
   - [SRC:PRD] 表示来自原始 PRD 原文
   - [SRC:D1] 表示来自某条已定结论
   - [SRC:B3] 表示来自某条问题/缺口
   - [SRC:PATCH] 表示来自补丁条款原文
3) 门禁一致性：如果当前门禁不是 PASS，则文档必须明确标注“草案/未冻结”，并在开头给出《开工前置条件清单》（来自 BLOCKED 的必补条款）。严禁写“正式冻结/门禁PASS”。如果交付类型是“正式冻结版”，则只能输出“正式冻结/可开工”措辞，且不得出现 TBD/AUTO_PROPOSED。
4) 平台通用：不要注入原文未出现的行业模板/业务对象；用中性表达。
5) 文档基础信息里的“生成日期”必须使用上面的生成时间（不要写历史日期）。
6) 若文档包含“重试次数/自动重试”等表述，必须明确“重试间隔/退避策略”；否则把它放入《开工前置条件清单》而不要写进正文规则。
7) 草案版防矛盾：如果《开工前置条件清单》声明某项“未定义/待补”，正文不得给出确定数值/确定字段名/确定错误码映射；最多只能写“必须明确X（候选口径见 D#，待冻结）”。
8) 每条缺口必须写清“原文依据”：在《开工前置条件清单》中，每个必补项后面必须附一行“依据：…”，内容只能是：
   - 直接引用原始 PRD 的一句短摘录（用引号包起来）+ [SRC:PRD]；或
   - 明确写“PRD原文未覆盖该点” + 对应的 [SRC:B#]。
   严禁出现没有依据的“建议/默认值/常见做法”。如果没有原文，就明确标注“PRD原文未覆盖该点”。这能让所有人对齐“为什么要补”。
9) 双层表达（强制结构）：草案版里，《开工前置条件清单》每个缺口必须按以下结构输出（顺序不可变）：
   - 影响（一句话，面向 PM）
   - 需要 PM 决策的问题（用问句，1-3 条，面向 PM，不要工程黑话；技术别名放括号里，例如 resource_version）
   - 工程别名（仅列字段/错误码/接口名的“占位符”，不写具体实现与数值）
   - 依据：…（按第 8 条）
   草案 FAIL 时，禁止在此处写“具体接口路径/具体状态枚举/具体退避序列”。
10) 术语过滤（草案正文）：在“正文”（不含工程附录）禁止出现以下工程词汇或符号：GET/POST/HTTP/枚举/回滚/自增/只读/ISO8601/localStorage/idle→/processing→/failed→/retrying→。
    如果必须表达同一含义，改写为产品能理解的描述（例如：把“回滚”写成“保证不留下半成功的脏数据”）。
    工程附录允许出现这些词，但仍需可追溯 [SRC:*] 且不得新增事实。

输出模板（草案版必须严格按此结构输出，标题必须一致）：
1) # PRD 2.0 — 可开工草案版
2) ## 文档基础信息（门禁 FAIL 时：必须写“草案（未冻结）”）
3) ## 开工前置条件清单（P0 / BLOCKED 必补条款）
4) ## PM 决策清单（默认阅读）
5) ## 正文（背景/目标/范围/功能需求/关键规则/验收标准/最小观测字段）——只写原 PRD 已明确部分
6) <details><summary>工程附录（折叠）</summary>
   （这里放：字段名/接口名/状态枚举/日志字段/错误码命名规则等工程细节与候选口径）
   </details>

## 原始 PRD（节选）
---
{(original_prd or '')[:8000]}
---

## V2 评审结论（节选）

### 阻断项/缺口（BLOCKED）
{blockers_section}

### 已裁决项（DECIDED/OPEN）
{decisions_section}

### PRD 补丁条款（附录参考）
{patch_section}

## 任务
请输出一份完整 PRD 2.0 Markdown，必须包含：
- 文档基础信息（含：版本、生成日期、门禁结论与交付类型）
- 背景、目标、范围/终端/约束、功能需求、关键规则、异常与错误码、状态机、权限与安全、验收标准（Given/When/Then）、最小观测字段
- 若为草案：开头必须有《开工前置条件清单》（从 BLOCKED 的必补条款中提炼，保留 B# 来源引用）
- 若为正式冻结版：不得出现 TBD/AUTO_PROPOSED/“待定”措辞

额外结构要求（草案版必须遵守）：
- 在《开工前置条件清单》之后，必须紧跟一节《PM 决策清单（默认阅读）》，把所有缺口翻译成“产品需要确认的问题”（每条 1-3 个问句 + 选项/填空），并给出依据（引用原文摘录或“PRD原文未覆盖该点”）。
- 文末必须有《工程附录（折叠）》：用 Markdown 的 <details><summary>…</summary>…</details> 包起来；把接口/字段名/状态机枚举/日志字段等工程细节放进去。正文不要出现这些工程细节。

直接输出 PRD 文本，不要解释。"""


def _enforce_delivery_semantics(md: str, *, gate: Dict[str, Any], export_kind: str) -> str:
    """
    Lightweight post-check to prevent obvious "BLOCKED written as PASS" accidents.
    This does NOT try to be a full consistency checker; it only hardens delivery semantics.
    """
    text = str(md or "").strip()
    if not text:
        return text
    gate_state = _clean((gate or {}).get("gate") or "").upper()
    export_kind = (export_kind or "draft").strip().lower()
    if export_kind != "frozen" and gate_state and gate_state != "PASS":
        # If not PASS, forbid "正式冻结"/"门禁PASS" style claims.
        text = re.sub(r"正式冻结", "草案（未冻结）", text)
        text = re.sub(r"门禁\s*：?\s*\*\*PASS\*\*", f"门禁：**{gate_state}**", text)
        text = re.sub(r"门禁\s*：?\s*PASS", f"门禁：{gate_state}", text)
        # Common confusion: "补齐后方可视为草案" contradicts "可开工草案版".
        # Normalize to "补齐后升级为冻结版/可下发".
        text = re.sub(r"补齐并冻结后方可视为草案（未冻结）版", "补齐并冻结后方可升级为冻结版（可下发）", text)
        text = re.sub(r"补齐后方可视为草案（未冻结）版", "补齐并冻结后方可升级为冻结版（可下发）", text)
        text = re.sub(r"补齐后方可视为草案版", "补齐并冻结后方可升级为冻结版（可下发）", text)
        # Draft safety net: when Gate is not PASS, avoid "making up" concrete constants/examples.
        # Only sanitize a few high-risk patterns that repeatedly caused hallucinations/contradictions.
        text = re.sub(r"\bERR_UNAUTHORIZED\b", "【待补：错误码枚举】", text)
        text = re.sub(r"\bERR_INVALID_INPUT\b", "【待补：错误码枚举】", text)
        # Avoid hardcoding retry backoff examples if not frozen.
        text = re.sub(r"指数退避", "【候选口径（待冻结）：退避策略】", text)
        text = re.sub(r"(?i)\b1s\s*、\s*2s\s*、\s*4s\b", "【候选口径（待冻结）：重试间隔序列】", text)
        text = re.sub(r"(?i)\b1s\s*,\s*2s\s*,\s*4s\b", "【候选口径（待冻结）：重试间隔序列】", text)
        # Avoid locking in response body field names when still blocked.
        text = re.sub(r"\bcurrent_version\b", "【待补：当前版本号字段名】", text)
        # Reduce naming drift in observability sections (display-only).
        text = re.sub(r"\bresponse\.", "resp.", text)
        # Fix common self-contradictions in draft headers/footers.
        text = re.sub(r"非草案（未冻结）", "草案（未冻结）", text)
        text = re.sub(r"待补齐缺口后方可视为草案（未冻结）版", "待补齐缺口并冻结后方可升级为冻结版（可下发）", text)
        # Avoid locking in specific API path examples in draft (often hallucinated).
        text = re.sub(r"\bGET\s*/resource/\{id\}\b", "【待补：刷新获取最新版本号的接口/机制】", text)
        text = re.sub(r"\bGET\s*/resource/\{id\}\s*接口\b", "【待补：刷新获取最新版本号的接口/机制】", text)
        # Reduce naming drift for version field in draft (display-only; keep neutral).
        text = re.sub(r"\bversion_id\b", "resource_version（或等价版本号字段）", text)
        # Fix common contradictory wording about draft status.
        text = re.sub(r"不可视为草案（未冻结）\s*PRD", "属于草案（未冻结），不可作为最终开测依据", text)
        text = re.sub(r"不可视为草案（未冻结）", "属于草案（未冻结）", text)

        # Best-effort: remove engineering blackwords from non-appendix正文 (display-only).
        # Split by first <details> (engineering appendix). Only sanitize the part before it.
        parts = re.split(r"(?i)<details[^>]*>", text, maxsplit=1)
        head = parts[0]
        tail = "<details" + parts[1] if len(parts) > 1 else ""
        head = re.sub(r"\bGET\b", "获取（接口方式待定）", head)
        head = re.sub(r"\bPOST\b", "提交（接口方式待定）", head)
        head = re.sub(r"\bHTTP\b", "接口协议", head)
        head = re.sub(r"枚举", "类型列表（需定）", head)
        head = re.sub(r"回滚", "保证不留下半成功的脏数据", head)
        head = re.sub(r"自增", "自动加 1", head)
        head = re.sub(r"只读", "仅用于读取", head)
        head = re.sub(r"ISO\s*8601|ISO8601", "标准时间格式", head)
        head = re.sub(r"localStorage", "本地缓存", head)
        head = re.sub(r"\bidle\b", "空闲态", head, flags=re.IGNORECASE)
        head = re.sub(r"\bprocessing\b", "处理中", head, flags=re.IGNORECASE)
        head = re.sub(r"\bretrying\b", "重试中", head, flags=re.IGNORECASE)
        head = re.sub(r"\bfailed\b", "失败", head, flags=re.IGNORECASE)
        text = head + tail
    return text

def rewrite_prd_llm(
    *,
    original_prd: str,
    blockers: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
    prd_patch: str,
    gate: Dict[str, Any],
    export_kind: str,
    llm_config_path: str,
    now_str: str,
    timeout: int = 120,
) -> Tuple[str, Optional[str]]:
    prompt = _build_rewrite_prompt(
        original_prd=original_prd,
        blockers=blockers,
        decisions=decisions,
        prd_patch=prd_patch,
        gate=gate,
        export_kind=export_kind,
        now_str=now_str,
    )
    try:
        text = call_llm_with_retry(
            messages=[{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            timeout=timeout,
            max_retries=2,
            retry_delay=2,
        )
        text = str(text or "").strip()
        if not text:
            return "", "LLM 返回为空"
        text = _enforce_delivery_semantics(text, gate=gate, export_kind=export_kind)
        return text, None
    except Exception as e:
        return "", f"LLM 调用失败: {e}"


def rewrite_prd(
    *,
    original_prd: str,
    blockers: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
    prd_patch: str,
    gate: Dict[str, Any],
    export_kind: str = "draft",
    llm_config_path: str,
    now_str: str = "",
    use_llm: bool = True,
    timeout: int = 120,
) -> Tuple[str, str, str]:
    """
    Generate PRD 2.0 document.

    Returns (prd_v2_markdown, mode_used, llm_error).
    mode_used is "llm" or "rule".
    """
    llm_error = ""
    if use_llm and llm_config_path:
        text, err = rewrite_prd_llm(
            original_prd=original_prd,
            blockers=blockers,
            decisions=decisions,
            prd_patch=prd_patch,
            gate=gate,
            export_kind=export_kind,
            llm_config_path=llm_config_path,
            now_str=now_str,
            timeout=timeout,
        )
        if text and not err:
            return text, "llm", ""
        llm_error = err or "LLM 输出为空"
    text = rewrite_prd_rule(
        original_prd=original_prd,
        blockers=blockers,
        decisions=decisions,
        prd_patch=prd_patch,
    )
    text = _enforce_delivery_semantics(text, gate=gate, export_kind=export_kind)
    return text, "rule", llm_error

