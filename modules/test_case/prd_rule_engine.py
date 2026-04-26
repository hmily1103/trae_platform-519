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
from typing import List, Dict, Any, Tuple

from .system_model import SystemModel, Transition
from utils.llm_client import call_llm

logger = logging.getLogger(__name__)
RULE_LIBRARY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prd_scan_rules.json")

STAGE2_DEFECT_SCAN_PROMPT = """
你是一名拥有10年经验的测试架构师。

你的任务是基于结构化 PRD 模型进行需求漏洞扫描。

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

规则：
1. 严禁臆测需求
2. 未说明内容必须标记【PRD未说明】
3. 每个问题必须定位到模块或业务规则
4. 风险等级分为 P0/P1/P2

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
  ]
}

PRD结构信息：
{structure_json}
"""


def _extract_json_dict(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _normalize_risk_level(value: str) -> str:
    v = (value or "").strip().upper()
    if v in {"P0", "P1", "P2"}:
        return v
    if v in {"HIGH", "CRITICAL"}:
        return "P0"
    if v in {"MEDIUM"}:
        return "P1"
    return "P2"


def _normalize_defect(item: Dict[str, Any], idx: int) -> Dict[str, Any]:
    d_type = str(item.get("type") or "未分类问题").strip()
    module = str(item.get("module") or "【PRD未说明】").strip()
    anchor = str(item.get("anchor") or "").strip()
    desc = str(item.get("description") or "【PRD未说明】").strip()
    reason = str(item.get("reason") or "【PRD未说明】").strip()
    suggestion = str(item.get("suggestion") or "【PRD未说明】").strip()
    risk_level = _normalize_risk_level(str(item.get("risk_level") or "P2"))
    source = str(item.get("source") or "llm").strip().lower()
    if source not in {"rule", "llm", "hybrid"}:
        source = "llm"
    defect_id = str(item.get("id") or f"D{idx:03d}").strip()
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


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _compose_prd_lines(prd_text: str) -> List[Tuple[int, str]]:
    lines = []
    for idx, line in enumerate((prd_text or "").splitlines(), start=1):
        s = _normalize_text(line)
        if s:
            lines.append((idx, s))
    return lines


def _find_anchor(prd_text: str, stage1_output: Dict[str, Any], module: str, defect_type: str, description: str) -> str:
    lines = _compose_prd_lines(prd_text)
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
    for i, line in lines:
        if any(k in line for k in uniq):
            return f"L{i}: {line[:120]}"
    first_feature = next((x for x in modules + flows + states if x and x != "【PRD未说明】"), "")
    if first_feature:
        return f"功能：{first_feature}"
    i, line = lines[0]
    return f"L{i}: {line[:120]}"


def _dynamic_suggestion(module: str, defect_type: str, risk_level: str, description: str) -> str:
    m = str(module or "")
    t = str(defect_type or "")
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
    if "异常" in t or "边界" in t:
        return "补充失败、超时、重试、回滚和降级策略，并给出用户提示与状态恢复规则。"
    if "安全" in t:
        return "补充防刷、防作弊、鉴权与风控策略，明确触发条件、拦截动作与告警机制。"
    if "外部依赖" in t:
        return "补充外部依赖清单、可用性要求和故障降级方案，明确依赖不可用时的回退逻辑。"
    if risk_level == "P0":
        return f"针对{m or '该模块'}先冻结实现口径，补齐可执行规则后再进入开发。"
    return f"针对{m or '该模块'}补充“{t or '该问题'}”的可执行规则、边界条件和验收标准。"


def _should_replace_suggestion(suggestion: str) -> bool:
    s = str(suggestion or "").strip()
    if not s:
        return True
    patterns = ["相关约束", "并明确模块、边界与异常处理", "建议补充", "该规则"]
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


def _enrich_defect(defect: Dict[str, Any], stage1_output: Dict[str, Any], prd_text: str) -> Dict[str, Any]:
    d = dict(defect)
    d["anchor"] = str(d.get("anchor") or "").strip() or _find_anchor(
        prd_text, stage1_output, str(d.get("module") or ""), str(d.get("type") or ""), str(d.get("description") or "")
    )
    if _should_replace_suggestion(str(d.get("suggestion") or "")):
        d["suggestion"] = _dynamic_suggestion(
            str(d.get("module") or ""),
            str(d.get("type") or ""),
            str(d.get("risk_level") or ""),
            str(d.get("description") or ""),
        )
    return d


def _load_rule_library() -> List[Dict[str, Any]]:
    try:
        if not os.path.exists(RULE_LIBRARY_FILE):
            return []
        with open(RULE_LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get("rules") if isinstance(data, dict) else []
        if not isinstance(rules, list):
            return []
        return [r for r in rules if isinstance(r, dict) and r.get("enabled", True)]
    except Exception as e:
        logger.warning("load rule library failed: %s", e)
        return []


def _rule_risk_level(rule: Dict[str, Any]) -> str:
    category = str(rule.get("category") or "").strip()
    core = bool(rule.get("core"))
    if core and category in {"state_machine", "flow"}:
        return "P0"
    if core:
        return "P1"
    return "P2"


def _rule_target_module(rule: Dict[str, Any]) -> str:
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
        return _is_unspecified(exceptions)
    if "中断流程缺失" in name:
        return not _contains_any(full_text, ["中断", "打断", "退出", "恢复"])
    if "规则边界缺失" in name or "边界条件缺失" in name:
        return _is_unspecified(edge_cases)
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


def run_stage2_rule_library_scan(stage1_output: Dict[str, Any], prd_text: str = "") -> List[Dict[str, Any]]:
    rules = _load_rule_library()
    defects: List[Dict[str, Any]] = []
    for i, rule in enumerate(rules, start=1):
        if not _match_rule(rule, stage1_output):
            continue
        if not _is_rule_applicable(rule, stage1_output, prd_text):
            continue
        module = _rule_target_module(rule)
        defect = {
            "id": f"R{i:03d}",
            "type": str(rule.get("name") or "规则命中"),
            "module": module,
            "description": str(rule.get("description") or "检测到规则风险").strip(),
            "risk_level": _rule_risk_level(rule),
            "reason": str(rule.get("risk") or "规则库命中").strip(),
            "suggestion": "",
            "source": "rule",
        }
        defect["anchor"] = _find_anchor(
            prd_text, stage1_output, module, str(rule.get("name") or ""), str(rule.get("description") or "")
        )
        defect["suggestion"] = _dynamic_suggestion(
            module, str(rule.get("name") or ""), _rule_risk_level(rule), str(rule.get("description") or "")
        )
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


def run_stage2_defect_scan(stage1_output: Dict[str, Any], llm_config_path: str, timeout: int = 90, prd_text: str = "") -> Dict[str, Any]:
    structure_json = json.dumps(stage1_output or {}, ensure_ascii=False, indent=2)
    prompt = STAGE2_DEFECT_SCAN_PROMPT.replace("{structure_json}", structure_json)
    rule_defects = run_stage2_rule_library_scan(stage1_output or {}, prd_text=prd_text or "")
    llm_defects: List[Dict[str, Any]] = []
    try:
        resp = call_llm(
            [{"role": "user", "content": prompt}],
            config_path=llm_config_path,
            stream=False,
            timeout=timeout,
        )
        data = _extract_json_dict(resp)
        raw_defects = data.get("defects") if isinstance(data, dict) else []
        if not isinstance(raw_defects, list):
            raw_defects = []
        llm_defects = [_normalize_defect(x if isinstance(x, dict) else {}, i + 1) for i, x in enumerate(raw_defects)]
    except Exception as e:
        logger.warning("run_stage2_defect_scan failed: %s", e)
        llm_defects = [
            {
                "id": "D001",
                "type": "扫描异常",
                "module": "扫描引擎",
                "description": "漏洞扫描阶段执行失败",
                "risk_level": "P1",
                "reason": str(e),
                "suggestion": "检查 LLM 配置或重试扫描",
                "source": "llm",
            }
        ]
    merged = _dedupe_defects(rule_defects + llm_defects)
    normalized = [_normalize_defect(d, i + 1) for i, d in enumerate(merged)]
    enriched = [_enrich_defect(d, stage1_output or {}, prd_text or "") for d in normalized]
    return {"defects": enriched}


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
        # 方便查询的索引
        self._transition_map: Dict[Tuple[str, str], Transition] = {
            (t.source, t.event): t for t in (model.transitions or [])
        }

    # 1. 简单规则冲突检测：基于关键字对
    def detect_conflicts(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        rules = " ".join(self.model.rules or []).lower()
        # 示例：一个规则说「最高优先级」，另一个说「允许被打断」
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

    # 2. 状态机缺失：States × Events 未定义
    def detect_state_missing(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        if not self.model.states or not self.model.events:
            return issues
        for s in self.model.states:
            for e in self.model.events:
                key = (s, e)
                if key not in self._transition_map:
                    # 未定义，不一定都是高风险，但至少是“待确认”
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

    # 3. 打断/恢复机制检测
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

    # 4. 优先级闭环检测（简单拓扑排序）
    def detect_priority_cycle(self) -> List[PRDIssue]:
        issues: List[PRDIssue] = []
        # 从规则中提取 “A > B” 形式的优先级关系（V1 简单正则/字符串匹配）
        import re

        graph: Dict[str, List[str]] = {}
        for rule in self.model.rules or []:
            m = re.findall(r"([\u4e00-\u9fa5A-Za-z0-9_]+)\s*[>＞]\s*([\u4e00-\u9fa5A-Za-z0-9_]+)", rule)
            for a, b in m:
                graph.setdefault(a, []).append(b)

        if not graph:
            return issues

        visited: Dict[str, int] = {}  # 0: 未访问, 1: 访问中, 2: 已完成

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

    # 5. 并发风险：Events × Events 未在规则中说明
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
                # 如果规则中没有同时提到这两个事件，认为并发行为未说明
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

    # 6. 边界条件/异常缺失（关键字缺失）
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

    # 汇总分析
    def analyze(self) -> Dict[str, Any]:
        issues: List[PRDIssue] = []
        issues += self.detect_conflicts()
        issues += self.detect_state_missing()
        issues += self.detect_resume_missing()
        issues += self.detect_priority_cycle()
        issues += self.detect_concurrency()
        issues += self.detect_edge_cases()

        # 总体评分：简单按最高风险与数量给个 0-10 分（engine_score）
        if not issues:
            engine_score = 10.0
        else:
            max_risk = max(i.risk_score for i in issues)
            # 风险越高，分数越低，做个线性压缩
            engine_score = max(0.0, 10.0 - max_risk * 0.8)

        # 维度化评分（用于页面展示与权重制）：完整度 / 流程 / 异常 / 数据 / 可测试性
        dimensions = {
            "completeness": 10.0,  # 覆盖度：场景/状态/流程是否齐全
            "flow": 10.0,          # 流程与状态机一致性
            "exceptions": 10.0,    # 异常与边界处理
            "data": 10.0,          # 数据与字段定义（预留，当前规则较少）
            "testability": 10.0,   # 可测试性（预留，当前规则较少）
        }

        for iss in issues:
            t = iss.type
            # 状态机与流程相关问题：影响 completeness 与 flow
            if t in ("state_missing", "priority_cycle", "rule_conflict"):
                dimensions["flow"] -= 1.5
                dimensions["completeness"] -= 1.0
            elif t == "concurrency_missing":
                dimensions["flow"] -= 1.0
            # 异常与边界相关
            elif t in ("resume_missing", "edge_cases_missing"):
                dimensions["exceptions"] -= 1.5
            # 预留：未来可根据类型扩展 data/testability 扣分

        # 截断到 [0, 10]
        for k, v in list(dimensions.items()):
            if v < 0.0:
                dimensions[k] = 0.0
            elif v > 10.0:
                dimensions[k] = 10.0

        # 维度权重制：当前各 20%，未来可做成可配置
        weighted_score = sum(dimensions.values()) / float(len(dimensions)) if dimensions else engine_score

        return {
            "issues": [i.to_dict() for i in issues],
            "quality_score": round(engine_score, 1),
            "dimension_scores": {k: round(v, 1) for k, v in dimensions.items()},
            "weighted_score": round(weighted_score, 1),
        }

