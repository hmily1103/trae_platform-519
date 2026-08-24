# -*- coding: utf-8 -*-
"""Agent 2.0 Planner 编排器（A2）。

职责：根据告警类型，按**规则表**（可审计、非 LLM）编排本次诊断要派出的
专职 Agent，并产出一份可展示的"诊断计划"（DiagnosisPlan）——前端能看到
"本次派了哪几个 Agent、为什么派 / 为什么没派"。

设计要点：
1. 规则表驱动：PLAN_RULES 静态定义 类型→Agent清单+理由，改动需过代码评审，
   LLM 不参与编排决策；
2. 与实现解耦：计划只引用 Agent 名称；真正的 Agent 实例由注册表
   （register_agent）提供，A3 完成适配器后注册进来；
3. 只读红线前置：注册时即校验 readonly=True，违规 Agent 根本进不了注册表
   （run_with_guard 的执行期校验是第二道防线）；
4. 未注册的 Agent（如阶段二的 source_code 未启用）不会派出，
   而是进入 plan.skipped 并附原因，前端明示"源码关联未启用"。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .base import BaseAgent

# ---- 专职 Agent 名称常量（与 A3 适配器的 name 一一对应）----
AGENT_LOG = "log_analysis"       # 日志分析（LLM 根因，封装 agent.py）
AGENT_PROBE = "device_probe"     # 设备取证（只读探针/动作，封装 action_executor.py）
AGENT_HISTORY = "history"        # 历史求证（知识库检索，封装 knowledge_base.py）
AGENT_SOURCE = "source_code"     # 源码只读关联（阶段二 B1~B3，可选启用）

_DISPLAY_NAMES = {
    AGENT_LOG: "日志分析",
    AGENT_PROBE: "设备取证",
    AGENT_HISTORY: "历史求证",
    AGENT_SOURCE: "源码关联",
}

# ---- 编排规则表（可审计核心）----
# 每条：告警类型 → [(agent_name, 派出理由), ...]
# 口径与 alert_engine.py 的 7 种规则类型一一对应；未列出的类型走 _DEFAULT_PLAN。
PLAN_RULES: Dict[str, List[tuple]] = {
    "crash": [
        (AGENT_LOG, "崩溃需 LLM 分析堆栈与根因"),
        (AGENT_PROBE, "崩溃现场需截图/只读探针取证（越早越真实）"),
        (AGENT_HISTORY, "崩溃类问题历史复发率高，检索已解决案例"),
        (AGENT_SOURCE, "有堆栈定位时关联源码片段佐证根因"),
    ],
    "exception": [
        (AGENT_LOG, "异常堆栈需 LLM 分析根因"),
        (AGENT_PROBE, "采集进程/内存快照辅助定位"),
        (AGENT_HISTORY, "检索同类异常的已解决案例"),
        (AGENT_SOURCE, "有堆栈定位时关联源码片段佐证根因"),
    ],
    "anr": [
        (AGENT_LOG, "ANR 需分析主线程阻塞上下文"),
        (AGENT_PROBE, "采集 ANR 现场（activity/进程状态）"),
        (AGENT_HISTORY, "检索历史 ANR 案例"),
    ],
    "oom": [  # 预留：部分环境将 OOM 独立成类型
        (AGENT_LOG, "OOM 需分析内存增长轨迹"),
        (AGENT_PROBE, "采集 meminfo 佐证内存状态"),
        (AGENT_HISTORY, "检索历史内存问题案例"),
    ],
    "frequency": [
        (AGENT_LOG, "高频告警需分析共性模式"),
        (AGENT_HISTORY, "检索是否为已知周期性问题"),
    ],
    "regex": [
        (AGENT_LOG, "自定义正则命中，需 LLM 判读语义"),
        (AGENT_HISTORY, "检索同规则历史案例"),
    ],
    "keyword": [
        (AGENT_LOG, "关键字命中，需 LLM 判读上下文语义"),
    ],
    "level": [
        (AGENT_LOG, "级别告警需判读错误密度与语义"),
    ],
}
# 未知类型的兜底计划（等价 1.0 单链路：仅日志分析）
_DEFAULT_PLAN: List[tuple] = [
    (AGENT_LOG, "未知告警类型，按默认口径仅做日志分析（等价 1.0 链路）"),
]

# 未注册 Agent 的跳过原因模板
_SKIP_REASONS = {
    AGENT_SOURCE: "源码关联未启用（未配置源码目录或阶段二未交付）",
}
_SKIP_REASON_DEFAULT = "该 Agent 未注册/未启用"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class PlannedAgent:
    """计划中的一个条目（派出 或 跳过 都留痕）。"""
    name: str
    display_name: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "display_name": self.display_name,
                "reason": self.reason}


@dataclass
class DiagnosisPlan:
    """一次告警的诊断计划——前端"诊断过程"时间线的第一屏。"""
    alert_id: str
    alert_type: str
    rule_name: str = ""
    agents: List[PlannedAgent] = field(default_factory=list)    # 实际派出
    skipped: List[PlannedAgent] = field(default_factory=list)   # 计划内但未启用
    created_at: str = field(default_factory=_now_iso)
    note: str = ""

    @property
    def agent_names(self) -> List[str]:
        return [a.name for a in self.agents]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alert_type": self.alert_type,
            "rule_name": self.rule_name,
            "agents": [a.to_dict() for a in self.agents],
            "skipped": [a.to_dict() for a in self.skipped],
            "created_at": self.created_at,
            "note": self.note,
        }


# ---- Agent 注册表 ----
_REGISTRY: Dict[str, BaseAgent] = {}


def register_agent(agent: BaseAgent) -> None:
    """注册专职 Agent。只读红线前置校验：readonly=False 直接拒绝入表。"""
    if not isinstance(agent, BaseAgent):
        raise TypeError("只能注册 BaseAgent 子类实例，收到: %r" % type(agent).__name__)
    if not agent.readonly:
        raise ValueError(
            "Agent %r 声明 readonly=False，违反只读红线，拒绝注册" % agent.name)
    _REGISTRY[agent.name] = agent


def unregister_agent(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_registered_agent(name: str) -> Optional[BaseAgent]:
    return _REGISTRY.get(name)


def registered_agent_names() -> List[str]:
    return sorted(_REGISTRY.keys())


def clear_registry() -> None:
    """仅供测试使用。"""
    _REGISTRY.clear()


# ---- 编排入口 ----
def build_plan(alert: Dict[str, Any],
               context: Optional[Dict[str, Any]] = None) -> DiagnosisPlan:
    """按规则表为一条告警生成诊断计划。

    - alert: 告警字典（需含 type；id/rule_name 可选）
    - context: 诊断上下文（当前编排不依赖，仅保留扩展位——
      例如未来按 context 中"设备离线"跳过取证）
    - 返回 DiagnosisPlan：agents=实际派出（已注册），skipped=计划内但未注册。

    保证：即使注册表为空，也返回结构完整的计划（全部进 skipped），
    调用方据此可安全降级回 1.0 单链路。
    """
    context = context or {}
    alert_type = str(alert.get("type") or "").lower().strip()
    entries = PLAN_RULES.get(alert_type)
    note = ""
    if entries is None:
        entries = _DEFAULT_PLAN
        note = "告警类型 %r 未在规则表中，使用默认计划" % (alert_type or "<空>")

    plan = DiagnosisPlan(
        alert_id=str(alert.get("id", "")),
        alert_type=alert_type,
        rule_name=str(alert.get("rule_name", "")),
        note=note,
    )
    for name, reason in entries:
        display = _DISPLAY_NAMES.get(name, name)
        agent = _REGISTRY.get(name)
        if agent is not None:
            plan.agents.append(PlannedAgent(name, display, reason))
        else:
            skip_reason = _SKIP_REASONS.get(name, _SKIP_REASON_DEFAULT)
            plan.skipped.append(PlannedAgent(name, display, skip_reason))
    return plan
