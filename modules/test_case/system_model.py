#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SystemModel 抽取：从 PRD 文本中提取状态 / 事件 / 规则等结构化信息。
V1 通过一次 LLM 调用返回 JSON，再做最基本的校验与兜底。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any

from utils.llm_client import call_llm

logger = logging.getLogger(__name__)

PRD_STRUCTURE_SCHEMA = {
    "background": "【PRD未说明】",
    "goal": "【PRD未说明】",
    "modules": ["【PRD未说明】"],
    "user_roles": ["【PRD未说明】"],
    "flows": ["【PRD未说明】"],
    "states": ["【PRD未说明】"],
    "business_rules": ["【PRD未说明】"],
    "data_structures": ["【PRD未说明】"],
    "permissions": ["【PRD未说明】"],
    "exceptions": ["【PRD未说明】"],
    "edge_cases": ["【PRD未说明】"],
    "dependencies": ["【PRD未说明】"],
    "non_functional_requirements": ["【PRD未说明】"],
}


@dataclass
class Transition:
    source: str
    event: str
    target: str


@dataclass
class SystemModel:
    """用于规则引擎的简化系统模型"""
    states: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    data_rules: List[str] = field(default_factory=list)
    data_structures: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    edge_cases: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    transitions: List[Transition] = field(default_factory=list)
    high_priority_events: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SystemModel":
        states = [s for s in (data.get("states") or []) if isinstance(s, str) and s.strip()]
        events = [e for e in (data.get("events") or []) if isinstance(e, str) and e.strip()]
        rules = [r for r in (data.get("rules") or []) if isinstance(r, str) and r.strip()]
        data_rules = [r for r in (data.get("data_rules") or []) if isinstance(r, str) and r.strip()]
        data_structures = [r for r in (data.get("data_structures") or []) if isinstance(r, str) and r.strip()]
        permissions = [r for r in (data.get("permissions") or []) if isinstance(r, str) and r.strip()]
        edge_cases = [r for r in (data.get("edge_cases") or []) if isinstance(r, str) and r.strip()]
        dependencies = [r for r in (data.get("dependencies") or []) if isinstance(r, str) and r.strip()]
        hp_events = [e for e in (data.get("high_priority_events") or []) if isinstance(e, str) and e.strip()]

        transitions: List[Transition] = []
        for t in data.get("transitions") or []:
            if not isinstance(t, dict):
                continue
            src = (t.get("from") or t.get("source") or "").strip()
            ev = (t.get("event") or "").strip()
            tgt = (t.get("to") or t.get("target") or "").strip()
            if src and ev:
                transitions.append(Transition(source=src, event=ev, target=tgt or ""))

        return cls(
            states=states,
            events=events,
            rules=rules,
            data_rules=data_rules,
            data_structures=data_structures,
            permissions=permissions,
            edge_cases=edge_cases,
            dependencies=dependencies,
            transitions=transitions,
            high_priority_events=hp_events,
        )


SYSTEM_MODEL_PROMPT = """
你是一名资深系统分析师，请将以下 PRD 文本解析成系统模型 JSON。

请只输出 JSON，不要任何解释或多余文字。

约束：
- 严禁臆测 PRD 未说明内容。
- 若某类信息在 PRD 中未出现，请在该字段数组中返回 ["【PRD未说明】"]（不要返回空数组）。

JSON 字段要求：
- states: 所有系统状态的数组，如 ["投屏","游戏","广告"]
- events: 所有关键触发事件的数组，如 ["用户点击投屏","广告推送"]
- rules: 与状态/事件相关的业务规则数组（自然语言即可）
- data_rules: 与数据校验、边界值、异常等相关的规则数组
- data_structures: 输入输出数据结构/字段定义/接口字段等（自然语言即可）
- permissions: 权限控制规则（角色/权限边界/谁可以做什么）
- edge_cases: 边界条件/极端条件/异常场景（如弱网/断网/超时/重复提交/并发）
- dependencies: 外部依赖系统/接口/第三方服务（如短信、支付、风控、账号体系等）
- transitions: 状态切换规则数组，每个元素形如:
  { "from": "广告", "event": "用户点击投屏", "to": "投屏" }
- high_priority_events: 需要重点关注的高优先级事件数组（如中断/打断/优先级相关）

示例（仅供参考，实际请根据 PRD 内容填充）：
{
  "states": ["投屏","游戏","广告"],
  "events": ["用户点击投屏","用户点击游戏","广告推送"],
  "rules": ["投屏优先级最高"],
  "data_rules": ["播放失败需要有错误码"],
  "data_structures": ["订单对象包含字段：id、amount、status（PRD未说明则标注）"],
  "permissions": ["仅管理员可删除订单；普通用户仅可取消自己的订单"],
  "edge_cases": ["弱网/断网、超时重试、重复点击/重复提交、并发下单/库存竞争"],
  "dependencies": ["支付系统、短信服务、用户中心"],
  "transitions": [
    {"from":"广告","event":"用户点击投屏","to":"投屏"}
  ],
  "high_priority_events": ["用户点击投屏","用户点击游戏","广告推送"]
}

现在请基于上述格式返回 JSON。

PRD 内容：
{content}
"""

STAGE1_STRUCTURE_PROMPT = """
你是一名资深产品架构师与测试架构师。

你的任务是对 PRD 文档进行结构化解析，将需求转化为机器可分析的需求模型。

请识别并提取以下信息：
1. 需求背景
2. 需求目标
3. 功能模块
4. 用户角色
5. 核心业务流程
6. 状态机
7. 关键业务规则
8. 输入输出数据结构
9. 权限控制规则
10. 异常处理机制
11. 边界条件
12. 外部依赖系统
13. 非功能需求

要求：
1. 严禁臆测 PRD 未说明内容
2. 文档未描述的信息标记为【PRD未说明】
3. 信息尽量精确到模块或功能点
4. 只输出 JSON，不要输出解释文字
5. **提取列表内容时，严禁使用 a. b. i. ii. 等行内扁平化序号**。如果有多个要点，请使用中文分号（；）隔开，或者使用标准换行。

输出格式：
{
  "background": "",
  "goal": "",
  "modules": [],
  "user_roles": [],
  "flows": [],
  "states": [],
  "business_rules": [],
  "data_structures": [],
  "permissions": [],
  "exceptions": [],
  "edge_cases": [],
  "dependencies": [],
  "non_functional_requirements": []
}

PRD 文档：
{content}
"""


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _normalize_stage1_output(data: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, default_value in PRD_STRUCTURE_SCHEMA.items():
        value = data.get(key)
        if isinstance(default_value, list):
            if isinstance(value, list):
                cleaned = [str(x).strip() for x in value if str(x).strip()]
                out[key] = cleaned if cleaned else list(default_value)
            elif isinstance(value, str) and value.strip():
                out[key] = [value.strip()]
            else:
                out[key] = list(default_value)
        else:
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
            else:
                out[key] = default_value
    return out


def extract_prd_structure(prd_text: str, llm_config_path: str, timeout: int = 90) -> Dict[str, Any]:
    prompt = STAGE1_STRUCTURE_PROMPT.replace("{content}", prd_text or "")
    try:
        resp = call_llm(
            [{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            stream=False,
            timeout=timeout,
        )
        data = _extract_json_object(resp)
        return _normalize_stage1_output(data)
    except Exception as e:
        logger.warning("extract_prd_structure failed: %s", e)
        return _normalize_stage1_output({})


def extract_system_model(prd_text: str, llm_config_path: str, timeout: int = 90) -> SystemModel:
    """
    调用 LLM 抽取 SystemModel，解析失败时返回空模型但不中断整体流程。
    """
    try:
        prompt = SYSTEM_MODEL_PROMPT.replace("{content}", prd_text or "")
        resp = call_llm(
            [{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            stream=False,
            timeout=timeout,
        )
        # 尝试提取 JSON 子串
        text = resp.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        data = json.loads(text)
        model = SystemModel.from_dict(data or {})
        logger.info("SystemModel extracted: %s states, %s events, %s transitions",
                    len(model.states), len(model.events), len(model.transitions))
        return model
    except Exception as e:
        logger.warning("extract_system_model failed: %s", e)
        return SystemModel()

