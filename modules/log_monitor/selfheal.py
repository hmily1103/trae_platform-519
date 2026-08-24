#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
log_monitor 自愈 Agent（Self-Heal Agent）

职责：在告警触发后，自动完成
    告警 -> LLM 根因分析 -> 只读排查采集 -> 证据回写 -> 人工确认建议
的自主闭环前半段。

设计原则（不影响平台其他功能）：
- 本文件为纯新增模块，不修改任何现有文件。导入仅依赖同目录的 .agent。
- 只读排查：默认只采集日志/指标作为证据，不执行重启/卸载等危险动作。
- 危险命令黑名单：任何含 restart/install/uninstall/rm/kill 等的 shell 一律拒绝。
- 通过 SELF_HEAL_MODE 控制行为：
    observe = 仅分析并给出建议（最安全，默认）
    collect = 分析 + 自动采集只读证据
    assist  = 分析 + 采集 + 标记可自动执行的修复（仍不自动执行）
"""

import logging
import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from .agent import get_agent
from .knowledge_base import get_knowledge_base

logger = logging.getLogger(__name__)

# 护栏：自愈模式（可由环境变量 LOG_SELF_HEAL_MODE 覆盖）
SELF_HEAL_MODE = os.environ.get("LOG_SELF_HEAL_MODE", "observe")  # observe | collect | assist | off

# 对外呈现：避免“自愈”误导，强调当前为诊断定位能力，自动修复列为远期规划
AGENT_NAME = "AI故障诊断 Agent"
AGENT_ROADMAP = "Diagnose → Assist → Self-Healing"
# 模式 -> 对外阶段标签（Observe 为当前默认阶段，仅做诊断与定位）
STAGE_LABELS = {
    "observe": "Observe（观察诊断）",
    "collect": "Collect（上下文采集）",
    "assist": "Assist（辅助处理）",
    "off": "Off（已关闭）",
}

# 护栏：自动关单开关默认关闭（详见 handle_alert 运行时读取，无需重启即可调整）
# 高危告警永不自动关单，仅 AUTO_RESOLVED 高信心案例在显式开启后才自动关。

# 护栏：LLM 分析失败时的重试上限（防死循环，仅重试分析、不重试执行）
MAX_ANALYZE_RETRIES = 1

# 护栏：判定"可自动关单"所需的历史案例关键词重合数下限
AUTO_CLOSE_MIN_OVERLAP = 2

# 护栏：危险动作黑名单（命中则拒绝执行任何 shell）
DANGEROUS_TOKENS = (
    "restart", "reboot", "install", "uninstall",
    " rm ", "kill", "am force-stop", "pm clear", "factory",
)

# 匹配 at com.x.Y.z(Y.java:123) 形式的堆栈帧（证据链/置信度共用）
_STACK_AT_RE = re.compile(r'\bat\s+[\w.$]+\([\w$.]+\.(java|kt):\d+\)')


def build_evidence_chain(
    root_cause: str = "",
    problem_location: str = "",
    impact: str = "",
    trigger_line: str = "",
    log_lines: Optional[List[str]] = None,
    context_meta: Optional[Dict[str, Any]] = None,
    device_context: Optional[Dict[str, str]] = None,
    probe_evidence: Optional[List[str]] = None,
    historical: Optional[List[Dict[str, Any]]] = None,
    action_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, str]]]:
    """构建结构化证据链（#25）：区分「直接证据 / 模型推断 / 历史案例引用」。

    目的：让每次 AI 结论可审计——用户能看到结论依据了哪些日志/设备信息/历史案例，
    并区分"日志里真实存在的直接证据"与"模型自己的推断"，提升可信度。
    纯函数、无副作用，供 selfheal.handle_alert 与 views 手动分析接口共用。
    """
    direct: List[Dict[str, str]] = []
    inferred: List[Dict[str, str]] = []
    references: List[Dict[str, str]] = []
    log_lines = log_lines or []

    # 直接证据①：触发日志行（告警的第一现场）
    if trigger_line:
        direct.append({
            "kind": "trigger_log", "label": "触发日志行",
            "detail": trigger_line[:300], "source": "监控日志流",
        })
    # 直接证据②：上下文中真实存在的异常堆栈帧
    stack_hits = [l.strip() for l in log_lines if l and _STACK_AT_RE.search(l)]
    if stack_hits:
        direct.append({
            "kind": "stack_frames", "label": "异常堆栈",
            "detail": "\n".join(stack_hits[:8]),
            "source": f"上下文日志（共 {len(stack_hits)} 帧，最多展示 8 帧）",
        })
    # 直接证据③：本次分析实际使用的日志窗口（#23 context_meta）
    if context_meta:
        rng = context_meta.get("range")
        detail = (
            f"截取策略 {context_meta.get('strategy') or '默认'}，"
            f"共 {context_meta.get('lines', len(log_lines))} 行"
        )
        if rng and len(rng) == 2:
            detail += f"，日志区间 {rng[0]}~{rng[1]}"
        direct.append({
            "kind": "context_window", "label": "分析用日志窗口",
            "detail": detail, "source": "动态上下文扩窗",
        })
    # 直接证据④：设备环境（adb 只读采集）
    if device_context:
        parts = [f"{k}={v}" for k, v in device_context.items() if v]
        if parts:
            direct.append({
                "kind": "device_context", "label": "设备环境",
                "detail": " / ".join(parts), "source": "adb 只读采集（getprop/dumpsys）",
            })
    # 直接证据⑤：只读探针输出（collect/assist 模式）
    # #26 起证据格式为 "$ 命令\n输出"，取命令行作为来源标签；失败/空输出不进证据链
    for i, ev in enumerate(probe_evidence or []):
        text = str(ev or "").strip()
        if not text:
            continue
        source = "adb 只读命令"
        body = text
        if text.startswith("$ ") and "\n" in text:
            first, body = text.split("\n", 1)
            source = first.strip()
            body = body.strip()
        if not body or body.startswith("采集失败") or body == "(无输出)":
            continue
        direct.append({
            "kind": "probe", "label": f"只读探针采集#{i + 1}",
            "detail": body[:200], "source": source,
        })

    # 直接证据⑥：告警动作产物（#29 只读动作执行链路：截图/白名单 shell 输出）
    if action_result and action_result.get("status") == "ok":
        a_type = action_result.get("type", "")
        detail = action_result.get("summary", "")
        if action_result.get("artifact"):
            detail += f"（产物: {action_result['artifact']}）"
        elif action_result.get("output"):
            detail += "\n" + str(action_result["output"])[:200]
        direct.append({
            "kind": "action_artifact",
            "label": "截图取证" if a_type == "screenshot" else "告警动作采集",
            "detail": detail,
            "source": f"告警动作自动执行（{action_result.get('command', a_type)[:80]}）",
        })

    # 问题定位：若能在堆栈帧中找到对应 文件:行号，则升级为直接证据；否则标为推断
    if problem_location:
        frags = re.findall(r'[\w$]+\.(?:java|kt):\d+', problem_location)
        stack_text = "\n".join(stack_hits)
        backed = bool(stack_hits) and bool(frags) and any(f in stack_text for f in frags)
        item = {
            "kind": "problem_location", "label": "问题定位",
            "detail": problem_location,
        }
        if backed:
            item["source"] = "由上下文日志堆栈直接提取（可回溯）"
            direct.append(item)
        else:
            item["basis"] = "LLM 推断（上下文日志未见对应堆栈行，建议人工核对）"
            inferred.append(item)

    # 模型推断：根因 / 影响判断
    if root_cause and not root_cause.startswith("分析失败"):
        inferred.append({
            "kind": "root_cause", "label": "根因结论",
            "detail": root_cause, "basis": "LLM 基于上述直接证据推断",
        })
    if impact:
        inferred.append({
            "kind": "impact", "label": "影响判断",
            "detail": impact, "basis": "LLM/规则按告警类型推断",
        })

    # 历史案例引用（RAG）
    for c in (historical or [])[:3]:
        references.append({
            "kind": "historical_case",
            "label": c.get("rule_name") or c.get("alert_type") or "历史案例",
            "detail": (c.get("root_cause") or "")[:200],
            "source": (
                f"知识库案例 {c.get('id', '')}"
                f"（相似度 {c.get('_score', '-')}，"
                f"{'已解决' if c.get('resolved') else '待复盘'}）"
            ),
        })
    return {"direct": direct, "inferred": inferred, "references": references}


# 每种告警类型的只读排查模板（adb shell 命令，均为只读采集）——#26 补强
# 注意：探针字符串不得包含 DANGEROUS_TOKENS 黑名单词（如 kill/restart），
#       因此 OOM 探针用 'lowmemory|lmkd' 而非 'lowmemorykiller'，否则会被自身护栏拒绝。
# 权限不足的探针（如 /data/anr）通过 2>/dev/null 自动降级为空输出，绝不阻断流程。
READONLY_PROBES = {
    "crash": [
        # 崩溃专用缓冲区：完整 FATAL 堆栈的第一手来源（主 logcat 可能已被刷掉）
        "logcat -b crash -d -t 300 || true",
        "logcat -d -t 200 | grep -iE 'FATAL|crash|exception' || true",
        # 进程是否存活/重启（判断崩溃后状态）
        "ps -A | grep -m 5 {package} || true",
        "dumpsys meminfo {package} || true",
    ],
    "anr": [
        "dumpsys activity processes || true",
        "logcat -d -t 200 | grep -iE 'ANR|not responding' || true",
        # ANR 现场 traces（普通权限大概率拒绝，降级为空；root 设备可拿到主线程栈）
        "cat /data/anr/traces.txt 2>/dev/null | head -120 || true",
        "ls /data/anr/ 2>/dev/null && cat /data/anr/anr_* 2>/dev/null | head -120 || true",
        # 主线程卡顿常伴随 CPU 争抢：看 top 占用
        "dumpsys cpuinfo | head -40 || true",
    ],
    "exception": [
        "logcat -b crash -d -t 150 || true",
        # 带上下文窗口的异常段（前2行/后8行），而非孤立单行
        "logcat -d -t 300 | grep -iE -B2 -A8 'Exception|Error' | head -120 || true",
    ],
    "oom": [
        "dumpsys meminfo {package} || true",
        "cat /proc/meminfo || true",
        # 目标进程内存明细（VmRSS/VmPeak 等；进程已死则降级为空）
        "cat /proc/$(pidof -s {package})/status 2>/dev/null | head -30 || true",
        # 低内存/GC 信号（避免黑名单词：用 lowmemory|lmkd）
        "logcat -d -t 300 | grep -iE 'lowmemory|lmkd|onTrimMemory|GC_' | head -60 || true",
        # 系统级内存概览
        "dumpsys meminfo | head -40 || true",
    ],
    "keyword": [
        "logcat -d -t 200 | grep -i '{keyword}' || true",
        # 关键词上下文窗口（前后各5行），便于 LLM 看到关键词所处语境
        "logcat -d -t 400 | grep -i -C 5 '{keyword}' | head -120 || true",
    ],
}


class SelfHealAgent:
    """日志自愈 Agent：分析告警并采集只读证据，闭环前半段。"""

    def __init__(self, device_id: str, target_package: str = "", mode: str = None):
        self.device_id = device_id
        self.target_package = target_package or "com.thunder.ktv"
        self.mode = (mode or SELF_HEAL_MODE).lower()
        self._agent = get_agent()

    def handle_alert(
        self,
        alert: Dict[str, Any],
        log_lines: List[str],
        context_meta: Optional[Dict[str, Any]] = None,
        action_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """处理一条告警，返回自愈结果（不含危险执行）。

        :param alert: 告警字典，至少可含 type/severity/rule_name/log_line
        :param log_lines: 告警上下文日志行列表（建议含前后各 10-20 行）
        :param context_meta: 可选，#23 动态扩窗产出的上下文元信息（进证据链）
        :param action_result: 可选，#29 告警动作产物（截图/只读输出，挂进直接证据）
        :return: 结构化自愈结果，供挂载方回写/展示
        """
        alert_type = (alert.get("type") or "keyword").lower()
        rule_name = alert.get("rule_name") or alert.get("name") or ""
        severity = alert.get("severity") or "medium"
        trigger_line = alert.get("log_line") or (log_lines[-1] if log_lines else "")

        # 0) RAG：检索历史相似案例，作为 LLM 分析参考（无案例时不影响原行为）
        historical = []
        try:
            kb = get_knowledge_base()
            historical = kb.search_similar(
                alert_type, f"{rule_name} {trigger_line}", top_k=3
            )
        except Exception as e:
            logger.warning(f"自愈知识库检索失败(忽略): {e}")

        # 0.5) 设备环境采集（只读、安全；所有模式均采集，让 LLM 分析有据可依，而非凭空猜测）
        device_context = self._collect_device_context()

        # 1) LLM 根因分析（复用现有 LogAnalysisAgent，注入历史案例；偶发失败重试 1 次）
        # 注意：以下字段必须先初始化——若 LLM 重试全部失败，后续引用不能 NameError
        root_cause = ""
        suggestions: List[str] = []
        problem_location = ""
        impact = ""
        investigation_path: List[str] = []
        suggested_patch = ""
        last_err = ""
        for attempt in range(MAX_ANALYZE_RETRIES + 1):
            try:
                result = self._agent.analyze(
                    log_lines=log_lines or [trigger_line],
                    alert_context={
                        "rule_name": rule_name,
                        "severity": severity,
                        "type": alert_type,
                        "log_line": trigger_line,
                        "historical_cases": historical,
                        "device_context": device_context,
                    },
                )
                root_cause = getattr(result, "root_cause", "") or ""
                suggestions = getattr(result, "suggestions", []) or []
                # L3 诊断决策结构化：问题定位 / 影响判断 / 排查路径
                problem_location = getattr(result, "problem_location", "") or ""
                impact = getattr(result, "impact", "") or ""
                investigation_path = getattr(result, "investigation_path", []) or []
                # L4 自动生成 Patch：针对根因的修复代码建议（仅片段，不落地）
                suggested_patch = getattr(result, "suggested_patch", "") or ""
                last_err = ""
                break
            except Exception as e:  # 分析失败重试，仍失败则留人工
                last_err = str(e)
                logger.warning(f"自愈 Agent LLM 分析失败(第{attempt + 1}次): {e}")
        if last_err and not root_cause:
            root_cause = f"分析失败: {last_err}"

        # 2) 只读排查采集（仅 collect/assist 模式）
        evidence: List[str] = []
        if self.mode in ("collect", "assist"):
            evidence = self._collect_readonly_evidence(alert_type, trigger_line)

        # 3) 复核评估：高危/无法定位 -> 交人工；命中已知已解决案例 -> 可自动关单；否则已分析待人工
        # 自动关单开关运行时读取，运维可动态调整，无需重启（默认关闭）
        auto_close_enabled = os.environ.get("LOG_SELF_HEAL_AUTO_CLOSE", "false").lower() in ("1", "true", "yes", "on")
        confidence, confidence_reason, conf_breakdown = self._assess_confidence(
            root_cause, historical, rule_name,
            problem_location=problem_location,
            log_lines=log_lines,
            context_meta=context_meta,
            evidence=evidence,
        )
        if severity == "high" or not root_cause or root_cause.startswith("分析失败"):
            status = "NEEDS_HUMAN"
            auto_closeable = False
        elif confidence == "high":
            status = "AUTO_RESOLVED"
            auto_closeable = auto_close_enabled
        else:
            status = "ANALYZED"
            auto_closeable = False

        # 4) 证据链（#25）：结构化记录本次结论的依据，区分直接证据/模型推断/历史引用
        evidence_chain = build_evidence_chain(
            root_cause=root_cause,
            problem_location=problem_location,
            impact=impact,
            trigger_line=trigger_line,
            log_lines=log_lines,
            context_meta=context_meta,
            device_context=device_context,
            probe_evidence=evidence,
            historical=historical,
            action_result=action_result,
        )

        return {
            "device_id": self.device_id,
            "alert_type": alert_type,
            "severity": severity,
            "root_cause": root_cause,
            "suggestions": suggestions,
            "problem_location": problem_location,
            "impact": impact,
            "investigation_path": investigation_path,
            "suggested_patch": suggested_patch,
            "evidence": evidence,
            "evidence_chain": evidence_chain,
            "context_meta": context_meta,
            "historical_cases": historical,
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "confidence_score": conf_breakdown.get("score", 0),
            "confidence_breakdown": conf_breakdown,
            "auto_closeable": auto_closeable,
            "status": status,
            "needs_human": status in ("NEEDS_HUMAN", "ANALYZED"),
            "mode": self.mode,
            "alert_id": alert.get("id") if isinstance(alert, dict) else None,
            "agent_name": AGENT_NAME,
            "stage": STAGE_LABELS.get(self.mode, self.mode),
            "roadmap": AGENT_ROADMAP,
            "device_context": device_context,
        }

    def _collect_device_context(self) -> Dict[str, str]:
        """采集设备环境信息（只读、安全、所有模式均采集）。

        目的：让 LLM 分析"有据"——知道崩溃发生在什么设备/系统/APK 上，
        而不是脱离环境盲猜。对应 L2 上下文收集 Agent 的「设备信息」部分。
        任何一项采集失败都降级为空字符串，绝不阻断分析流程。
        """
        props = {
            "model": "ro.product.model",
            "android_version": "ro.build.version.release",
            "firmware": "ro.build.display.id",
        }
        ctx: Dict[str, str] = {}
        for key, prop in props.items():
            cmd = self._safe_adb(f"getprop {prop}", "")
            if not cmd:
                ctx[key] = ""
                continue
            try:
                proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
                ctx[key] = (proc.stdout or "").strip()
            except Exception:
                ctx[key] = ""
        # APK 版本（需知目标包名）
        apk_version = ""
        if self.target_package:
            cmd = self._safe_adb("dumpsys package {package} | grep -m1 versionName", "")
            if cmd:
                try:
                    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
                    m = re.search(r"versionName=([^\s]+)", proc.stdout or "")
                    if m:
                        apk_version = m.group(1)
                except Exception:
                    pass
        ctx["apk_version"] = apk_version
        return ctx

    def _assess_confidence(
        self,
        root_cause: str,
        historical: List[Dict[str, Any]],
        rule_name: str = "",
        problem_location: str = "",
        log_lines: Optional[List[str]] = None,
        context_meta: Optional[Dict[str, Any]] = None,
        evidence: Optional[List[str]] = None,
    ) -> tuple:
        """置信度分项打分（#25 三信号 → #30 分项 0-100，保持规则化可解释）。

        四个维度各打 0-100，加权得综合分（权重见 WEIGHTS）：
        - stack   (权重30)：命中堆栈帧（problem_location 非空 且 日志真实存在 at ...java:行号）;
                            仅模型给出定位但无堆栈佐证给 50（未核实）
        - history (权重40)：命中已解决历史案例（沿用旧口径，自动关单唯一准入）;
                            已解决但未达阈值给 60（部分重合）
        - context (权重15)：本次分析拿到的日志行数充足度（>=10 满分 / 5-9 半分）
        - probe   (权重15)：只读探针采集到有效输出（collect/assist 模式才有）

        返回 (level, reason, breakdown)。等级与旧版完全一致（自动关单口径不放宽）：
        - high  = 历史命中（history_hit）
        - medium= 历史未中 且 堆栈命中 + 上下文充足
        - low   = 其余 / 根因无效
        高危 / 无根因由调用方单独判 NEEDS_HUMAN。
        """
        # 维度权重（单一事实来源，前端同步展示）
        STACK_W, HISTORY_W, CONTEXT_W, PROBE_W = 30, 40, 15, 15
        log_lines = log_lines or []
        evidence = evidence or []

        # 维度① 堆栈
        stack_hit = bool(problem_location) and any(
            _STACK_AT_RE.search(l or "") for l in log_lines
        )
        if stack_hit:
            stack_score, stack_note = 100, "命中堆栈帧（已定位到具体 文件:行号）"
        elif problem_location:
            stack_score, stack_note = 50, "模型给出问题定位但日志无对应堆栈佐证（未核实）"
        else:
            stack_score, stack_note = 0, "未命中堆栈（无法定位到具体代码行）"

        # 维度② 历史案例（旧口径原样保留，history_hit 仍决定 high）
        history_hit = False
        if not historical:
            history_score, history_note = 0, "无历史相似案例"
        else:
            top = historical[0]
            score = top.get("_score", 0)
            same_rule = bool(top.get("rule_name")) and top.get("rule_name") == rule_name
            if not top.get("resolved", False):
                history_score, history_note = 0, "历史案例未标记已解决"
            elif score >= 1 + AUTO_CLOSE_MIN_OVERLAP or same_rule:
                history_hit = True
                history_score, history_note = 100, (
                    f"命中已知已解决案例 {top.get('id', '')}"
                    f"（重合度 {score}，同规则={same_rule}）"
                )
            else:
                history_score, history_note = 60, (
                    f"历史案例部分重合（重合度 {score} < {1 + AUTO_CLOSE_MIN_OVERLAP}，"
                    f"同规则={same_rule}），未达自动关单阈值"
                )

        # 维度③ 上下文充足度（结合 #23 的 context_meta）
        ctx_lines = len(log_lines)
        if context_meta and isinstance(context_meta.get("lines"), int):
            ctx_lines = max(ctx_lines, context_meta.get("lines", 0))
        if ctx_lines >= 10:
            ctx_score, ctx_note = 100, f"上下文充足（{ctx_lines}行）"
        elif ctx_lines >= 5:
            ctx_score, ctx_note = 60, f"上下文偏少（{ctx_lines}行）"
        else:
            ctx_score, ctx_note = 0, f"上下文不足（{ctx_lines}行）"

        # 维度④ 只读探针有效输出
        if not evidence:
            probe_score, probe_note = 0, "未启用只读探针采集（非 collect/assist 模式）"
        else:
            useful = [
                e for e in evidence
                if e and not e.strip().endswith("(无输出)") and "采集失败" not in e
            ]
            if useful:
                probe_score, probe_note = 100, f"只读探针采集到有效输出（{len(useful)}/{len(evidence)} 条）"
            else:
                probe_score, probe_note = 40, "只读探针已执行但未采集到有效输出"

        # 加权综合分（0-100）
        total = round(
            stack_score * STACK_W / 100
            + history_score * HISTORY_W / 100
            + ctx_score * CONTEXT_W / 100
            + probe_score * PROBE_W / 100
        )

        breakdown = {
            "score": total,
            "stack": stack_score,
            "history": history_score,
            "context": ctx_score,
            "probe": probe_score,
            "weights": {"stack": STACK_W, "history": HISTORY_W, "context": CONTEXT_W, "probe": PROBE_W},
            "notes": {
                "stack": stack_note, "history": history_note,
                "context": ctx_note, "probe": probe_note,
            },
        }

        # 等级判定（与旧版完全一致，自动关单口径不放宽）
        reason = (
            f"命中堆栈={'是' if stack_hit else '否'}；"
            f"历史案例：{history_note}；"
            f"上下文={'充足' if ctx_lines >= 10 else '不足'}（{ctx_lines}行）"
        )
        if not (root_cause and not root_cause.startswith("分析失败")):
            # 根因无效（分析失败）→ 诊断不可信，分项评分整项作废（score 与各维俱为 0，保持可解释）
            for _d in ("stack", "history", "context", "probe"):
                breakdown[_d] = 0
            breakdown["score"] = 0
            breakdown["notes"]["overall"] = "本次根因无效，诊断不可信，分项评分作废"
            return "low", "本次根因无效；" + reason, breakdown
        if history_hit:
            return "high", reason, breakdown
        if stack_hit and ctx_lines >= 10:
            return "medium", reason, breakdown
        return "low", reason, breakdown

    def _collect_readonly_evidence(self, alert_type: str, trigger_line: str) -> List[str]:
        probes = READONLY_PROBES.get(alert_type, READONLY_PROBES["keyword"])
        collected: List[str] = []
        for probe in probes:
            cmd = self._safe_adb(probe, trigger_line)
            if not cmd:
                continue
            # 证据标签：让证据链能看出这段输出来自哪条只读命令（#26）
            label = f"$ {probe.split('||')[0].strip()}"
            try:
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=20
                )
                out = (proc.stdout or "").strip()
                if not out:
                    collected.append(f"{label}\n(无输出)")
                    continue
                # 截断策略：保头+保尾（crash 缓冲的 FATAL 头在前面，不能只留尾部）
                if len(out) > 1200:
                    out = out[:600] + "\n...(中间截断)...\n" + out[-500:]
                collected.append(f"{label}\n{out}")
            except Exception as e:
                collected.append(f"{label}\n采集失败: {e}")
        return collected

    def _safe_adb(self, probe: str, trigger_line: str) -> Optional[str]:
        """构造安全的 adb shell 命令；命中黑名单则拒绝。"""
        if any(tok in probe.lower() for tok in DANGEROUS_TOKENS):
            logger.warning(f"自愈 Agent 拒绝危险命令: {probe}")
            return None
        filled = probe.format(
            package=shlex.quote(self.target_package),
            keyword=(trigger_line[:40] if trigger_line else ""),
        )
        return f"adb -s {shlex.quote(self.device_id)} shell {filled}"


def get_self_heal_agent(
    device_id: str, target_package: str = "", mode: str = None
) -> SelfHealAgent:
    """获取自愈 Agent 实例（便于挂载方统一构造）。"""
    return SelfHealAgent(device_id, target_package, mode)
