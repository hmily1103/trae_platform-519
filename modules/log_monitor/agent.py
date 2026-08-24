#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志分析 Agent - 基于 LLM 的智能日志分析
提供根因分析、排查建议、摘要生成
支持 Gemini、DeepSeek、OpenAI 等（通过 utils.llm_client）
"""

import os
import re
import json
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

from utils.logger import setup_logger
from utils.llm_client import call_llm, load_llm_config

logger = setup_logger('log_agent')


@dataclass
class AnalysisResult:
    """分析结果

    L3 诊断决策结构化（问题定位 / 影响判断 / 排查路径）在此体现，
    让 Agent 从「看懂日志」升级为「感知→判断→决策」：
    - problem_location: 尽量精确到代码行（堆栈 at ...(Class.java:123)）
    - impact: 站在「影响谁、影响多大」角度判断，而非只描述技术原因
    - investigation_path: 有序排查路径，从确认现场到定位代码再到验证修复
    """
    root_cause: str
    suggestions: List[str]
    summary: str
    problem_location: str = ""          # L3 诊断决策：问题定位（类/方法/代码行）
    impact: str = ""                    # L3 诊断决策：影响判断（受影响功能/阻断程度/用户影响）
    investigation_path: List[str] = field(default_factory=list)  # L3 诊断决策：有序排查路径
    suggested_patch: str = ""          # L4 自动生成 Patch：针对根因的修复代码建议（仅片段，不落地）
    raw_response: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'root_cause': self.root_cause,
            'suggestions': self.suggestions,
            'summary': self.summary,
            'problem_location': self.problem_location,
            'impact': self.impact,
            'investigation_path': self.investigation_path,
            'suggested_patch': self.suggested_patch,
        }


class LogAnalysisAgent:
    """日志分析 Agent"""

    SYSTEM_PROMPT = """你是一个专业的 Android 应用日志分析专家。你的任务是根据提供的 logcat 日志，完成「诊断决策」：定位问题、判断影响、给出有序排查路径。

请用中文回答，输出格式必须严格遵循以下 JSON 结构（不要包含其他文字或 markdown 标记）：
{
  "root_cause": "简要描述根本原因（1-3句话）",
  "problem_location": "问题定位：从堆栈 at com.xxx.Class.method(Class.java:行号) 提取最具体的出错位置（类/方法/代码行），不要泛泛而谈'空指针异常'",
  "impact": "影响判断：受影响的业务功能/模块、是否阻断主流程、用户影响面、严重程度评估（1-3句话）",
  "investigation_path": ["排查步骤1（具体可操作）", "排查步骤2", "排查步骤3"],
  "suggestions": ["修复/规避建议1", "修复/规避建议2", "修复/规避建议3"],
  "suggested_patch": "针对根因的最小可落地修复代码片段（仅片段，不要整文件；若无法给出有效修复则填空字符串 ''，不要编造）",
  "summary": "一句话总结"
}

要求：
- root_cause: 结合日志上下文推断最可能的根本原因
- problem_location: 必须尽量精确到代码行（堆栈中的 at ...(Class.java:123)）；若日志无堆栈则说明证据不足，不要编造
- impact: 站在「这个故障影响谁、影响多大」的角度判断，而非只复述技术原因
- investigation_path: 3-5 步有序排查路径，从「复现/确认现场」到「定位代码」再到「验证修复」
- suggestions: 3-5 条具体可操作的修复/规避建议
- suggested_patch: 给出最小可落地的修复代码建议（如加判空保护、加 try-catch、修正空集合处理、补全初始化），语言需与项目一致（Java 用 Java 片段、Kotlin 用 Kotlin 片段）；可附 1-2 行中文注释说明意图；若根因不足以支撑可靠修复，填空字符串，严禁编造代码
- summary: 不超过 50 字的总结
"""

    # 按告警类型定制的额外提示（在通用 prompt 基础上追加）
    PROMPT_BY_TYPE = {
        'anr': """
重点关注：主线程阻塞、死锁、耗时操作跑在主线程、Binder 超时、系统服务响应慢。
排查方向：检查主线程是否有网络/IO、锁竞争、ANR 堆栈中的 blocked/waiting 状态。
""",
        'crash': """
重点关注：堆栈信息、异常类型、崩溃线程、native crash 的 signal 和 backtrace。
排查方向：从堆栈定位崩溃代码行、检查空指针/数组越界/类型转换、native 层内存问题。
""",
        'exception': """
重点关注：异常类型、堆栈调用链、异常抛出位置。
排查方向：根据异常类型（NPE/IllegalState/IOException 等）定位根因代码。
""",
        'keyword': """
根据触发告警的关键词，结合日志上下文分析根因。若为 OutOfMemoryError，重点关注内存泄漏、大对象分配、Bitmap 未回收。
""",
    }

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path

    def _call_llm(self, messages: List[Dict], timeout: int = 30) -> str:
        """调用 LLM API（非流式），优先使用 Gemini"""
        return call_llm(
            messages,
            config_path=self.config_path,
            stream=False,
            timeout=timeout
        )

    def _parse_response(self, content: str) -> AnalysisResult:
        """解析 LLM 返回的 JSON"""
        content = content.strip()
        # 去除可能的 markdown 代码块
        if content.startswith('```'):
            lines = content.split('\n')
            if lines[0].startswith('```'):
                lines = lines[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            content = '\n'.join(lines)
        try:
            obj = json.loads(content)
            return AnalysisResult(
                root_cause=obj.get('root_cause', '无法解析'),
                problem_location=obj.get('problem_location', '') or '',
                impact=obj.get('impact', '') or '',
                investigation_path=obj.get('investigation_path', []) or [],
                suggested_patch=obj.get('suggested_patch', '') or '',
                suggestions=obj.get('suggestions', []) or [],
                summary=obj.get('summary', ''),
                raw_response=content
            )
        except json.JSONDecodeError as e:
            logger.warning(f'LLM 返回非 JSON 格式: {content[:200]}...', exc_info=True)
            return AnalysisResult(
                root_cause='解析失败',
                problem_location='',
                impact='',
                investigation_path=[],
                suggested_patch='',
                suggestions=['请检查 LLM 返回格式'],
                summary=content[:200] if content else '无内容',
                raw_response=content
            )

    def analyze(
        self,
        log_lines: List[str],
        alert_context: Optional[Dict[str, Any]] = None
    ) -> AnalysisResult:
        """
        分析日志，返回根因和排查建议

        :param log_lines: 日志行列表（建议包含告警前后各 10-20 行）
        :param alert_context: 可选，告警上下文 {rule_name, severity, log_line, type}
        :return: AnalysisResult
        """
        # 限制日志长度，避免超出 token 限制。
        # #23 按告警类型分级限长：crash 需保留完整堆栈，anr/oom 需更大上下文。
        _MAX_LINES_BY_TYPE = {'crash': 150, 'anr': 120, 'oom': 100, 'exception': 60}
        _atype = ''
        if alert_context:
            _atype = (alert_context.get('type') or '').lower()
        max_lines = _MAX_LINES_BY_TYPE.get(_atype, 50)
        if len(log_lines) > max_lines:
            # 保头保尾截中间：头部通常含异常首行/FATAL 头，尾部含最新堆栈与告警行
            head = max_lines // 3
            tail = max_lines - head
            log_lines = (
                log_lines[:head]
                + ['... (中间日志已按长度限制截断) ...']
                + log_lines[-tail:]
            )
        log_text = '\n'.join(log_lines)

        alert_info = ''
        type_hint = ''
        if alert_context:
            alert_type = (alert_context.get('type') or '').lower()
            type_hint = self.PROMPT_BY_TYPE.get(alert_type, '')
            alert_info = f"""
【告警信息】
- 规则: {alert_context.get('rule_name', '')}
- 严重程度: {alert_context.get('severity', '')}
- 类型: {alert_context.get('type', '')}
- 触发日志: {alert_context.get('log_line', '')[:300]}
"""
            if type_hint:
                alert_info += f"\n【分析要点】{type_hint}"
            # L2 上下文收集：注入设备环境，让 LLM 结合机型/系统/版本分析，而非盲猜
            dev = alert_context.get('device_context')
            if dev:
                dev_text = "\n".join(
                    f"- {k}: {v}" for k, v in dev.items() if v
                )
                if dev_text:
                    alert_info += f"\n【设备环境】\n{dev_text}"
            # RAG：注入历史相似案例，帮助 LLM 借鉴既往根因（不影响无案例时的行为）
            hist = alert_context.get('historical_cases')
            if hist:
                cases_text = "\n".join(
                    f"- 类型:{c.get('alert_type','')} 根因:{c.get('root_cause','')} "
                    f"建议:{' / '.join(c.get('suggestions', [])[:2])}"
                    for c in hist[:3]
                )
                if cases_text:
                    alert_info += (
                        f"\n【历史相似案例参考（仅作借鉴，不代表本次结论）】\n{cases_text}"
                    )
        user_content = f"""请分析以下 Android logcat 日志，找出异常或错误的根本原因，并给出排查建议。
{alert_info}
【日志内容】
```
{log_text}
```
"""
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
        try:
            response = self._call_llm(messages, timeout=60)
            result = self._parse_response(response)
        except Exception as e:
            logger.exception(f'LLM 调用失败: {e}')
            raise

        # L3 诊断决策兜底：模型未给出定位时，从堆栈自动提取最贴近崩溃的应用代码行；
        # 模型未给影响判断时，按告警类型给一句具体影响描述。
        # 目的：保证「诊断决策」块始终有精确内容（即便模型偷懒只回旧 schema）。
        if not result.problem_location:
            loc = self._extract_stack_location(log_lines)
            if loc:
                result.problem_location = loc
        if not result.impact and alert_context:
            result.impact = self._default_impact(
                alert_context.get('type', ''), alert_context.get('severity', '')
            )
        return result

    # 匹配 at com.x.Y.z(Y.java:123) 形式的堆栈帧
    _STACK_RE = re.compile(
        r'at\s+([a-zA-Z0-9_$.]+)\.([a-zA-Z0-9_$]+)\(([A-Za-z0-9_$.]+\.java):(\d+)\)'
    )

    @classmethod
    def _extract_stack_location(cls, log_lines: List[str]) -> str:
        """从日志堆栈中提取最贴近崩溃的应用代码行（优先非系统栈）。"""
        frames = []
        for line in (log_lines or []):
            for m in cls._STACK_RE.finditer(line or ''):
                frames.append((m.group(1), m.group(2), m.group(3), m.group(4)))
        if not frames:
            return ""
        # 优先应用自有代码（排除 android/java/com.android/dalvik/javax 系统栈）
        sys_prefixes = ('android.', 'java.', 'com.android.', 'dalvik.', 'javax.')
        app_frames = [f for f in frames if not f[0].startswith(sys_prefixes)]
        chosen = app_frames[0] if app_frames else frames[0]
        return f"{chosen[0]}.{chosen[1]}() ({chosen[2]}:{chosen[3]})"

    @staticmethod
    def _default_impact(alert_type: str, severity: str) -> str:
        """按告警类型给一句具体影响描述（模型未提供时的兜底）。"""
        t = (alert_type or '').lower()
        base = {
            'crash': '应用进程崩溃终止，触发当前操作的用户将遇到闪退/黑屏，相关功能完全不可用。',
            'anr': '主线程阻塞超阈值，当前界面失去响应，用户无法操作，持续将弹出 ANR 对话框。',
            'exception': '未捕获异常可能导致该功能模块异常退出或数据不一致，视调用位置可能影响主流程。',
            'oom': '内存不足，系统可能杀掉进程或引发连锁崩溃，低内存设备上更易复现。',
            'keyword': '命中关键词告警，需结合上下文判断是否影响功能；性能类关键词可能影响流畅度。',
        }.get(t, '异常可能影响相关功能稳定性，需结合日志进一步判断。')
        if severity == 'high':
            base += '（当前为高危，建议优先处理。）'
        return base

    def generate_runbook(
        self,
        alert_type: str,
        log_snippet: Optional[str] = None,
    ) -> str:
        """
        根据告警类型生成简易排查 runbook（3～5 步），面向开发/测试。
        :param alert_type: anr / crash / exception / keyword 等
        :param log_snippet: 可选，触发日志片段，便于 runbook 更贴切
        :return: runbook 正文
        """
        type_desc = {
            'anr': 'ANR（应用无响应）',
            'crash': '应用崩溃',
            'exception': '未捕获异常',
            'keyword': '关键词告警',
        }.get((alert_type or '').lower(), alert_type or '告警')
        hint = self.PROMPT_BY_TYPE.get((alert_type or '').lower(), '')
        user_content = f"""请针对「{type_desc}」类告警，生成一份简短的排查 runbook，面向开发/测试人员。
要求：3～5 步，每步一句话，具体可操作。用中文，不要 markdown 标题符号，直接输出步骤。
"""
        if hint:
            user_content += f"\n参考方向：{hint.strip()}\n"
        if log_snippet:
            user_content += f"\n触发日志片段（供参考）：\n{log_snippet[:500]}\n"
        user_content += "\n请直接输出 runbook 步骤内容，无需 JSON。"
        messages = [
            {"role": "user", "content": user_content}
        ]
        try:
            return self._call_llm(messages, timeout=30) or "生成失败"
        except Exception as e:
            logger.warning(f"runbook 生成失败: {e}")
            return f"Runbook 生成失败: {e}"


def get_agent() -> LogAnalysisAgent:
    """获取 Agent 单例"""
    if not hasattr(get_agent, '_instance'):
        get_agent._instance = LogAnalysisAgent()
    return get_agent._instance
