# -*- coding: utf-8 -*-
"""Flask 桥接层（#92）：为 Next.js 诊断台 + Mastra 提供只读数据 API 与诊断回退。

设计要点（与现有"只读红线 / 人工在环"约束一致）：
- 全部端点只读：绝不写设备、绝不改代码、绝不自动关单。
- 命令由服务端按 type 枚举生成，不拼接任意用户输入（杜绝命令注入）。
- 复用 action_executor.validate_shell_action + DANGEROUS_TOKENS 作为二次只读护栏。
- 认证复用全局 before_request（ENABLE_API_AUTH + X-API-Key），Mastra 调用已带 Key。
- 响应采用 Mastra 探针期望的「扁平结构」（非 Flask success_response 的 {ok,data} 嵌套），
  保证 Mastra 端 data.logs / data.snapshot / data.cases 直接命中。

端点：
  GET  /api/device/<deviceId>/logs?lines=N        → {logs, truncated}
  GET  /api/device/<deviceId>/probe?type=cpu|mem|top|ps → {snapshot}
  POST /api/history/search  {keywords, topK}       → {cases:[{id,summary,rootCause}]}
  POST /api/diagnose  {deviceId, alertType, logs, stackTrace} → DiagnosisResult(兼容 Mastra)
"""
import logging
import shlex
import subprocess
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

# 复用存量只读护栏（单一事实来源，避免两套口径）
from .action_executor import validate_shell_action

logger = logging.getLogger(__name__)

bridge_bp = Blueprint('bridge', __name__)

# 单条 adb 命令超时（秒）
_ADB_TIMEOUT = 25

# probe 类型 → 只读 shell 子命令（固定枚举，不接受任意输入）
PROBE_COMMANDS = {
    'cpu': 'dumpsys cpuinfo',
    'mem': 'dumpsys meminfo',
    'top': 'top -n 1',
    'ps': 'ps -A',
}


def _run_adb_readonly(device_id: str, shell_cmd: str) -> str:
    """执行一条只读 adb shell 命令，返回输出文本；失败返回可读错误文本。

    安全：先经 validate_shell_action 白名单 + 危险 token 校验，再执行。
    """
    reason = validate_shell_action(shell_cmd)
    if reason:
        return f'[安全护栏拒绝] {reason}'
    full = f"adb -s {shlex.quote(device_id)} shell {shell_cmd}"
    try:
        proc = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=_ADB_TIMEOUT)
        out = (proc.stdout or '').strip()
        if not out and proc.stderr:
            out = (proc.stderr or '').strip()[:800]
        if len(out) > 20000:
            out = out[-20000:]
        return out
    except subprocess.TimeoutExpired:
        return '[命令超时]'
    except Exception as e:  # pragma: no cover - 防御性兜底
        return f'[执行失败] {e}'


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@bridge_bp.route('/api/device/<path:device_id>/logs', methods=['GET'])
def api_device_logs(device_id: str):
    """只读拉取设备实时日志片段（对应 Mastra fetch-logs 工具）。"""
    lines = max(10, min(_to_int(request.args.get('lines', 500), 500), 2000))
    shell_cmd = f"logcat -d -t {lines}"
    reason = validate_shell_action(shell_cmd)
    if reason:
        return jsonify({'logs': f'[安全护栏拒绝] {reason}', 'truncated': False}), 200
    out = _run_adb_readonly(device_id, shell_cmd)
    truncated = len(out) > 20000
    return jsonify({'logs': out, 'truncated': truncated})


@bridge_bp.route('/api/device/<path:device_id>/probe', methods=['GET'])
def api_device_probe(device_id: str):
    """只读设备快照（对应 Mastra fetch-device-snapshot 工具）。"""
    ptype = request.args.get('type', 'cpu')
    if ptype not in PROBE_COMMANDS:
        return jsonify({
            'snapshot': f'[未知探针类型] {ptype}，可选: {list(PROBE_COMMANDS.keys())}'
        }), 200
    out = _run_adb_readonly(device_id, PROBE_COMMANDS[ptype])
    return jsonify({'snapshot': out})


@bridge_bp.route('/api/history/search', methods=['POST'])
def api_history_search():
    """检索历史相似案例（对应 Mastra lookup-history 工具）。"""
    body = request.get_json(force=True, silent=True) or {}
    keywords = body.get('keywords') or []
    if not isinstance(keywords, list):
        keywords = [keywords]
    top_k = max(1, min(_to_int(body.get('topK', 5), 5), 20))
    query = ' '.join(str(k) for k in keywords if k)
    if not query:
        return jsonify({'cases': []}), 200
    try:
        from .knowledge_base import get_knowledge_base
        kb = get_knowledge_base()
        # alert_type 留空：按关键词重合检索全量历史（与 Mastra 仅传 keywords 的语义一致）
        cards = kb.search_similar('', query, top_k=top_k) or []
        cases: List[Dict[str, Any]] = []
        for c in cards:
            summary = c.get('alert_type') or ''
            if c.get('rule_name'):
                summary = (summary + ' | ' + c['rule_name']).strip(' |')
            cases.append({
                'id': c.get('id', ''),
                'summary': summary,
                'rootCause': c.get('final_root_cause') or c.get('root_cause') or '',
            })
        return jsonify({'cases': cases})
    except Exception as e:
        logger.exception('历史检索失败: %s', e)
        # 降级：返回空，不崩（Mastra 端按空 cases 处理）
        return jsonify({'cases': []}), 200


def _build_failed_result(alert_type: str, message: str) -> Dict[str, Any]:
    """构造与 Mastra DiagnosisResult 兼容的 failed 结构（供前端统一渲染）。"""
    return {
        'status': 'failed',
        'alertType': alert_type,
        'rootCause': None,
        'confidence': {'stack': 0, 'history': 0, 'context': 0, 'probe': 0, 'overall': 0},
        'evidence': [],
        'plan': [],
        'agents': {'log': 'failed', 'probe': 'skipped', 'history': 'skipped', 'source': 'skipped'},
        'needs_human_approval': True,
        'suggestions': [message],
    }


@bridge_bp.route('/api/diagnose', methods=['POST'])
def api_diagnose():
    """诊断回退端点（Web 在 Mastra 不可用时直连 Flask 诊断）。

    复用 log_monitor 存量 LLM 分析能力，输出与 Mastra DiagnosisResult 兼容的扁平结构，
    使 Next.js 前端无论走 Mastra 还是 Flask 回退都能用同一套渲染逻辑。
    """
    body = request.get_json(force=True, silent=True) or {}
    device_id = (body.get('deviceId') or '').strip()
    alert_type = (body.get('alertType') or body.get('alert_type') or '').strip() or 'exception'
    logs = body.get('logs') or ''
    stack_trace = body.get('stackTrace') or ''

    if logs:
        log_lines = logs.split('\n')
    elif stack_trace:
        log_lines = stack_trace.split('\n')
    else:
        # 无日志输入：尝试回拉设备实时日志（仍只读）
        pulled = _run_adb_readonly(device_id, 'logcat -d -t 200') if device_id else ''
        log_lines = pulled.split('\n') if pulled and not pulled.startswith('[') else []

    if not log_lines:
        return jsonify(_build_failed_result(
            alert_type, '未提供日志且无法拉取设备日志，请在请求体中传入 log_lines')), 200

    alert_context = {
        'rule_name': alert_type,
        'severity': 'high',
        'type': alert_type,
        'log_line': log_lines[-1][:300] if log_lines else '',
    }
    try:
        from .agent import get_agent
        from .knowledge_base import get_knowledge_base
        agent = get_agent()
        result = agent.analyze(log_lines=log_lines, alert_context=alert_context)
        data = result.to_dict()

        kb = get_knowledge_base()
        hist = kb.search_similar(alert_type, alert_context['log_line'], top_k=3) or []

        evidence = []
        if data.get('problem_location'):
            evidence.append({'type': 'direct', 'content': '问题定位: ' + data['problem_location'], 'source': 'log-analysis'})
        if data.get('impact'):
            evidence.append({'type': 'inferred', 'content': '影响: ' + data['impact'], 'source': 'log-analysis'})
        for c in hist[:3]:
            evidence.append({
                'type': 'references',
                'content': '历史案例 %s: %s' % (c.get('id', ''), c.get('final_root_cause') or c.get('root_cause', '')),
                'source': 'history',
            })

        root_cause = (data.get('root_cause') or '').strip()
        overall = 70 if root_cause else 0
        return jsonify({
            'status': 'ok' if root_cause else 'partial',
            'alertType': alert_type,
            'rootCause': root_cause or None,
            'confidence': {
                'stack': 70 if root_cause else 0,
                'history': 40 if hist else 0,
                'context': 0,
                'probe': 0,
                'overall': overall,
            },
            'evidence': evidence,
            'plan': ['log'] + (['history'] if hist else []),
            'agents': {
                'log': 'ok',
                'history': 'ok' if hist else 'skipped',
                'probe': 'skipped',
                'source': 'skipped',
            },
            'needs_human_approval': True,
            'suggestions': data.get('suggestions') or ['（只读诊断完成）如需修复请在人工确认后由运维执行。'],
        })
    except Exception as e:
        logger.exception('回退诊断失败: %s', e)
        return jsonify(_build_failed_result(alert_type, '诊断服务异常: %s' % e)), 200
