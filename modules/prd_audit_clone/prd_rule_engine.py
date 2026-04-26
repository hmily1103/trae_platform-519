#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRD 漏洞检测规则引擎 v1

基于 SystemModel（states / events / rules / transitions / high_priority_events），
实现基础规则：
- 规则冲突（简单关键词冲突）
- 状态机缺失（States × Events 未定义）
- 打断/恢复缺失
- 优先级闭环（简单环检测）
- 并发风险（Events × Events 未定义）
- 异常/边界条件缺失（关键词缺失）
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple, Optional
import threading

from .system_model import SystemModel, Transition, _extract_first_json_object
from utils.llm_client import call_llm, call_llm_with_retry

logger = logging.getLogger(__name__)
RULE_LIBRARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prd_scan_rules.json")
RULE_LIBRARY_V2_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prd_scan_rules_v2.json")

# 性能优化：Stage2 每次扫描都会读规则库文件，改成按 mtime 失效的内存缓存
_RULE_LIBRARY_CACHE_LOCK = threading.Lock()
_RULE_LIBRARY_CACHE: Dict[str, Any] = {"key": None, "rules": []}

STAGE2_DEFECT_SCAN_PROMPT = """
你是一名拥有10年经验的测试架构师和需求评审专家。

你的任务是基于结构化 PRD 模型进行需求漏洞扫描。你不是在“分类填表”，而是在做真正的业务评审。

检测维度包括：
1 逻辑矛盾
2 功能缺失
3 业务流程断裂
4 状态机不完整
5 权限设计漏洞
6 数据一致性风险
7 异常流程缺失
8 描述模糊与歧义
9 边界条件缺失
10 并发风险
11 安全风险
12 外部依赖风险
13 可测试性缺失：功能描述无量化验收标准、无明确预期结果、关键指标未定义
14 隐含假设挖掘：从需求描述中识别“产品经理觉得不言自明但实际未明文声明”的假设和边界条件

【最重要的业务化要求】
1. 每条 defect 必须挂在一个具体功能点上，优先从 modules / features / flows / states / actions 中选最贴近的一项。
2. 严禁使用“业务流程”“功能规则”“相关模块”“该功能”“该模块”“全局”这类框架词作为 module。
3. 每条 defect 的 anchor 必须引用 PRD 原文中的一句话、一个字段定义或一行关键规则；不要只写抽象概括。
4. description 必须说清楚“哪个真实功能名 + 哪个具体操作路径 + 漏了什么规则”，不能泛泛而谈。
5. reason 必须写成业务影响链路，解释“为什么这是问题”，不能只是重复 description 的缩写。
6. suggestion 必须写成“可以直接补进 PRD 的文字方向”，不能输出占位符、套话或空泛建议。

【严格禁止】
1. 禁止输出任何占位符，如【XX】、【待补充】、【某功能】、TBD、TODO、XX。
2. 禁止输出“建议补充相关规则”“体验不一致引发投诉”这类偷懒套话。
3. 禁止脱离 PRD 的真实功能名，只写抽象名词。
4. 禁止使用 markdown 代码块、解释性前言、总结性后语。

【第13维度-可测试性缺失】特别要求：
对于每个功能模块，识别以下类型的可测试性问题：
- 主观词汇：描述中使用"快"、"流畅"、"友好"、"及时"、"尽快"、"适当"等无法量化的词汇
- 性能指标缺失：涉及加载、响应、并发、容量等性能要求时，未提供具体数值指标
- 验收标准模糊：缺少"通过/失败"判定条件，无法建立明确的测试验收基线
- 操作耗时模糊：描述操作时长时使用"很快"、"片刻"、"短时间内"等模糊表述
- 外部依赖验证未定义：依赖第三方系统或外部条件的功能，未说明如何验证依赖可用性

【第14维度-隐含假设挖掘】特别要求：
对于每个功能模块，识别以下类型的隐含假设：
- 数值边界：阈值/上限/下限未明确（如"金额不能太大"中的"太大"是多大？）
- 单位未定义：重量/时间/长度等单位未声明
- 触发条件模糊："特殊情况"、"酌情处理"、"视情况"等未定义
- 数据格式：输入格式、输出格式、编码方式等未说明
- 分支逻辑缺失："否则"、"如果...否则"中的否则分支未说明
- 默认行为：未说明时的默认处理方式
- 适用范围：适用人群/场景/环境的边界未定义

【few-shot 参考示例】
示例1：
{
  "module": "投屏入口",
  "anchor": "L18: 用户点击“开始投屏”后，系统直接进入投屏态",
  "description": "投屏入口缺少失败分支定义，例如：用户点击“开始投屏”后如果设备鉴权失败，PRD 没写系统是停留在当前页、提示重试，还是回退到待连接态。",
  "risk_level": "P1",
  "reason": "开始投屏 -> 设备鉴权失败 -> 页面状态无定义 -> 用户重复点击或误判成功 -> 现场演示卡住并引发重复请求。",
  "suggestion": "在“开始投屏”流程后补充失败分支：当设备鉴权失败时，页面停留在投屏入口页，展示“设备未授权，请重试”的提示，并允许用户重新发起连接。"
}

示例2：
{
  "module": "红包金额输入框",
  "anchor": "L42: 用户输入红包金额后点击确认发送",
  "description": "红包金额输入缺少边界规则，例如：PRD 没写最小金额、最大金额、是否允许 0 或小数位数，开发和测试会各自猜口径。",
  "risk_level": "P2",
  "reason": "用户输入 0/负数/超大金额 -> 前后端校验口径不一致 -> 可能出现接口报错、金额展示异常或资金风险。",
  "suggestion": "在“红包金额输入”规则中补充：金额最小值、最大值、小数位数、是否允许 0、超界时的前端提示文案及后端错误码。"
}

只输出 JSON：
{
  "defects":[
    {
      "id":"",
      "type":"",
      "module":"",
      "anchor":"",
      "description":"",
      "risk_level":"P0/P1/P2",
      "reason":"",
      "suggestion":""
    }
  ],
  "hidden_assumptions":[
    {
      "id":"HA001",
      "module":"所属模块",
      "prd_statement":"原文描述",
      "hidden_assumption":"隐含的假设是什么",
      "question_to_ask":"应该向产品经理确认的问题",
      "impact":"如果没定义清楚会有什么风险",
      "suggestion":"建议的量化/明确化方式"
    }
  ]
}

规则：
1. 严禁臆测需求。
2. 若无法定位到真实模块和原文锚点，则不要输出该 defect。
3. 未说明内容可写“未说明”，但不要使用任何带【】的占位表达。
4. 每个问题必须定位到具体模块、功能点或业务规则。
5. 风险等级分为 P0/P1/P2。
6. hidden_assumptions 至少识别3个，最多10个最有价值的隐含假设。

PRD结构信息：
{structure_json}
"""

STAGE2_SHIFT_LEFT_ASSETS_PROMPT = """
你是一名拥有15年经验的测试开发专家和架构师。

你的任务是基于结构化 PRD 模型进行「测试左移（Shift-Left）」资产生成。
这些资产将帮助研发在编写代码前就定好规范，并帮助测试提前准备数据和监控。

请根据以下 PRD 结构化数据以及**已识别出的核心高价值缺陷**，分析并生成以下资产：
特别是要针对核心缺陷（如并发、一致性、权限、异常恢复等）生成针对性的 Mock、契约和埋点建议。

1. **测试数据建议 (Test Data Advisor)**: 针对每个核心输入项，提供等价类、边界值和典型异常数据。
2. **API 契约建议 (API Contract)**: 识别需求中的接口，生成标准的 OpenAPI/Swagger (YAML/JSON) 片段。
3. **Mock 数据方案 (Mock Data)**: 为前端开发提供符合契约的 Mock 响应数据，必须包含异常流程的 Mock（如 503、401、弱网超时）。
4. **可观测性建议 (Observability)**: 识别核心业务路径，建议关键埋点、日志监控点和性能指标。

请严格按以下 JSON 格式输出，必须且只能输出合法的 JSON 对象，严禁使用 markdown 标记（如 ```json ），严禁包含任何前言或后语文字：
{
  "test_data_advisor": [
    {
      "module": "所属模块",
      "field": "字段/输入项名称",
      "rules": "业务规则简述",
      "suggestions": [
        {"type": "有效边界", "value": "示例值", "reason": "说明"},
        {"type": "无效边界", "value": "示例值", "reason": "说明"},
        {"type": "特殊异常", "value": "示例值", "reason": "说明"}
      ]
    }
  ],
  "api_contracts": [
    {
      "api_name": "接口名称/功能点",
      "method": "GET/POST/PUT/DELETE",
      "path": "/api/v1/...",
      "swagger_snippet": "OpenAPI YAML 或 JSON 片段字符串",
      "mock_response": "JSON 格式的 Mock 数据字符串"
    }
  ],
  "observability_points": [
    {
      "business_flow": "业务流程名称",
      "critical_path": "关键步骤",
      "logging_suggestion": "日志记录点及关键字段建议",
      "metric_suggestion": "建议监控的性能指标或业务指标",
      "alert_rule": "建议的告警规则"
    }
  ]
}

【结构化 PRD 数据】：
{structure_json}

【已识别的核心高价值缺陷】：
{defects_json}
"""

PRD_OPTIMIZATION_PROMPT = """
你是一名拥有15年经验的资深产品专家和需求架构师。

任务：基于「原始 PRD」和「审计出的缺陷列表」，输出一份**优化润色后、可直接用于开发的 PRD**。

要求：
1. **消除模糊词**：将“快”、“流畅”、“及时”、“适当”等主观词汇，替换为具体的量化指标（如“响应时间 < 200ms”、“帧率 > 60fps”）。
2. **补全缺失逻辑**：针对审计发现的“异常流程缺失”、“边界未定义”、“状态死路”，自动补全合理的闭环逻辑。
3. **结构化增强**：确保 PRD 包含：功能概述、前置条件、核心流程、异常流程、验收标准（量化）、外部依赖。
4. **保持核心意图**：在优化的同时，必须忠实于产品经理的原始业务目标，不要随意添加或删除核心功能。
5. **Markdown 格式**：直接输出优化后的完整 PRD（Markdown 格式），不要包含任何解释或开场白。

【原始 PRD 内容】：
{prd_content}

【审计出的缺陷列表】：
{defects_json}

请输出优化后的 PRD：
"""

TEST_CASE_GENERATION_PROMPT = """
你是一名拥有15年经验的高级测试工程师，擅长编写高质量、高覆盖率的测试用例。

任务：基于以下结构化 PRD 数据，自动生成一套完整的**功能测试用例表**。

要求：
1. **覆盖全面**：包含正向功能流程、异常分支处理、边界值校验、交互冲突及可测试性要点。
2. **结构规范**：每个用例必须包含：用例ID、所属模块、功能点、前置条件、测试步骤（1. 2. 3.）、预期结果。
3. **关联审计**：针对审计发现的缺陷点（如：描述模糊、逻辑死路），特别设计对应的验证用例。
4. **输出格式**：必须且只能输出合法的 JSON 数组，严禁使用 markdown 标记（如 ```json ），严禁包含任何前言或后语文字。

JSON 数组格式要求如下：
[
  {
    "case_id": "TC-模块名-001",
    "module": "模块名",
    "feature": "功能点名称",
    "precondition": "前置条件描述",
    "steps": "1. 步骤一\\n2. 步骤二\\n3. 步骤三",
    "expected": "预期结果描述",
    "priority": "P0/P1/P2"
  }
]

【结构化 PRD 数据】：
{structure_json}

【审计发现的缺陷点】：
{defects_json}

请输出测试用例数组：
"""


def _extract_json_dict(text: str) -> Dict[str, Any]:
    data = _extract_first_json_object(text)
    return data if isinstance(data, dict) else {}


def _strip_json_fence(text: str) -> str:
    raw = (text or "").strip()
    for pattern in (r"```json\s*([\s\S]*?)\s*```", r"```\s*([\s\S]*?)\s*```"):
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return raw


def _extract_first_json_array(text: str) -> List[Any]:
    raw = _strip_json_fence(text)
    if not raw:
        return []
    start = raw.find("[")
    if start == -1:
        return []
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
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(raw[start : i + 1])
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    break
        i += 1
    end = raw.rfind("]")
    if end != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _pick_first_list(data: Dict[str, Any], keys: List[str]) -> List[Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _parse_llm_defects_response(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    仅解析大模型返回的缺陷列表和隐含假设，与本地规则库 JSON 完全分离。
    返回 (defects列表, hidden_assumptions列表)
    任何解析异常或非预期结构均返回空列表，不抛错。
    """
    try:
        raw = _strip_json_fence(text)
        data: Any = _extract_first_json_object(raw)
        if not data:
            try:
                data = json.loads(raw)
            except Exception:
                data = _extract_first_json_array(raw)

        raw_defects: List[Any] = []
        raw_assumptions: List[Any] = []

        if isinstance(data, list):
            raw_defects = data
        elif isinstance(data, dict):
            raw_defects = _pick_first_list(
                data,
                [
                    "defects",
                    "issues",
                    "findings",
                    "problems",
                    "risks",
                    "defect_list",
                    "issue_list",
                    "漏洞",
                    "缺陷",
                    "缺陷列表",
                    "问题列表",
                ],
            )
            if not raw_defects and isinstance(data.get("data"), dict):
                raw_defects = _pick_first_list(
                    data.get("data") or {},
                    ["defects", "issues", "findings", "漏洞", "缺陷", "缺陷列表", "问题列表"],
                )
            raw_assumptions = _pick_first_list(
                data,
                ["hidden_assumptions", "assumptions", "assumption_list", "隐含假设", "假设列表"],
            )
            if not raw_assumptions and isinstance(data.get("data"), dict):
                raw_assumptions = _pick_first_list(
                    data.get("data") or {},
                    ["hidden_assumptions", "assumptions", "隐含假设", "假设列表"],
                )
            if not raw_defects and any(k in data for k in ["type", "description", "risk_level", "问题类型", "问题描述", "风险等级"]):
                raw_defects = [data]

        defects_out = []
        for i, x in enumerate(raw_defects):
            if not isinstance(x, dict):
                continue
            defects_out.append(_normalize_defect(x, i + 1))

        assumptions_out = []
        for i, x in enumerate(raw_assumptions):
            if not isinstance(x, dict):
                continue
            assumptions_out.append({
                "id": str(x.get("id") or x.get("编号") or f"HA{i+1:03d}").strip(),
                "module": str(x.get("module") or x.get("所属模块") or "【PRD未说明】").strip(),
                "prd_statement": str(x.get("prd_statement") or x.get("原文描述") or "【PRD未说明】").strip(),
                "hidden_assumption": str(x.get("hidden_assumption") or x.get("隐含假设") or "【PRD未说明】").strip(),
                "question_to_ask": str(x.get("question_to_ask") or x.get("待确认问题") or "【PRD未说明】").strip(),
                "impact": str(x.get("impact") or x.get("影响") or "【PRD未说明】").strip(),
                "suggestion": str(x.get("suggestion") or x.get("建议") or "【PRD未说明】").strip(),
            })

        if not defects_out and raw:
            logger.warning("Stage2 LLM 返回已收到但未解析出 defects，raw_preview=%s", raw[:800])

        return defects_out, assumptions_out
    except Exception as e:
        logger.warning("Stage2 LLM 响应解析失败: %s", e)
        return [], []


def _normalize_risk_level(value: str) -> str:
    v = (value or "").strip().upper()
    if v in {"P0", "P1", "P2"}:
        return v
    if v in {"HIGH", "CRITICAL"}:
        return "P0"
    if v in {"MEDIUM"}:
        return "P1"
    return "P2"


_PLACEHOLDER_TEXT_RE = re.compile(r"【[^】]*】|\bTBD\b|\bTODO\b|\bXX\b|待补充|占位符|placeholder", re.IGNORECASE)
_GENERIC_MODULE_TERMS = {
    "业务流程", "功能规则", "相关模块", "相关功能", "该功能", "该模块", "全局", "系统", "模块", "页面", "流程", "规则",
}


def _sanitize_output_text(value: Any) -> str:
    s = str(value or "")
    s = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", s)
    s = s.replace("```json", "").replace("```", "")
    return _normalize_text(s)


def _contains_placeholder_text(value: Any, allow_unspecified: bool = False) -> bool:
    s = _sanitize_output_text(value)
    if not s:
        return False
    if allow_unspecified and s == "【PRD未说明】":
        return False
    return bool(_PLACEHOLDER_TEXT_RE.search(s))


def _is_generic_module_text(value: Any) -> bool:
    s = _sanitize_output_text(value)
    if not s or s == "【PRD未说明】":
        return True
    if s in _GENERIC_MODULE_TERMS:
        return True
    return any(term in s for term in _GENERIC_MODULE_TERMS)


def _compact_text(value: Any) -> str:
    s = _sanitize_output_text(value)
    return re.sub(r"[，。；：:、“”‘’\-\s]", "", s)


def _is_weak_reason(description: str, reason: str) -> bool:
    rs = _sanitize_output_text(reason)
    if not rs or rs == "【PRD未说明】":
        return True
    if _contains_placeholder_text(rs, allow_unspecified=True):
        return True
    if rs in {"原因同上", "影响同上", "未说明"}:
        return True
    ds_compact = _compact_text(description)
    rs_compact = _compact_text(rs)
    if ds_compact and rs_compact and (rs_compact in ds_compact or ds_compact in rs_compact):
        return True
    if any(x in rs for x in ["存在问题", "体验不一致", "引发投诉", "需要补充规则"]):
        return True
    return False


def _pick_best_specific_label(stage1_output: Dict[str, Any], context_text: str) -> str:
    candidates: List[str] = []
    for key in ["modules", "features", "actions", "flows", "states", "business_rules"]:
        candidates.extend(_as_text_list((stage1_output or {}).get(key)))
    best = ""
    best_score = 0
    text = _sanitize_output_text(context_text)
    for cand in _dedupe_keep_order([_sanitize_output_text(x) for x in candidates if _sanitize_output_text(x)]):
        if cand == "【PRD未说明】":
            continue
        score = 0
        if cand in text:
            score += 3
        parts = [p for p in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,}", cand) if len(p) >= 2]
        score += sum(1 for p in parts if p in text)
        if score > best_score:
            best = cand
            best_score = score
    return best


def _build_reason_chain(module: str, defect_type: str, description: str) -> str:
    feature = _sanitize_output_text(module) or "该功能点"
    blob = " ".join([feature, _sanitize_output_text(defect_type), _sanitize_output_text(description)])
    if any(k in blob for k in ["边界", "最小值", "最大值", "空值", "非法值", "长度"]):
        return f"{feature}输入极值或非法值 -> 前后端校验口径不一致 -> 可能出现接口报错、结果异常或状态错乱 -> 联调和上线阶段高概率返工。"
    if any(k in blob for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"{feature}缺少角色边界或鉴权规则 -> 未授权用户可误操作或越权访问 -> 形成隐私、资损或合规风险 -> 事后难以追溯。"
    if any(k in blob for k in ["异常", "失败", "超时", "重试", "降级", "中断"]):
        return f"{feature}遇到失败、超时或中断时没有闭环定义 -> 用户重复操作或误以为成功 -> 页面状态与真实结果对不上 -> 现场演示容易翻车。"
    if any(k in blob for k in ["状态", "并发", "冲突", "回滚", "恢复", "幂等"]):
        return f"{feature}缺少状态流转或并发裁决规则 -> 多操作同时发生时系统行为不可预测 -> 容易出现状态漂移、重复执行或恢复失败。"
    if any(k in blob for k in ["验收", "成功标准", "指标", "日志", "可测试"]):
        return f"{feature}没有量化验收口径 -> 开发无法判断何时算完成，测试无法稳定验收 -> 问题会被推迟到联调或上线后暴露。"
    return f"{feature}缺少可执行规则 -> 开发和测试会按各自理解实现与验收 -> 最终表现不一致并放大为交付风险。"


def _build_direct_prd_patch(module: str, defect_type: str, description: str) -> str:
    feature = _sanitize_output_text(module) or "该功能点"
    blob = " ".join([feature, _sanitize_output_text(defect_type), _sanitize_output_text(description)])
    if any(k in blob for k in ["边界", "最小值", "最大值", "空值", "非法值", "长度"]):
        return f"在 PRD 中补充“{feature}”的输入边界：最小值、最大值、默认值、是否允许空值/非法值，以及超界时的前端提示和后端错误码。"
    if any(k in blob for k in ["权限", "安全", "越权", "鉴权", "风控"]):
        return f"在 PRD 中补充“{feature}”的角色权限矩阵：谁可查看、谁可操作、越权时如何拦截、返回什么提示，以及是否记录审计日志。"
    if any(k in blob for k in ["异常", "失败", "超时", "重试", "降级", "中断"]):
        return f"在 PRD 中补充“{feature}”的失败闭环：超时/失败/中断时页面展示什么提示、是否允许重试、状态回退到哪里、数据如何恢复。"
    if any(k in blob for k in ["状态", "并发", "冲突", "回滚", "恢复", "幂等"]):
        return f"在 PRD 中补充“{feature}”的状态机规则：触发事件、转移条件、冲突裁决、回滚路径和恢复后的目标状态。"
    if any(k in blob for k in ["验收", "成功标准", "指标", "日志", "可测试"]):
        return f"在 PRD 中补充“{feature}”的验收口径：什么算成功、什么算失败、关键指标阈值是多少，以及日志与监控要记录哪些字段。"
    return f"在 PRD 中补充“{feature}”的触发条件、处理动作、异常分支和验收标准，避免研发和测试各自猜测实现口径。"


def _normalize_defect(item: Dict[str, Any], idx: int) -> Dict[str, Any]:
    d_type = _sanitize_output_text(item.get("type") or item.get("issue_type") or item.get("问题类型") or "未分类问题")
    module = _sanitize_output_text(item.get("module") or item.get("所属模块") or item.get("feature_module") or "【PRD未说明】")
    anchor = _sanitize_output_text(item.get("anchor") or item.get("evidence") or item.get("原文锚点") or "")
    desc = _sanitize_output_text(item.get("description") or item.get("problem") or item.get("问题描述") or "【PRD未说明】")
    reason = _sanitize_output_text(item.get("reason") or item.get("impact") or item.get("原因") or item.get("风险说明") or "【PRD未说明】")
    suggestion = _sanitize_output_text(item.get("suggestion") or item.get("fix") or item.get("建议") or item.get("修复建议") or "【PRD未说明】")
    risk_level = _normalize_risk_level(str(item.get("risk_level") or item.get("severity") or item.get("风险等级") or "P2"))
    source = _sanitize_output_text(item.get("source") or "llm").lower()
    if source not in {"rule", "llm", "hybrid", "system"}:
        source = "llm"
    defect_id = _sanitize_output_text(item.get("id") or f"D{idx:03d}")
    return {
        "id": defect_id,
        "type": d_type,
        "module": module,
        "anchor": anchor,
        "description": desc,
        "risk_level": risk_level,
        "reason": reason,
        "suggestion": suggestion,
        "source": source,
    }


def _as_text_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _is_unspecified(items: List[str]) -> bool:
    if not items:
        return True
    return all((x == "【PRD未说明】" or not x.strip()) for x in items)


def _contains_any(text: str, keywords: List[str]) -> bool:
    return any(k in text for k in keywords)


def _coverage_hits(text: str, keyword_groups: List[List[str]]) -> Dict[str, bool]:
    """
    keyword_groups: [["超时","timeout"], ["弱网","断网"], ...]
    返回每组是否命中，用于判断“写了 exceptions 但覆盖不全”的情况。
    """
    t = _normalize_text(text or "")
    hits = {}
    for group in keyword_groups:
        key = "/".join(group)
        hits[key] = any(k in t for k in group)
    return hits


def _is_exception_coverage_insufficient(stage1_output: Dict[str, Any]) -> bool:
    """
    exceptions/edge_cases 不为空也可能覆盖不足：
    - 仅写“异常处理：提示错误”但没有超时/弱网/重试/回滚/重复提交等。
    """
    exceptions = _as_text_list(stage1_output.get("exceptions"))
    edge_cases = _as_text_list(stage1_output.get("edge_cases"))
    permissions = _as_text_list(stage1_output.get("permissions"))
    # 将结构字段拼成一个可检索文本
    text = " ".join(exceptions + edge_cases + permissions)
    if not text or _is_unspecified(exceptions) and _is_unspecified(edge_cases):
        return True

    groups = [
        ["超时", "timeout"],
        ["弱网", "断网", "网络"],
        ["失败", "错误", "error", "异常"],
        ["重试", "retry"],
        ["回滚", "补偿", "撤销"],
        ["幂等", "去重", "重复提交", "重复点击"],
    ]
    hits = _coverage_hits(text, groups)
    # 至少命中其中 3 组才认为异常覆盖“有一定落地”
    return sum(1 for v in hits.values() if v) < 3


def _is_boundary_coverage_insufficient(stage1_output: Dict[str, Any]) -> bool:
    edge_cases = _as_text_list(stage1_output.get("edge_cases"))
    text = " ".join(edge_cases)
    if not text or _is_unspecified(edge_cases):
        return True
    groups = [
        ["最大", "最小", "上限", "下限", "范围"],
        ["并发", "同时", "抢占"],
        ["重复", "幂等", "去重", "重复提交"],
        ["弱网", "断网", "超时"],
    ]
    hits = _coverage_hits(text, groups)
    return sum(1 for v in hits.values() if v) < 2


def _build_coverage_matrix(stage1_output: Dict[str, Any]) -> Dict[str, Any]:
    """
    输出一个可解释的“异常/边界覆盖速览”，用于报告与回归测试。
    """
    exceptions = _as_text_list(stage1_output.get("exceptions"))
    edge_cases = _as_text_list(stage1_output.get("edge_cases"))
    text = " ".join(exceptions + edge_cases)

    exception_groups = [
        ("超时", ["超时", "timeout"]),
        ("弱网/断网", ["弱网", "断网", "网络"]),
        ("失败/错误码", ["失败", "错误", "error", "错误码"]),
        ("重试", ["重试", "retry"]),
        ("回滚/补偿", ["回滚", "补偿", "撤销"]),
        ("幂等/重复提交", ["幂等", "去重", "重复提交", "重复点击"]),
    ]
    boundary_groups = [
        ("范围/上下限", ["最大", "最小", "上限", "下限", "范围"]),
        ("并发", ["并发", "同时", "抢占"]),
        ("重复操作", ["重复", "幂等", "去重", "重复提交", "重复点击"]),
        ("弱网/超时", ["弱网", "断网", "超时"]),
    ]

    def _mk(groups):
        out = []
        for name, kws in groups:
            out.append({
                "item": name,
                "covered": any(k in _normalize_text(text) for k in kws),
                "keywords": kws,
            })
        return out

    return {
        "exception_coverage": _mk(exception_groups),
        "boundary_coverage": _mk(boundary_groups),
        "raw_text_sample": (text[:500] if text else "【PRD未说明】"),
    }

def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _compose_prd_lines(prd_text: str) -> List[Tuple[int, str]]:
    lines = []
    for idx, line in enumerate((prd_text or "").splitlines(), start=1):
        s = _normalize_text(line)
        if s:
            lines.append((idx, s))
    return lines


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _trim_stage1_for_stage2(stage1_output: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stage2 LLM 只需要用于“漏洞扫描”的结构化字段。
    将 stage1_output 裁剪成“小而全”的结构，降低 token 与超时概率。
    """
    if not isinstance(stage1_output, dict):
        return {}

    caps = {
        "modules": 25,
        "features": 30,
        "actions": 30,
        "flows": 25,
        "states": 30,
        "business_rules": 40,
        "data_structures": 25,
        "permissions": 20,
        "exceptions": 20,
        "edge_cases": 25,
        "dependencies": 20,
        "non_functional_requirements": 15,
    }

    # Stage2 扫描相关字段白名单（不包含 source_map/transitions，减少 token）
    keys = [
        "product_name",
        "background",
        "goal",
        "modules",
        "features",
        "actions",
        "flows",
        "states",
        "business_rules",
        "data_structures",
        "permissions",
        "exceptions",
        "edge_cases",
        "dependencies",
        "non_functional_requirements",
    ]

    out: Dict[str, Any] = {}
    for k in keys:
        if k not in stage1_output:
            continue
        v = stage1_output.get(k)
        if isinstance(v, list):
            arr = [str(x).strip() for x in v if str(x).strip()]
            if not arr:
                arr = ["【PRD未说明】"]
            arr = _dedupe_keep_order(arr)[: caps.get(k, 20)]
            # 避免大量占位符浪费 token
            if len(arr) > 1 and all(x == "【PRD未说明】" for x in arr):
                arr = ["【PRD未说明】"]
            out[k] = arr
        elif isinstance(v, str):
            s = v.strip()
            if s:
                out[k] = s
        else:
            continue

    # 保底：关键数组字段至少给一个占位
    for arr_key in [
        "modules",
        "features",
        "actions",
        "flows",
        "states",
        "business_rules",
        "permissions",
        "exceptions",
        "edge_cases",
        "dependencies",
        "non_functional_requirements",
    ]:
        if arr_key not in out:
            out[arr_key] = ["【PRD未说明】"]

    return out


def _find_anchor(
    prd_text: str,
    stage1_output: Dict[str, Any],
    module: str,
    defect_type: str,
    description: str,
    composed_lines: Optional[List[Tuple[int, str]]] = None,
) -> str:
    # 1) 优先使用 Stage1 的 source_map（最稳定）
    sm = stage1_output.get("source_map") if isinstance(stage1_output, dict) else None
    if isinstance(sm, dict):
        modules = _as_text_list(stage1_output.get("modules"))
        flows = _as_text_list(stage1_output.get("flows"))
        states = _as_text_list(stage1_output.get("states"))
        mod = (module or "").strip()
        if mod and mod in modules:
            idx = modules.index(mod)
            anchors = sm.get("modules")
            if isinstance(anchors, list) and idx < len(anchors) and str(anchors[idx]).strip():
                a = str(anchors[idx]).strip()
                if a and a != "【PRD未说明】":
                    return a
        # 若 defect_type 更像流程/状态问题，优先给 flows/states 的首个可用锚点
        t = (defect_type or "")
        if any(k in t for k in ["流程", "中断", "并发", "重试"]) and flows:
            anchors = sm.get("flows")
            if isinstance(anchors, list):
                for a in anchors:
                    s = str(a or "").strip()
                    if s and s != "【PRD未说明】":
                        return s
        if any(k in t for k in ["状态", "跳转", "回滚"]) and states:
            anchors = sm.get("states")
            if isinstance(anchors, list):
                for a in anchors:
                    s = str(a or "").strip()
                    if s and s != "【PRD未说明】":
                        return s

    # 性能优化：一次 Stage2 运行内缓存 prd 行号/内容，避免每个 defect 重建
    lines = composed_lines if composed_lines is not None else _compose_prd_lines(prd_text)
    if not lines:
        return module or "【PRD未说明】"
    modules = _as_text_list(stage1_output.get("modules"))
    flows = _as_text_list(stage1_output.get("flows"))
    states = _as_text_list(stage1_output.get("states"))
    keywords = [module, defect_type]
    keywords += modules[:8] + flows[:6] + states[:6]
    desc_terms = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,}", description or "")
    keywords += desc_terms[:6]
    uniq = []
    for k in keywords:
        s = str(k or "").strip()
        if s and s not in uniq and s != "【PRD未说明】":
            uniq.append(s)
    # 2) 多关键词打分选择最优行，避免“第一个命中就返回”导致漂移
    best = None
    for i, line in lines:
        score = 0
        for k in uniq:
            if k in line:
                # 模块/类型权重更高
                if k == module or k == defect_type:
                    score += 3
                else:
                    score += 1
        if score <= 0:
            continue
        cand = (score, -i, i, line)
        if best is None or cand > best:
            best = cand
    if best is not None:
        _, _, i, line = best
        return f"L{i}: {line[:120]}"
    first_feature = next((x for x in modules + flows + states if x and x != "【PRD未说明】"), "")
    if first_feature:
        return f"功能：{first_feature}"
    i, line = lines[0]
    return f"L{i}: {line[:120]}"


def _dynamic_suggestion(module: str, defect_type: str, risk_level: str, description: str) -> str:
    m = str(module or "")
    t = str(defect_type or "")
    desc = str(description or "")
    blob = " ".join([m, t, desc])
    if any(k in blob for k in ["同步时效", "同步延迟", "时效性", "多久同步", "列表同步"]):
        return "补充同步 SLA：明确 TV 端完成后多少秒/分钟内同步到手机端列表，超时提示什么状态，并补告警和补偿机制。"
    if any(k in blob for k in ["二维码", "扫码", "鉴权", "token", "401", "未授权", "有效期"]):
        return "补充二维码鉴权规则：账号绑定关系、二维码有效期、错误账号扫码返回码、失效提示和审计日志。"
    if any(k in blob for k in ["云端", "上传", "503", "待上传", "降级", "自动重传"]):
        return "补充云端失败降级方案：本地暂存、待上传标记、自动重传时机、失败提示文案和避免重复文件的规则。"
    if any(k in blob for k in ["五维评分", "评分开关", "设置开关", "生效时机"]):
        return "补充五维评分录音开关的生效时机：影响当前歌曲还是下一首歌曲、切换中是否影响已在录内容、页面如何提示。"
    if any(k in blob for k in ["转台", "关台", "重开台", "清空", "此处规则需确认"]):
        return "补充转台/关台/重开台的统一规则：到底清空还是保留录音，并同步定义页面提示、列表状态和客服口径。"
    if any(k in blob for k in ["性能", "QPS", "CPU", "采样率", "比特率", "保存期限", "时延", "容量"]):
        return "补充性能指标：编码耗时、上传时延、CPU/内存占用、采样率、比特率、并发上限和保存期限。"
    if "投屏" in m and ("安全" in t or "权限" in t):
        return "补充跨包房隔离校验机制（房间ID绑定、会话令牌校验、同网段限制）并记录审计日志与拦截策略。"
    if "状态" in t:
        return "补充状态机定义（状态、触发事件、转移条件、终态），并明确异常回滚与恢复路径。"
    if "并发" in t:
        return "补充并发控制方案（请求串行化、幂等键、锁或队列），并定义冲突时的处理优先级。"
    if "权限" in t:
        return "补充角色-权限矩阵，明确谁可查看、触发、配置、管理，并定义越权拦截行为。"
    if "数据" in t or "字段" in t:
        return "补充字段契约（类型、长度、必填、默认值、来源）并明确跨系统一致性与校验规则。"
    if "边界" in t or any(k in desc for k in ["最小值", "最大值", "上限", "下限", "长度", "范围", "空值", "非法值"]):
        return "补充输入边界与字段约束：最小值、最大值、长度、是否必填、默认值、非法值处理方式，以及超界时的提示和错误码。"
    if "异常" in t:
        return "补充失败、超时、重试、回滚和降级策略，并给出用户提示与状态恢复规则。"
    if "安全" in t:
        return "补充防刷、防作弊、鉴权与风控策略，明确触发条件、拦截动作与告警机制。"
    if "外部依赖" in t:
        return "补充外部依赖清单：依赖名称、用途、版本、负责人、系统权限、超时阈值以及依赖不可用时的降级和回退逻辑。"
    if risk_level == "P0":
        return f"针对{m or '该模块'}先冻结实现口径，补齐可执行规则后再进入开发。"
    return f"针对{m or '该模块'}补充“{t or '该问题'}”的可执行规则、边界条件和验收标准。"


def _should_replace_suggestion(suggestion: str) -> bool:
    s = str(suggestion or "").strip()
    if not s:
        return True
    if _contains_placeholder_text(s):
        return True
    patterns = [
        "相关约束",
        "并明确模块、边界与异常处理",
        "建议补充",
        "该规则",
        "可执行规则、边界条件和验收标准",
        "先冻结实现口径",
        "针对该模块",
        "针对相关模块",
    ]
    return any(p in s for p in patterns)


def _is_rule_applicable(rule: Dict[str, Any], stage1_output: Dict[str, Any], prd_text: str) -> bool:
    name = str(rule.get("name") or "")
    modules = _as_text_list(stage1_output.get("modules"))
    states = _as_text_list(stage1_output.get("states"))
    flows = _as_text_list(stage1_output.get("flows"))
    text = _normalize_text(" ".join(modules + states + flows + [prd_text]))
    if "状态" in name:
        return len(states) >= 2 or _contains_any(text, ["状态", "切换", "流转"])
    if "并发" in name:
        return _contains_any(text, ["并发", "同时", "多个", "抢占", "优先级"])
    if "接口幂等性" in name or "重试机制" in name:
        return _contains_any(text, ["接口", "请求", "调用", "发送", "提交", "消息"])
    if "字段定义" in name or "数据来源" in name or "数据一致性" in name:
        return _contains_any(text, ["字段", "数据", "入参", "出参", "来源", "同步", "写入"])
    if "外部依赖" in name:
        return _contains_any(text, ["依赖", "第三方", "服务", "系统", "接口"])
    if "权限控制" in name or "安全防护" in name:
        return _contains_any(text, ["权限", "角色", "登录", "鉴权", "安全", "风控", "越权"])
    return True


def _enrich_defect(
    defect: Dict[str, Any],
    stage1_output: Dict[str, Any],
    prd_text: str,
    composed_lines: Optional[List[Tuple[int, str]]] = None,
) -> Dict[str, Any]:
    d = dict(defect)
    context_text = " ".join([
        str(d.get("module") or ""),
        str(d.get("type") or ""),
        str(d.get("description") or ""),
        str(d.get("reason") or ""),
        str(d.get("anchor") or ""),
    ])
    if _is_generic_module_text(d.get("module")):
        guessed_module = _pick_best_specific_label(stage1_output or {}, context_text)
        if guessed_module:
            d["module"] = guessed_module
    anchor = _sanitize_output_text(d.get("anchor") or "")
    if not anchor or _contains_placeholder_text(anchor, allow_unspecified=True):
        anchor = _find_anchor(
            prd_text,
            stage1_output,
            str(d.get("module") or ""),
            str(d.get("type") or ""),
            str(d.get("description") or ""),
            composed_lines=composed_lines,
        )
    d["anchor"] = anchor
    if _is_weak_reason(str(d.get("description") or ""), str(d.get("reason") or "")):
        d["reason"] = _build_reason_chain(
            str(d.get("module") or ""),
            str(d.get("type") or ""),
            str(d.get("description") or ""),
        )
    if _should_replace_suggestion(str(d.get("suggestion") or "")):
        d["suggestion"] = _build_direct_prd_patch(
            str(d.get("module") or ""),
            str(d.get("type") or ""),
            str(d.get("description") or ""),
        )
    d["description"] = _sanitize_output_text(d.get("description") or "")
    d["reason"] = _sanitize_output_text(d.get("reason") or "")
    d["suggestion"] = _sanitize_output_text(d.get("suggestion") or "")
    if _contains_placeholder_text(d["description"], allow_unspecified=True):
        d["description"] = d["description"].replace("【PRD未说明】", "未说明")
    if _contains_placeholder_text(d["reason"], allow_unspecified=True):
        d["reason"] = _build_reason_chain(
            str(d.get("module") or ""),
            str(d.get("type") or ""),
            str(d.get("description") or ""),
        )
    if _contains_placeholder_text(d["suggestion"], allow_unspecified=True):
        d["suggestion"] = _build_direct_prd_patch(
            str(d.get("module") or ""),
            str(d.get("type") or ""),
            str(d.get("description") or ""),
        )
    if _is_generic_module_text(d.get("module")) and str(d.get("anchor") or "").startswith("L"):
        guessed_module = _pick_best_specific_label(stage1_output or {}, str(d.get("anchor") or ""))
        if guessed_module:
            d["module"] = guessed_module
    if _is_generic_module_text(d.get("module")):
        d["module"] = _sanitize_output_text(d.get("module") or "") or "当前功能点"
    return d


def _load_json_file_first_object(file_path: str) -> Dict[str, Any]:
    """本地规则库专用：读文件并解析为单个 JSON 对象，多段/损坏时只取第一段，与 LLM 用 JSON 分离。"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        return _extract_first_json_object(raw) or {}
    except Exception:
        return {}


def _load_rule_library() -> List[Dict[str, Any]]:
    """仅加载本地规则库 JSON（prd_scan_rules*.json），与大模型返回的 llm_stage2_response_schema 完全分离。"""
    try:
        m1 = os.path.getmtime(RULE_LIBRARY_FILE) if os.path.exists(RULE_LIBRARY_FILE) else None
        m2 = os.path.getmtime(RULE_LIBRARY_V2_FILE) if os.path.exists(RULE_LIBRARY_V2_FILE) else None
        key = (m1, m2)

        with _RULE_LIBRARY_CACHE_LOCK:
            if _RULE_LIBRARY_CACHE.get("key") == key and isinstance(_RULE_LIBRARY_CACHE.get("rules"), list):
                # 缓存命中
                return _RULE_LIBRARY_CACHE["rules"]

        rules_all: List[Dict[str, Any]] = []
        if os.path.exists(RULE_LIBRARY_FILE):
            data = _load_json_file_first_object(RULE_LIBRARY_FILE)
            rules = data.get("rules") if isinstance(data, dict) else []
            if isinstance(rules, list):
                rules_all.extend([r for r in rules if isinstance(r, dict) and r.get("enabled", True)])
        if os.path.exists(RULE_LIBRARY_V2_FILE):
            data2 = _load_json_file_first_object(RULE_LIBRARY_V2_FILE)
            rules2 = data2.get("rules") if isinstance(data2, dict) else []
            if isinstance(rules2, list):
                rules_all.extend([r for r in rules2 if isinstance(r, dict) and r.get("enabled", True)])

        with _RULE_LIBRARY_CACHE_LOCK:
            _RULE_LIBRARY_CACHE["key"] = key
            _RULE_LIBRARY_CACHE["rules"] = rules_all
        return rules_all
    except Exception as e:
        logger.warning("load rule library failed: %s", e)
        return []


def _is_v2_rule(rule: Dict[str, Any]) -> bool:
    return bool(rule.get("rule_id") or rule.get("rule_name") or rule.get("detector"))


def _v2_risk_level(rule: Dict[str, Any]) -> str:
    v = str(rule.get("severity") or "").strip().upper()
    return v if v in {"P0", "P1", "P2"} else "P2"


def _v2_target_module(rule: Dict[str, Any]) -> str:
    category = str(rule.get("category") or "").strip().upper()
    mapping = {
        "STATE_MACHINE": "状态机",
        "FLOW": "业务流程",
        "CONCURRENCY": "并发控制",
        "PERMISSION": "权限与安全",
        "EXCEPTION": "异常流程",
        "DATA": "数据结构",
        "TEST": "可测试性",
        "TECH": "系统与技术",
        "CONSISTENCY": "业务规则",
        "UX": "用户体验",
        "TEST_VERIFIABILITY": "可测试性",
    }
    return mapping.get(category, "【PRD未说明】")


def _detector_state_missing_global_states(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    states = _as_text_list(stage1_output.get("states"))
    return _is_unspecified(states)


def _detector_state_missing_transitions_text(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    states = _as_text_list(stage1_output.get("states"))
    if _is_unspecified(states):
        return False
    flows = _as_text_list(stage1_output.get("flows"))
    rules = _as_text_list(stage1_output.get("business_rules"))
    text = " ".join(flows + rules + [prd_text or ""])
    # 若完全没有切换相关词，认为缺失
    return not _contains_any(text, ["切换", "进入", "退出", "打断", "恢复", "回到", "返回", "优先级"])


def _detector_flow_success_only(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    exceptions = _as_text_list(stage1_output.get("exceptions"))
    edge_cases = _as_text_list(stage1_output.get("edge_cases"))
    return _is_unspecified(exceptions) or _is_unspecified(edge_cases) or _is_exception_coverage_insufficient(stage1_output)


def _detector_flow_interrupt_without_resume(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    flows = _as_text_list(stage1_output.get("flows"))
    rules = _as_text_list(stage1_output.get("business_rules"))
    text = " ".join(flows + rules + [prd_text or ""])
    has_interrupt = _contains_any(text, ["中断", "打断", "退出", "返回", "关闭", "取消"])
    has_resume = _contains_any(text, ["恢复", "继续", "回到", "返回到", "续播", "续用"])
    return bool(has_interrupt and not has_resume)


def _detector_conc_missing_arbitration(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    flows = _as_text_list(stage1_output.get("flows"))
    rules = _as_text_list(stage1_output.get("business_rules"))
    edge_cases = _as_text_list(stage1_output.get("edge_cases"))
    text = " ".join(flows + rules + edge_cases + [prd_text or ""])
    # 如果连并发关键词都没有提到，视为缺失
    return not _contains_any(text, ["并发", "同时", "抢占", "排队", "优先级", "先到先得"])


# --- 可测试性检测器 ---
def _detector_testability_subjective_words(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    """检测 PRD 中是否包含主观体验词汇且无量化标准"""
    full_text = _normalize_text(
        " ".join([
            str(stage1_output.get("goal") or ""),
            " ".join(_as_text_list(stage1_output.get("modules"))),
            " ".join(_as_text_list(stage1_output.get("business_rules"))),
            " ".join(_as_text_list(stage1_output.get("non_functional_requirements"))),
            prd_text or "",
        ])
    )
    subjective_words = ["快", "流畅", "友好", "及时", "尽快", "快速", "适当", "尽量", "可能", "大约"]
    if not _contains_any(full_text, subjective_words):
        return False
    # 有主观词但无数值指标 → 命中
    numeric_indicators = ["ms", "秒", "fps", "%", "毫秒", "帧率", "并发", "阈值", "次数"]
    return not _contains_any(full_text, numeric_indicators)


def _detector_testability_missing_perf_metrics(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    """检测涉及性能要求但无数值指标"""
    nfr = _as_text_list(stage1_output.get("non_functional_requirements"))
    business_rules = _as_text_list(stage1_output.get("business_rules"))
    text = " ".join(nfr + business_rules + [prd_text or ""])
    perf_keywords = ["性能", "加载", "响应", "并发", "吞吐", "容量", "延迟", "超时", "卡顿"]
    if not _contains_any(text, perf_keywords):
        return False
    # 有性能描述但无数值
    numeric_indicators = ["ms", "秒", "fps", "%", "毫秒", "帧率", "并发数", "QPS", "TPS", "P99", "P95"]
    return not _contains_any(text, numeric_indicators)


def _detector_testability_vague_acceptance(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    """检测验收标准模糊：缺少明确的通过/失败判定条件"""
    business_rules = _as_text_list(stage1_output.get("business_rules"))
    goal = str(stage1_output.get("goal") or "")
    text = " ".join(business_rules) + " " + goal + " " + (prd_text or "")
    # 有功能描述但无验收判定词
    acceptance_keywords = ["通过", "失败", "达标", "合格", "验收条件", "判定", "判定标准", "成功条件", "完成标准"]
    if not _contains_any(text, acceptance_keywords):
        return True
    # 有验收词但无数值或具体指标
    vague_patterns = ["通过即可", "视为通过", "认为通过", "就算完成", "即完成", "就算成功"]
    return _contains_any(text, vague_patterns)


def _detector_testability_vague_duration(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    """检测操作耗时描述模糊"""
    full_text = _normalize_text(
        " ".join([
            str(stage1_output.get("goal") or ""),
            " ".join(_as_text_list(stage1_output.get("modules"))),
            " ".join(_as_text_list(stage1_output.get("business_rules"))),
            " ".join(_as_text_list(stage1_output.get("flows"))),
            prd_text or "",
        ])
    )
    # 有耗时相关描述但使用模糊词汇
    duration_keywords = ["耗时", "时间", "速度", "延迟", "加载", "响应", "处理"]
    vague_time_words = ["很快", "片刻", "短时间", "尽快", "及时", "随时", "立刻", "立即", "一定时间"]
    if not _contains_any(full_text, duration_keywords):
        return False
    return _contains_any(full_text, vague_time_words)


def _detector_testability_dependency_unverified(stage1_output: Dict[str, Any], prd_text: str) -> bool:
    """检测外部依赖验证未定义"""
    dependencies = _as_text_list(stage1_output.get("dependencies"))
    business_rules = _as_text_list(stage1_output.get("business_rules"))
    text = " ".join(dependencies + business_rules + [prd_text or ""])
    # 有依赖描述但无验证方法
    if not _contains_any(text, ["依赖", "第三方", "外部", "服务", "系统", "接口"]):
        return False
    verify_keywords = ["验证", "检查", "心跳", "健康检查", "可用性", "监控", "告警", "探活"]
    return not _contains_any(text, verify_keywords)


_V2_DETECTORS = {
    "state.missing_global_states": _detector_state_missing_global_states,
    "state.missing_transitions_text": _detector_state_missing_transitions_text,
    "flow.success_only": _detector_flow_success_only,
    "flow.interrupt_without_resume": _detector_flow_interrupt_without_resume,
    "conc.missing_arbitration": _detector_conc_missing_arbitration,
    "testability.subjective_words": _detector_testability_subjective_words,
    "testability.missing_performance_metrics": _detector_testability_missing_perf_metrics,
    "testability.vague_acceptance": _detector_testability_vague_acceptance,
    "testability.vague_duration": _detector_testability_vague_duration,
    "testability.dependency_unverified": _detector_testability_dependency_unverified,
}


def _match_rule_v2(rule: Dict[str, Any], stage1_output: Dict[str, Any], prd_text: str) -> bool:
    detector_key = str(rule.get("detector") or "").strip()
    if not detector_key:
        return False
    fn = _V2_DETECTORS.get(detector_key)
    if not fn:
        return False
    try:
        return bool(fn(stage1_output or {}, prd_text or ""))
    except Exception as e:
        logger.warning("v2 detector failed (%s): %s", detector_key, e)
        return False

def _rule_risk_level(rule: Dict[str, Any]) -> str:
    if _is_v2_rule(rule):
        return _v2_risk_level(rule)
    category = str(rule.get("category") or "").strip()
    core = bool(rule.get("core"))
    if core and category in {"state_machine", "flow"}:
        return "P0"
    if core:
        return "P1"
    return "P2"


def _rule_target_module(rule: Dict[str, Any]) -> str:
    if _is_v2_rule(rule):
        return _v2_target_module(rule)
    category = str(rule.get("category") or "").strip()
    mapping = {
        "consistency": "业务规则",
        "state_machine": "状态机",
        "flow": "业务流程",
        "data": "数据结构",
        "testability": "可测试性",
        "technical": "系统与技术",
    }
    return mapping.get(category, "【PRD未说明】")


def _match_rule(rule: Dict[str, Any], stage1_output: Dict[str, Any]) -> bool:
    # v2 detector first (additive)
    if _is_v2_rule(rule):
        # v2 match depends on prd_text; handled in run_stage2_rule_library_scan
        return False
    name = str(rule.get("name") or "")
    modules = _as_text_list(stage1_output.get("modules"))
    flows = _as_text_list(stage1_output.get("flows"))
    states = _as_text_list(stage1_output.get("states"))
    business_rules = _as_text_list(stage1_output.get("business_rules"))
    data_structures = _as_text_list(stage1_output.get("data_structures"))
    permissions = _as_text_list(stage1_output.get("permissions"))
    exceptions = _as_text_list(stage1_output.get("exceptions"))
    edge_cases = _as_text_list(stage1_output.get("edge_cases"))
    dependencies = _as_text_list(stage1_output.get("dependencies"))
    nfr = _as_text_list(stage1_output.get("non_functional_requirements"))
    goal = str(stage1_output.get("goal") or "")
    full_text = " ".join(modules + flows + states + business_rules + data_structures + permissions + exceptions + edge_cases + dependencies + nfr + [goal])

    if "异常流程缺失" in name:
        return _is_unspecified(exceptions) or _is_exception_coverage_insufficient(stage1_output)
    if "中断流程缺失" in name:
        return not _contains_any(full_text, ["中断", "打断", "退出", "恢复"])
    if "规则边界缺失" in name or "边界条件缺失" in name:
        return _is_unspecified(edge_cases) or _is_boundary_coverage_insufficient(stage1_output)
    if "权限控制缺失" in name:
        return _is_unspecified(permissions)
    if "外部依赖未定义" in name:
        return _is_unspecified(dependencies)
    if "字段定义缺失" in name:
        return _is_unspecified(data_structures)
    if "状态孤岛" in name or "状态死路" in name or "非法状态跳转" in name:
        return _is_unspecified(states) or len(states) < 2
    if "状态回滚缺失" in name:
        return not _contains_any(full_text, ["回滚", "补偿", "撤销"])
    if "并发操作未定义" in name or "高并发风险" in name:
        return not _contains_any(full_text, ["并发", "限流", "排队", "锁", "幂等", "重复提交"])
    if "重试机制缺失" in name:
        return not _contains_any(full_text, ["重试"])
    if "接口幂等性缺失" in name:
        return not _contains_any(full_text, ["幂等", "去重", "重复提交"])
    if "模糊词检测" in name or "不可测试描述" in name:
        return _contains_any(full_text, ["适当", "尽量", "及时", "可能", "大约", "快速"])
    if "成功标准缺失" in name:
        return not _contains_any(goal + " " + " ".join(business_rules), ["成功", "验收", "指标", "通过条件", "SLA", "ms"])
    if "日志记录缺失" in name:
        return not _contains_any(full_text, ["日志", "审计"])
    if "安全防护缺失" in name:
        return not _contains_any(full_text, ["安全", "鉴权", "风控", "防刷", "加密", "越权"])
    if "数据来源不明确" in name:
        return _is_unspecified(dependencies)
    if "数据一致性风险" in name:
        return not _contains_any(full_text, ["一致性", "事务", "对账", "补偿"])
    return False


def run_stage2_rule_library_scan(
    stage1_output: Dict[str, Any],
    prd_text: str = "",
    composed_lines: Optional[List[Tuple[int, str]]] = None,
) -> List[Dict[str, Any]]:
    rules = _load_rule_library()
    defects: List[Dict[str, Any]] = []
    for i, rule in enumerate(rules, start=1):
        if _is_v2_rule(rule):
            if not _match_rule_v2(rule, stage1_output, prd_text or ""):
                continue
        else:
            if not _match_rule(rule, stage1_output):
                continue
        if not _is_rule_applicable(rule, stage1_output, prd_text):
            continue
        module = _rule_target_module(rule)
        rule_name = str(rule.get("rule_name") or rule.get("name") or "规则命中")
        rule_desc = str(rule.get("description") or "检测到规则风险").strip()
        rule_reason = str(rule.get("risk_reason") or rule.get("risk") or "规则库命中").strip()
        rule_id = str(rule.get("rule_id") or "").strip()
        defect = {
            "id": (rule_id or f"R{i:03d}"),
            "type": rule_name,
            "module": module,
            "description": rule_desc,
            "risk_level": _rule_risk_level(rule),
            "reason": rule_reason,
            "suggestion": "",
            "source": "rule",
        }
        defect["anchor"] = _find_anchor(
            prd_text,
            stage1_output,
            module,
            rule_name,
            rule_desc,
            composed_lines=composed_lines,
        )
        # v2: use rule suggestion if provided; else fallback dynamic suggestion
        suggestion = str(rule.get("suggestion") or "").strip()
        if suggestion:
            defect["suggestion"] = suggestion
        else:
            defect["suggestion"] = _dynamic_suggestion(module, rule_name, _rule_risk_level(rule), rule_desc)
        defects.append(defect)
    return defects


def _dedupe_defects(defects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for d in defects:
        key = (str(d.get("type") or ""), str(d.get("module") or ""), str(d.get("description") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def run_stage2_defect_scan(stage1_output: Dict[str, Any], llm_config_path: str, timeout: int = 90, prd_text: str = "", llm_config_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    trimmed_structure = _trim_stage1_for_stage2(stage1_output or {})
    # 紧凑 JSON（不缩进）减少 token
    structure_json = json.dumps(trimmed_structure, ensure_ascii=False, separators=(",", ":"))
    prompt = STAGE2_DEFECT_SCAN_PROMPT.replace("{structure_json}", structure_json)
    composed_lines = _compose_prd_lines(prd_text or "") if str(prd_text or "").strip() else []
    rule_defects = run_stage2_rule_library_scan(
        stage1_output or {},
        prd_text=prd_text or "",
        composed_lines=composed_lines,
    )
    llm_defects: List[Dict[str, Any]] = []
    hidden_assumptions: List[Dict[str, Any]] = []
    llm_scan_ok = True
    llm_error: Optional[str] = None
    try:
        resp = call_llm_with_retry(
            messages=[{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            timeout=timeout,
            max_retries=1
        )
        llm_defects, hidden_assumptions = _parse_llm_defects_response(resp or "")
    except Exception as e:
        logger.warning("run_stage2_defect_scan LLM 调用失败: %s", e)
        llm_scan_ok = False
        llm_error = str(e)
        # 占位项：仅用于工具栏提示与离线检测，source=system 不计入 LLM 扫描命中
        llm_defects = [
            {
                "id": "D001",
                "type": "扫描异常",
                "module": "扫描引擎",
                "description": "漏洞扫描阶段执行失败（大模型调用异常）",
                "risk_level": "P1",
                "reason": str(e),
                "suggestion": "检查 LLM 配置或重试扫描",
                "source": "system",
            }
        ]
    merged = _dedupe_defects(rule_defects + llm_defects)
    normalized = [_normalize_defect(d, i + 1) for i, d in enumerate(merged)]
    enriched = [_enrich_defect(d, stage1_output or {}, prd_text or "", composed_lines=composed_lines) for d in normalized]
    coverage = _build_coverage_matrix(stage1_output or {})
    scan_meta = {
        "llm_scan_ok": llm_scan_ok,
        "llm_error": llm_error,
        "llm_defects_parsed": len(llm_defects) if llm_scan_ok else 0,
        "rule_defects_count": len(rule_defects),
        "hidden_assumptions_count": len(hidden_assumptions),
    }
    
    return {"defects": enriched, "coverage": coverage, "scan_meta": scan_meta, "hidden_assumptions": hidden_assumptions}


def run_stage2_shift_left_analysis(stage1_output: Dict[str, Any], defects: List[Dict[str, Any]], llm_config_path: str, timeout: int = 120, llm_config_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    生成测试左移资产：测试数据、API契约、Mock、埋点建议。
    引入 defects 上下文，确保左移资产能够涵盖核心的高价值洞察（如幽灵录音、五维开关等）。
    """
    trimmed_structure = _trim_stage1_for_stage2(stage1_output or {})
    structure_json = json.dumps(trimmed_structure, ensure_ascii=False, separators=(",", ":"))
    
    from .pipeline import _merge_core_issues, _build_core_issue_title
    merged_defects = _merge_core_issues(defects or [])
    trimmed_defects = []
    for d in merged_defects[:5]:
        trimmed_defects.append({
            "core_issue": _build_core_issue_title(d),
            "description": d.get("description"),
        })
    defects_json = json.dumps(trimmed_defects, ensure_ascii=False, separators=(",", ":"))
    
    prompt = STAGE2_SHIFT_LEFT_ASSETS_PROMPT.replace("{structure_json}", structure_json).replace("{defects_json}", defects_json)
    
    try:
        resp = call_llm_with_retry(
            messages=[{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            timeout=timeout,
            max_retries=1
        )
        
        logger.error(f"DEBUG SHIFT LEFT RAW RESP:\n{resp}")
        
        # 尝试去掉 markdown 标记
        raw = (resp or "").strip()
        for pattern in (r"```json\s*([\s\S]*?)\s*```", r"```\s*([\s\S]*?)\s*```"):
            m = re.search(pattern, raw, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                break
                
        data = _extract_first_json_object(raw)
        if not isinstance(data, dict):
            return {}
            
        return {
            "test_data_advisor": data.get("test_data_advisor", []),
            "api_contracts": data.get("api_contracts", []),
            "observability_points": data.get("observability_points", [])
        }
    except Exception as e:
        logger.warning("run_stage2_shift_left_analysis LLM 调用失败: %s", e)
        return {}


def run_test_case_generation(stage1_output: Dict[str, Any], defects: List[Dict[str, Any]], llm_config_path: str, timeout: int = 150, llm_config_override: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    基于 PRD 结构化数据和审计缺陷，自动生成功能测试用例。
    这里也对 defects 进行精简过滤，只取最具业务价值的前 5 个核心洞察，避免凑数用例泛滥。
    """
    trimmed_structure = _trim_stage1_for_stage2(stage1_output or {})
    structure_json = json.dumps(trimmed_structure, ensure_ascii=False, separators=(",", ":"))
    
    # 将冗余的缺陷做去重与提纯处理，参考 L3 的 _merge_core_issues 思路
    from .pipeline import _merge_core_issues, _build_core_issue_title
    
    merged_defects = _merge_core_issues(defects or [])
    
    trimmed_defects = []
    # 只取最核心的前 5 个（即门面级别的业务洞察）
    for d in merged_defects[:5]:
        title = _build_core_issue_title(d)
        trimmed_defects.append({
            "type": d.get("type"),
            "core_issue": title,
            "description": d.get("description"),
            "suggestion": d.get("suggestion")
        })
        
    defects_json = json.dumps(trimmed_defects, ensure_ascii=False, separators=(",", ":"))
    
    prompt = TEST_CASE_GENERATION_PROMPT.replace("{structure_json}", structure_json).replace("{defects_json}", defects_json)
    
    try:
        resp = call_llm_with_retry(
            messages=[{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            config_override=llm_config_override,
            timeout=timeout,
            max_retries=1
        )
        
        logger.error(f"DEBUG TEST CASE RAW RESP:\n{resp}")
        
        # 因为我们要求大模型返回数组，所以尝试提取第一个 JSON 数组
        raw = (resp or "").strip()
        # 如果模型加了 markdown，去掉它
        for pattern in (r"```json\s*([\s\S]*?)\s*```", r"```\s*([\s\S]*?)\s*```"):
            m = re.search(pattern, raw, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                break
                
        # 尝试直接解析
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            # 找第一个 [ 和最后一个 ]
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(raw[start:end+1])
                    if isinstance(data, list):
                        return data
                except:
                    pass
                    
        # 兜底：如果用旧的 object 提取方式
        data = _extract_first_json_object(resp or "[]")
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.warning("run_test_case_generation LLM 调用失败: %s", e)
        return []


@dataclass
class PRDIssue:
    type: str
    message: str
    location: str = ""
    impact: int = 1
    probability: int = 1

    @property
    def risk_score(self) -> int:
        return self.impact * self.probability

    @property
    def risk_level(self) -> str:
        score = self.risk_score
        if score >= 8:
            return "high"
        if score >= 4:
            return "medium"
        return "low"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "location": self.location,
            "impact": self.impact,
            "probability": self.probability,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
        }


class PRDRuleEngine:
    def __init__(self, model: SystemModel):
        self.model = model
        self._transition_map: Dict[Tuple[str, str], Transition] = {
            (t.source, t.event): t for t in (model.transitions or [])
        }

    def detect_conflicts(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        rules = " ".join(self.model.rules or []).lower()
        if "优先级最高" in rules and ("被打断" in rules or "可被打断" in rules):
            issues.append(
                PRDIssue(
                    type="rule_conflict",
                    message="存在“优先级最高”同时又允许被打断的描述，可能存在规则冲突。",
                    location="规则描述",
                    impact=3,
                    probability=3,
                )
            )
        return issues

    def detect_state_missing(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        if not self.model.states or not self.model.events:
            return issues
        for s in self.model.states:
            for e in self.model.events:
                key = (s, e)
                if key not in self._transition_map:
                    issues.append(
                        PRDIssue(
                            type="state_missing",
                            message=f"状态“{s}”在事件“{e}”下的行为未在 PRD 中明确说明。",
                            location="状态机/切换规则",
                            impact=3,
                            probability=2,
                        )
                    )
        return issues

    def detect_resume_missing(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        rules_text = " ".join(self.model.rules or []).replace("\n", " ")
        if "打断" in rules_text and "恢复" not in rules_text and "回到" not in rules_text:
            issues.append(
                PRDIssue(
                    type="resume_missing",
                    message="PRD 中提到了“打断/中断”，但未说明恢复后的行为或返回状态。",
                    location="规则描述",
                    impact=3,
                    probability=2,
                )
            )
        return issues

    def detect_priority_cycle(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        graph: Dict[str, List[str]] = {}
        for rule in self.model.rules or []:
            m = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9_]+)\s*[>＞]\s*([\u4e00-\u9fa5A-Za-z0-9_]+)", rule)
            for a, b in m:
                graph.setdefault(a, []).append(b)

        if not graph:
            return issues

        visited: Dict[str, int] = {}

        def dfs(node: str) -> bool:
            visited[node] = 1
            for nei in graph.get(node, []):
                if visited.get(nei, 0) == 0:
                    if dfs(nei):
                        return True
                elif visited.get(nei) == 1:
                    return True
            visited[node] = 2
            return False

        has_cycle = False
        for node in graph.keys():
            if visited.get(node, 0) == 0 and dfs(node):
                has_cycle = True
                break

        if has_cycle:
            issues.append(
                PRDIssue(
                    type="priority_cycle",
                    message="优先级规则中存在环（例如 A>B, B>C, C>A），可能导致无法判定最终优先级。",
                    location="优先级规则",
                    impact=3,
                    probability=3,
                )
            )
        return issues

    def detect_concurrency(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        events = self.model.events or []
        if len(events) <= 1:
            return issues

        rules_text = " ".join(self.model.rules or []).replace("\n", " ")
        for i, e1 in enumerate(events):
            for j, e2 in enumerate(events):
                if i >= j:
                    continue
                if e1 in rules_text and e2 in rules_text:
                    continue
                issues.append(
                    PRDIssue(
                        type="concurrency_missing",
                        message=f"PRD 未说明事件“{e1}”与“{e2}”同时发生时的处理逻辑（并发风险）。",
                        location="并发事件处理",
                        impact=3,
                        probability=2,
                    )
                )
        return issues

    def detect_edge_cases(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        all_text = " ".join((self.model.rules or []) + (self.model.data_rules or [])).replace("\n", " ")
        keywords = ["异常", "失败", "超时", "断开", "重试", "错误码", "网络", "弱网"]
        if not any(k in all_text for k in keywords):
            issues.append(
                PRDIssue(
                    type="edge_cases_missing",
                    message="PRD 中几乎没有涉及异常/失败/超时/网络等边界条件的描述，建议补充异常流程。",
                    location="异常与边界规则",
                    impact=2,
                    probability=2,
                )
            )
        return issues

    def analyze(self) -> Dict[str, Any]:
        issues: List[PRDIssue] = []
        issues += self.detect_conflicts()
        issues += self.detect_state_missing()
        issues += self.detect_resume_missing()
        issues += self.detect_priority_cycle()
        issues += self.detect_concurrency()
        issues += self.detect_edge_cases()

        if not issues:
            engine_score = 10.0
        else:
            max_risk = max(i.risk_score for i in issues)
            engine_score = max(0.0, 10.0 - max_risk * 0.8)

        dimensions = {
            "completeness": 10.0,
            "flow": 10.0,
            "exceptions": 10.0,
            "data": 10.0,
            "testability": 10.0,
        }

        for iss in issues:
            t = iss.type
            if t in ("state_missing", "priority_cycle", "rule_conflict"):
                dimensions["flow"] -= 1.5
                dimensions["completeness"] -= 1.0
            elif t == "concurrency_missing":
                dimensions["flow"] -= 1.0
            elif t in ("resume_missing", "edge_cases_missing"):
                dimensions["exceptions"] -= 1.5

        for k, v in list(dimensions.items()):
            if v < 0.0:
                dimensions[k] = 0.0
            elif v > 10.0:
                dimensions[k] = 10.0

        weighted_score = sum(dimensions.values()) / float(len(dimensions)) if dimensions else engine_score

        return {
            "issues": [i.to_dict() for i in issues],
            "quality_score": round(engine_score, 1),
            "dimension_scores": {k: round(v, 1) for k, v in dimensions.items()},
            "weighted_score": round(weighted_score, 1),
        }
