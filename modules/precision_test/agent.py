"""精准回归 多步风险分析 Agent（Flask 侧编排，不迁移架构）。

把原来 analyze_code_diff 里的「单次 LLM 黑盒调用」升级为 3 步可追溯编排：
  1. 粗分 (Candidate)：从 diff + CodeGraph 代码影响链 识别候选风险区域
  2. 细化 (Classify)：逐条给出分类/优先级/影响面/置信度 + 建议测试点
  3. 校验 (Validate)：去重、覆盖核对（CodeGraph 受影响测试是否都有对应风险）、补缺

设计原则（对齐平台 Agent 红线）：
  - 只读 + 失败即降级：任何一步 LLM 调用失败/解析失败，退回到上一步结果或规则默认风险，
    绝不抛错中断主流程（normalize_analysis 仍可用）。
  - 可审计：每步的输入/输出/耗时/状态写入 agent_trace，前端可展开查看「AI 怎么推导的」。
  - 证据闭环：CodeGraph 算出的代码影响作为硬证据喂入 prompt，不再是孤儿元数据。

Mastra 作为可选后端：若配置了 MASTRA_ANALYSIS_URL 且可达，可将粗分委托 Mastra 多步分析，
否则走本地 3 步（见 mastra_client）。
"""

import json
import logging
import time

from .engine import (
    extract_json_object,
    _default_risks,
    _normalize_priority,
    CATEGORY_EXECUTORS,
    refine_category,
)

try:
    from utils.llm_client import call_llm
except Exception:  # pragma: no cover - 仅在非 Flask 上下文独立测试时可能失效
    call_llm = None

try:
    from .tools import TOOL_SCHEMAS, dispatch_tool_call
except Exception:
    TOOL_SCHEMAS, dispatch_tool_call = [], None

logger = logging.getLogger(__name__)

ALLOWED_CATEGORIES = set(CATEGORY_EXECUTORS)
CANDIDATE_CATEGORIES = "、".join(ALLOWED_CATEGORIES)

# 六个分类的判定信号与「异常」反例，注入每一步 prompt，抑制 LLM 把普通业务改动
# 一股脑归为「异常」（error/exception 字样在业务代码里处处可见，但那不是韧性主线）。
CATEGORY_DEFS = (
    "风险分类定义（只能选其一）：\n"
    "· 点歌：点歌/切歌/收藏/歌单/队列/搜索等曲库与队列（song/queue/点歌/歌曲/收藏/切歌/歌单）\n"
    "· 播放：起播/暂停/seek/解码/音画/卡顿/黑屏/无声等播放链路（player/codec/播放/解码/音画/起播）\n"
    "· 设备：机顶盒固件/型号/启动/重启/ADB/配置等盒子相关（stb/firmware/机顶盒/固件/重启/ADB）\n"
    "· 服务端：接口/数据库/缓存/服务/房台等业务后端（api/service/接口/数据库/缓存/房台/服务器）\n"
    "· 跨端：机顶盒/移动端/中控/包厢之间的状态同步（sync/中控/包厢/状态一致/多端）\n"
    "· 异常【仅在改动主线就是韧性/容灾本身时才选】：超时重试/断网弱网/降级熔断/断电容灾等，"
    "且不属于上述任一业务子系统。\n"
    "关键反例：普通业务代码里的 try/except、错误返回、日志、参数校验都属于该功能的一部分，"
    "应归入对应业务分类；不要因为文本出现『error/异常/失败』字样就判为「异常」。"
)

_SYSTEM_PREAMBLE = (
    "你是 VOD 点播系统（Android 机顶盒 + ARM 服务器后端）的测试风险分析专家。"
    "输出必须是严格 JSON，不要包含任何解释性文字或 markdown 代码块标记。"
)

_LLM_TIMEOUT = 90
_LLM_TEMPERATURE = 0.1


def _codegraph_evidence_block(codegraph_impact):
    """把 CodeGraph 影响结果转成喂给 LLM 的证据文本（闭环关键）。"""
    if not isinstance(codegraph_impact, dict):
        return ""
    changed = codegraph_impact.get("changed_files") or []
    tests = codegraph_impact.get("affected_tests") or []
    symbols = codegraph_impact.get("impact_symbols") or []
    if not (changed or tests or symbols):
        return ""
    lines = ["【代码影响分析 CodeGraph（硬证据）】"]
    if changed:
        lines.append("改动文件: " + ", ".join(changed))
    if symbols:
        top = symbols[:15]
        lines.append("影响符号链: " + "; ".join(
            f"{s.get('name','?')}({s.get('file','')})" for s in top
        ))
    if tests:
        lines.append("受影响测试文件: " + ", ".join(tests))
    return "\n".join(lines) + "\n"


def _call_llm_json(prompt, *, expect_keys=("risks",)):
    """调用 LLM 并返回解析后的 dict；失败抛出，由编排层降级。"""
    if call_llm is None:
        raise RuntimeError("llm_client 不可用")
    text = call_llm(
        messages=[{"role": "user", "content": _SYSTEM_PREAMBLE + "\n\n" + prompt}],
        timeout=_LLM_TIMEOUT,
        temperature=_LLM_TEMPERATURE,
    )
    data = extract_json_object(text)
    if not isinstance(data, dict) or not any(k in data for k in expect_keys):
        raise ValueError("LLM 返回无法解析为期望结构")
    return data


# ---------------------------------------------------------------------------
# ReAct 工具调用链路（精准回归 Agent 升格：从提示链到真 Agent 的核心）
# ---------------------------------------------------------------------------
def _build_react_system_prompt():
    return (
        "你是 VOD 点播系统（Android 机顶盒 + ARM 服务器后端）的测试风险分析 Agent。"
        "你可以调用只读工具来核实代码改动的影响，再给出精准的回归风险与测试点。"
        "工具不会修改任何代码或设备，仅供你检索信息。\n"
        "分析步骤：先理解改动与需求，必要时调用工具核实（如查某符号是否真的被改、查改动影响面），"
        "综合后输出最终 JSON。\n"
        "风险分类（只能选其一）：点歌/播放/设备/服务端/跨端/异常。"
        "异常仅当改动主线就是韧性/容灾本身（超时重试/断网弱网/降级熔断/断电容灾）时才选；"
        "普通业务的 try/except/错误返回/日志不是异常，应归入对应业务分类。"
    )


def _build_react_user_prompt(code_diff, requirement, project_type, summary, cg_block):
    changed = (summary.get("files") or []) if isinstance(summary, dict) else []
    return (
        f"项目类型: {project_type}\n"
        f"需求说明: {requirement}\n"
        f"改动摘要文件: {', '.join(changed)}\n\n"
        f"{cg_block}"
        f"代码 Diff:\n{code_diff}\n\n"
        "请先分析本次改动最可能影响的功能区域，并在需要时调用工具核实（例如查某函数/类是否真实被改动、"
        "或计算改动影响面）。最终请输出 JSON：\n"
        '{"risks": [{"category":"分类","title":"标题","scope":"影响范围","evidence":["证据"],'
        '"priority":"P0/P1/P2","impact_type":"direct/indirect","affected_users":"受影响方",'
        '"confidence":"high/medium/low"}],'
        '"test_points": [{"risk_index":1,"type":"场景类型","title":"测试点标题",'
        '"steps":"操作步骤","expected":"预期结果","priority":"P0/P1/P2"}]}'
    )


def _summarize_observation(obs):
    if not isinstance(obs, dict):
        return str(obs)[:300]
    if obs.get("available") is False:
        return f"[工具不可用] {obs.get('reason','')}"
    if "matches" in obs:
        m = obs["matches"][:5]
        tail = f" 等共{obs.get('count', len(obs['matches']))}条" if obs.get("count", 0) > 5 else ""
        return "命中: " + "; ".join(f"{x.get('file')}:{x.get('line')} {x.get('snippet','')[:60]}" for x in m) + tail
    if "impacted" in obs:
        return "[CodeGraph 影响] " + str(obs["impacted"])[:300]
    return str(obs)[:300]


def _run_react(code_diff, requirement, project_type, summary, codegraph_impact=None):
    """ReAct 循环：think -> (act/observe) -> 产出最终 JSON。返回 (risks, points, trace, degraded)。"""
    from utils.llm_client import call_llm_with_tools
    cg_block = _codegraph_evidence_block(codegraph_impact)
    messages = [
        {"role": "system", "content": _build_react_system_prompt()},
        {"role": "user", "content": _build_react_user_prompt(code_diff, requirement, project_type, summary, cg_block)},
    ]
    trace = []
    degraded = False
    FINAL_HINT = (
        "请基于以上所有思考与工具结果，输出最终 JSON："
        '{"risks":[...], "test_points":[...]}。'
    )
    for step in range(6):
        try:
            resp = call_llm_with_tools(messages, tools=TOOL_SCHEMAS, timeout=120)
        except Exception as exc:
            logger.warning("ReAct 调用异常: %s", exc)
            break
        if resp.get("degraded"):
            degraded = True
            break
        content = (resp.get("content") or "").strip()
        tool_calls = resp.get("tool_calls") or []
        trace.append({
            "step": step, "type": "think",
            "content": content[:600],
            "tool_calls": [tc.get("name") for tc in tool_calls],
        })
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {"id": tc.get("id", ""), "type": "function",
                     "function": {"name": tc.get("name", ""),
                                  "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                try:
                    obs = dispatch_tool_call(tc.get("name"), tc.get("arguments")) if dispatch_tool_call else {"available": False, "reason": "工具集未加载"}
                except Exception as exc:
                    obs = {"available": False, "reason": f"工具异常: {exc}"}
                trace.append({
                    "step": step, "type": "tool",
                    "name": tc.get("name"), "arguments": tc.get("arguments"),
                    "observation": _summarize_observation(obs),
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tc.get("name"),
                    "content": json.dumps(obs, ensure_ascii=False)[:4000],
                })
            continue
        parsed = extract_json_object(content)
        if parsed and (parsed.get("risks") or parsed.get("test_points")):
            risks, points = _finalize(parsed.get("risks", []), parsed.get("test_points", []))
            if risks:
                trace.append({"step": step, "type": "final", "risks": len(risks), "test_points": len(points)})
                return risks, points, trace, False
        messages.append({"role": "user", "content": FINAL_HINT})
    return [], [], trace, degraded


def _critic_and_revise(risks, points, code_diff, requirement):
    """真 Critic：检测矛盾/证据缺失/低置信，必要时触发一轮 LLM 修订。"""
    if not risks:
        return risks, points
    issues = []
    for r in risks:
        if str(r.get("priority", "")).upper() in ("P0", "P1"):
            ev = r.get("evidence") or []
            if not ev or all((str(e).strip() in ("", "代码变更", "代码变更相关")) for e in ev):
                issues.append(f"高风险项「{r.get('title','')}」缺少具体证据")
    low = sum(1 for r in risks if str(r.get("confidence", "")).lower() == "low")
    if risks and low / len(risks) > 0.5:
        issues.append(f"低置信风险占比过高({low}/{len(risks)})")
    if not issues:
        return risks, points
    if call_llm is None:
        return risks, points
    feedback = "；".join(issues)
    prompt = (
        f"{_SYSTEM_PREAMBLE}\n\n"
        f"需求: {requirement}\n\n代码 Diff:\n{code_diff}\n\n"
        f"以下是待修订的风险与测试点 JSON：\n{json.dumps({'risks': risks, 'test_points': points}, ensure_ascii=False, indent=2)}\n\n"
        f"Critic 发现问题：{feedback}。请修订后重新输出完整 JSON（结构同上）。"
    )
    try:
        text = call_llm(messages=[{"role": "user", "content": prompt}], timeout=90, temperature=0.1)
        parsed = extract_json_object(text)
        if parsed and (parsed.get("risks") or parsed.get("test_points")):
            revised_risks, revised_points = _finalize(parsed.get("risks", []), parsed.get("test_points", []))
            if revised_risks:
                return revised_risks, revised_points
    except Exception as exc:
        logger.warning("Critic 修订失败，保留原结果: %s", exc)
    return risks, points


def _build_candidate_prompt(code_diff, requirement, project_type, summary, cg_block):
    return (
        f"项目类型: {project_type}\n"
        f"需求说明: {requirement}\n"
        f"改动摘要文件: {', '.join(summary.get('files') or [])}\n\n"
        f"{cg_block}"
        f"代码 Diff:\n{code_diff}\n\n"
        "任务：识别本次改动最可能引入测试风险的功能区域（粗分，不评价优先级）。\n"
        f"{CATEGORY_DEFS}\n"
        "先判断改动属于哪个业务子系统，再选分类；只有改动主线确实是韧性/容灾时才选「异常」。\n"
        "输出 JSON 结构：\n"
        '{"candidates": [{"category": "分类", "title": "风险标题", '
        '"scope": "影响范围描述", "evidence": ["文件或符号证据"]}]}'
    )


def _build_classify_prompt(candidates):
    cand_text = "\n".join(
        f"{i+1}. [{c.get('category','?')}] {c.get('title','')} —— {c.get('scope','')}"
        for i, c in enumerate(candidates)
    )
    return (
        "以下为候选风险区域，请逐条细化分类并补充测试点：\n"
        f"{cand_text}\n\n"
        f"{CATEGORY_DEFS}\n"
        "若候选自带分类，请沿用；重新判断时也只从上述分类中选，且遵循反例约束"
        "（业务代码的错误兜底归入对应业务分类，不归「异常」）。\n"
        "对每个候选，给出：priority(P0/P1/P2)、impact_type(direct/indirect)、"
        "affected_users、confidence(high/medium/low)。\n"
        "并为每条风险提出 1-2 个最值得先测的测试点（用 risk_index 指向上面的序号）。\n"
        "输出 JSON 结构：\n"
        '{"risks": [{"category":"分类","title":"标题","scope":"影响范围",'
        '"evidence":["证据"],"priority":"P0/P1/P2","impact_type":"direct/indirect",'
        '"affected_users":"受影响方","confidence":"high/medium/low"}],'
        '"test_points": [{"risk_index": 1, "type":"场景类型", "title":"测试点标题",'
        '"steps":"操作步骤","expected":"预期结果","priority":"P0/P1/P2"}]}'
    )


def _build_validate_prompt(risks, test_points, cg_block):
    risks_text = "\n".join(
        f"{i+1}. [{r.get('category','?')}/{r.get('priority','?')}] {r.get('title','')}"
        for i, r in enumerate(risks)
    )
    tp_text = "\n".join(
        f"- 测试点(指向风险#{t.get('risk_index','?')}): {t.get('title','')}"
        for t in test_points
    )
    return (
        f"{cg_block}"
        "请校验并补全以下风险与测试点，确保：\n"
        "1) 风险分类必须属于允许集合；2) 去除重复风险；3) 每个风险至少对应 1 个测试点；\n"
        "4) 若 CodeGraph 列出的受影响测试文件尚未被任何测试点覆盖，请补一条对应测试点；\n"
        "5) risk_index 与下方风险序号一致。\n"
        f"现有风险：\n{risks_text}\n现有测试点：\n{tp_text}\n\n"
        "输出最终 JSON（结构同前）：\n"
        '{"risks":[...同上...], "test_points":[{"risk_index":1,"type":"场景类型",'
        '"title":"标题","steps":"步骤","expected":"预期","priority":"P0/P1/P2"}]}'
    )


def _finalize(raw_risks, raw_points):
    """给风险分配 R0n 稳定 id，并把测试点的 risk_index 映射成 risk_ids(R0n)。"""
    risks = []
    for i, r in enumerate(raw_risks or [], 1):
        if not isinstance(r, dict):
            continue
        category = str(r.get("category") or "").strip()
        # 回正：无效分类走 detect 兜底；被误判的「异常」归位到命中的功能分类
        category = refine_category(
            category,
            f"{r.get('title', '')} {r.get('scope', '')} "
            f"{' '.join(r.get('evidence') or [])}",
        )
        risks.append({
            "id": f"R{i:02d}",
            "category": category,
            "title": str(r.get("title") or f"{category}风险").strip(),
            "priority": _normalize_priority(r.get("priority")),
            "impact_type": "indirect" if str(r.get("impact_type")).lower() == "indirect" else "direct",
            "affected_users": str(r.get("affected_users") or "相关包厢与用户").strip(),
            "scope": str(r.get("scope") or r.get("description") or "待进一步确认影响范围").strip(),
            "evidence": r.get("evidence") or ["代码变更"],
            "confidence": str(r.get("confidence") or "medium").lower()
            if str(r.get("confidence") or "").lower() in {"high", "medium", "low"} else "medium",
        })

    id_by_index = {i + 1: r["id"] for i, r in enumerate(risks)}
    points = []
    for t in raw_points or []:
        if not isinstance(t, dict):
            continue
        idx = t.get("risk_index") or t.get("risk_refs") or 1
        if isinstance(idx, list):
            rid = [id_by_index.get(int(x)) for x in idx if str(x).isdigit() and int(x) in id_by_index]
        else:
            rid = [id_by_index.get(int(idx))] if str(idx).isdigit() and int(idx) in id_by_index else []
        rid = [x for x in rid if x]
        if not rid:
            continue
        points.append({
            "risk_ids": rid,
            "priority": _normalize_priority(t.get("priority"), risks[0]["priority"] if risks else "P1"),
            "type": str(t.get("type") or "功能场景").strip(),
            "title": str(t.get("title") or "验证风险").strip(),
            "precondition": str(t.get("precondition") or "测试环境与数据准备完成").strip(),
            "steps": str(t.get("steps") or "执行对应业务操作").strip(),
            "expected": str(t.get("expected") or "结果符合需求且状态一致").strip(),
            "mode": "",
            "source": "llm",
        })
    return risks, points


def run_risk_agent(code_diff, requirement, project_type, summary, codegraph_impact=None):
    """运行 3 步风险分析 Agent。

    返回 dict：{"risks": [...], "test_points": [...], "agent_trace": [...],
               "analysis_source": "agent", "agent_error": ""}
    任何一步失败都会降级，保证返回的 risks 永不为空（最终由 normalize_analysis 兜底）。
    """
    trace = []
    cg_block = _codegraph_evidence_block(codegraph_impact)
    risks, points = [], []
    agent_error = ""

    # ---- 新阶段主路径：ReAct 工具调用链路（真 Agent） ----
    # 若底层 LLM 支持 function calling（DeepSeek/OpenAI 等），让 Agent 自主决定调用
    # 只读工具（查代码符号/查影响面）后再产出结论；否则 call_llm_with_tools 返回
    # degraded=True，此处 fall through 到下方 Mastra/三步链（向后兼容，零回归）。
    try:
        react_risks, react_points, react_trace, react_degraded = _run_react(
            code_diff, requirement, project_type, summary, codegraph_impact
        )
        if not react_degraded and react_risks:
            react_risks, react_points = _critic_and_revise(
                react_risks, react_points, code_diff, requirement
            )
            return {
                "risks": react_risks, "test_points": react_points,
                "agent_trace": react_trace, "analysis_source": "agent_react",
                "agent_error": "",
            }
        if react_degraded:
            logger.info("ReAct 降级（provider 不支持 tools），回退 Mastra/三步链")
            trace.append({"step": "react", "status": "degraded_to_legacy",
                          "detail": "provider 不支持 function calling"})
    except Exception as exc:
        logger.warning("ReAct 失败，降级旧链: %s", exc)
        trace.append({"step": "react", "status": "error_fallback", "error": str(exc)})
    # ---- 以下为降级路径（Mastra 委托 + 本地三步），保持不变 ----

    # ---- 可选：委托 Mastra 多步分析（配置 MASTRA_ANALYSIS_URL 且可达时）----
    # Mastra 在 4111 端口自行完成 粗分->细化->校验，本地不再重复调用 LLM。
    # 失败或未配置时静默回退到下面的本地三步编排（架构不变）。
    try:
        from .mastra_client import call_mastra_analysis
        ma = call_mastra_analysis(code_diff, requirement, project_type)
        if isinstance(ma, dict) and not ma.get("error") and ma.get("risks"):
            m_risks, m_points = _finalize(ma.get("risks", []), ma.get("test_points", []))
            if m_risks:
                trace.append({
                    "step": "mastra", "status": "ok", "duration_ms": 0,
                    "risks": len(m_risks), "test_points": len(m_points), "error": "",
                })
                return {
                    "risks": m_risks, "test_points": m_points,
                    "agent_trace": trace, "analysis_source": "agent_mastra",
                    "agent_error": "", "mastra_analysis": ma,
                }
    except Exception as exc:
        logger.warning("Mastra 委托失败，回退本地 Agent: %s", exc)

    # ---- Step 1: 粗分 Candidate ----
    step_start = time.time()
    candidates = []
    try:
        data = _call_llm_json(
            _build_candidate_prompt(code_diff, requirement, project_type, summary, cg_block),
            expect_keys=("candidates",),
        )
        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError("候选风险为空")
        trace.append({
            "step": "candidate", "status": "ok",
            "duration_ms": int((time.time() - step_start) * 1000),
            "candidates": len(candidates),
            "error": "",
        })
    except Exception as exc:
        agent_error = f"candidate: {exc}"
        # 降级：用规则默认风险，后续步骤跳过 LLM
        risks = _default_risks(requirement, code_diff, summary)
        trace.append({
            "step": "candidate", "status": "fallback_rules",
            "duration_ms": int((time.time() - step_start) * 1000),
            "candidates": 0, "error": str(exc),
        })
        # 规则风险已就绪，直接收尾（不再调用 LLM 细化）
        return {
            "risks": risks, "test_points": [],
            "agent_trace": trace, "analysis_source": "agent", "agent_error": agent_error,
        }

    # ---- Step 2: 细化 Classify ----
    step_start = time.time()
    try:
        data = _call_llm_json(
            _build_classify_prompt(candidates),
            expect_keys=("risks", "test_points"),
        )
        raw_risks = data.get("risks") or []
        raw_points = data.get("test_points") or []
        if not raw_risks:
            raise ValueError("细化后风险为空")
        risks, points = _finalize(raw_risks, raw_points)
        if not risks:
            raise ValueError("细化后无有效风险")
        trace.append({
            "step": "classify", "status": "ok",
            "duration_ms": int((time.time() - step_start) * 1000),
            "risks": len(risks), "test_points": len(points), "error": "",
        })
    except Exception as exc:
        agent_error = (agent_error + "; " if agent_error else "") + f"classify: {exc}"
        # 降级：把候选直接当风险（默认优先级），测试点交给模板补充
        raw_candidates_as_risks = [
            {"category": c.get("category", "异常"), "title": c.get("title", "风险"),
             "scope": c.get("scope", ""), "evidence": c.get("evidence") or ["代码变更"],
             "priority": "P1", "impact_type": "indirect", "affected_users": "相关包厢与用户",
             "confidence": "low"}
            for c in candidates
        ]
        risks, _ = _finalize(raw_candidates_as_risks, [])
        trace.append({
            "step": "classify", "status": "fallback_candidates",
            "duration_ms": int((time.time() - step_start) * 1000),
            "risks": len(risks), "test_points": 0, "error": str(exc),
        })

    # ---- Step 3: 校验 Validate ----
    step_start = time.time()
    try:
        data = _call_llm_json(
            _build_validate_prompt(
                [{"category": r["category"], "title": r["title"], "priority": r["priority"]} for r in risks],
                [{"risk_index": i + 1, "title": p["title"]} for i, p in enumerate(points)],
                cg_block,
            ),
            expect_keys=("risks", "test_points"),
        )
        raw_risks = data.get("risks") or []
        raw_points = data.get("test_points") or []
        if raw_risks:
            final_risks, final_points = _finalize(raw_risks, raw_points)
            if final_risks:
                risks, points = final_risks, final_points
        trace.append({
            "step": "validate", "status": "ok",
            "duration_ms": int((time.time() - step_start) * 1000),
            "risks": len(risks), "test_points": len(points), "error": "",
        })
    except Exception as exc:
        agent_error = (agent_error + "; " if agent_error else "") + f"validate: {exc}"
        trace.append({
            "step": "validate", "status": "skip_passthrough",
            "duration_ms": int((time.time() - step_start) * 1000),
            "risks": len(risks), "test_points": len(points), "error": str(exc),
        })

    return {
        "risks": risks, "test_points": points,
        "agent_trace": trace, "analysis_source": "agent", "agent_error": agent_error,
    }
