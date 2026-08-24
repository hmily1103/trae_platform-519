# -*- coding: utf-8 -*-
"""源码只读关联 Agent（B2 + B3）。

B2 堆栈→源码定位：
- 从 context["problem_location"] 或 log_lines 中解析出 文件名:行号
- 用 SourceCodeIndex 查找源码文件并读取 ±15 行片段
- 挂进证据链新类别"源码证据"

B3 LLM 结合源码复判根因：
- 把源码片段 + 告警上下文喂给 LLM 做第二轮根因确认
- 有源码佐证时，finding.artifacts 带 confirmed=True/False + 复判理由
- 建议修复代码仅展示不落地（与 L4 现有口径一致）

安全红线：
- 只读：SourceCodeIndex 本身只读，本模块不含任何写操作
- 源码片段不写入工程文件，仅作为证据/LLM 输入
- LLM 建议的 patch 仅展示，不落地
"""
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from .base import AgentFinding, BaseAgent, STATUS_SKIPPED, STATUS_FAILED

logger = logging.getLogger(__name__)

# 与 selfheal.py 一致的堆栈帧正则（捕获完整文件名+行号）
_STACK_AT_RE = re.compile(r'\bat\s+[\w.$]+\(([\w$.]+\.(?:java|kt)):(\d+)\)')
# 从 problem_location / 日志行中提取 文件名:行号
_FILE_LINE_RE = re.compile(r'([\w$]+\.(?:java|kt)):(\d+)')

_SUMMARY_MAX = 80

# 哨兵：区分"未提供 llm_caller（走懒加载默认）"与"显式传 None（禁用 LLM）"
_NO_LLM = object()


def _clip(text: str, limit: int = _SUMMARY_MAX) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_stack_locations(
    problem_location: str = "",
    log_lines: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """从 problem_location 和日志行中解析出源码定位点。

    返回 [{"file": "PlayerManager.java", "line": 88, "source": "stack_frame"|"problem_location"}, ...]
    去重：同一 file:line 只保留第一个来源。
    """
    seen = set()
    results: List[Dict[str, Any]] = []

    # 1) problem_location 中的 文件名:行号
    if problem_location:
        for m in _FILE_LINE_RE.finditer(problem_location):
            key = (m.group(1), int(m.group(2)))
            if key not in seen:
                seen.add(key)
                results.append({"file": key[0], "line": key[1], "source": "problem_location"})

    # 2) 日志堆栈帧 at ...(Class.java:123)
    if log_lines:
        for line in log_lines:
            for m in _STACK_AT_RE.finditer(line or ""):
                key = (m.group(1), int(m.group(2)))
                if key not in seen:
                    seen.add(key)
                    results.append({"file": key[0], "line": key[1], "source": "stack_frame"})

    return results


class SourceCodeAdapter(BaseAgent):
    """源码只读关联 Agent（B2 定位 + B3 LLM 复判）。

    执行流程：
    1. 解析 problem_location / log_lines 提取源码定位点
    2. 用 SourceCodeIndex 查找文件并读取 ±15 行片段
    3. 把源码片段 + 告警上下文喂给 LLM 做第二轮根因确认（B3）
    4. 返回 AgentFinding：源码证据 + 复判结论

    降级策略：
    - 源码索引未启用 → skipped
    - 无堆栈定位点 → skipped
    - 找不到源码文件 → skipped（附原因）
    - LLM 复判失败 → 仍然返回 ok（源码片段本身是有价值的证据，复判只是增强）
    """

    name = "source_code"
    display_name = "源码关联"

    def __init__(
        self,
        index: Optional[Any] = None,
        llm_caller: Any = _NO_LLM,
        context_lines: int = 15,
    ):
        # index: SourceCodeIndex 实例（可注入替身）
        # llm_caller: call_llm 的替身；_NO_LLM=懒加载默认，None=显式禁用
        self._index = index
        self._llm_caller = llm_caller
        self._context_lines = context_lines

    def _get_index(self) -> Any:
        if self._index is not None:
            return self._index
        from .source_index import get_index
        return get_index()

    def _get_llm_caller(self) -> Optional[Callable[..., str]]:
        if self._llm_caller is not _NO_LLM:
            return self._llm_caller  # 显式传入（callable 或 None）
        try:
            from utils.llm_client import call_llm
            return call_llm
        except Exception:
            return None

    def run(self, alert: Dict[str, Any], context: Dict[str, Any]) -> AgentFinding:
        idx = self._get_index()
        if not idx.is_enabled():
            return self.skipped("源码关联未启用（未配置源码目录）")

        # 1) 解析源码定位点
        problem_location = str(context.get("problem_location") or "")
        log_lines: List[str] = list(context.get("log_lines") or [])
        locations = parse_stack_locations(problem_location, log_lines)

        if not locations:
            return self.skipped("未检测到堆栈/问题定位中的源码位置")

        # 2) 查找源码文件并读取片段
        snippets: List[Dict[str, Any]] = []
        not_found: List[str] = []
        for loc in locations[:5]:  # 最多取 5 个定位点
            hits = idx.find_files(loc["file"])
            if not hits:
                not_found.append("%s:%d" % (loc["file"], loc["line"]))
                continue
            # 取第一个匹配文件的片段
            snippet = idx.read_snippet(hits[0], loc["line"], context=self._context_lines)
            if snippet:
                snippets.append({
                    "file": snippet["file"],
                    "target_line": snippet["target_line"],
                    "start_line": snippet["start_line"],
                    "end_line": snippet["end_line"],
                    "lines": snippet["lines"],
                    "source": loc["source"],
                })

        if not snippets:
            detail = "未找到源码文件: " + ", ".join(not_found) if not_found else "源码片段读取失败"
            return self.skipped(detail)

        # 3) B3: LLM 结合源码复判根因
        review = self._llm_review(alert, context, snippets)

        # 4) 组装证据
        evidence: List[Dict[str, Any]] = []
        for snip in snippets[:3]:  # 证据链最多挂 3 条
            line_texts = "\n".join(
                "%4d  %s" % (l["lineno"], l["text"])
                for l in snip["lines"]
            )
            evidence.append({
                "kind": "source_code",
                "desc": "源码片段 %s:%d（%s）" % (
                    snip["file"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                    snip["target_line"],
                    "堆栈定位" if snip["source"] == "stack_frame" else "问题定位",
                ),
                "detail": line_texts,
            })

        summary_parts = ["关联到 %d 处源码" % len(snippets)]
        if not_found:
            summary_parts.append("（%d 处未找到）" % len(not_found))
        if review and review.get("confirmed") is True:
            summary_parts.append("，LLM 复判确认根因")
        elif review and review.get("confirmed") is False:
            summary_parts.append("，LLM 复判存疑")

        artifacts: Dict[str, Any] = {
            "snippets": snippets,
            "not_found": not_found,
            "review": review,
        }

        return self.ok(
            summary=_clip("".join(summary_parts)),
            evidence=evidence,
            artifacts=artifacts,
        )

    def _llm_review(
        self,
        alert: Dict[str, Any],
        context: Dict[str, Any],
        snippets: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """B3: 把源码片段 + 告警上下文喂给 LLM 做第二轮根因确认。

        返回 {"confirmed": bool, "reason": str, "suggested_fix": str} 或 None（LLM 不可用/失败）。
        建议修复代码仅展示不落地。
        """
        caller = self._get_llm_caller()
        if caller is None:
            return None

        # 组装源码上下文
        source_text = ""
        for snip in snippets[:3]:
            fname = snip["file"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            source_text += "\n--- %s (around line %d) ---\n" % (fname, snip["target_line"])
            for l in snip["lines"]:
                marker = " >>> " if l["is_target"] else "     "
                source_text += "%s%d  %s\n" % (marker, l["lineno"], l["text"])

        root_cause = str(context.get("root_cause") or "")
        alert_msg = str(alert.get("message") or alert.get("log_line") or "")
        alert_type = str(alert.get("type") or "")

        prompt = (
            "你是 Android 应用诊断专家。以下是崩溃/异常告警的根因分析和对应的源码片段。\n"
            "请判断源码片段是否能佐证该根因，并给出简短结论。\n\n"
            "【告警类型】%s\n"
            "【告警信息】%s\n"
            "【初步根因】%s\n"
            "【源码片段】\n%s\n\n"
            "请以 JSON 格式回答（不要包含 markdown 代码块标记）：\n"
            '{"confirmed": true/false, "reason": "简短说明源码是否佐证根因", '
            '"suggested_fix": "如有修复建议则给出代码片段，无则空字符串"}'
        ) % (alert_type, alert_msg[:500], root_cause[:500], source_text[:3000])

        try:
            import json
            raw = caller(
                messages=[{"role": "user", "content": prompt}],
                timeout=30,
                max_tokens=800,
            )
            if not raw:
                return None
            # 尝试解析 JSON（容错：LLM 可能包裹 markdown）
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            result = json.loads(text)
            return {
                "confirmed": bool(result.get("confirmed")),
                "reason": str(result.get("reason", "")),
                "suggested_fix": str(result.get("suggested_fix", "")),
            }
        except Exception as exc:
            logger.warning("[source_agent] LLM 复判失败(忽略): %s", exc)
            return None
