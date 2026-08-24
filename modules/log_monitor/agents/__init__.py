# -*- coding: utf-8 -*-
"""Agent 2.0 —— 多 Agent 诊断编排包。

红线（继承 1.0，不可放宽）：
- 所有专职 Agent 只读：不修改工程代码、不写设备状态、不自动关单；
- 任一 Agent 失败/超时 → 降级，不阻塞整体诊断；
- 编排计划规则表驱动，可审计可解释。
"""
from .base import AgentFinding, BaseAgent, run_with_guard  # noqa: F401
from .planner import (  # noqa: F401
    DiagnosisPlan, PlannedAgent, build_plan,
    register_agent, unregister_agent, get_registered_agent,
    registered_agent_names,
)
from .adapters import (  # noqa: F401
    LogAnalysisAdapter, DeviceProbeAdapter, HistoryAdapter,
    register_builtin_agents,
)
from .executor import (  # noqa: F401
    PlanExecution, execute_plan, DEFAULT_TOTAL_BUDGET,
)
from .source_index import (  # noqa: F401
    SourceCodeIndex, get_index, reset_index,
)
from .source_agent import (  # noqa: F401
    SourceCodeAdapter, parse_stack_locations,
)
from .synthesizer import synthesize, AGENT_V2_ENABLED  # noqa: F401
