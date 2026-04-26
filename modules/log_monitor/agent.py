#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志分析 Agent - 基于 LLM 的智能日志分析
提供根因分析、排查建议、摘要生成
支持 Gemini、DeepSeek、OpenAI 等（通过 utils.llm_client）
"""

import os
import json
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from utils.logger import setup_logger
from utils.llm_client import call_llm, load_llm_config

logger = setup_logger('log_agent')


@dataclass
class AnalysisResult:
    """分析结果"""
    root_cause: str
    suggestions: List[str]
    summary: str
    raw_response: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'root_cause': self.root_cause,
            'suggestions': self.suggestions,
            'summary': self.summary,
        }


class LogAnalysisAgent:
    """日志分析 Agent"""

    SYSTEM_PROMPT = """你是一个专业的 Android 应用日志分析专家。你的任务是根据提供的 logcat 日志，分析异常或错误的根本原因，并给出排查建议。

请用中文回答，输出格式必须严格遵循以下 JSON 结构（不要包含其他文字或 markdown 标记）：
{
  "root_cause": "简要描述根本原因（1-3句话）",
  "suggestions": ["排查建议1", "排查建议2", "排查建议3"],
  "summary": "一句话总结"
}

要求：
- root_cause: 结合日志上下文推断最可能的根本原因
- suggestions: 3-5 条具体可操作的排查建议
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
                suggestions=obj.get('suggestions', []) or [],
                summary=obj.get('summary', ''),
                raw_response=content
            )
        except json.JSONDecodeError as e:
            logger.warning(f'LLM 返回非 JSON 格式: {content[:200]}...', exc_info=True)
            return AnalysisResult(
                root_cause='解析失败',
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
        # 限制日志长度，避免超出 token 限制
        max_lines = 50
        if len(log_lines) > max_lines:
            log_lines = log_lines[-max_lines:]
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
            return self._parse_response(response)
        except Exception as e:
            logger.exception(f'LLM 调用失败: {e}')
            raise

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
