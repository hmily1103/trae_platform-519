# -*- coding: utf-8 -*-
"""Agent 2.0 协议层（A1）。

定义多 Agent 诊断的统一协议：
- ``AgentFinding``：所有专职 Agent 的统一输出结构（产物 + 证据 + 耗时 + 状态）；
- ``BaseAgent``：专职 Agent 抽象基类（统一输入 alert_dict + context）；
- ``run_with_guard``：单 Agent 超时/异常降级封装——协议层保证"永不抛出"。

设计约束（红线）：
1. 只读——Agent 不得修改工程代码、不得改设备状态、不得写业务数据；
2. 失败即降级——任何异常/超时都收敛为一个可展示的 AgentFinding，
   调用方（Planner/Synthesizer）永远拿到结构化结果，不需要 try/except；
3. 可审计——status/degrade_reason/error 全程留痕。
"""
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Agent 状态常量（收敛口径，前端按此渲染）
STATUS_OK = "ok"              # 正常完成，产物可用
STATUS_FAILED = "failed"      # 执行异常，已降级
STATUS_TIMEOUT = "timeout"    # 超时，已降级
STATUS_SKIPPED = "skipped"    # 按计划跳过（如源码关联未启用）
VALID_STATUSES = (STATUS_OK, STATUS_FAILED, STATUS_TIMEOUT, STATUS_SKIPPED)

# 单 Agent 默认超时（秒）——A4 并行执行时沿用
DEFAULT_AGENT_TIMEOUT = 15


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class AgentFinding:
    """专职 Agent 的统一输出。

    - agent_name: Agent 标识（如 log_analysis / device_probe / history / source_code）
    - status: ok / failed / timeout / skipped
    - summary: 一句话结论（前端时间线直接展示）
    - evidence: 证据列表，每项 {kind, desc, detail}，
      Synthesizer 会将其合并进统一证据链（build_evidence_chain 扩展）
    - artifacts: 结构化产物（如截图 URL、根因文本、历史案例列表），键由各 Agent 约定
    - duration_ms: 执行耗时（毫秒）
    - error: 失败/超时的原因（可审计）
    - degrade_reason: 降级说明（前端明示"为什么这项没跑出来"）
    """
    agent_name: str
    status: str = STATUS_OK
    summary: str = ""
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    started_at: str = field(default_factory=_now_iso)
    error: str = ""
    degrade_reason: str = ""

    def __post_init__(self):
        if self.status not in VALID_STATUSES:
            raise ValueError(
                "非法 Agent 状态: %r（允许: %s）" % (self.status, "/".join(VALID_STATUSES))
            )

    @property
    def usable(self) -> bool:
        """产物是否可用于综合裁决（仅 ok 状态计入证据链）。"""
        return self.status == STATUS_OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "artifacts": dict(self.artifacts),
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "error": self.error,
            "degrade_reason": self.degrade_reason,
        }


class BaseAgent(ABC):
    """专职 Agent 抽象基类。

    子类只需实现 ``run(alert, context)``：
    - alert: 告警字典（与现有 alert_dict 同构：id/type/rule_name/message/severity...）
    - context: 诊断上下文（device_id/package/log_lines/context_meta/action_result...）
    - 返回 AgentFinding；子类内部**不需要**兜底 try/except，
      协议层 run_with_guard 统一负责降级。

    只读红线：readonly 恒为 True，Planner 在装配计划时会断言该标记，
    任何声明 readonly=False 的 Agent 会被拒绝编排。
    """

    #: Agent 唯一标识（如 "log_analysis"）
    name = "base"
    #: 前端展示名（如 "日志分析"）
    display_name = "基础 Agent"
    #: 只读红线标记——子类不得覆写为 False
    readonly = True

    @abstractmethod
    def run(self, alert: Dict[str, Any], context: Dict[str, Any]) -> AgentFinding:
        """执行诊断子任务，返回 AgentFinding。"""

    # ---- 便捷构造 ----
    def ok(self, summary: str, evidence: Optional[List[Dict[str, Any]]] = None,
           artifacts: Optional[Dict[str, Any]] = None) -> AgentFinding:
        return AgentFinding(
            agent_name=self.name, status=STATUS_OK, summary=summary,
            evidence=evidence or [], artifacts=artifacts or {},
        )

    def skipped(self, reason: str) -> AgentFinding:
        return AgentFinding(
            agent_name=self.name, status=STATUS_SKIPPED,
            summary="已跳过", degrade_reason=reason,
        )


def run_with_guard(agent: BaseAgent, alert: Dict[str, Any],
                   context: Dict[str, Any],
                   timeout: float = DEFAULT_AGENT_TIMEOUT) -> AgentFinding:
    """带护栏地执行单个 Agent：超时/异常一律降级为结构化 AgentFinding，永不抛出。

    实现说明：
    - 在守护线程中执行，超时后放弃等待（线程自然结束，不强杀——
      各 Agent 内部的子进程调用自身带 timeout，不会泄漏长任务）；
    - 返回值三种来源：正常结果 / timeout 占位 / failed 占位。
    """
    if not isinstance(agent, BaseAgent):
        return AgentFinding(
            agent_name=getattr(agent, "name", "unknown"), status=STATUS_FAILED,
            summary="执行失败", error="非法 Agent：未实现 BaseAgent 协议",
            degrade_reason="协议校验失败，已降级",
        )
    if not agent.readonly:
        # 只读红线：拒绝执行任何声明可写的 Agent
        return AgentFinding(
            agent_name=agent.name, status=STATUS_FAILED,
            summary="执行被拒绝", error="Agent 声明 readonly=False，违反只读红线",
            degrade_reason="只读护栏拦截，已降级",
        )

    holder: Dict[str, Any] = {}

    def _target():
        try:
            holder["result"] = agent.run(alert, context)
        except Exception as exc:  # noqa: BLE001 —— 协议层兜底，必须宽捕获
            holder["error"] = "%s: %s" % (type(exc).__name__, exc)

    start = time.time()
    worker = threading.Thread(target=_target, daemon=True,
                              name="agent-%s" % agent.name)
    worker.start()
    worker.join(timeout)
    elapsed_ms = int((time.time() - start) * 1000)

    if worker.is_alive():
        return AgentFinding(
            agent_name=agent.name, status=STATUS_TIMEOUT,
            summary="执行超时", duration_ms=elapsed_ms,
            error="超过 %.0fs 未完成" % timeout,
            degrade_reason="单 Agent 超时护栏触发，已降级",
        )
    if "error" in holder:
        return AgentFinding(
            agent_name=agent.name, status=STATUS_FAILED,
            summary="执行失败", duration_ms=elapsed_ms,
            error=holder["error"],
            degrade_reason="Agent 内部异常，已降级",
        )
    result = holder.get("result")
    if not isinstance(result, AgentFinding):
        return AgentFinding(
            agent_name=agent.name, status=STATUS_FAILED,
            summary="执行失败", duration_ms=elapsed_ms,
            error="Agent 返回了非 AgentFinding 结果: %r" % type(result).__name__,
            degrade_reason="协议返回值校验失败，已降级",
        )
    result.duration_ms = elapsed_ms
    return result
