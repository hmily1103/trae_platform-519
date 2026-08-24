# -*- coding: utf-8 -*-
"""Agent 2.0 存量能力适配器（A3）。

把三个存量模块各包一层 BaseAgent 适配器（不改内部逻辑，纯封装）：
- ``LogAnalysisAdapter``   → agent.py         （LLM 日志分析，name=log_analysis）
- ``DeviceProbeAdapter``   → action_executor.py（只读取证动作，name=device_probe）
- ``HistoryAdapter``       → knowledge_base.py（历史案例检索，name=history）

设计要点：
1. 纯封装：适配器只做「输入翻译（alert/context → 存量接口参数）+
   输出装配（存量返回 → AgentFinding）」，存量模块零改动；
2. 可注入依赖：构造函数允许注入替身（analyzer/executor/kb），
   单元测试不打真实 LLM / adb / 知识库文件；生产环境走默认懒加载单例；
3. 取证不重跑：DeviceProbeAdapter 优先复用 context 里已有的 action_result
   （#29 在 _trigger_self_heal 开头已执行过动作，重复执行会二次截图/采集）；
4. 只读红线：三个适配器 readonly 恒为 True，且底层能力本身只读
   （action_executor 白名单 + custom_shell 拒绝；knowledge_base 仅检索）。
"""
from typing import Any, Callable, Dict, List, Optional

from .base import AgentFinding, BaseAgent, STATUS_FAILED

# 摘要最长展示字符（前端时间线一行放得下）
_SUMMARY_MAX = 80


def _clip(text: str, limit: int = _SUMMARY_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# ① 日志分析 Agent（封装 agent.py 的 LogAnalysisAgent.analyze）
# ---------------------------------------------------------------------------
class LogAnalysisAdapter(BaseAgent):
    """LLM 日志分析：alert + log_lines → 根因/建议/定位/影响。

    输入翻译：
    - log_lines ← context["log_lines"]
    - alert_context ← alert 的 rule_name/severity/type/message
      + context 的 device_context / historical_cases（RAG，若上游已备好）
    输出装配：
    - artifacts = AnalysisResult.to_dict()（root_cause/suggestions/...）
    - evidence: 有 problem_location 时挂一条堆栈定位证据
    """

    name = "log_analysis"
    display_name = "日志分析"

    def __init__(self, analyzer: Optional[Any] = None):
        # analyzer 需提供 .analyze(log_lines, alert_context) → 有 to_dict() 的结果
        self._analyzer = analyzer

    def _get_analyzer(self) -> Any:
        if self._analyzer is not None:
            return self._analyzer
        from ..agent import get_agent  # 懒加载：避免测试环境触发 LLM 配置读取
        return get_agent()

    def run(self, alert: Dict[str, Any], context: Dict[str, Any]) -> AgentFinding:
        log_lines: List[str] = list(context.get("log_lines") or [])
        alert_context: Dict[str, Any] = {
            "rule_name": alert.get("rule_name", ""),
            "severity": alert.get("severity", ""),
            "type": alert.get("type", ""),
            "log_line": alert.get("message", "") or alert.get("log_line", ""),
        }
        if context.get("device_context"):
            alert_context["device_context"] = context["device_context"]
        if context.get("historical_cases"):
            alert_context["historical_cases"] = context["historical_cases"]

        result = self._get_analyzer().analyze(log_lines, alert_context)
        data: Dict[str, Any] = (
            result.to_dict() if hasattr(result, "to_dict") else dict(result)
        )
        evidence: List[Dict[str, Any]] = []
        if data.get("problem_location"):
            evidence.append({
                "kind": "stack_location",
                "desc": "堆栈定位",
                "detail": data["problem_location"],
            })
        root_cause = data.get("root_cause", "")
        return self.ok(
            summary=_clip(root_cause) or "分析完成（未给出明确根因）",
            evidence=evidence,
            artifacts=data,
        )


# ---------------------------------------------------------------------------
# ② 设备取证 Agent（封装 action_executor.execute_action）
# ---------------------------------------------------------------------------
class DeviceProbeAdapter(BaseAgent):
    """只读设备取证：截图 / 白名单 shell 采集。

    执行策略（按序）：
    1. context 已带 action_result（#29 在告警触发时已先行执行）→ 直接复用，
       绝不二次截图/采集；
    2. 否则 context 给了 action + device_id → 调 execute_action 执行；
    3. 都没有 → skipped（规则未配置动作 / 无设备）。

    输出装配：
    - 动作 status=ok → Finding ok，产物挂 action_artifact 证据；
    - 动作 refused/error → Finding failed（留痕可审计，不进证据链）。
    """

    name = "device_probe"
    display_name = "设备取证"

    def __init__(self, executor: Optional[Callable[..., Dict[str, Any]]] = None):
        # executor 签名同 action_executor.execute_action
        self._executor = executor

    def _get_executor(self) -> Callable[..., Dict[str, Any]]:
        if self._executor is not None:
            return self._executor
        from ..action_executor import execute_action  # 懒加载
        return execute_action

    def run(self, alert: Dict[str, Any], context: Dict[str, Any]) -> AgentFinding:
        action_result: Optional[Dict[str, Any]] = context.get("action_result")
        if action_result is None:
            action = str(context.get("action") or "").strip()
            device_id = str(context.get("device_id") or "").strip()
            if not action or action == "none":
                return self.skipped("规则未配置取证动作")
            if not device_id:
                return self.skipped("无设备 ID，取证跳过")
            action_result = self._get_executor()(
                action, device_id,
                alert_id=str(alert.get("id", "")),
                package=str(context.get("package", "")),
            )

        status = (action_result or {}).get("status", "")
        summary = (action_result or {}).get("summary", "")
        if status == "ok":
            evidence = [{
                "kind": "action_artifact",
                "desc": "只读取证产物（%s）" % (action_result.get("type") or "action"),
                "detail": action_result.get("artifact")
                          or _clip(action_result.get("output", ""), 200),
            }]
            return self.ok(
                summary=_clip(summary) or "取证完成",
                evidence=evidence,
                artifacts={"action_result": action_result},
            )
        # refused / error：留痕降级，不进证据链（usable=False）
        return AgentFinding(
            agent_name=self.name, status=STATUS_FAILED,
            summary=_clip(summary) or "取证未完成",
            error=str((action_result or {}).get("output", "")) or status or "未知失败",
            degrade_reason="动作执行结果为 %s，产物不计入证据链" % (status or "空"),
            artifacts={"action_result": action_result or {}},
        )


# ---------------------------------------------------------------------------
# ③ 历史求证 Agent（封装 knowledge_base.search_similar）
# ---------------------------------------------------------------------------
class HistoryAdapter(BaseAgent):
    """历史案例检索：同类型/关键词重合的已有知识卡。

    输入翻译：query = 触发日志 + 规则名（与 selfheal 现有检索口径一致）
    输出装配：
    - artifacts["cases"] = 命中的知识卡列表（含 _score）
    - evidence: 每条命中案例挂一条 history_case 证据（最多 3 条）
    - 未命中也算 ok（"查过了没有"本身就是有效结论）
    """

    name = "history"
    display_name = "历史求证"

    def __init__(self, kb: Optional[Any] = None, top_k: int = 3):
        # kb 需提供 .search_similar(alert_type, query_text, top_k)
        self._kb = kb
        self._top_k = top_k

    def _get_kb(self) -> Any:
        if self._kb is not None:
            return self._kb
        from ..knowledge_base import get_knowledge_base  # 懒加载
        return get_knowledge_base()

    def run(self, alert: Dict[str, Any], context: Dict[str, Any]) -> AgentFinding:
        alert_type = str(alert.get("type", "")).lower()
        query = "%s %s" % (
            alert.get("message", "") or alert.get("log_line", ""),
            alert.get("rule_name", ""),
        )
        cases = self._get_kb().search_similar(alert_type, query.strip(),
                                              top_k=self._top_k) or []
        if not cases:
            return self.ok(
                summary="未检索到相似历史案例",
                evidence=[],
                artifacts={"cases": []},
            )
        evidence = []
        for c in cases[:3]:
            evidence.append({
                "kind": "history_case",
                "desc": "历史案例 %s（重合度 %s，已解决=%s）" % (
                    c.get("id", ""), c.get("_score", 0),
                    "是" if c.get("resolved") else "否"),
                "detail": _clip(str(c.get("root_cause", "")), 200),
            })
        top = cases[0]
        return self.ok(
            summary="命中 %d 条历史案例（最高重合度 %s）" % (
                len(cases), top.get("_score", 0)),
            evidence=evidence,
            artifacts={"cases": cases},
        )


# ---------------------------------------------------------------------------
# 批量注册（生产入口在 A4 并行执行器里调用）
# ---------------------------------------------------------------------------
def register_builtin_agents() -> List[str]:
    """把存量能力适配器注册进 Planner 注册表，返回注册的名称列表。

    幂等：重复调用会覆盖同名注册（实例无状态，覆盖安全）。
    源码关联（source_code）也在此注册——B2/B3 已交付，但仅当
    SourceCodeIndex.is_enabled() 时实际生效（未配置源码目录时 Agent
    内部自动 skipped）。
    """
    from .planner import register_agent
    agents = [LogAnalysisAdapter(), DeviceProbeAdapter(), HistoryAdapter()]
    for a in agents:
        register_agent(a)
    # 源码关联 Agent（B2+B3）：始终注册，内部按 is_enabled() 降级 skipped
    from .source_agent import SourceCodeAdapter
    register_agent(SourceCodeAdapter())
    return [a.name for a in agents] + ["source_code"]
