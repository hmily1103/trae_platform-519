# -*- coding: utf-8 -*-
"""
Stage4：测试点矩阵生成器
输入：Stage1 结构解析 + Stage2 漏洞扫描
输出：功能级测试矩阵（正常/异常/边界/并发/中断恢复）
纯增量模块，不修改现有 Stage1/2/3。
"""

from typing import Dict, Any, List, Tuple
import logging
import re

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _clean_text(value: Any) -> str:
    s = str(value or "")
    s = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", s)
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_module_name(value: Any) -> str:
    s = _clean_text(value)
    s = s.replace("", "").replace("【", "").replace("】", "")
    # Remove leading numbering like "1.", "i.", "(1)"
    s = re.sub(r"^(\d+\.|[iIvVxX]+\.|\(\d+\))\s*", "", s)
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9/_\-（）()：:、，,. ]", "", s)
    s = re.sub(r"\s+", " ", s).strip("：:，, ")
    return s


def _is_valid_module_name(name: str) -> bool:
    if not name or len(name) < 2:
        return False
    # Blocklist for generic terms and noise
    if name in ["PRD未说明", "要求", "注意", "说明", "备注", "简介", "概述", "背景", "目标", "功能", "规则"]:
        return False
    # Filter out long sentences or fragments
    if len(name) > 20: 
        return False
    # Filter out strings with sentence punctuation
    if any(c in name for c in [":", "：", ",", "，", "。", ";", "；"]):
        return False
    # Filter out property-like names
    if "优先级" in name:
        return False
    return True


def _is_unspecified(items: List[str]) -> bool:
    if not items:
        return True
    return all((x == "【PRD未说明】" or not x.strip()) for x in items)


def evaluate_test_matrix(test_matrix: Dict[str, Any], stage1_output: Dict[str, Any]) -> Dict[str, Any]:
    """
    对 Stage4 产物做“可用性评分”(0-10) 与明细。
    目标：用于产品化展示，不作为阻断条件。
    """
    tm = test_matrix or {}
    stage1 = stage1_output or {}

    function_matrix = tm.get("function_matrix") if isinstance(tm.get("function_matrix"), list) else []
    boundary_matrix = tm.get("boundary_matrix") if isinstance(tm.get("boundary_matrix"), list) else []
    concurrent_matrix = tm.get("concurrent_matrix") if isinstance(tm.get("concurrent_matrix"), dict) else {}
    permission_matrix = tm.get("permission_matrix") if isinstance(tm.get("permission_matrix"), list) else []
    state_matrix = tm.get("state_matrix") if isinstance(tm.get("state_matrix"), dict) else {}

    modules = _as_list(stage1.get("modules"))
    modules = [m for m in modules if m != "【PRD未说明】"]
    roles = _as_list(stage1.get("user_roles"))
    roles = [r for r in roles if r != "【PRD未说明】"]
    states = _as_list(stage1.get("states"))
    states = [s for s in states if s != "【PRD未说明】"]

    # 1) 功能矩阵覆盖
    target_modules = len(modules)
    shown_modules = len(function_matrix)
    module_coverage = 1.0 if target_modules <= 0 else min(1.0, shown_modules / float(min(target_modules, 6) or 1))
    s_function = 4.0 * module_coverage  # 0-4

    # 2) 边界覆盖项完整性（固定 6 项）
    expected_boundary = {"超时", "弱网/断网", "失败/错误码", "重试", "回滚/补偿", "幂等/重复提交"}
    got_boundary = {str(x.get("item") or "") for x in boundary_matrix if isinstance(x, dict)}
    completeness = len(expected_boundary & got_boundary) / 6.0
    covered_cnt = sum(1 for x in boundary_matrix if isinstance(x, dict) and bool(x.get("covered")))
    cover_ratio = covered_cnt / float(len(boundary_matrix) or 1)
    s_boundary = 2.0 * (0.6 * completeness + 0.4 * cover_ratio)  # 0-2

    # 3) 并发矩阵
    concurrent_cnt = len(list(concurrent_matrix.keys()))
    s_concurrent = 2.0 if concurrent_cnt >= 3 else (1.0 if concurrent_cnt >= 1 else 0.0)  # 0-2

    # 4) 权限矩阵（有角色则应输出）
    if roles:
        s_perm = 1.0 if permission_matrix else 0.0
    else:
        s_perm = 1.0  # 无角色时不扣分，但明细提示

    # 5) 状态矩阵（有 states 则应输出）
    if states:
        s_state = 1.0 if state_matrix else 0.0
    else:
        s_state = 1.0

    overall = round(min(10.0, s_function + s_boundary + s_concurrent + s_perm + s_state), 1)
    details = {
        "function_matrix": round(s_function, 1),
        "boundary_matrix": round(s_boundary, 1),
        "concurrent_matrix": round(s_concurrent, 1),
        "permission_matrix": round(s_perm, 1),
        "state_matrix": round(s_state, 1),
        "stats": {
            "modules_total": target_modules,
            "modules_shown": shown_modules,
            "boundary_items": len(boundary_matrix),
            "boundary_covered": covered_cnt,
            "concurrent_items": concurrent_cnt,
            "roles_total": len(roles),
            "states_total": len(states),
        },
        "notes": [],
    }
    if roles and not permission_matrix:
        details["notes"].append("存在用户角色但未生成权限矩阵。")
    if states and not state_matrix:
        details["notes"].append("存在状态列表但未生成状态矩阵。")
    if completeness < 1.0:
        details["notes"].append("边界/异常覆盖项不完整（建议补齐固定 6 项）。")
    return {"overall": overall, "details": details}


class TestMatrixGenerator:
    """从 Stage1/Stage2 生成测试矩阵，供测试团队使用。"""

    def __init__(self, stage1_output: Dict[str, Any], stage2_output: Dict[str, Any]):
        self.stage1 = stage1_output or {}
        self.stage2 = stage2_output or {}
        self.defects = self.stage2.get("defects") or []
        if not isinstance(self.defects, list):
            self.defects = []

    def generate(self) -> Dict[str, Any]:
        """生成完整测试矩阵，任意异常返回空结构，不抛错。"""
        try:
            return {
                "function_matrix": self._build_function_matrix(),
                "state_matrix": self._build_state_matrix(),
                "concurrent_matrix": self._build_concurrent_matrix(),
                "boundary_matrix": self._build_boundary_matrix(),
                "permission_matrix": self._build_permission_matrix(),
            }
        except Exception as e:
            logger.warning("TestMatrixGenerator.generate failed: %s", e)
            return {
                "function_matrix": [],
                "state_matrix": {},
                "concurrent_matrix": {},
                "boundary_matrix": [],
                "permission_matrix": [],
            }

    def _build_function_matrix(self) -> List[Dict[str, Any]]:
        modules = _as_list(self.stage1.get("modules"))
        modules = [_normalize_module_name(m) for m in modules]
        modules = [m for m in modules if _is_valid_module_name(m)]
        modules = list(dict.fromkeys(modules))[:12]
        if not modules:
            return []

        test_types = ["正常流程", "异常流程", "边界条件", "并发场景", "中断恢复"]
        type_keywords = {
            "正常流程": ["流程", "主流程"],
            "异常流程": ["异常", "失败", "错误", "重试"],
            "边界条件": ["边界", "上限", "下限", "范围", "最大", "最小"],
            "并发场景": ["并发", "同时", "抢占", "幂等", "重复"],
            "中断恢复": ["中断", "打断", "恢复", "回滚"],
        }
        risk_default = {
            "正常流程": "P2",
            "异常流程": "P0",
            "边界条件": "P1",
            "并发场景": "P1",
            "中断恢复": "P0",
        }
        generic_expected = {
            "正常流程": "主流程能成功闭环，状态与输出符合预期。",
            "异常流程": "异常时返回可识别错误与可执行提示，不出现静默失败。",
            "边界条件": "边界输入被正确拦截或处理，系统不崩溃且状态一致。",
            "并发场景": "并发触发时有明确裁决顺序，结果具备幂等性与一致性。",
            "中断恢复": "中断后可恢复到可控状态，不丢关键上下文与资源。",
        }
        all_text = " ".join(
            _as_list(self.stage1.get("flows"))
            + _as_list(self.stage1.get("business_rules"))
            + _as_list(self.stage1.get("exceptions"))
            + _as_list(self.stage1.get("edge_cases"))
            + _as_list(self.stage1.get("actions"))
        )
        all_text = _clean_text(all_text)

        matrix = []
        for idx, module in enumerate(modules, start=1):
            row = {"module": module, "test_types": {}}
            module_related_text = []
            for f in _as_list(self.stage1.get("flows")):
                fs = _clean_text(f)
                if module in fs:
                    module_related_text.append(fs)
            for r in _as_list(self.stage1.get("business_rules")):
                rs = _clean_text(r)
                if module in rs:
                    module_related_text.append(rs)
            source_text = " ".join(module_related_text) if module_related_text else all_text
            source_text = _clean_text(source_text)
            for tt_idx, tt in enumerate(test_types, start=1):
                kws = type_keywords.get(tt, [])
                matched_defects: List[Dict[str, Any]] = []
                for d in self.defects:
                    if not isinstance(d, dict):
                        continue
                    d_module = _normalize_module_name(d.get("module") or "")
                    d_type = _clean_text(d.get("type") or "")
                    d_desc = _clean_text(d.get("description") or "")
                    same_module = (d_module and (d_module == module or module in d_module or d_module in module))
                    keyword_hit = any(k in d_type or k in d_desc for k in kws)
                    if same_module and keyword_hit:
                        matched_defects.append(d)
                has_defect = len(matched_defects) > 0
                has_evidence = any(k in source_text for k in kws)
                if has_defect:
                    first = matched_defects[0]
                    risk_level = str(first.get("risk_level") or risk_default[tt]).upper()
                    status = "缺失"
                    suggestion = _clean_text(first.get("suggestion") or "建议补充该场景测试规则与验收标准")
                    evidence = _clean_text(first.get("description") or first.get("type") or "")
                elif has_evidence:
                    risk_level = "P2"
                    status = "覆盖"
                    suggestion = ""
                    evidence = "PRD 已出现相关规则描述"
                else:
                    risk_level = risk_default[tt]
                    status = "待确认"
                    suggestion = "PRD 未明确该测试维度，建议补充流程、触发条件和验收标准"
                    evidence = "未检索到明确规则"
                row["test_types"][tt] = {
                    "status": status,
                    "risk_level": risk_level,
                    "case_id": f"TC-{idx:02d}-{tt_idx:02d}",
                    "expected": generic_expected[tt],
                    "evidence": evidence,
                    "suggestion": suggestion,
                }
            matrix.append(row)
        return matrix

    def _build_state_matrix(self) -> Dict[str, Any]:
        """状态维度：是否有入口/出口（基于 states/flows 文本推断，无 transitions 时简化）。"""
        states = _as_list(self.stage1.get("states"))
        states = [s for s in states if s != "【PRD未说明】"]
        if not states:
            return {}

        flows_text = " ".join(_as_list(self.stage1.get("flows")) + _as_list(self.stage1.get("business_rules")))
        matrix = {}
        for state in states:
            mentioned = state in flows_text
            matrix[state] = {
                "has_entry": mentioned,
                "has_exit": mentioned,
                "test_priority": "P1" if not mentioned else "P2",
                "note": "状态未在流程/规则中提及，建议补充转换说明" if not mentioned else "",
            }
        return matrix

    def _build_concurrent_matrix(self) -> Dict[str, Any]:
        """并发场景：多事件/多流程同时触发的覆盖。"""
        flows = _as_list(self.stage1.get("flows"))
        rules = _as_list(self.stage1.get("business_rules"))
        events = []
        for f in flows:
            if f != "【PRD未说明】" and len(f) > 1:
                events.append(f[:30])
        for r in rules:
            if "优先" in r or "同时" in r or "打断" in r:
                for part in r.replace("、", " ").split():
                    if len(part) >= 2 and part not in events:
                        events.append(part[:20])
        events = list(dict.fromkeys(events))[:8]

        if len(events) < 2:
            return {}

        matrix = {}
        for i, e1 in enumerate(events):
            for j, e2 in enumerate(events):
                if i >= j:
                    continue
                key = f"{e1} + {e2}"
                has_rule = any(
                    e1 in r and e2 in r for r in rules
                ) or any("并发" in str(d.get("type") or "") for d in self.defects)
                matrix[key] = {
                    "status": "已定义" if has_rule else "未定义",
                    "risk_level": "P1" if not has_rule else "P2",
                    "suggestion": "需补充并发裁决规则" if not has_rule else "",
                }
        return matrix

    def _build_boundary_matrix(self) -> List[Dict[str, Any]]:
        """边界/异常覆盖项。"""
        exceptions = _as_list(self.stage1.get("exceptions"))
        edge_cases = _as_list(self.stage1.get("edge_cases"))
        items = [
            ("超时", ["超时", "timeout"]),
            ("弱网/断网", ["弱网", "断网", "网络"]),
            ("失败/错误码", ["失败", "错误", "错误码"]),
            ("重试", ["重试", "retry"]),
            ("回滚/补偿", ["回滚", "补偿", "撤销"]),
            ("幂等/重复提交", ["幂等", "去重", "重复提交", "重复点击"]),
        ]
        text = " ".join(exceptions + edge_cases)
        matrix = []
        for name, kws in items:
            covered = any(k in text for k in kws)
            matrix.append({
                "item": name,
                "covered": covered,
                "suggestion": "" if covered else "建议在 PRD 中补充说明",
            })
        return matrix

    def _build_permission_matrix(self) -> List[Dict[str, Any]]:
        """角色 × 模块权限（简化：仅列出角色与模块，权限待确认）。"""
        roles = _as_list(self.stage1.get("user_roles"))
        modules = _as_list(self.stage1.get("modules"))
        roles = [r for r in roles if r != "【PRD未说明】"]
        modules = [m for m in modules if m != "【PRD未说明】"]
        if not roles:
            return []
        matrix = []
        for role in roles[:10]:
            matrix.append({
                "role": role,
                "modules": modules[:8],
                "note": "权限需在 PRD 中明确" if not _as_list(self.stage1.get("permissions")) else "",
            })
        return matrix
