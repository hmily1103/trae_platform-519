# -*- coding: utf-8 -*-
"""
Stage5：系统图生成器
根据 Stage1 结构解析生成：状态机图、权限矩阵说明、并发冲突说明
输出为 Mermaid 或 Markdown，供前端渲染或直接放入报告。
纯增量模块，不修改现有 Stage1/2/3。
"""

from typing import Dict, Any, List
import re
import logging

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _safe_label(s: str, max_len: int = 20) -> str:
    """用于 Mermaid 节点标签，去掉不合法字符。"""
    if not s:
        return "N"
    s = re.sub(r"[\s\[\]()（）【】]+", "_", s)
    s = s[:max_len] if len(s) > max_len else s
    return s or "N"


def evaluate_diagrams(diagrams: Dict[str, Any], stage1_output: Dict[str, Any]) -> Dict[str, Any]:
    """
    对 Stage5 产物做“可用性评分”(0-10) 与明细。
    目标：用于产品化展示，不作为阻断条件。
    """
    dg = diagrams or {}
    stage1 = stage1_output or {}

    state_diagram = str(dg.get("state_diagram") or "")
    permission_matrix = str(dg.get("permission_matrix") or "")
    concurrency_diagram = str(dg.get("concurrency_diagram") or "")

    roles = _as_list(stage1.get("user_roles"))
    roles = [r for r in roles if r != "【PRD未说明】"]
    states = _as_list(stage1.get("states"))
    states = [s for s in states if s != "【PRD未说明】"]

    # state diagram quality (0-4)
    if states:
        s_state = 4.0 if "```mermaid" in state_diagram and "stateDiagram" in state_diagram else 1.5
    else:
        s_state = 4.0 if ("未定义" in state_diagram or "补充" in state_diagram) else 2.0

    # permission matrix quality (0-3)
    if roles:
        s_perm = 3.0 if ("|" in permission_matrix and "角色" in permission_matrix) else 1.0
    else:
        s_perm = 3.0 if ("未定义" in permission_matrix or "补充" in permission_matrix) else 2.0

    # concurrency diagram quality (0-3)
    # 有 mermaid graph TD 视为高质量；否则只要有明确提示也给基础分
    if "```mermaid" in concurrency_diagram and ("graph TD" in concurrency_diagram or "graph LR" in concurrency_diagram):
        s_conc = 3.0
    elif ("不足" in concurrency_diagram or "补充" in concurrency_diagram or "无" in concurrency_diagram):
        s_conc = 2.0
    else:
        s_conc = 1.0 if concurrency_diagram.strip() else 0.0

    overall = round(min(10.0, s_state + s_perm + s_conc), 1)
    details = {
        "state_diagram": round(s_state, 1),
        "permission_matrix": round(s_perm, 1),
        "concurrency_diagram": round(s_conc, 1),
        "stats": {"roles_total": len(roles), "states_total": len(states)},
        "notes": [],
    }
    if roles and ("待确认" in permission_matrix) and ("权限" in permission_matrix):
        details["notes"].append("权限矩阵仍为“待确认”占位，建议后续抽取关键操作与边界。")
    return {"overall": overall, "details": details}


class DiagramGenerator:
    """从 Stage1 生成状态图、权限矩阵、并发冲突说明。"""

    def __init__(self, stage1_output: Dict[str, Any]):
        self.stage1 = stage1_output or {}

    def generate_all(self) -> Dict[str, str]:
        """生成全部图表文本，任一步失败则该项返回说明文案，不抛错。"""
        try:
            return {
                "state_diagram": self.generate_state_diagram(),
                "permission_matrix": self.generate_permission_matrix(),
                "concurrency_diagram": self.generate_concurrency_diagram(),
            }
        except Exception as e:
            logger.warning("DiagramGenerator.generate_all failed: %s", e)
            return {
                "state_diagram": "系统图生成异常，请查看结构解析与流程。",
                "permission_matrix": "权限矩阵生成异常。",
                "concurrency_diagram": "并发图生成异常。",
            }

    def generate_state_diagram(self) -> str:
        """状态机图（Mermaid stateDiagram-v2）。无 transitions 时按 states 顺序连线。"""
        states = _as_list(self.stage1.get("states"))
        states = [s for s in states if s != "【PRD未说明】"]
        if not states:
            return "状态机未定义，请在 PRD 中补充 states 或流程中的状态说明。"

        lines = ["```mermaid", "stateDiagram-v2"]
        labels = [_safe_label(s) for s in states]
        lines.append(f"    [*] --> {labels[0]}")
        for i in range(len(labels) - 1):
            lines.append(f"    {labels[i]} --> {labels[i + 1]}")
        lines.append(f"    {labels[-1]} --> [*]")
        lines.append("```")
        return "\n".join(lines)

    def generate_permission_matrix(self) -> str:
        """权限矩阵（Markdown 表格）。"""
        roles = _as_list(self.stage1.get("user_roles"))
        modules = _as_list(self.stage1.get("modules"))
        roles = [r for r in roles if r != "【PRD未说明】"]
        modules = [m for m in modules if m != "【PRD未说明】"]

        if not roles:
            return "用户角色未定义，请在 PRD 中补充 user_roles，以便生成权限矩阵。"

        head = "| 角色 | " + " | ".join(modules[:6]) + " |"
        sep = "|" + "|".join([" --- " for _ in range(min(6, len(modules)) + 1)]) + "|"
        rows = [head, sep]
        for role in roles[:8]:
            row = f"| {role} | " + " | ".join(["待确认" for _ in modules[:6]]) + " |"
            rows.append(row)
        return "### 权限矩阵\n\n" + "\n".join(rows)

    def generate_concurrency_diagram(self) -> str:
        """并发冲突说明（Mermaid graph 或文字）。"""
        rules = _as_list(self.stage1.get("business_rules"))
        flows = _as_list(self.stage1.get("flows"))
        events = []
        for r in rules:
            if "优先" in r or "打断" in r or "同时" in r:
                for w in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,}", r):
                    if w in ("投屏", "游戏", "广告", "数字人", "播放", "推送", "点击"):
                        events.append(w)
        for f in flows:
            if f != "【PRD未说明】":
                part = f.split("：")[0] if "：" in f else f[:10]
                if part and part not in events:
                    events.append(part)
        events = list(dict.fromkeys(events))[:5]

        if len(events) < 2:
            return "无高优先级并发场景或事件不足，请在 PRD 中补充业务规则与流程。"

        lines = ["```mermaid", "graph TD"]
        for i, e in enumerate(events):
            n = _safe_label(e, 12)
            lines.append(f"    E{i}[{n}]")
        lines.append("    C[并发裁决]")
        for i in range(len(events)):
            lines.append(f"    E{i} --> C")
        lines.append("    C --> P1[优先级]")
        lines.append("    C --> P2[排队]")
        lines.append("```")
        return "\n".join(lines)
