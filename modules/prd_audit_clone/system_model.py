#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SystemModel 抽取：从 PRD 文本中提取状态 / 事件 / 规则等结构化信息。
V1 通过一次 LLM 调用返回 JSON，再做最基本的校验与兜底。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any

from utils.llm_client import call_llm

logger = logging.getLogger(__name__)


def _extract_first_json_object(text: str) -> Dict[str, Any]:
    """
    从 LLM 返回文本中抽取第一个完整 JSON 对象，兼容 DeepSeek 等返回 ```json ... ``` 或多段 JSON 的情况。
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    # 1) 去掉 markdown 代码块，优先取 ```json ... ``` 内容
    for pattern in (r"```json\s*([\s\S]*?)\s*```", r"```\s*([\s\S]*?)\s*```"):
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            break
    # 2) 找第一个 {，再用括号匹配取到对应的 }
    start = raw.find("{")
    if start == -1:
        return {}
    depth = 0
    in_string = None
    escape = False
    i = start
    while i < len(raw):
        c = raw[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == "\\" and in_string:
            escape = True
            i += 1
            continue
        if in_string:
            if c == in_string:
                in_string = None
            i += 1
            continue
        if c in ('"', "'"):
            in_string = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    break
        i += 1
    # 3) 兜底：整段从第一个 { 到最后一个 }
    end = raw.rfind("}")
    if end != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            pass
    return {}

PRD_STRUCTURE_SCHEMA = {
    # 顶层信息
    "product_name": "【PRD未说明】",
    "background": "【PRD未说明】",
    "goal": "【PRD未说明】",
    # 能力视角
    "modules": ["【PRD未说明】"],
    "features": ["【PRD未说明】"],
    # 角色与流程视角
    "user_roles": ["【PRD未说明】"],
    "flows": ["【PRD未说明】"],
    "states": ["【PRD未说明】"],
    "business_rules": ["【PRD未说明】"],
    # 数据与权限视角
    "data_structures": ["【PRD未说明】"],
    "permissions": ["【PRD未说明】"],
    # 异常与边界视角
    "exceptions": ["【PRD未说明】"],
    "edge_cases": ["【PRD未说明】"],
    # 依赖与非功视角
    "dependencies": ["【PRD未说明】"],
    "non_functional_requirements": ["【PRD未说明】"],
    # 成功指标视角
    "success_metrics": ["【PRD未说明】"],
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
1. 产品名称 / 产品对象
2. 需求背景
3. 需求目标
4. 功能模块
5. 关键功能/特性列表（features）
6. 用户角色
7. 核心业务流程
8. 状态机（可文字描述）
9. 关键业务规则
10. 输入输出数据结构
11. 权限控制规则
12. 异常处理机制
13. 边界条件
14. 外部依赖系统
15. 非功能需求（性能/安全/可用性等）
16. 成功指标或验收指标（success metrics）

要求：
1. 严禁臆测 PRD 未说明内容
2. 文档未描述的信息标记为【PRD未说明】
3. 信息尽量精确到模块或功能点
4. 只输出 JSON，不要输出解释文字
5. **必须输出 source_map**：用于锚点定位。source_map 的每个字段是一个字符串数组，长度与对应字段一致，
   每项为原文行号范围，如 "L12-L18"；若无法定位则填 "【PRD未说明】"。
6. **提取列表内容时，严禁使用 a. b. i. ii. 等行内扁平化序号**。如果有多个要点，请使用中文分号（；）隔开，或者使用标准换行。同时请确保输出的句子**通顺、连贯、符合中文阅读习惯**，不要输出生硬的单词组合或残句。

输出格式：
{
  "product_name": "",
  "background": "",
  "goal": "",
  "modules": [],
  "features": [],
  "user_roles": [],
  "flows": [],
  "states": [],
  "business_rules": [],
  "data_structures": [],
  "permissions": [],
  "exceptions": [],
  "edge_cases": [],
  "dependencies": [],
  "non_functional_requirements": [],
  "success_metrics": [],
  "source_map": {
    "product_name": [],
    "modules": [],
    "features": [],
    "user_roles": [],
    "flows": [],
    "states": [],
    "business_rules": [],
    "data_structures": [],
    "permissions": [],
    "exceptions": [],
    "edge_cases": [],
    "dependencies": [],
    "non_functional_requirements": [],
    "success_metrics": []
  }
}

PRD 文档：
{content}
"""


def _extract_json_object(text: str) -> Dict[str, Any]:
    data = _extract_first_json_object(text)
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
    # 可选：保留 source_map（用于锚点更精确）
    sm = data.get("source_map")
    if isinstance(sm, dict):
        normalized = {}
        for k in PRD_STRUCTURE_SCHEMA.keys():
            if isinstance(PRD_STRUCTURE_SCHEMA[k], list):
                arr = sm.get(k)
                if isinstance(arr, list):
                    cleaned = [str(x).strip() for x in arr if str(x).strip()]
                elif isinstance(arr, str) and arr.strip():
                    cleaned = [arr.strip()]
                else:
                    cleaned = []
                # 尽量对齐长度；对不齐则按已知项填充，其余补【PRD未说明】
                target_len = len(out.get(k) or [])
                if target_len <= 0:
                    target_len = len(cleaned)
                if target_len > 0:
                    padded = (cleaned + ["【PRD未说明】"] * target_len)[:target_len]
                else:
                    padded = cleaned or ["【PRD未说明】"]
                normalized[k] = padded
        out["source_map"] = normalized
    return out


def _number_prd_lines(text: str, max_lines: int = 400, max_chars: int = 24000) -> str:
    """
    将 PRD 原文转为带行号的文本，提升 LLM 锚点定位准确度。
    - 限制行数与总字符，避免 prompt 过长
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    numbered = []
    total = 0
    for i, line in enumerate(lines[:max_lines], start=1):
        s = line.rstrip()
        numbered_line = f"L{i:04d} {s}"
        numbered.append(numbered_line)
        total += len(numbered_line) + 1
        if total >= max_chars:
            break
    return "\n".join(numbered).strip()


def extract_prd_structure(prd_text: str, llm_config_path: str, timeout: int = 90, llm_config_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # Phase1：先做 rule-first 的离线解析，作为“基础结构”与质量提示（不依赖 LLM）
    base_stage1 = None
    try:
        from .prd_parse_engine import sectionize, build_stage1_base
        sec = sectionize(prd_text or "")
        base_stage1 = build_stage1_base(sec)
    except Exception as e:
        logger.warning("rule-first parse failed (non-blocking): %s", e)

    numbered = _number_prd_lines(prd_text or "")
    prompt = STAGE1_STRUCTURE_PROMPT.replace("{content}", numbered or (prd_text or ""))
    try:
        resp = call_llm(
            [{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            stream=False,
            timeout=timeout,
        )
        data = _extract_json_object(resp)
        llm_out = _normalize_stage1_output(data)
        # 合并：LLM 优先，但保留 rule-first 的 blocks/quality/required/conflicts 作为增量字段；
        # 若 LLM 在某字段为【PRD未说明】，用 rule-first 的值补齐。
        if base_stage1 and isinstance(base_stage1, dict):
            base_s1 = base_stage1.get("stage1") if isinstance(base_stage1.get("stage1"), dict) else {}
            for k, v in (base_s1 or {}).items():
                if k not in llm_out:
                    llm_out[k] = v
                    continue
                # 仅对数组字段做“缺省补齐”
                if isinstance(llm_out.get(k), list) and isinstance(v, list):
                    if not llm_out[k] or all(x == "【PRD未说明】" for x in llm_out[k]):
                        llm_out[k] = v
                if isinstance(llm_out.get(k), str) and isinstance(v, str):
                    if (llm_out.get(k) or "").strip() in {"", "【PRD未说明】"} and v.strip():
                        llm_out[k] = v
            # 增量字段
            llm_out["blocks"] = base_stage1.get("blocks") or []
            llm_out["parse_quality"] = base_stage1.get("parse_quality") or {}
            llm_out["required_elements"] = base_stage1.get("required_elements") or {}
            llm_out["conflict_candidates"] = base_stage1.get("conflict_candidates") or []
        return llm_out
    except Exception as e:
        logger.warning("extract_prd_structure failed: %s", e)
        # 无 LLM：返回 rule-first 的基础结构（若也失败则回退空 schema）
        if base_stage1 and isinstance(base_stage1, dict) and isinstance(base_stage1.get("stage1"), dict):
            out = _normalize_stage1_output(base_stage1.get("stage1") or {})
            out["blocks"] = base_stage1.get("blocks") or []
            out["parse_quality"] = base_stage1.get("parse_quality") or {}
            out["required_elements"] = base_stage1.get("required_elements") or {}
            out["conflict_candidates"] = base_stage1.get("conflict_candidates") or []
            return out
        return _normalize_stage1_output({})


def extract_system_model(prd_text: str, llm_config_path: str, timeout: int = 90, llm_config_override: Optional[Dict[str, Any]] = None) -> SystemModel:
    """
    调用 LLM 抽取 SystemModel，解析失败时返回空模型但不中断整体流程。
    """
    try:
        prompt = SYSTEM_MODEL_PROMPT.replace("{content}", prd_text or "")
        resp = call_llm(
            [{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            stream=False,
            timeout=timeout,
        )
        data = _extract_first_json_object(resp)
        model = SystemModel.from_dict(data or {})
        logger.info("SystemModel extracted: %s states, %s events, %s transitions",
                    len(model.states), len(model.events), len(model.transitions))
        return model
    except Exception as e:
        logger.warning("extract_system_model failed: %s", e)
        return SystemModel()
