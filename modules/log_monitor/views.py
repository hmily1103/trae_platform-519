"""
Log Monitor Flask Views
提供日志监控的 Web API
"""
import json
import os
import threading
import time
import re
from collections import deque
from datetime import datetime
from flask import Blueprint, render_template, request, Response, stream_with_context, send_file
from utils.response import success_response, error_response, validate_required
from utils.logger import setup_logger
from core.runtime.manager import get_runtime_manager
from core.runtime.model import RuntimeStatus
from .core.adb_controller import AdbController
from .core.log_analyzer import LogAnalyzer
from .alert_engine import AlertEngine, AlertRule, validate_rule_pattern
from .voice_tracker import VoiceCommandTracker
from .voice_tracker_store import (
    list_voice_sessions,
    load_voice_session,
    save_voice_session,
)
from .log_session_store import save_session_full_log, session_full_log_path
from .alert_store import upsert_alert, load_recent, store_statistics
from .agent import get_agent
from .selfheal import get_self_heal_agent, build_evidence_chain
from .knowledge_base import get_knowledge_base
from .action_executor import execute_action, action_result_to_str

# ========== 性能优化：队列容量上限 ==========
# logcat 典型速率 50-200 行/秒，30000 条可覆盖 2.5-10 分钟上下文，
# 且自愈采集全量遍历时内存可控（~15MB vs 原先无界可达 350MB+）
LOG_QUEUE_MAX = 30000
ALERT_QUEUE_MAX = 5000
SELF_HEAL_RESULTS_MAX = 2000
from .agents import (
    AGENT_V2_ENABLED, register_builtin_agents, build_plan,
    execute_plan, synthesize,
)

log_monitor_bp = Blueprint('log_monitor', __name__, template_folder='templates')
logger = setup_logger('log_monitor_module')

# 全局变量：管理多个监控任务
MONITOR_TASKS = {}  # {task_id: {'controller': AdbController, 'device_id': str, 'start_time': float, 'alert_engine': AlertEngine, 'voice_tracker': VoiceCommandTracker}}
MONITOR_TASKS_LOCK = threading.Lock()


def _execute_alert_action(alert_engine, alert_dict, device_id, target_package,
                          alert_queue=None, alert_queue_lock=None):
    """#29 执行规则配置的只读动作（screenshot / 白名单 shell），并回写 action_taken。

    安全边界：只读动作真实执行；custom_shell 自由命令一律拒绝（结果中记录拒绝原因）。
    回写三处：AlertRecord.action_taken、告警落盘、告警队列（含 SSE action_update 事件）。
    :return: 动作结果 dict（挂进自愈证据链用）；未配置动作返回 None
    """
    try:
        rule_id = alert_dict.get("rule_id")
        rule = alert_engine.rules.get(rule_id) if (alert_engine and rule_id) else None
        action = (getattr(rule, "action", "") or "").strip()
        if not action or action.lower() == "none":
            return None
        result = execute_action(action, device_id, alert_dict.get("id", ""), target_package)
        if not result:
            return None
        action_str = action_result_to_str(result)
        alert_dict["action_taken"] = action_str
        alert_id = alert_dict.get("id")
        # 1) 回写 AlertRecord（api_get_alerts 来源）
        if alert_engine is not None and alert_id:
            for rec in alert_engine.alert_history:
                if rec.id == alert_id:
                    rec.action_taken = action_str
                    break
        # 2) 落盘（重启后仍可查动作产物）
        try:
            upsert_alert({**alert_dict, "action_taken": action_str})
        except Exception as e:
            logger.warning(f"动作结果落盘失败(忽略): {e}")
        # 3) 队列回写 + SSE 更新事件（前端实时刷新截图/输出）
        if alert_queue is not None and alert_id:
            with alert_queue_lock:
                for item in alert_queue:
                    if item.get("id") == alert_id:
                        item["action_taken"] = action_str
                        break
                alert_queue.append({
                    "action_update": True,
                    "id": alert_id,
                    "action_taken": action_str,
                    "timestamp": time.time(),
                })
        logger.info(f"告警动作执行完成: alert={alert_id} type={result.get('type')} status={result.get('status')}")
        return result
    except Exception as e:
        logger.warning(f"告警动作执行失败(忽略，不影响诊断): {e}")
        return None


def _run_agent_v2(alert_dict, log_lines, ctx_meta, action_result,
                  device_id, target_package, mode,
                  alert_queue=None, alert_queue_lock=None):
    """Agent 2.0 多 Agent 诊断入口（C1/C3）。

    流程：注册 Agent → Planner 编排 → 并行执行 → Synthesizer 综合裁决。
    返回兼容 1.0 的 result dict；降级时返回 None（调用方退回 1.0 handle_alert）。

    SSE 事件（通过 alert_queue 推送）：
    - agent_v2_plan：诊断计划（派了哪些 Agent、为什么）
    - agent_v2_update：各 Agent 执行状态 + 综合结论
    """
    try:
        register_builtin_agents()  # 幂等

        # 构建诊断上下文
        context = {
            "log_lines": log_lines,
            "context_meta": ctx_meta,
            "action_result": action_result,
            "device_id": device_id,
            "package": target_package,
            "mode": mode,
        }
        # 历史案例（让 history Agent 与 1.0 口径一致）
        try:
            kb = get_knowledge_base()
            alert_type = str(alert_dict.get("type", "")).lower()
            query = "%s %s" % (
                alert_dict.get("message", "") or alert_dict.get("log_line", ""),
                alert_dict.get("rule_name", ""),
            )
            historical = kb.search_similar(alert_type, query.strip(), top_k=3) or []
            context["historical_cases"] = historical
        except Exception:
            context["historical_cases"] = []

        # 1) Planner 编排
        plan = build_plan(alert_dict, context)

        # 推送诊断计划事件（前端时间线第一屏）
        if alert_queue is not None and alert_queue_lock is not None:
            try:
                with alert_queue_lock:
                    alert_queue.append({
                        "agent_v2_plan": True,
                        "id": alert_dict.get("id"),
                        "plan": plan.to_dict(),
                        "timestamp": time.time(),
                    })
            except Exception:
                pass

        # 2) 并行执行
        execution = execute_plan(plan, alert_dict, context)

        # 3) Synthesizer 综合裁决
        result = synthesize(execution, alert_dict, context)

        # 推送执行结果事件（前端时间线更新）
        if alert_queue is not None and alert_queue_lock is not None:
            try:
                with alert_queue_lock:
                    alert_queue.append({
                        "agent_v2_update": True,
                        "id": alert_dict.get("id"),
                        "execution": execution.to_dict(),
                        "result": result,
                        "timestamp": time.time(),
                    })
            except Exception:
                pass

        if result is None:
            logger.info("[Agent V2] 综合裁决降级，退回 1.0 单链路")
        else:
            logger.info(
                "[Agent V2] 诊断完成: agents=%s status=%s confidence=%s",
                [f["agent_name"] + ":" + f["status"] for f in execution.findings],
                result.get("status"), result.get("confidence"),
            )
        return result
    except Exception as e:
        logger.warning("[Agent V2] 执行失败(退回 1.0): %s" % e)
        return None


def _trigger_self_heal(device_id, target_package, alert_dict, log_line, results, lock,
                       alert_engine=None, alert_queue=None, alert_queue_lock=None,
                       log_queue=None, log_queue_lock=None):
    """异步触发自愈 Agent 处理告警（Step1 挂载触发，不阻塞日志流）。

    自愈结果：① 暂存于 results 列表；② 回写到对应的 AlertRecord 与告警队列（Step2 持久化）。
    行为由环境变量 LOG_SELF_HEAL_MODE 控制：off=关闭，observe=仅分析（默认），
    collect/assist=分析+只读证据采集。
    自动关单由 LOG_SELF_HEAL_AUTO_CLOSE 控制（默认关闭，仅 AUTO_RESOLVED 高信心案例才关）。
    """
    try:
        # #29 动作先行：崩溃瞬间截图/只读采集，越早现场越真实（与自愈模式解耦，off 也执行）
        action_result = _execute_alert_action(
            alert_engine, alert_dict, device_id, target_package,
            alert_queue, alert_queue_lock,
        )
        mode = os.environ.get("LOG_SELF_HEAL_MODE", "observe").lower()
        if mode == "off":
            return
        agent = get_self_heal_agent(device_id, target_package, mode=mode)
        # 取崩溃前后上下文日志（含 at ...java:行号 堆栈），让根因定位到代码行而非通用套话。
        # 回退：无上下文时仍用单行（保持原行为）。
        log_lines = [log_line] if log_line else []
        ctx_meta = None
        if log_queue is not None and log_line:
            try:
                with log_queue_lock:
                    all_logs = [(i, item.get('log', '')) for i, item in enumerate(log_queue) if item.get('log')]
                # #23 动态扩窗：按告警类型（crash/anr/oom/...）决定上下文截取策略
                ctx, ctx_meta = _get_dynamic_context_around_alert(
                    all_logs, log_line, alert_dict.get('type', '')
                )
                if ctx and len(ctx) > 1:
                    log_lines = ctx
            except Exception as e:
                logger.warning(f"自愈上下文采集失败(回退单行): {e}")
        # #25 context_meta 直接传入 handle_alert，进证据链（结果中亦包含 context_meta 字段）
        # #29 action_result 一并传入：动作产物（截图/只读输出）作为直接证据挂进证据链
        # Agent 2.0 入口：开关开启时走多 Agent 编排，降级安全退回 1.0
        result = None
        if AGENT_V2_ENABLED:
            result = _run_agent_v2(
                alert_dict, log_lines, ctx_meta, action_result,
                device_id, target_package, mode,
                alert_queue, alert_queue_lock,
            )
        if result is None:
            result = agent.handle_alert(alert_dict, log_lines, context_meta=ctx_meta,
                                        action_result=action_result)
        if isinstance(result, dict) and ctx_meta:
            result.setdefault('context_meta', ctx_meta)
        with lock:
            results.append({
                "alert_id": alert_dict.get("id"),
                "result": result,
                "created_at": time.time(),
            })
        # Step2：回写自愈结果到告警对象（持久化，供前端展示/复盘）
        alert_id = alert_dict.get("id")
        if alert_id:
            # 1) AlertEngine 的 AlertRecord（api_get_alerts 来源）
            if alert_engine is not None:
                for rec in alert_engine.alert_history:
                    if rec.id == alert_id:
                        rec.self_heal = result
                        break
            # 1.5) 落盘：合并自愈结果（保留 task_id 等既有字段）
            try:
                upsert_alert({**alert_dict, "self_heal": result})
            except Exception as e:
                logger.warning(f"自愈结果落盘失败(忽略): {e}")
            # 2) 告警队列里的 dict（stream_alerts SSE 来源）
            if alert_queue is not None:
                with alert_queue_lock:
                    for item in alert_queue:
                        if item.get("id") == alert_id:
                            item["self_heal"] = result
                            break
                    # 追加一条更新事件：告警已先于自愈送达前端，
                    # 自愈完成后通过此事件实时刷新卡片（Step3 前端展示）
                    alert_queue.append({
                        "self_heal_update": True,
                        "id": alert_id,
                        "self_heal": result,
                        "timestamp": time.time(),
                    })
        # Step5：复核关单 —— 高危永不自动关；仅当自愈判定 AUTO_RESOLVED 且显式开启自动关单时，
        # 才自动确认关单（闭环收口）。其余状态仍交人工（不擅自关单）。
        if (result.get("status") == "AUTO_RESOLVED"
                and result.get("auto_closeable")
                and alert_id and alert_engine is not None):
            try:
                if alert_engine.acknowledge_alert(alert_id, "self_heal_agent"):
                    logger.info(f"自愈 Agent 自动关单: device={device_id} alert={alert_id}")
            except Exception as e:
                logger.warning(f"自愈自动关单失败(忽略，留人工): {e}")

        logger.info(
            f"自愈 Agent 处理完成: device={device_id} "
            f"type={result.get('alert_type')} status={result.get('status')}"
        )
    except Exception as e:
        logger.warning(f"自愈 Agent 触发失败: {e}")


def _get_context_around_alert(
    all_logs: list, alert_log_line: str, before: int = 25, after: int = 25, fallback: int = 50
) -> list:
    """
    获取告警前后的日志上下文。若找到告警行则取前后各 before/after 行；否则退回最近 fallback 条。
    """
    if not all_logs:
        return [alert_log_line] if alert_log_line else []
    # 查找告警行（支持子串匹配，日志可能被截断）
    alert_snippet = (alert_log_line or '')[:200]
    idx = -1
    alert_full = alert_log_line or ''
    for i, (_, log) in enumerate(all_logs):
        if alert_snippet and (alert_snippet in log or (alert_full and log in alert_full)):
            idx = i
            break
    if idx >= 0:
        start = max(0, idx - before)
        end = min(len(all_logs), idx + after + 1)
        return [log for _, log in all_logs[start:end]]
    # 未找到则退回最近 fallback 条，并确保包含告警行
    recent = [log for _, log in all_logs[-fallback:]]
    if alert_log_line and alert_log_line not in '\n'.join(recent):
        recent.append(alert_log_line)
    return recent


# ========== #23 动态上下文扩窗：按告警类型决定截取策略 ==========
# 各类型窗口参数：crash 需要完整堆栈（向后大幅前探），anr 需要主线程/锁/CPU 段，
# oom 需要更多"告警前"的内存压力日志，exception 适度放宽，其余保持原行为。
_CTX_WINDOWS = {
    'crash':     {'before': 10, 'after': 120, 'fallback': 120},
    'anr':       {'before': 15, 'after': 90,  'fallback': 100},
    'oom':       {'before': 40, 'after': 40,  'fallback': 80},
    'exception': {'before': 20, 'after': 30,  'fallback': 60},
}
_CTX_DEFAULT_WIN = {'before': 20, 'after': 20, 'fallback': 50}

# 崩溃堆栈行：at com.x.Y(Z.java:123) / Caused by: / java.lang.Xxx / "... N more"
_STACK_LINE_RE = re.compile(
    r'(^|\s)(at\s+[\w.$<>\[\]]+\(|Caused by:|java\.lang\.\w|android\.\w[\w.]*(?:Exception|Error)|\.\.\.\s*\d+\s+more)'
)
# 崩溃起始标记
_CRASH_HEAD_RE = re.compile(r'FATAL EXCEPTION|\*\*\* FATAL|Fatal signal|beginning of crash|AndroidRuntime.*Process:')
# 内存相关日志（OOM 场景窗口外补充用）
_MEM_LINE_RE = re.compile(
    r'OutOfMemory|lowmemorykiller|onTrimMemory|GC_|GrowHeap|dalvik-heap|Alloc(?:ation)?\s*fail|kill(?:ing)?\s+.*adj|meminfo|Low on memory',
    re.I,
)


def _get_dynamic_context_around_alert(all_logs: list, alert_log_line: str, alert_type: str):
    """
    按告警类型动态扩窗截取上下文（#23）。

    - crash: 回溯定位 FATAL 头，向后前探直到堆栈结束（保完整堆栈）
    - anr:   加大向后窗口，覆盖主线程 BLOCKED/锁等待/CPU usage 段
    - oom:   加大向前窗口，并从更早日志中补充内存相关行
    - 其他:  维持原固定窗口行为

    :return: (log_lines, ctx_meta)  ctx_meta 记录实际策略/范围/行数，供证据链使用
    """
    atype = (alert_type or '').lower()
    win = _CTX_WINDOWS.get(atype, _CTX_DEFAULT_WIN)
    meta = {
        'strategy': atype if atype in _CTX_WINDOWS else 'default',
        'window': f"-{win['before']}/+{win['after']}",
        'matched': False,
        'lines': 0,
    }
    if not all_logs:
        lines = [alert_log_line] if alert_log_line else []
        meta['lines'] = len(lines)
        return lines, meta

    # 定位告警行（与 _get_context_around_alert 相同的子串匹配逻辑）
    alert_snippet = (alert_log_line or '')[:200]
    alert_full = alert_log_line or ''
    idx = -1
    for i, (_, log) in enumerate(all_logs):
        if alert_snippet and (alert_snippet in log or (alert_full and log in alert_full)):
            idx = i
            break
    if idx < 0:
        recent = [log for _, log in all_logs[-win['fallback']:]]
        if alert_log_line and alert_log_line not in '\n'.join(recent):
            recent.append(alert_log_line)
        meta['strategy'] += '_fallback'
        meta['lines'] = len(recent)
        return recent, meta

    start = max(0, idx - win['before'])
    end = min(len(all_logs), idx + win['after'] + 1)

    if atype == 'crash':
        # ① 回溯（最多 30 行）找 FATAL 头，确保堆栈从头开始
        for j in range(idx, max(0, idx - 30) - 1, -1):
            if _CRASH_HEAD_RE.search(all_logs[j][1]):
                start = max(0, j - 5)
                break
        # ② 向后前探至堆栈结束：连续 8 行非堆栈行即认为堆栈段结束
        non_stack = 0
        last_stack = idx
        for j in range(idx + 1, min(len(all_logs), idx + win['after'] + 1)):
            if _STACK_LINE_RE.search(all_logs[j][1]) or _CRASH_HEAD_RE.search(all_logs[j][1]):
                last_stack = j
                non_stack = 0
            else:
                non_stack += 1
                if non_stack > 8:
                    break
        end = min(len(all_logs), last_stack + 6)
        meta['strategy'] = 'crash_full_stack'

    lines = [log for _, log in all_logs[start:end]]

    if atype == 'oom':
        # 从窗口外更早的日志中补充内存相关行（最多 30 条，保持时间顺序）
        scan_start = max(0, idx - 300)
        extra = [log for _, log in all_logs[scan_start:start] if _MEM_LINE_RE.search(log)]
        if extra:
            lines = extra[-30:] + ['--- 以上为窗口外补充的内存相关日志 ---'] + lines
            meta['extra_mem_lines'] = min(len(extra), 30)
        meta['strategy'] = 'oom_memory'

    meta['matched'] = True
    meta['lines'] = len(lines)
    meta['range'] = [start, end]
    return lines, meta


@log_monitor_bp.route('/')
def index():
    """主页面"""
    return render_template('log_monitor_index.html')


@log_monitor_bp.route('/alerts', methods=['GET'])
def alerts_history_page():
    """独立告警历史页（全屏，可筛选，自愈详情可展开）"""
    return render_template('alerts.html')


@log_monitor_bp.route('/file_analyze', methods=['GET', 'POST'])
def file_analyze_page():
    """日志文件分析页面（GET/POST 均返回页面，避免 405）"""
    return render_template('log_file_analyze.html')


@log_monitor_bp.route('/api/devices', methods=['GET'])
def api_get_devices():
    """获取设备列表"""
    try:
        controller = AdbController()
        devices = controller.get_connected_devices()
        return success_response(data={'devices': devices})
    except Exception as e:
        logger.error(f'获取设备列表失败: {e}', exc_info=True)
        return error_response(
            message='获取设备列表失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/connect', methods=['POST'])
def api_connect_device():
    """连接设备"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'ip')
        if validation_error:
            return validation_error
        
        ip = data.get('ip')
        port = int(data.get('port', 8787))
        
        controller = AdbController()
        success = controller.connect_device(ip, port)
        
        if success:
            return success_response(
                data={'device_id': f"{ip}:{port}"},
                message=f'设备连接成功: {ip}:{port}'
            )
        else:
            return error_response(
                message='设备连接失败，请检查设备状态和网络',
                error='connection failed',
                status_code=400
            )
    except Exception as e:
        logger.error(f'连接设备失败: {e}', exc_info=True)
        return error_response(
            message='连接设备失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/disconnect', methods=['POST'])
def api_disconnect_device():
    """断开设备连接"""
    try:
        data = request.get_json() or {}
        device_id = data.get('device_id')
        
        controller = AdbController()
        controller.disconnect_device(device_id)
        
        return success_response(message='设备已断开连接')
    except Exception as e:
        logger.error(f'断开设备失败: {e}', exc_info=True)
        return error_response(
            message='断开设备失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/voice_tracker/history', methods=['GET'])
def api_voice_tracker_history():
    """获取语音指令追踪历史（运行中来自内存；结束后来自已保存会话文件）"""
    task_id = request.args.get('task_id')
    if not task_id:
        return error_response(message='缺少 task_id', status_code=400)

    with MONITOR_TASKS_LOCK:
        task_info = MONITOR_TASKS.get(task_id)
        if task_info:
            tracker = task_info.get('voice_tracker')
            if not tracker:
                return error_response(message='追踪器未初始化', status_code=500)
            history = list(tracker.get_history())
            history.reverse()
            return success_response(data=history)

    saved = load_voice_session(task_id)
    if saved and saved.get('items') is not None:
        history = list(saved['items'])
        history.reverse()
        return success_response(data=history)

    return error_response(message='任务不存在或无已保存的语音记录', status_code=404)


@log_monitor_bp.route('/api/voice_tracker/sessions', methods=['GET'])
def api_voice_tracker_sessions():
    """列出已保存的语音追踪会话（停止监控时写入）"""
    try:
        limit = int(request.args.get('limit', 50))
    except ValueError:
        limit = 50
    rows = list_voice_sessions(limit=limit)
    return success_response(data=rows)


@log_monitor_bp.route('/api/session_logs/download', methods=['GET'])
def api_session_logs_download():
    """下载停止监控时保存的完整 logcat 文本（与面板是否渲染无关）。"""
    task_id = request.args.get('task_id')
    if not task_id:
        return error_response(message='缺少 task_id', status_code=400)
    path = session_full_log_path(task_id)
    if not os.path.isfile(path):
        return error_response(
            message='未找到已保存的日志文件（请停止监控后再导出，或确认选择了正确的会话）',
            error='not_found',
            status_code=404,
        )
    safe_name = ''.join(c if c.isalnum() or c in '-_' else '_' for c in task_id) + '.log'
    return send_file(path, as_attachment=True, download_name=f'log_monitor_{safe_name}', mimetype='text/plain')

@log_monitor_bp.route('/api/start', methods=['POST'])
def api_start_monitor():
    """开始监控"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'device_id')
        if validation_error:
            return validation_error
        
        device_id = data.get('device_id')
        task_id = data.get('task_id', f"log_monitor_{int(time.time())}")
        min_log_level = data.get('min_log_level', 'Verbose')
        
        # 检查是否已有任务在运行
        with MONITOR_TASKS_LOCK:
            if task_id in MONITOR_TASKS:
                return error_response(
                    message='监控任务已在运行',
                    error='task already running',
                    status_code=400
                )
        
        # Create Runtime
        runtime_id = None
        try:
            runtime = get_runtime_manager().create_runtime(
                name=f"Log Monitor: {device_id}",
                module="log_monitor",
                context={
                    'device_id': device_id,
                    'task_id': task_id,
                    'target_package': data.get('target_package', 'com.thunder.ktv')
                }
            )
            runtime_id = runtime.runtime_id
            get_runtime_manager().update_status(runtime_id, RuntimeStatus.RUNNING)
            logger.info(f"Runtime created for Log Monitor: {runtime_id}")
        except Exception as e:
            logger.warning(f"Failed to create Runtime for Log Monitor: {e}")

        # 创建控制器并开始监控
        controller = AdbController()
        
        # 创建日志队列（用于 SSE 流）—— 有容量上限，防内存无限增长
        log_queue = []
        log_queue_seq = 0  # 单调递增序列号，SSE 用 seq 差分替代索引切片
        log_queue_lock = threading.Lock()
        
        # 创建告警队列（有容量上限）
        alert_queue = []
        alert_queue_seq = 0
        alert_queue_lock = threading.Lock()
        
        # 自愈 Agent 结果收集（Step1 挂载触发，Step2 回写，有容量上限）
        self_heal_results = []
        self_heal_results_seq = 0
        self_heal_lock = threading.Lock()
        
        # 创建告警引擎
        alert_engine = AlertEngine()
        
        # 创建语音指令追踪器
        voice_tracker = VoiceCommandTracker()
        
        def log_callback(log_line, analysis_result):
            """日志回调函数"""
            nonlocal log_queue_seq, alert_queue_seq
            with log_queue_lock:
                log_queue_seq += 1
                log_queue.append({
                    'seq': log_queue_seq,
                    'log': log_line,
                    'analysis': analysis_result[0] if analysis_result else None,
                    'timestamp': time.time()
                })
                # 容量保护：超出上限时从头部裁剪，只保留最近的一半
                if len(log_queue) > LOG_QUEUE_MAX:
                    # 保留最近 LOG_QUEUE_MAX//2 条，足够 SSE 重连追数据
                    trim_to = LOG_QUEUE_MAX // 2
                    log_queue[:] = log_queue[-trim_to:]
            
            # 更新语音指令状态机
            try:
                voice_tracker.process_log(log_line)
            except Exception as e:
                logger.error(f"处理语音指令状态失败: {e}")
            
            # 检查告警
            alerts = alert_engine.check_log(log_line, device_id, target_package)
            if alerts:
                with alert_queue_lock:
                    for alert in alerts:
                        alert_queue_seq += 1
                        d = alert.to_dict()
                        d['seq'] = alert_queue_seq
                        alert_queue.append(d)
                    if len(alert_queue) > ALERT_QUEUE_MAX:
                        alert_queue[:] = alert_queue[-ALERT_QUEUE_MAX//2:]
                # 落盘：每次告警写入历史（与自愈结果后续合并），mode=off 也能记录
                try:
                    for alert in alerts:
                        upsert_alert({**alert.to_dict(), "task_id": task_id})
                except Exception as e:
                    logger.warning(f"告警落盘失败(忽略): {e}")
                # 自愈 Agent 异步触发（Step1：挂载触发，不阻塞主流程）
                try:
                    if os.environ.get("LOG_SELF_HEAL_MODE", "observe").lower() != "off":
                        for alert in alerts:
                            threading.Thread(
                                target=_trigger_self_heal,
                                args=(device_id, target_package, alert.to_dict(), log_line, self_heal_results, self_heal_lock,
                                      alert_engine, alert_queue, alert_queue_lock, log_queue, log_queue_lock),
                                daemon=True,
                            ).start()
                except Exception as e:
                    logger.error(f"自愈 Agent 线程启动失败: {e}")
        
        target_package = data.get('target_package', 'com.thunder.ktv')
        
        controller.start_monitoring(
            device_id=device_id,
            log_callback=log_callback,
            min_log_level=min_log_level,
            target_package=target_package
        )
        
        # 保存任务信息
        with MONITOR_TASKS_LOCK:
            MONITOR_TASKS[task_id] = {
                'controller': controller,
                'device_id': device_id,
                'start_time': time.time(),
                'log_queue': log_queue,
                'log_queue_lock': log_queue_lock,
                'log_queue_seq': log_queue_seq,
                'alert_queue': alert_queue,
                'alert_queue_lock': alert_queue_lock,
                'alert_queue_seq': alert_queue_seq,
                'self_heal_results': self_heal_results,
                'self_heal_lock': self_heal_lock,
                'self_heal_results_seq': self_heal_results_seq,
                'alert_engine': alert_engine,
                'voice_tracker': voice_tracker,
                'target_package': target_package,
                'runtime_id': runtime_id
            }
        
        logger.info(f'日志监控已启动: {task_id}, 设备: {device_id}')
        return success_response(
            data={'task_id': task_id},
            message='日志监控已启动'
        )
    except Exception as e:
        if 'runtime_id' in locals() and runtime_id:
            get_runtime_manager().update_status(runtime_id, RuntimeStatus.FAILED, error=str(e))
        logger.error(f'启动监控失败: {e}', exc_info=True)
        return error_response(
            message='启动监控失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/stop', methods=['POST'])
def api_stop_monitor():
    """停止监控"""
    try:
        data = request.get_json() or {}
        task_id = data.get('task_id')
        
        if not task_id:
            return error_response(
                message='缺少任务ID',
                error='task_id required',
                status_code=400
            )
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='未找到运行中的监控任务',
                    error='task not found',
                    status_code=404
                )
            
            # 持久化完整 logcat 文本（面板未渲染/专注语音模式时仍可导出）
            log_queue = task_info.get("log_queue") or []
            log_queue_lock = task_info.get("log_queue_lock")
            log_lines: list = []
            if log_queue_lock:
                with log_queue_lock:
                    for item in log_queue:
                        ln = item.get("log")
                        if ln is not None:
                            log_lines.append(str(ln))
            else:
                for item in log_queue:
                    ln = item.get("log")
                    if ln is not None:
                        log_lines.append(str(ln))
            try:
                save_session_full_log(task_id, log_lines)
            except Exception as e:
                logger.warning(f"保存完整日志失败: {e}", exc_info=True)

            # 停止监控
            controller = task_info['controller']
            controller.stop_monitoring()
            
            # 更新 Runtime 状态
            runtime_id = task_info.get('runtime_id')
            if runtime_id:
                get_runtime_manager().update_status(runtime_id, RuntimeStatus.CANCELLED)

            # 持久化语音追踪记录（停止后仍可从「语音记录历史」查看）
            voice_tracker = task_info.get('voice_tracker')
            device_id = task_info.get('device_id', '')
            if voice_tracker:
                try:
                    save_voice_session(task_id, device_id, list(voice_tracker.get_history()))
                except Exception as e:
                    logger.warning(f'保存语音追踪会话失败: {e}', exc_info=True)

            # 清理任务
            del MONITOR_TASKS[task_id]
        
        logger.info(f'日志监控已停止: {task_id}')
        return success_response(message='日志监控已停止')
    except Exception as e:
        logger.error(f'停止监控失败: {e}', exc_info=True)
        return error_response(
            message='停止监控失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/stream_logs')
def stream_logs():
    """SSE 日志流"""
    task_id = request.args.get('task_id')
    
    if not task_id:
        return error_response(
            message='缺少任务ID',
            error='task_id required',
            status_code=400
        )
    
    def generate():
        """生成 SSE 流"""
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                return
        
        log_queue = task_info['log_queue']
        log_queue_lock = task_info['log_queue_lock']
        last_seq = 0  # 用序列号追踪，队列裁剪后不漂移
        
        try:
            while True:
                # 检查任务是否还在运行
                with MONITOR_TASKS_LOCK:
                    if task_id not in MONITOR_TASKS:
                        yield f"data: {json.dumps({'done': True})}\n\n"
                        break
                
                # 获取新日志（按 seq 差分，兼容队列裁剪）
                with log_queue_lock:
                    new_logs = [item for item in log_queue if item.get('seq', 0) > last_seq]
                    if new_logs:
                        last_seq = new_logs[-1].get('seq', last_seq)
                
                # 批量发送——多条日志一次推送，减少网络往返
                if new_logs:
                    yield f"data: {json.dumps(new_logs)}\n\n"
                
                # 短暂休眠，避免 CPU 占用过高
                time.sleep(0.1)
                
        except GeneratorExit:
            # 客户端断开连接
            pass
        except Exception as e:
            logger.error(f'SSE 流错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@log_monitor_bp.route('/stream_alerts')
def stream_alerts():
    """SSE 告警流"""
    task_id = request.args.get('task_id')
    
    if not task_id:
        return error_response(
            message='缺少任务ID',
            error='task_id required',
            status_code=400
        )
    
    def generate():
        """生成告警 SSE 流"""
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                return
        
        alert_queue = task_info.get('alert_queue', [])
        alert_queue_lock = task_info.get('alert_queue_lock')
        last_seq = 0  # 用序列号追踪，队列裁剪后不漂移
        
        try:
            while True:
                # 检查任务是否还在运行
                with MONITOR_TASKS_LOCK:
                    if task_id not in MONITOR_TASKS:
                        yield f"data: {json.dumps({'done': True})}\n\n"
                        break
                
                # 获取新告警（按 seq 差分，兼容队列裁剪）
                if alert_queue_lock:
                    with alert_queue_lock:
                        new_alerts = [item for item in alert_queue if item.get('seq', 0) > last_seq]
                        if new_alerts:
                            last_seq = new_alerts[-1].get('seq', last_seq)
                else:
                    new_alerts = []
                
                # 发送新告警 / 自愈更新事件
                for alert_item in new_alerts:
                    if alert_item.get('self_heal_update'):
                        yield f"data: {json.dumps({'type': 'self_heal_update', 'data': alert_item})}\n\n"
                    elif alert_item.get('action_update'):
                        # #29 动作执行完成事件：前端实时刷新截图/只读输出
                        yield f"data: {json.dumps({'type': 'action_update', 'data': alert_item})}\n\n"
                    elif alert_item.get('agent_v2_plan'):
                        # Agent 2.0 诊断计划事件
                        yield f"data: {json.dumps({'type': 'agent_v2_plan', 'data': alert_item})}\n\n"
                    elif alert_item.get('agent_v2_update'):
                        # Agent 2.0 执行状态 + 综合结论事件
                        yield f"data: {json.dumps({'type': 'agent_v2_update', 'data': alert_item})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'alert', 'data': alert_item})}\n\n"
                
                # 等待一段时间再检查
                time.sleep(0.5)
                
        except GeneratorExit:
            pass
        except Exception as e:
            logger.error(f'告警流错误: {e}', exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@log_monitor_bp.route('/api/alerts', methods=['GET'])
def api_get_alerts():
    """获取告警记录。无 task_id 时返回全局落盘历史（支持筛选，重启后仍可查）。"""
    try:
        task_id = request.args.get('task_id')
        severity = request.args.get('severity')
        acknowledged = request.args.get('acknowledged')
        try:
            limit = max(1, min(1000, int(request.args.get('limit', 200))))
        except (ValueError, TypeError):
            limit = 200

        ack_filter = None
        if acknowledged is not None:
            ack_filter = str(acknowledged).lower() in ('1', 'true', 'yes', 'on')

        # 无 task_id：全局历史（落盘），支持设备/严重度/确认状态筛选
        if not task_id:
            alerts = load_recent(
                limit=limit,
                device_id=request.args.get('device_id'),
                severity=severity,
                acknowledged=ack_filter,
            )
            alerts = [_enrich_alert_record_for_test(a) for a in alerts]
            return success_response(data={'alerts': alerts, 'global': True})

        # 有 task_id：优先落盘历史，回退内存（向后兼容现有监控面板/历史弹窗）
        stored = load_recent(
            limit=limit,
            task_id=task_id,
            severity=severity,
            acknowledged=ack_filter,
        )
        if stored:
            stored = [_enrich_alert_record_for_test(a) for a in stored]
            return success_response(data={'alerts': stored})

        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return success_response(data={'alerts': []})

            alerts = alert_engine.get_alerts(
                severity=severity if severity else None,
                acknowledged=bool(acknowledged) if acknowledged else None,
                limit=limit
            )
            return success_response(data={
                'alerts': [_enrich_alert_record_for_test(alert.to_dict()) for alert in alerts]
            })
    except Exception as e:
        logger.error(f'获取告警失败: {e}', exc_info=True)
        return error_response(
            message='获取告警失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alerts/<alert_id>/acknowledge', methods=['POST'])
def api_acknowledge_alert(alert_id):
    """确认告警（内存确认失败时回退到落盘历史确认，使历史页可复盘关单）"""
    try:
        task_id = request.args.get('task_id')
        data = request.get_json(silent=True) or {}
        user = data.get('user', 'system')
        # L5/#31 验证闭环：记录人工验证结论（测试视角）
        resolution = data.get('resolution', 'verified_fixed')
        if resolution not in (
            'verified_fixed', 'not_fixed', 'partially_fixed',
            'not_reproducible', 'false_positive', 'known_issue'
        ):
            resolution = 'verified_fixed'
        try:
            reproduced_count = int(data.get('reproduced_count') or 0)
        except (TypeError, ValueError):
            reproduced_count = 0
        regression_status = (data.get('regression_status') or '').strip()
        if regression_status not in ('new_issue', 'regression', 'reopen', 'stable_fixed', ''):
            regression_status = ''

        # #27/#31 结构化沉淀补充字段（人工确认时可选填写，全部可为空）
        kb_extra = {
            'resolution': resolution,
            'final_root_cause': (data.get('final_root_cause') or '').strip(),
            'fix_action': (data.get('fix_action') or '').strip(),
            'affected_versions': (data.get('affected_versions') or '').strip(),
            'fixed_version': (data.get('fixed_version') or '').strip(),
            'owner_module': (data.get('owner_module') or '').strip(),
            'verification_version': (data.get('verification_version') or '').strip(),
            'reproduced_count': max(0, reproduced_count),
            'regression_status': regression_status,
            'verifier': (data.get('verifier') or user or '').strip(),
            'verification_note': (data.get('verification_note') or '').strip(),
        }

        rec_dict = None
        sh = None

        # 1) 内存确认（任务仍活跃）
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id) if task_id else None
            alert_engine = task_info.get('alert_engine') if task_info else None
            if alert_engine and alert_engine.acknowledge_alert(alert_id, user):
                recs = alert_engine.get_alerts(limit=10000)
                rec = next((a for a in recs if a.id == alert_id), None)
                if rec is not None:
                    rec_dict = rec.to_dict()
                    sh = getattr(rec, "self_heal", None)
                    # 落盘：合并确认状态
                    try:
                        upsert_alert(rec_dict)
                    except Exception as e:
                        logger.warning(f"确认状态落盘失败(忽略): {e}")

        # 2) 回退：落盘历史确认（任务已停 / 无 task_id）
        if rec_dict is None:
            stored = load_recent(limit=200000, task_id=task_id)
            stored_rec = next((r for r in stored if r.get('id') == alert_id), None)
            if stored_rec:
                stored_rec['acknowledged'] = True
                stored_rec['acknowledged_by'] = user
                stored_rec['acknowledged_at'] = datetime.now().isoformat()
                upsert_alert(stored_rec)
                rec_dict = stored_rec
                sh = stored_rec.get('self_heal')

        if rec_dict is None:
            return error_response(
                message='告警不存在',
                error='alert not found',
                status_code=404
            )

        # L5/#31 验证闭环：记录人工验证结论并落盘（resolution + 结构化补充 + 时间）
        try:
            rec_dict['resolution'] = resolution
            rec_dict['resolved_at'] = datetime.now().isoformat()
            rec_dict['verification_version'] = kb_extra['verification_version']
            rec_dict['reproduced_count'] = kb_extra['reproduced_count']
            rec_dict['regression_status'] = kb_extra['regression_status']
            rec_dict['verifier'] = kb_extra['verifier']
            rec_dict['verification_note'] = kb_extra['verification_note']
            # 结构化补充字段随告警一起落盘（复盘时可见）
            for k in ('final_root_cause', 'fix_action', 'affected_versions',
                      'fixed_version', 'owner_module'):
                if kb_extra.get(k):
                    rec_dict[k] = kb_extra[k]
            upsert_alert(rec_dict)
        except Exception as e:
            logger.warning(f"验证结论落盘失败(忽略): {e}")

        # Step4: 人工确认即沉淀自愈案例（仅当有有效根因时），供后续同类告警 RAG 复用。
        # #27：误报（false_positive）也沉淀——由知识库自动标记 resolved=False 并在检索时强降权，
        # 防止同样的误判反复污染推荐；verified_fixed 案例则加权靠前。
        promoted = False
        try:
            if sh and (sh.get("root_cause") or kb_extra.get("final_root_cause")):
                get_knowledge_base().add_case(
                    rec_dict, sh,
                    resolved=(resolution in ('verified_fixed', 'known_issue')),
                    source="acknowledge",
                    extra=kb_extra,
                )
                promoted = True
        except Exception as e:
            logger.warning(f"自愈案例沉淀失败(忽略): {e}")

        return success_response(
            message='告警已确认',
            data={'promoted': promoted, 'resolution': resolution}
        )
    except Exception as e:
        logger.error(f'确认告警失败: {e}', exc_info=True)
        return error_response(
            message='确认告警失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alerts/statistics', methods=['GET'])
def api_get_alert_statistics():
    """获取告警统计。无 task_id 时统计全局落盘历史。"""
    try:
        task_id = request.args.get('task_id')

        # 无 task_id：全局落盘历史统计
        if not task_id:
            stats = store_statistics()
            return success_response(data={'statistics': stats})

        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )

            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return success_response(data={
                    'total': 0,
                    'by_severity': {'high': 0, 'medium': 0, 'low': 0},
                    'by_type': {},
                    'acknowledged': 0,
                    'unacknowledged': 0
                })

            stats = alert_engine.get_statistics(device_id=task_info['device_id'])
            return success_response(data={'statistics': stats})
    except Exception as e:
        logger.error(f'获取告警统计失败: {e}', exc_info=True)
        return error_response(
            message='获取告警统计失败',
                error=str(e),
                status_code=500
            )


@log_monitor_bp.route('/api/alerts/<alert_id>/bug_report', methods=['POST'])
def api_generate_bug_report(alert_id):
    """根据告警记录生成结构化 Bug 单（标题+描述），供测试一键提交/复制/导出。"""
    try:
        records = load_recent(limit=200000)
        rec = next((r for r in records if r.get('id') == alert_id), None)
        if rec is None:
            return error_response(message='告警不存在', error='alert not found', status_code=404)
        bug = _build_bug_report(rec)
        return success_response(data=bug)
    except Exception as e:
        logger.error(f'生成Bug单失败: {e}', exc_info=True)
        return error_response(message='生成Bug单失败', error=str(e), status_code=500)


@log_monitor_bp.route('/api/alerts/test_report', methods=['GET'])
def api_generate_test_report():
    """生成测试回归/复测报告（按筛选条件汇总）。"""
    try:
        task_id = request.args.get('task_id')
        severity = request.args.get('severity')
        acknowledged = request.args.get('acknowledged')
        device_id = request.args.get('device_id')
        try:
            limit = max(1, min(5000, int(request.args.get('limit', 500))))
        except (TypeError, ValueError):
            limit = 500

        ack_filter = None
        if acknowledged is not None and acknowledged != '':
            ack_filter = str(acknowledged).lower() in ('1', 'true', 'yes', 'on')

        records = load_recent(
            limit=limit,
            task_id=task_id if task_id else None,
            device_id=device_id if device_id else None,
            severity=severity if severity else None,
            acknowledged=ack_filter,
        )

        scope_parts = []
        if task_id:
            scope_parts.append(f'任务 {task_id}')
        else:
            scope_parts.append('全局历史')
        if device_id:
            scope_parts.append(f'设备 {device_id}')
        if severity:
            scope_parts.append(f'严重度 {severity}')
        scope_label = ' / '.join(scope_parts)

        report = _build_test_report(records, scope_label=scope_label)
        return success_response(data=report)
    except Exception as e:
        logger.error(f'生成测试报告失败: {e}', exc_info=True)
        return error_response(message='生成测试报告失败', error=str(e), status_code=500)


def _build_bug_report(rec):
    """把落盘告警 + 自愈结果拼装成结构化 Bug 单（Markdown）。"""
    sh = rec.get('self_heal') or {}
    dev_ctx = sh.get('device_context') or {}
    severity = rec.get('severity') or sh.get('severity') or 'unknown'
    alert_type = rec.get('type') or sh.get('alert_type') or rec.get('rule_name') or '异常'
    rule_name = rec.get('rule_name') or ''
    device_id = rec.get('device_id') or sh.get('device_id') or '未知'
    package = rec.get('package_name') or '未知'
    log_line = rec.get('log_line') or ''
    timestamp = rec.get('timestamp') or rec.get('created_at') or ''
    root_cause = sh.get('root_cause') or '（AI 未给出根因）'
    confidence = sh.get('confidence') or '未知'
    status = sh.get('status') or '未知'
    suggestions = sh.get('suggestions') or []
    problem_location = sh.get('problem_location') or ''
    impact = sh.get('impact') or ''
    investigation_path = sh.get('investigation_path') or []
    suggested_patch = sh.get('suggested_patch') or ''
    resolution = rec.get('resolution') or ''
    resolution_label = _TEST_RESOLUTION_LABELS.get(resolution, '未确认')
    verification_version = rec.get('verification_version') or ''
    reproduced_count = int(rec.get('reproduced_count') or 0)
    regression_status = rec.get('regression_status') or _infer_regression_hint(rec)
    verifier = rec.get('verifier') or ''
    verification_note = rec.get('verification_note') or ''
    test_recommendations = _build_test_recommendations(rec, sh)
    version_insights = _collect_version_insights(rec, sh)

    t = (alert_type or '').lower()
    if any(k in t for k in ('crash', 'exception', 'fatal')):
        sev_tag = 'Crash'
    elif 'anr' in t:
        sev_tag = 'ANR'
    else:
        sev_tag = (severity or 'unknown').upper()

    exc_class = ''
    m = re.search(r'\b([A-Z]\w*Exception)\b', log_line)
    if m:
        exc_class = m.group(1)
    module = ''
    m = re.search(r'\b([A-Z]\w*(?:\.[A-Z]\w*)+)\s*:', log_line)
    if m:
        module = m.group(1)
    if not module:
        m = re.search(r'at\s+([a-zA-Z0-9_.]+)\.([a-zA-Z0-9_]+)\(', log_line)
        if m:
            module = m.group(1).split('.')[-1]
    if not module:
        module = rule_name or device_id
    exc_or_type = exc_class or alert_type

    title = f'【{sev_tag}】【{exc_or_type}】{module} 发生 {exc_or_type}'

    lines = []
    lines.append('## 问题概述')
    lines.append(f'{alert_type} 导致客户端异常（严重度：{severity}）')
    lines.append('')
    lines.append('## 环境信息')
    lines.append(f'- 设备ID：{device_id}')
    lines.append(f'- 设备型号：{dev_ctx.get("model") or "—"}')
    lines.append(f'- 系统版本：{dev_ctx.get("android_version") or "—"}')
    lines.append(f'- 固件版本：{dev_ctx.get("firmware") or "—"}')
    lines.append(f'- APK 版本：{dev_ctx.get("apk_version") or "—"}')
    lines.append(f'- 时间：{timestamp}')
    lines.append(f'- 监控规则：{rule_name or "—"}')
    lines.append(f'- 包名：{package}')
    lines.append(f'- 业务模块：{module}（自动提取）')
    lines.append('')
    lines.append('## 异常日志')
    lines.append('```')
    lines.append(log_line)
    lines.append('```')
    lines.append('')
    lines.append('## AI 诊断（AI故障诊断 Agent）')
    lines.append(f'- 根因：{root_cause}')
    conf_map = {'high': '高', 'medium': '中', 'low': '低'}
    lines.append(f'- 置信度：{conf_map.get(confidence, confidence)}')
    conf_reason = sh.get('confidence_reason') or ''
    if conf_reason:
        lines.append(f'- 置信依据：{conf_reason}')
    lines.append(f'- 当前状态：{status}')
    lines.append('')
    lines.append('## 测试验证结论')
    lines.append(f'- 当前结论：{resolution_label}')
    if verification_version:
        lines.append(f'- 验证版本：{verification_version}')
    if reproduced_count:
        lines.append(f'- 复现次数：{reproduced_count}')
    if regression_status:
        reg_map = {
            'new_issue': '首次发现',
            'regression': '疑似回归',
            'reopen': '已修复后再次出现',
            'stable_fixed': '连续验证稳定修复',
        }
        lines.append(f'- 回归判断：{reg_map.get(regression_status, regression_status)}')
    if verifier:
        lines.append(f'- 验证人：{verifier}')
    if verification_note:
        lines.append(f'- 验证备注：{verification_note}')
    if version_insights.get('known_versions'):
        lines.append(f'- 关联版本：{", ".join(version_insights.get("known_versions", []))}')
    lines.append('')
    # 证据链（#25）：区分直接证据 / 模型推断 / 历史引用，让结论可审计
    ec = sh.get('evidence_chain') or {}
    if ec.get('direct') or ec.get('inferred') or ec.get('references'):
        lines.append('## 证据链（直接证据 vs 模型推断）')
        if ec.get('direct'):
            lines.append('### 直接证据')
            for e in ec['direct']:
                src = f"（来源：{e.get('source')}）" if e.get('source') else ''
                detail = (e.get('detail') or '').replace('\n', '\n  ')
                lines.append(f"- 【{e.get('label', '')}】{detail} {src}")
        if ec.get('inferred'):
            lines.append('### 模型推断')
            for e in ec['inferred']:
                basis = f"（依据：{e.get('basis')}）" if e.get('basis') else ''
                lines.append(f"- 【{e.get('label', '')}】{e.get('detail', '')} {basis}")
        if ec.get('references'):
            lines.append('### 历史案例引用')
            for e in ec['references']:
                src = f"（{e.get('source')}）" if e.get('source') else ''
                lines.append(f"- 【{e.get('label', '')}】{e.get('detail', '')} {src}")
        lines.append('')
    lines.append('## 问题定位（L3 诊断决策）')
    lines.append(problem_location or '（AI 未提供定位，日志可能缺少完整堆栈）')
    lines.append('')
    lines.append('## 影响判断（L3 诊断决策）')
    lines.append(impact or '（AI 未提供影响判断）')
    lines.append('')
    lines.append('## 排查路径（L3 诊断决策）')
    if investigation_path:
        for i, p in enumerate(investigation_path, 1):
            lines.append(f'{i}. {p}')
    else:
        lines.append('（AI 未提供排查路径）')
    lines.append('')
    lines.append('## 修复建议')
    if suggestions:
        for i, s in enumerate(suggestions, 1):
            lines.append(f'{i}. {s}')
    else:
        lines.append('（AI 未给出建议）')
    lines.append('')
    # 相似历史案例（RAG 推荐）：展示历史相似问题及以往修复方式
    historical = sh.get('historical_cases') or []
    lines.append('## 相似历史案例（推荐）')
    if historical:
        for c in historical:
            rc = c.get('root_cause') or ''
            sugs = c.get('suggestions') or []
            # #27 有人工确认的最终根因/处理方式即视为高质量案例，不过滤
            low_q = (not (c.get('final_root_cause') or c.get('fix_action'))
                     and (len(rc) < 6 or len(sugs) == 0 or (len(sugs) == 1 and len(sugs[0]) < 6)))
            if low_q:
                continue
            rule_c = c.get('rule_name') or c.get('alert_type') or '未知'
            # #27 质量标识：人工已验证 > 已解决 > 待复盘
            if c.get('verified'):
                resolved_c = '人工已验证'
            elif c.get('resolved'):
                resolved_c = '已解决'
            else:
                resolved_c = '待复盘'
            hits_c = int(c.get('hit_count', 0) or 0)
            hits_tag = f' · 复用{hits_c}次' if hits_c > 0 else ''
            lines.append(f'- 历史案例（{rule_c} · {resolved_c}{hits_tag}）：')
            # #27 人工确认的最终根因/处理方式优先展示
            if c.get('final_root_cause'):
                lines.append(f'  - 最终根因（人工确认）：{c["final_root_cause"]}')
            lines.append(f'  - 以往根因{"（AI）" if c.get("final_root_cause") else ""}：{rc}')
            if c.get('fix_action'):
                lines.append(f'  - 最终处理方式：{c["fix_action"]}')
            ver_meta = ' / '.join(filter(None, [
                f'影响版本 {c["affected_versions"]}' if c.get('affected_versions') else '',
                f'修复版本 {c["fixed_version"]}' if c.get('fixed_version') else '',
                f'责任模块 {c["owner_module"]}' if c.get('owner_module') else '',
            ]))
            if ver_meta:
                lines.append(f'  - {ver_meta}')
            for j, s in enumerate(sugs[:2], 1):
                lines.append(f'  - 以往修复{j}：{s}')
    else:
        lines.append('（暂无历史相似案例，可在确认告警后沉淀为知识库）')
    lines.append('')
    # L4 自动生成 Patch：展示 AI 给出的修复代码建议（仅片段，需人工评审后落地，不自动改代码）
    lines.append('## 建议修复代码（L4 自动生成 · 需人工评审后落地）')
    if suggested_patch:
        lines.append('```')
        lines.append(suggested_patch)
        lines.append('```')
    else:
        lines.append('（AI 未生成可靠修复代码，建议结合排查路径人工修复）')
    lines.append('')
    lines.append('## 测试下一步建议')
    for i, item in enumerate(test_recommendations, 1):
        lines.append(f'{i}. {item}')
    lines.append('')
    lines.append('## 备注')
    lines.append('本 Bug 单由「AI故障诊断 Agent」自动生成；设备环境信息来自上下文收集（L2 能力），诊断决策（问题定位/影响判断/排查路径）来自 L3 能力，建议修复代码（L4 生成）仅供人工参考，不自动写入工程。')
    description = '\n'.join(lines)

    return {
        'title': title,
        'description': description,
        'severity': severity,
        'alert_type': alert_type,
        'device_id': device_id,
        'alert_id': rec.get('id'),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }


def _build_test_report(records, scope_label='全部告警'):
    """生成测试回归/复测报告（Markdown）。"""
    records = records or []
    generated_at = datetime.now().isoformat(timespec='seconds')
    resolution_counts = {k: 0 for k in _TEST_RESOLUTION_LABELS.keys()}
    severity_counts = {'high': 0, 'medium': 0, 'low': 0}
    regression_counts = {'new_issue': 0, 'regression': 0, 'reopen': 0, 'stable_fixed': 0}
    unresolved = 0
    verified = 0
    with_ai = 0

    for rec in records:
        sev = rec.get('severity')
        if sev in severity_counts:
            severity_counts[sev] += 1
        res = rec.get('resolution') or ''
        if res in resolution_counts:
            resolution_counts[res] += 1
        if rec.get('self_heal'):
            with_ai += 1
        if res == 'verified_fixed':
            verified += 1
        if res in ('', 'not_fixed', 'partially_fixed', 'not_reproducible'):
            unresolved += 1
        reg = rec.get('regression_status') or _infer_regression_hint(rec)
        if reg in regression_counts:
            regression_counts[reg] += 1

    top_records = sorted(
        records,
        key=lambda r: (
            {'high': 3, 'medium': 2, 'low': 1}.get(r.get('severity'), 0),
            r.get('timestamp') or '',
        ),
        reverse=True,
    )[:20]

    lines = []
    lines.append(f'# 测试回归/复测报告 - {scope_label}')
    lines.append('')
    lines.append('## 概览')
    lines.append(f'- 生成时间：{generated_at}')
    lines.append(f'- 告警总数：{len(records)}')
    lines.append(f'- 已接入 AI 诊断：{with_ai}')
    lines.append(f'- 已验证修复：{verified}')
    lines.append(f'- 仍需关注：{unresolved}')
    lines.append('')
    lines.append('## 严重度分布')
    lines.append(f'- 高：{severity_counts["high"]}')
    lines.append(f'- 中：{severity_counts["medium"]}')
    lines.append(f'- 低：{severity_counts["low"]}')
    lines.append('')
    lines.append('## 验证结论分布')
    for key, label in _TEST_RESOLUTION_LABELS.items():
        lines.append(f'- {label}：{resolution_counts.get(key, 0)}')
    lines.append('')
    lines.append('## 回归判断')
    reg_map = {
        'new_issue': '首次发现',
        'regression': '疑似回归',
        'reopen': '已修复后再次出现',
        'stable_fixed': '连续验证稳定修复',
    }
    for key, label in reg_map.items():
        lines.append(f'- {label}：{regression_counts.get(key, 0)}')
    lines.append('')
    lines.append('## 重点问题（最多 20 条）')
    if not top_records:
        lines.append('（本次范围内暂无告警）')
    else:
        for idx, rec in enumerate(top_records, 1):
            sh = rec.get('self_heal') or {}
            res = rec.get('resolution') or ''
            res_label = _TEST_RESOLUTION_LABELS.get(res, '未确认')
            title = rec.get('rule_name') or rec.get('type') or '未知告警'
            device = rec.get('device_id') or '未知设备'
            timestamp = rec.get('timestamp') or rec.get('created_at') or '-'
            version_insights = _collect_version_insights(rec, sh)
            ver = version_insights.get('current_version') or ''
            lines.append(f'{idx}. [{rec.get("severity", "unknown")}] {title} / {res_label}')
            lines.append(f'   - 时间：{timestamp}')
            lines.append(f'   - 设备：{device}')
            if ver:
                lines.append(f'   - 版本：{ver}')
            if version_insights.get('known_versions'):
                lines.append(f'   - 关联版本：{", ".join(version_insights.get("known_versions", []))}')
            if version_insights.get('regression_hint'):
                lines.append(f'   - 回归判断：{reg_map.get(version_insights.get("regression_hint"), version_insights.get("regression_hint"))}')
            if rec.get('verification_note'):
                lines.append(f'   - 验证备注：{rec.get("verification_note")}')
            root_cause = sh.get('root_cause') or ''
            if root_cause:
                lines.append(f'   - AI 根因：{root_cause[:180]}')
            lines.append(f'   - 日志：{(rec.get("log_line") or "")[:180]}')
    lines.append('')
    lines.append('## 测试建议')
    if unresolved:
        lines.append('1. 优先复测“验证未修复 / 部分修复 / 当前未复现”的问题，并补充版本与设备信息。')
    else:
        lines.append('1. 本轮未发现需继续跟踪的问题，可转入稳定性观察。')
    if regression_counts.get('regression') or regression_counts.get('reopen'):
        lines.append('2. 对疑似回归或复开问题建议单独建回归专题，关联历史修复版本。')
    if with_ai < len(records):
        lines.append('3. 部分告警尚无 AI 诊断，建议补抓完整日志后重新分析。')
    lines.append('4. 对高频同类问题建议补充知识库案例，提升后续自动推荐质量。')

    return {
        'title': f'测试回归报告_{scope_label}_{datetime.now().strftime("%Y%m%d_%H%M%S")}',
        'description': '\n'.join(lines),
        'generated_at': generated_at,
        'total': len(records),
    }


def _enrich_alert_record_for_test(rec: dict) -> dict:
    """为前端补充测试维度展示字段。"""
    if not isinstance(rec, dict):
        return rec
    out = dict(rec)
    sh = out.get('self_heal') or {}
    version_insights = _collect_version_insights(out, sh)
    out['regression_hint'] = version_insights.get('regression_hint') or ''
    out['known_versions'] = version_insights.get('known_versions') or []
    out['current_version'] = version_insights.get('current_version') or ''
    out['occurrence_count'] = int(out.get('occurrence_count') or 1)
    out['related_lines'] = out.get('related_lines') or []
    return out


def _enrich_with_historical(result, alert_context, log_text):
    """把知识库相似历史案例挂到分析结果上（让手动分析也能看到'以往怎么修的'）。"""
    data = result.to_dict()
    try:
        kb = get_knowledge_base()
        ctx = alert_context or {}
        hc_type = ctx.get('type') or ''
        hc_query = (ctx.get('rule_name') or '') + ' ' + (log_text or '')
        data['historical_cases'] = kb.search_similar(hc_type, hc_query, top_k=3)
    except Exception:
        data['historical_cases'] = []
    return data


def _attach_evidence_chain(data, log_lines, ctx_meta=None, trigger_line=''):
    """#25 为手动 AI 分析结果挂上证据链（与自愈链路同一构建器，保证口径一致）。"""
    try:
        data['evidence_chain'] = build_evidence_chain(
            root_cause=data.get('root_cause', ''),
            problem_location=data.get('problem_location', ''),
            impact=data.get('impact', ''),
            trigger_line=trigger_line,
            log_lines=log_lines,
            context_meta=ctx_meta,
            historical=data.get('historical_cases'),
        )
    except Exception as e:
        logger.warning(f"证据链构建失败(忽略): {e}")
    return data


_TEST_RESOLUTION_LABELS = {
    'verified_fixed': '已验证修复',
    'not_fixed': '验证未修复',
    'partially_fixed': '部分修复',
    'not_reproducible': '当前未复现',
    'false_positive': '误报',
    'known_issue': '已知问题',
}


def _infer_regression_hint(rec: dict) -> str:
    """根据历史记录给出简单的回归/复开辅助判断。"""
    if not rec:
        return ''
    if rec.get('regression_status'):
        return rec.get('regression_status')
    rule_name = rec.get('rule_name') or ''
    device_id = rec.get('device_id') or ''
    if not rule_name:
        return ''
    try:
        history = load_recent(limit=2000, device_id=device_id if device_id else None)
    except Exception:
        history = []
    same = [r for r in history if r.get('id') != rec.get('id') and r.get('rule_name') == rule_name]
    if not same:
        return 'new_issue'
    if any((r.get('resolution') == 'verified_fixed') for r in same):
        return 'reopen'
    if any((r.get('acknowledged') and r.get('resolution') in ('not_fixed', 'partially_fixed')) for r in same):
        return 'regression'
    return ''


def _collect_version_insights(rec: dict, sh: dict) -> dict:
    """收集版本/复现维度信息，便于测试快速判断是否回归。"""
    versions = set()
    cur = rec.get('verification_version') or (sh.get('device_context') or {}).get('apk_version') or ''
    if cur:
        versions.add(cur)
    for c in (sh.get('historical_cases') or [])[:5]:
        for v in (c.get('affected_versions'), c.get('fixed_version')):
            if v:
                versions.add(v)
    return {
        'current_version': cur,
        'known_versions': sorted(versions),
        'regression_hint': rec.get('regression_status') or _infer_regression_hint(rec),
    }


def _build_test_recommendations(rec: dict, sh: dict) -> list:
    """生成测试视角的下一步验证建议。"""
    recommendations = []
    alert_type = str(rec.get('type') or sh.get('alert_type') or '').lower()
    dev_ctx = sh.get('device_context') or {}
    if alert_type == 'crash':
        recommendations.append('请按原操作路径复测崩溃是否稳定消失，并附上 crash buffer 日志。')
    elif alert_type == 'anr':
        recommendations.append('请重点复测主流程卡顿是否消失，并补抓 ANR traces / cpuinfo。')
    elif alert_type == 'oom':
        recommendations.append('请在低内存设备上回归验证，并观察 meminfo / GC 日志是否仍异常。')
    else:
        recommendations.append('请按原复现路径回归验证，并确认是否仍出现相同关键日志。')

    if dev_ctx.get('apk_version'):
        recommendations.append(f'建议在当前 APK 版本 {dev_ctx.get("apk_version")} 与目标修复版本上各复测一次，确认差异。')
    if sh.get('confidence') == 'low':
        recommendations.append('当前 AI 结论置信度较低，建议补充更完整的上下文日志后再提单。')
    if rec.get('resolution') in ('not_fixed', 'partially_fixed'):
        recommendations.append('建议保留复现视频、复现步骤和版本号，作为回归未通过证据。')
    return recommendations[:4]


@log_monitor_bp.route('/api/analyze', methods=['GET', 'POST', 'OPTIONS'])
def api_analyze_logs():
    """AI 日志分析 - 根因分析与排查建议"""
    if request.method == 'OPTIONS':
        return '', 204
    if request.method == 'GET':
        return success_response(message='请使用 POST 方法，传入 log_lines 或 task_id+alert_id', data={'usage': 'POST with log_lines or task_id+alert_id'})
    try:
        data = request.get_json() or {}
        task_id = data.get('task_id')
        alert_id = data.get('alert_id')
        log_lines = data.get('log_lines', [])

        # 方式1: 传入原始日志
        if log_lines:
            alert_context = data.get('alert_context')
            agent = get_agent()
            result = agent.analyze(log_lines=log_lines, alert_context=alert_context)
            trigger = log_lines[-1] if log_lines else ''
            data1 = _enrich_with_historical(result, alert_context, trigger)
            data1 = _attach_evidence_chain(data1, log_lines, trigger_line=trigger)
            return success_response(data=data1)

        # 方式2: 从运行中的任务获取告警 + 上下文日志
        if not task_id or not alert_id:
            return error_response(
                message='请提供 task_id 和 alert_id，或直接提供 log_lines',
                error='missing_params',
                status_code=400
            )

        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在或已停止',
                    error='task not found',
                    status_code=404
                )
            alert_engine = task_info.get('alert_engine')
            log_queue = task_info.get('log_queue', [])
            log_queue_lock = task_info.get('log_queue_lock')

        if not alert_engine:
            return error_response(
                message='告警引擎不可用',
                error='no alert engine',
                status_code=500
            )

        # 查找告警
        alerts = alert_engine.get_alerts(limit=500)
        alert_record = next((a for a in alerts if a.id == alert_id), None)
        if not alert_record:
            return error_response(
                message='告警不存在',
                error='alert not found',
                status_code=404
            )

        # 获取告警前后的日志作为上下文（优先告警周边，否则退回最近50条）
        with log_queue_lock:
            all_logs = [(i, item.get('log', '')) for i, item in enumerate(log_queue) if item.get('log')]
        # #23 动态扩窗：按告警类型截取（crash 保完整堆栈 / anr 保主线程段 / oom 补内存段）
        context_logs, ctx_meta = _get_dynamic_context_around_alert(
            all_logs, alert_record.log_line, alert_record.type
        )

        alert_context = {
            'rule_name': alert_record.rule_name,
            'severity': alert_record.severity,
            'type': alert_record.type,
            'log_line': alert_record.log_line,
        }

        agent = get_agent()
        result = agent.analyze(log_lines=context_logs, alert_context=alert_context)
        data = _enrich_with_historical(result, alert_context, alert_record.log_line or '')
        if isinstance(data, dict) and ctx_meta:
            data.setdefault('context_meta', ctx_meta)
        data = _attach_evidence_chain(
            data, context_logs, ctx_meta=ctx_meta, trigger_line=alert_record.log_line or ''
        )
        return success_response(data=data)

    except FileNotFoundError as e:
        return error_response(
            message=str(e),
            error='llm_not_configured',
            status_code=400
        )
    except ValueError as e:
        return error_response(
            message=str(e),
            error='config_error',
            status_code=400
        )
    except Exception as e:
        logger.exception(f'AI 日志分析失败: {e}')
        return error_response(
            message=f'分析失败: {str(e)}',
            error='analysis_failed',
            status_code=500
        )


@log_monitor_bp.route('/api/runbook', methods=['POST'])
def api_generate_runbook():
    """根据告警类型生成简易排查 runbook（3～5 步）。"""
    try:
        data = request.get_json() or {}
        alert_type = (data.get('alert_type') or '').strip()
        alert_id = data.get('alert_id')
        task_id = data.get('task_id')
        log_snippet = data.get('log_snippet') or ''

        if not alert_type and (alert_id and task_id):
            with MONITOR_TASKS_LOCK:
                task_info = MONITOR_TASKS.get(task_id)
                if task_info:
                    alert_engine = task_info.get('alert_engine')
                    if alert_engine:
                        alerts = alert_engine.get_alerts(limit=500)
                        alert_record = next((a for a in alerts if a.id == alert_id), None)
                        if alert_record:
                            alert_type = alert_record.type or 'keyword'
                            if not log_snippet and alert_record.log_line:
                                log_snippet = alert_record.log_line[:400]

        if not alert_type:
            alert_type = 'keyword'

        agent = get_agent()
        runbook = agent.generate_runbook(alert_type=alert_type, log_snippet=log_snippet or None)
        return success_response(data={'runbook': runbook})
    except FileNotFoundError:
        return error_response(
            message='LLM 未配置，请先在用例管理中配置 LLM',
            error='llm_not_configured',
            status_code=400
        )
    except Exception as e:
        logger.exception(f'生成 runbook 失败: {e}')
        return error_response(
            message=f'生成失败: {str(e)}',
            error='runbook_failed',
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules', methods=['GET'])
def api_get_alert_rules():
    """获取告警规则列表"""
    try:
        task_id = request.args.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return success_response(data={'rules': []})
            
            rules = list(alert_engine.rules.values())
            return success_response(data={
                'rules': [
                    {
                        'id': r.id,
                        'name': r.name,
                        'type': r.type,
                        'pattern': r.pattern,
                        'severity': r.severity,
                        'enabled': r.enabled,
                        'description': r.description,
                        'action': r.action
                    }
                    for r in rules
                ]
            })
    except Exception as e:
        logger.error(f'获取告警规则失败: {e}', exc_info=True)
        return error_response(
            message='获取告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules', methods=['POST'])
def api_create_alert_rule():
    """创建告警规则"""
    try:
        data = request.get_json() or {}
        validation_error = validate_required(data, 'task_id', 'name', 'type', 'pattern')
        if validation_error:
            return validation_error
        
        task_id = data.get('task_id')

        # #28 保存前预检：regex 类型的非法正则直接报错给用户，杜绝"能存但永不触发"的静默漏报
        pattern_err = validate_rule_pattern(data.get('type', ''), data.get('pattern', ''))
        if pattern_err:
            return error_response(
                message=pattern_err,
                error='invalid pattern',
                status_code=400
            )

        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return error_response(
                    message='告警引擎不存在',
                    error='alert engine not found',
                    status_code=404
                )
            
            rule = AlertRule(
                id=data.get('id', f"rule_{int(time.time())}"),
                name=data['name'],
                type=data['type'],
                pattern=data['pattern'],
                severity=data.get('severity', 'medium'),
                enabled=data.get('enabled', True),
                description=data.get('description', ''),
                action=data.get('action', '')
            )
            
            if alert_engine.add_rule(rule):
                return success_response(
                    data={'rule': {
                        'id': rule.id,
                        'name': rule.name,
                        'type': rule.type,
                        'pattern': rule.pattern,
                        'severity': rule.severity,
                        'enabled': rule.enabled,
                        'description': rule.description,
                        'action': rule.action
                    }},
                    message='告警规则创建成功'
                )
            else:
                return error_response(
                    message='规则ID已存在',
                    error='rule id exists',
                    status_code=400
                )
    except Exception as e:
        logger.error(f'创建告警规则失败: {e}', exc_info=True)
        return error_response(
            message='创建告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules/<rule_id>', methods=['PUT'])
def api_update_alert_rule(rule_id):
    """更新告警规则"""
    try:
        data = request.get_json() or {}
        task_id = request.args.get('task_id') or data.get('task_id')
        
        if not task_id:
            return error_response(
                message='缺少任务ID',
                error='task_id required',
                status_code=400
            )
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return error_response(
                    message='告警引擎不存在',
                    error='alert engine not found',
                    status_code=404
                )
            
            # 获取现有规则或创建新规则对象
            existing_rule = alert_engine.rules.get(rule_id)
            if not existing_rule:
                return error_response(
                    message='规则不存在',
                    error='rule not found',
                    status_code=404
                )

            # #28 更新前预检：以"更新后生效的 type+pattern"为准做正则合法性校验
            eff_type = data.get('type', existing_rule.type)
            eff_pattern = data.get('pattern', existing_rule.pattern)
            pattern_err = validate_rule_pattern(eff_type, eff_pattern)
            if pattern_err:
                return error_response(
                    message=pattern_err,
                    error='invalid pattern',
                    status_code=400
                )

            # 更新字段
            rule = AlertRule(
                id=rule_id,
                name=data.get('name', existing_rule.name),
                type=data.get('type', existing_rule.type),
                pattern=data.get('pattern', existing_rule.pattern),
                severity=data.get('severity', existing_rule.severity),
                enabled=data.get('enabled', existing_rule.enabled),
                description=data.get('description', existing_rule.description),
                action=data.get('action', existing_rule.action)
            )
            
            if alert_engine.update_rule(rule):
                return success_response(
                    data={'rule': {
                        'id': rule.id,
                        'name': rule.name,
                        'type': rule.type,
                        'pattern': rule.pattern,
                        'severity': rule.severity,
                        'enabled': rule.enabled,
                        'description': rule.description,
                        'action': rule.action
                    }},
                    message='告警规则更新成功'
                )
            else:
                return error_response(
                    message='更新规则失败',
                    error='update failed',
                    status_code=500
                )
    except Exception as e:
        logger.error(f'更新告警规则失败: {e}', exc_info=True)
        return error_response(
            message='更新告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules/<rule_id>', methods=['DELETE'])
def api_delete_alert_rule(rule_id):
    """删除告警规则"""
    try:
        task_id = request.args.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            task_info = MONITOR_TASKS.get(task_id)
            if not task_info:
                return error_response(
                    message='任务不存在',
                    error='task not found',
                    status_code=404
                )
            
            alert_engine = task_info.get('alert_engine')
            if not alert_engine:
                return error_response(
                    message='告警引擎不存在',
                    error='alert engine not found',
                    status_code=404
                )
            
            if alert_engine.delete_rule(rule_id):
                return success_response(message='告警规则删除成功')
            else:
                return error_response(
                    message='规则不存在',
                    error='rule not found',
                    status_code=404
                )
    except Exception as e:
        logger.error(f'删除告警规则失败: {e}', exc_info=True)
        return error_response(
            message='删除告警规则失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/alert-rules/test-match', methods=['POST'])
def api_test_rule_match():
    """#28 规则试匹配：贴样例日志行即可验证规则是否命中（不落库、不触发告警）。

    入参: {type, pattern, sample_lines: [str, ...]}（sample_lines 也兼容单字符串 sample_line）
    出参: {valid, error, results: [{line, matched}], matched_count}
    """
    try:
        data = request.get_json() or {}
        rule_type = (data.get('type') or 'keyword').strip()
        pattern = data.get('pattern') or ''
        samples = data.get('sample_lines')
        if samples is None:
            single = data.get('sample_line')
            samples = [single] if single else []
        if isinstance(samples, str):
            samples = [samples]
        samples = [str(s) for s in samples if str(s).strip()][:50]  # 上限 50 行防滥用

        # 正则合法性预检（与保存口径一致）
        pattern_err = validate_rule_pattern(rule_type, pattern)
        if pattern_err:
            return success_response(data={
                'valid': False,
                'error': pattern_err,
                'results': [],
                'matched_count': 0,
            })

        # 用与线上完全相同的 AlertRule.matches() 试匹配，保证"试的=真的"
        probe_rule = AlertRule(
            id='__test__', name='试匹配', type=rule_type,
            pattern=pattern, severity='low',
        )
        results = [{'line': line, 'matched': bool(probe_rule.matches(line))} for line in samples]
        return success_response(data={
            'valid': True,
            'error': '',
            'results': results,
            'matched_count': sum(1 for r in results if r['matched']),
        })
    except Exception as e:
        logger.error(f'规则试匹配失败: {e}', exc_info=True)
        return error_response(
            message='规则试匹配失败',
            error=str(e),
            status_code=500
        )


@log_monitor_bp.route('/api/status', methods=['GET'])
def api_get_status():
    """获取监控任务状态"""
    try:
        task_id = request.args.get('task_id')
        
        with MONITOR_TASKS_LOCK:
            if task_id:
                # 获取特定任务状态
                task_info = MONITOR_TASKS.get(task_id)
                if not task_info:
                    return error_response(
                        message='任务不存在',
                        error='task not found',
                        status_code=404
                    )
                
                return success_response(data={
                    'task_id': task_id,
                    'device_id': task_info['device_id'],
                    'start_time': task_info['start_time'],
                    'running_time': int(time.time() - task_info['start_time']),
                    'is_running': True
                })
            else:
                # 获取所有任务状态
                tasks = []
                for tid, info in MONITOR_TASKS.items():
                    tasks.append({
                        'task_id': tid,
                        'device_id': info['device_id'],
                        'start_time': info['start_time'],
                        'running_time': int(time.time() - info['start_time'])
                    })
                
                return success_response(data={
                    'has_running_task': len(tasks) > 0,
                    'tasks': tasks
                })
    except Exception as e:
        logger.error(f'获取状态失败: {e}', exc_info=True)
        return error_response(
            message='获取状态失败',
            error=str(e),
            status_code=500
        )
