# -*- coding: utf-8 -*-
"""Agent 2.0 并行执行器（A4）。

职责：接收 Planner 产出的 DiagnosisPlan，用线程池**并行**执行计划中的
专职 Agent，并施加双层护栏：

1. 单 Agent 超时（默认 15s）——由协议层 run_with_guard 负责，超时降级为
   status=timeout 的 AgentFinding；
2. 整体预算（默认 40s）——本模块负责，预算耗尽后未完成的 Agent 一律
   降级为 timeout（degrade_reason 明示"整体预算耗尽"），不再等待。

红线与降级承诺：
- execute_plan **永不抛出**：任何 Agent 失败/超时/未注册都收敛为结构化
  AgentFinding，调用方永远拿到完整的 PlanExecution；
- 任一 Agent 失败不阻塞其余 Agent；
- 全部 Agent 失败时 usable_findings 为空，调用方据此安全退回 1.0 单链路
  ——最坏情况不会比现在差。
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import (
    AgentFinding, DEFAULT_AGENT_TIMEOUT,
    STATUS_FAILED, STATUS_OK, STATUS_SKIPPED, STATUS_TIMEOUT,
    run_with_guard,
)
from .planner import DiagnosisPlan, get_registered_agent

# 整体预算（秒）：所有并行 Agent 必须在此时间内收齐结果
DEFAULT_TOTAL_BUDGET = 40

# 线程池上限（防御：计划再大也不会无限开线程）
_MAX_WORKERS = 6


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class PlanExecution:
    """一次诊断计划的执行结果——前端"诊断过程"时间线的第二屏。

    - findings: agent_name → AgentFinding（含派出的与计划内跳过的，全量留痕）
    - elapsed_ms: 并行执行总耗时（毫秒）
    - budget_exceeded: 是否触发整体预算护栏
    """
    plan: DiagnosisPlan
    findings: Dict[str, AgentFinding] = field(default_factory=dict)
    elapsed_ms: int = 0
    budget_exceeded: bool = False
    executed_at: str = field(default_factory=_now_iso)

    @property
    def usable_findings(self) -> List[AgentFinding]:
        """可进证据链的产物（仅 status=ok）。"""
        return [f for f in self.findings.values() if f.usable]

    @property
    def degraded_findings(self) -> List[AgentFinding]:
        """发生降级的条目（failed/timeout），前端明示。"""
        return [f for f in self.findings.values()
                if f.status in (STATUS_FAILED, STATUS_TIMEOUT)]

    @property
    def all_degraded(self) -> bool:
        """派出的 Agent 是否全军覆没（调用方据此退回 1.0 单链路）。"""
        dispatched = [f for f in self.findings.values()
                      if f.status != STATUS_SKIPPED]
        return bool(dispatched) and all(not f.usable for f in dispatched)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "findings": {name: f.to_dict() for name, f in self.findings.items()},
            "elapsed_ms": self.elapsed_ms,
            "budget_exceeded": self.budget_exceeded,
            "executed_at": self.executed_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def execute_plan(plan: DiagnosisPlan,
                 alert: Dict[str, Any],
                 context: Optional[Dict[str, Any]] = None,
                 agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
                 total_budget: float = DEFAULT_TOTAL_BUDGET) -> PlanExecution:
    """并行执行诊断计划。永不抛出。

    - plan: Planner 产出的 DiagnosisPlan
    - alert / context: 透传给各 Agent（与 run_with_guard 口径一致）
    - agent_timeout: 单 Agent 超时（秒），由 run_with_guard 执行
    - total_budget: 整体预算（秒），预算耗尽后未完成者降级为 timeout
    """
    context = context or {}
    execution = PlanExecution(plan=plan)
    start = time.time()

    # ① 计划内跳过的条目：原样留痕（前端明示"为什么没派"）
    for entry in plan.skipped:
        execution.findings[entry.name] = AgentFinding(
            agent_name=entry.name, status=STATUS_SKIPPED,
            summary="已跳过", degrade_reason=entry.reason,
        )

    if not plan.agents:
        execution.elapsed_ms = int((time.time() - start) * 1000)
        return execution

    # ② 解析 Agent 实例（计划与执行之间注册表可能变化，防御处理）
    runnable = []   # [(entry, agent_instance)]
    for entry in plan.agents:
        agent = get_registered_agent(entry.name)
        if agent is None:
            execution.findings[entry.name] = AgentFinding(
                agent_name=entry.name, status=STATUS_FAILED,
                summary="执行失败",
                error="计划派出后 Agent 已从注册表移除",
                degrade_reason="Agent 不可用，已降级",
            )
        else:
            runnable.append((entry, agent))

    if not runnable:
        execution.elapsed_ms = int((time.time() - start) * 1000)
        return execution

    # ③ 并行执行 + 整体预算护栏
    deadline = start + max(total_budget, 0.001)
    pool = ThreadPoolExecutor(
        max_workers=min(len(runnable), _MAX_WORKERS),
        thread_name_prefix="diag-agent",
    )
    try:
        futures = {}
        for entry, agent in runnable:
            fut = pool.submit(run_with_guard, agent, alert, context, agent_timeout)
            futures[entry.name] = fut

        for name, fut in futures.items():
            remaining = deadline - time.time()
            if remaining <= 0:
                # 预算已耗尽：不再等待，直接降级
                if fut.done():
                    execution.findings[name] = _safe_result(fut, name)
                else:
                    fut.cancel()
                    execution.budget_exceeded = True
                    execution.findings[name] = _budget_timeout(name, total_budget)
                continue
            try:
                execution.findings[name] = _validate(fut.result(timeout=remaining), name)
            except FutureTimeout:
                fut.cancel()
                execution.budget_exceeded = True
                execution.findings[name] = _budget_timeout(name, total_budget)
            except Exception as exc:  # noqa: BLE001 —— 兜底，理论上 run_with_guard 不抛
                execution.findings[name] = AgentFinding(
                    agent_name=name, status=STATUS_FAILED,
                    summary="执行失败", error="%s: %s" % (type(exc).__name__, exc),
                    degrade_reason="执行器兜底降级",
                )
    finally:
        # 不等待未完成线程（其内部子进程调用自带 timeout，不会泄漏长任务）
        pool.shutdown(wait=False)

    execution.elapsed_ms = int((time.time() - start) * 1000)
    return execution


def _budget_timeout(name: str, total_budget: float) -> AgentFinding:
    return AgentFinding(
        agent_name=name, status=STATUS_TIMEOUT,
        summary="执行超时",
        error="整体预算 %.0fs 耗尽时仍未完成" % total_budget,
        degrade_reason="整体预算护栏触发，已降级",
    )


def _safe_result(fut, name: str) -> AgentFinding:
    """future 已 done 时取结果的防御封装。"""
    try:
        return _validate(fut.result(timeout=0), name)
    except Exception as exc:  # noqa: BLE001
        return AgentFinding(
            agent_name=name, status=STATUS_FAILED,
            summary="执行失败", error="%s: %s" % (type(exc).__name__, exc),
            degrade_reason="执行器兜底降级",
        )


def _validate(result: Any, name: str) -> AgentFinding:
    """run_with_guard 理应返回 AgentFinding；再校验一层，保持永不抛出。"""
    if isinstance(result, AgentFinding):
        return result
    return AgentFinding(
        agent_name=name, status=STATUS_FAILED,
        summary="执行失败",
        error="执行结果非 AgentFinding: %r" % type(result).__name__,
        degrade_reason="执行器返回值校验失败，已降级",
    )
