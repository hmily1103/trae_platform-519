import os
import base64
import io
import time
from typing import List, Dict
import matplotlib
matplotlib.use('Agg')  # Force non-interactive backend
import matplotlib.pyplot as plt
from datetime import datetime
import json


def _load_event_auto_analysis(evidence_dir: str) -> Dict:
    if not evidence_dir:
        return {}
    try:
        path = os.path.join(evidence_dir, 'auto_analysis.json')
        if not os.path.isfile(path):
            return {}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _collect_tv_events(summary_data: Dict) -> List[Dict]:
    events: List[Dict] = []
    events.extend(list(summary_data.get("tv_stall_events_deduped", summary_data.get("tv_stall_events", [])) or []))
    events.extend(list(summary_data.get("tv_stall_risk_events_deduped", summary_data.get("tv_stall_risk_events", [])) or []))
    events.extend(list(summary_data.get("tv_freeze_events_deduped", summary_data.get("tv_freeze_events", [])) or []))
    return [event for event in events if isinstance(event, dict)]


def _pick_representative_tv_event(summary_data: Dict) -> Dict:
    def _event_score(event: Dict):
        confidence = str(event.get("confidence_level", "") or "").lower()
        event_type = str(event.get("type", "") or "").upper()
        confirmed_rank = 2 if confidence == "confirmed" or event_type == "TV_FREEZE" else (1 if confidence in {"risk", "medium", "low"} else 0)
        return (
            confirmed_rank,
            float(event.get("max_frame_gap_ms", 0) or 0.0),
            int(event.get("duration_ms", 0) or 0),
            float(event.get("video_fps", 0) or 0.0) * -1.0,
            str(event.get("start_time") or event.get("timestamp") or ""),
        )

    events = _collect_tv_events(summary_data)
    if not events:
        return {}
    return max(events, key=_event_score)


def _looks_like_tool_process(process_name: str) -> bool:
    name = str(process_name or "").strip().lower()
    if not name:
        return True
    tool_keywords = ["grep", "head", "tail", " logcat", "ps ", "ps|", "findstr", "thunder_logcat"]
    return any(keyword in name for keyword in tool_keywords)


def _infer_owner_from_process_name(process_name: str) -> str:
    process = str(process_name or "").lower()
    if not process:
        return "待补证据"
    if (
        process.startswith("/system/bin/")
        or "surfaceflinger" in process
        or "mediaserver" in process
        or "composer" in process
        or "audioserver" in process
        or "media.codec" in process
        or "media.extractor" in process
    ):
        return "系统/固件侧"
    if "com.thunder.ktv" in process or "player" in process:
        return "应用侧"
    return "应用/系统联合排查"


def _summarize_event_cpu_suspects(summary_data: Dict) -> Dict:
    suspect_map: Dict[str, Dict] = {}
    for event in _collect_tv_events(summary_data):
        contention = event.get("cpu_contention") or {}
        candidate = contention.get("top_candidate") or {}
        process_name = str(candidate.get("process", "") or event.get("suspect_process", "") or "").strip()
        if not process_name or process_name in {"无", "N/A"} or _looks_like_tool_process(process_name):
            continue
        peak_cpu = float(candidate.get("peak_cpu_percent", 0) or 0.0)
        bucket = suspect_map.setdefault(process_name, {"count": 0, "peak_cpu_percent": 0.0})
        bucket["count"] += 1
        bucket["peak_cpu_percent"] = max(float(bucket.get("peak_cpu_percent", 0.0) or 0.0), peak_cpu)
    if not suspect_map:
        return {}
    process_name, stats = max(
        suspect_map.items(),
        key=lambda item: (int(item[1].get("count", 0) or 0), float(item[1].get("peak_cpu_percent", 0.0) or 0.0)),
    )
    return {
        "process": process_name,
        "count": int(stats.get("count", 0) or 0),
        "peak_cpu_percent": float(stats.get("peak_cpu_percent", 0.0) or 0.0),
        "owner": _infer_owner_from_process_name(process_name),
    }

class ReportGenerator:
    """
    V2 HTML 报告生成器
    包含:
    1. 基础测试信息
    2. 决策结论 (Pass/Fail/Grade)
    3. 性能趋势图 (Matplotlib -> Base64)
    4. 错误汇总
    """
    
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        # 设置中文字体，防止乱码 (尝试常见字体)
        plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS', 'sans-serif'] 
        plt.rcParams['axes.unicode_minus'] = False

    def _build_monitoring_scope_summary(self, summary_data: Dict) -> Dict:
        package_name = str(summary_data.get("package_name", "") or "目标播放器进程")
        avg_player_cpu = float(summary_data.get("avg_player_cpu_percent", 0) or 0.0)
        avg_system_cpu = float(summary_data.get("avg_system_cpu_percent", 0) or 0.0)
        playback_path = summary_data.get("playback_path_summary", {}) or {}
        decoder_summary = summary_data.get("decoder_stuck_summary", {}) or {}
        responsibility = summary_data.get("responsibility_summary", {}) or {}
        suspect_process = str(responsibility.get("suspect_process", "") or "无")
        items = [
            f"App层：{package_name}（CPU {avg_player_cpu:.1f}%）",
            f"系统服务层：整机 CPU {avg_system_cpu:.1f}% ，同时采样 Top 进程、重复实例和系统服务抢占",
            f"硬件解码层：{playback_path.get('route', '未识别')} / Decoder {decoder_summary.get('decoder_name') or '未识别'}",
            f"显示合成层：Display {summary_data.get('tv_display_id', 'N/A')} / Surface {'已锁定' if summary_data.get('tv_surface_locked') else '未锁定'} / FPS {float(summary_data.get('avg_video_fps', 0) or 0.0):.1f}",
        ]
        conclusion = "当前报告采用整机与播放链路联合监控，不会只按单一 App 进程 CPU 下结论。"
        if suspect_process and suspect_process != "无":
            conclusion = f"当前风险更偏 {suspect_process} 所在链路；测试包名只用于锚定播放器进程状态，不代表只监控该进程。"
        return {
            "package_note": "测试包名仅用于锚定播放器进程状态，不代表只监控该进程。",
            "items": items,
            "conclusion": conclusion,
        }

    def _build_core_conclusion_summary(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> Dict:
        diagnosis = (root_cause_data or {}).get("final_diagnosis", {}) or {}
        responsibility = summary_data.get("responsibility_summary", {}) or {}
        dev_priority = self._build_dev_priority_plan(summary_data, root_cause_data)
        process_failure = summary_data.get("process_failure_summary", {}) or {}
        top_suspects = list((root_cause_data or {}).get("top_suspect_processes") or [])
        representative_event = _pick_representative_tv_event(summary_data)
        representative_auto = _load_event_auto_analysis(str(representative_event.get("evidence_dir", "") or ""))

        package_name = str(summary_data.get("package_name", "") or "播放器进程")
        avg_player_cpu = float(summary_data.get("avg_player_cpu_percent", 0) or 0.0)
        avg_pss_mb = float(summary_data.get("avg_pss_mb", 0) or 0.0)
        severe_ratio = float(summary_data.get("tv_severe_stall_sample_ratio_percent", 0) or 0.0)
        p99_gap = float(summary_data.get("tv_frame_gap_p99_ms", 0) or 0.0)
        effective_ratio = float(summary_data.get("effective_screen_anomaly_ratio_percent", 0) or 0.0)
        effective_duration_ms = int(summary_data.get("effective_screen_anomaly_duration_ms", 0) or 0)
        target = str(dev_priority.get("target", "") or responsibility.get("suspect_process", "") or "无")
        owner = str(dev_priority.get("owner", "") or responsibility.get("owner", "") or "待确认")

        event_suspect = _summarize_event_cpu_suspects(summary_data)
        if event_suspect.get("process") and int(event_suspect.get("count", 0) or 0) >= 2:
            target = str(event_suspect.get("process", "") or target or "")
            if not owner or "寰" in owner:
                owner = str(event_suspect.get("owner", "") or owner or "")

        app_health = (
            f"App ({package_name}) —— {'存在异常' if process_failure.get('has_player_failure') else '健康'}："
            f"CPU {avg_player_cpu:.1f}%，内存 {avg_pss_mb:.0f}MB，"
            + (
                f"Crash {int(process_failure.get('crash_count', 0) or 0)} / ANR {int(process_failure.get('anr_count', 0) or 0)} / PID异常 {int(process_failure.get('total_failure_count', 0) or 0)}"
                if process_failure.get("has_player_failure")
                else "无 Crash / ANR / PID 异常"
            )
        )

        owner_display = owner
        suspect_line = f"{owner_display} —— {target}"
        if target and target not in {"无", "N/A"} and owner in {"待确认", "待补证据"}:
            owner_display = "系统/固件侧（待补硬证据）"
            suspect_line = f"{owner_display} —— 嫌疑 {target}"
        if top_suspects:
            first = top_suspects[0] or {}
            name = str(first.get("process", "") or target or "无").strip()
            count = int(first.get("count", 0) or 0)
            peak_cpu = float(first.get("peak_cpu_percent", 0) or 0.0)
            if name:
                suspect_line = f"{owner_display} —— {name}" + (f"：命中 {count} 次，峰值 CPU {peak_cpu:.1f}%" if count > 0 or peak_cpu > 0 else "")

        actions = list(dev_priority.get("logs", []) or [])
        artifacts = list(dev_priority.get("artifacts", []) or [])
        actions.extend(artifacts[:2])
        if not actions:
            actions = ["优先查看代表性事件目录中的日志、Top 快照和时间窗证据"]

        evidence_gap = list((responsibility.get("promotion_hints", []) or [])) or list((responsibility.get("confirmation_gap_reasons", []) or []))
        gap_text = "；".join(str(item) for item in evidence_gap[:2]) if evidence_gap else "当前无额外补证提醒"

        severity = (
            f"{severe_ratio:.2f}% 的样本达到严重停顿，P99 帧间隔 {p99_gap:.0f} ms"
            if severe_ratio > 0 or p99_gap > 0
            else "当前未检测到明显的人眼可感知严重停顿"
        )

        if effective_duration_ms > 0:
            severity += (
                f"锛岀湡姝ｈ鍏ョ粨璁虹殑寮傚父绱绾?{effective_duration_ms / 1000.0:.1f} 绉掞紝"
                f"鍗犲叏绋?{effective_ratio:.2f}%"
            )

        representative_summary = ""
        if representative_event:
            event_time = str(representative_event.get("start_time") or representative_event.get("timestamp") or "N/A")
            event_gap = float(representative_event.get("max_frame_gap_ms", 0) or 0.0)
            event_process = str((((representative_event.get("cpu_contention") or {}).get("top_candidate") or {}).get("process", "")) or "")
            if not event_process and event_suspect.get("process"):
                event_process = str(event_suspect.get("process") or "")
            if representative_auto:
                representative_summary = (
                    f"{event_time} ?????"
                    f"{str(representative_auto.get('diagnosis_title', '') or '???????')}?"
                    f"{str(representative_auto.get('diagnosis_detail', '') or '').strip()}"
                )
            else:
                representative_summary = (
                    f"{event_time} ?????????? {event_gap:.0f} ms"
                    + (f"?CPU ???? {event_process}" if event_process else "")
                )

        return {
            "decision": decision_text,
            "severity": severity,
            "app_health": app_health,
            "suspect_line": suspect_line,
            "actions": actions[:3],
            "gap_text": gap_text,
            "representative_summary": representative_summary,
        }

    def _render_core_conclusion_card(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> str:
        core = self._build_core_conclusion_summary(summary_data, root_cause_data, decision_text)
        actions_html = "".join(f"<li>{line}</li>" for line in (core.get("actions") or []))
        return f"""
        <div class="card full" style="border:2px solid #fde68a;background:#fffbeb;">
            <h3>🎯 30秒核心结论</h3>
            <p><strong>结论：</strong>{core.get('decision', 'N/A')}</p>
            <p><strong>严重性：</strong>{core.get('severity', 'N/A')}</p>
            <p><strong>责任划分：</strong></p>
            <ul>
                <li>{core.get('app_health', 'N/A')}</li>
                <li>{core.get('suspect_line', 'N/A')}</li>
            </ul>
            <p><strong>研发先做这 3 件事：</strong></p>
            <ul>{actions_html}</ul>
            <p><strong>?????????</strong>{core.get('representative_summary', '??????????')}</p>
            <p class="muted"><strong>补证提醒：</strong>{core.get('gap_text', '当前无额外补证提醒')}</p>
        </div>
        """

    def _build_core_conclusion_summary(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> Dict:
        diagnosis = (root_cause_data or {}).get("final_diagnosis", {}) or {}
        responsibility = summary_data.get("responsibility_summary", {}) or {}
        dev_priority = self._build_dev_priority_plan(summary_data, root_cause_data)
        process_failure = summary_data.get("process_failure_summary", {}) or {}
        top_suspects = list((root_cause_data or {}).get("top_suspect_processes") or [])
        representative_event = _pick_representative_tv_event(summary_data)
        representative_auto = _load_event_auto_analysis(str(representative_event.get("evidence_dir", "") or ""))

        package_name = str(summary_data.get("package_name", "") or "播放器进程")
        avg_player_cpu = float(summary_data.get("avg_player_cpu_percent", 0) or 0.0)
        avg_pss_mb = float(summary_data.get("avg_pss_mb", 0) or 0.0)
        severe_ratio = float(summary_data.get("tv_severe_stall_sample_ratio_percent", 0) or 0.0)
        p99_gap = float(summary_data.get("tv_frame_gap_p99_ms", 0) or 0.0)
        effective_ratio = float(summary_data.get("effective_screen_anomaly_ratio_percent", 0) or 0.0)
        effective_duration_ms = int(summary_data.get("effective_screen_anomaly_duration_ms", 0) or 0)
        target = str(dev_priority.get("target", "") or responsibility.get("suspect_process", "") or "无")
        owner = str(dev_priority.get("owner", "") or responsibility.get("owner", "") or "待补证据")
        evidence_strength = self._normalize_evidence_strength(
            diagnosis.get("evidence_strength") or responsibility.get("evidence_strength") or {}
        )

        if process_failure.get("has_player_failure"):
            app_health = (
                f"App（{package_name}）存在异常：CPU {avg_player_cpu:.1f}% | 内存 {avg_pss_mb:.0f} MB | "
                f"Crash {int(process_failure.get('crash_count', 0) or 0)} / "
                f"ANR {int(process_failure.get('anr_count', 0) or 0)} / "
                f"PID异常 {int(process_failure.get('total_failure_count', 0) or 0)}"
            )
            stability_status = "稳定性未通过：播放器进程本身存在异常"
        else:
            app_health = (
                f"App（{package_name}）健康：CPU {avg_player_cpu:.1f}% | 内存 {avg_pss_mb:.0f} MB | "
                "无 Crash / ANR / PID 异常"
            )
            stability_status = "稳定性通过：未见 Crash、ANR、进程退出或重启"

        suspect_line = f"最高相关链路：{owner} —— {target}"
        if event_suspect.get("process") and int(event_suspect.get("count", 0) or 0) >= 2:
            suspect_line = (
                f"{owner_display} 鈥斺€?{event_suspect.get('process')}"
                + f"锛氬懡涓?{int(event_suspect.get('count', 0) or 0)} 娆★紝宄板€?CPU {float(event_suspect.get('peak_cpu_percent', 0.0) or 0.0):.1f}%"
            )
        elif top_suspects:
            first = top_suspects[0] or {}
            name = str(first.get("process", "") or target or "无").strip()
            count = int(first.get("count", 0) or 0)
            peak_cpu = float(first.get("peak_cpu_percent", 0) or 0.0)
            suffix = ""
            if count > 0 or peak_cpu > 0:
                suffix = f"（命中 {count} 次，峰值 CPU {peak_cpu:.1f}%）"
            suspect_line = f"高相关对象：{name}{suffix}"
        elif target and target not in {"无", "N/A"}:
            suspect_line = f"高相关对象：{target}（需结合日志互证）"

        if event_suspect.get("process") and int(event_suspect.get("count", 0) or 0) >= 2:
            suspect_line = (
                f"{owner_display} 鈥斺€?{event_suspect.get('process')}"
                + f"锛氬懡涓?{int(event_suspect.get('count', 0) or 0)} 娆★紝宄板€?CPU {float(event_suspect.get('peak_cpu_percent', 0.0) or 0.0):.1f}%"
            )

        actions = list(dev_priority.get("logs", []) or [])
        artifacts = list(dev_priority.get("artifacts", []) or [])
        actions.extend(artifacts[:2])
        if not actions:
            actions = ["优先查看代表性事件目录中的日志、Top 快照和时间窗证据"]

        evidence_gap = list((responsibility.get("promotion_hints", []) or [])) or list(
            (responsibility.get("confirmation_gap_reasons", []) or [])
        )
        gap_text = "；".join(str(item) for item in evidence_gap[:2]) if evidence_gap else "当前无额外补证提醒。"

        if severe_ratio > 0 or p99_gap > 0:
            severity = f"严重停顿样本 {severe_ratio:.2f}% ，P99 帧间隔 {p99_gap:.0f} ms"
        else:
            severity = "当前未检测到明显的人眼可感知严重停顿"
        if effective_duration_ms > 0:
            severity += (
                f"，真正计入结论的异常累计约 {effective_duration_ms / 1000.0:.1f} 秒，"
                f"占全程 {effective_ratio:.2f}%"
            )

        smoothness_status = str(summary_data.get("perceptual_recommendation", "") or "流畅性结果待补充")
        root_cause_status = str(diagnosis.get("summary") or summary_data.get("executive_statement") or "当前暂无明确根因")
        if evidence_strength in {"insufficient", "risk"}:
            root_cause_status = f"{root_cause_status}（当前仍以风险/待补证据口径呈现）"

        representative_summary = ""
        if representative_event:
            event_time = str(representative_event.get("start_time") or representative_event.get("timestamp") or "N/A")
            event_gap = float(representative_event.get("max_frame_gap_ms", 0) or 0.0)
            event_process = str(
                ((((representative_event.get("cpu_contention") or {}).get("top_candidate") or {}).get("process", "")) or "")
            )
            if representative_auto:
                representative_summary = (
                    f"{event_time} | {str(representative_auto.get('diagnosis_title', '') or '代表事件分析')}："
                    f"{str(representative_auto.get('diagnosis_detail', '') or '').strip()}"
                )
            else:
                representative_summary = (
                    f"{event_time} | 代表事件最大帧间隔 {event_gap:.0f} ms"
                    + (f" | 相关进程 {event_process}" if event_process else "")
                )

        return {
            "decision": decision_text,
            "severity": severity,
            "app_health": app_health,
            "suspect_line": suspect_line,
            "actions": actions[:3],
            "gap_text": gap_text,
            "representative_summary": representative_summary,
            "stability_status": stability_status,
            "smoothness_status": smoothness_status,
            "root_cause_status": root_cause_status,
        }

    def _render_core_conclusion_card(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> str:
        core = self._build_core_conclusion_summary(summary_data, root_cause_data, decision_text)
        actions_html = "".join(f"<li>{line}</li>" for line in (core.get("actions") or []))
        return f"""
        <div class="card full" style="border:2px solid #fde68a;background:#fffbeb;">
            <h3>🎯 30秒核心结论</h3>
            <p><strong>准入结论：</strong>{core.get('decision', 'N/A')}</p>
            <p><strong>严重性：</strong>{core.get('severity', 'N/A')}</p>
            <p><strong>三段式判断：</strong></p>
            <ul>
                <li><strong>稳定性：</strong>{core.get('stability_status', 'N/A')}</li>
                <li><strong>流畅性：</strong>{core.get('smoothness_status', 'N/A')}</li>
                <li><strong>根因定位：</strong>{core.get('root_cause_status', 'N/A')}</li>
            </ul>
            <p><strong>关键事实：</strong></p>
            <ul>
                <li>{core.get('app_health', 'N/A')}</li>
                <li>{core.get('suspect_line', 'N/A')}</li>
            </ul>
            <p><strong>研发先做这 3 件事：</strong></p>
            <ul>{actions_html}</ul>
            <p><strong>代表事件分析：</strong>{core.get('representative_summary', '当前暂无代表事件')}</p>
            <p class="muted"><strong>补证提醒：</strong>{core.get('gap_text', '当前无额外补证提醒。')}</p>
        </div>
        """

    def _render_core_conclusion_card(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> str:
        core = self._build_core_conclusion_summary(summary_data, root_cause_data, decision_text)
        actions_html = "".join(f"<li>{line}</li>" for line in (core.get("actions") or []))
        severe_ratio = float(summary_data.get("tv_severe_stall_sample_ratio_percent", 0) or 0.0)
        p99_gap = float(summary_data.get("tv_frame_gap_p99_ms", 0) or 0.0)
        effective_ratio = float(summary_data.get("effective_screen_anomaly_ratio_percent", 0) or 0.0)
        confirmed_stall_count = int(summary_data.get("tv_stall_count", 0) or 0)
        risk_stall_count = int(summary_data.get("tv_stall_risk_count", 0) or 0)
        test_mode = str(summary_data.get("test_mode", "") or "")
        if test_mode == "business_stress":
            playback_stat = f"业务压力模式，主动点歌/切歌 {summary_data.get('song_count', 0)} 次"
            success_rate_text = f"{float(summary_data.get('success_rate', 0) or 0):.1f}%"
        avg_video_fps = float(summary_data.get("avg_video_fps", 0) or 0.0)

        if decision_text == "不建议上线":
            problem_level = "用户体验风险：P0候选"
            if test_mode and test_mode != "monitor_only":
                problem_level = "体验等级：严重（发布阻断）"
        elif decision_text == "建议灰度观察":
            problem_level = "体验等级：高风险观察"
        else:
            problem_level = "P2 观察项"

        if severe_ratio >= 10.0 or p99_gap >= 3000:
            user_impact = "★★★★☆"
        elif severe_ratio >= 3.0 or p99_gap >= 1000:
            user_impact = "★★★☆☆"
        elif effective_ratio > 0:
            user_impact = "★★☆☆☆"
        else:
            user_impact = "★☆☆☆☆"

        occurrence_text = f"确认卡顿 {confirmed_stall_count} 次 / 风险样本 {risk_stall_count} 次"
        if confirmed_stall_count <= 0 and risk_stall_count <= 0:
            occurrence_text = "本轮未捕获电视端卡顿或高风险抖动样本"

        fps_hint = ""
        if avg_video_fps > 0 and (confirmed_stall_count > 0 or risk_stall_count > 0 or effective_ratio > 0):
            fps_hint = (
                f"<p class=\"muted\"><strong>口径说明：</strong>视频 FPS {avg_video_fps:.1f} 反映的是采样周期平均值，"
                "不代表每一帧都连续稳定输出；若 Surface 在局部时间窗内停止更新，仍可能出现平均 FPS 正常但肉眼可见的瞬时冻结。</p>"
            )

        return f"""
        <div class="card full" style="border:2px solid #fde68a;background:#fffbeb;">
            <h3>🎯 30秒核心结论</h3>
            <p><strong>准入结论：</strong>{core.get('decision', 'N/A')}</p>
            <p><strong>严重性：</strong>{core.get('severity', 'N/A')}</p>
            <p><strong>三段式判断：</strong></p>
            <ul>
                <li><strong>稳定性：</strong>{core.get('stability_status', 'N/A')}</li>
                <li><strong>流畅性：</strong>{core.get('smoothness_status', 'N/A')}</li>
                <li><strong>根因定位：</strong>{core.get('root_cause_status', 'N/A')}</li>
            </ul>
            <p><strong>AI诊断摘要：</strong></p>
            <ul>
                <li><strong>问题等级：</strong>{problem_level}</li>
                <li><strong>用户影响：</strong>{user_impact}</li>
                <li><strong>发生频率：</strong>{occurrence_text}</li>
                <li><strong>当前判断：</strong>{core.get('suspect_line', 'N/A')}</li>
            </ul>
            {fps_hint}
            <p><strong>关键事实：</strong></p>
            <ul>
                <li>{core.get('app_health', 'N/A')}</li>
                <li>{core.get('suspect_line', 'N/A')}</li>
            </ul>
            <p><strong>研发先做这 3 件事：</strong></p>
            <ul>{actions_html}</ul>
            <p><strong>代表事件分析：</strong>{core.get('representative_summary', '当前暂无代表事件')}</p>
            <p class="muted"><strong>补证提醒：</strong>{core.get('gap_text', '当前无额外补证提醒。')}</p>
        </div>
        """

    def _render_performance_trend_section(
        self,
        pss_chart_b64: str,
        cpu_chart_b64: str,
        fps_chart_b64: str,
    ) -> str:
        cards = []
        if pss_chart_b64:
            cards.append(
                f"""
                <div class="card">
                    <h3>内存趋势</h3>
                    <img src="data:image/png;base64,{pss_chart_b64}" alt="Memory Trend" style="width:100%;border-radius:8px;" />
                </div>
                """
            )
        if cpu_chart_b64:
            cards.append(
                f"""
                <div class="card">
                    <h3>CPU 趋势</h3>
                    <img src="data:image/png;base64,{cpu_chart_b64}" alt="CPU Trend" style="width:100%;border-radius:8px;" />
                </div>
                """
            )
        if fps_chart_b64:
            cards.append(
                f"""
                <div class="card">
                    <h3>视频 FPS 趋势</h3>
                    <img src="data:image/png;base64,{fps_chart_b64}" alt="FPS Trend" style="width:100%;border-radius:8px;" />
                </div>
                """
            )
        if not cards:
            return """
            <div class="card full">
                <h3>性能趋势图</h3>
                <p class="muted">当前没有可展示的趋势数据。请确认本轮存在有效采样样本。</p>
            </div>
            """
        return f"""
        <div class="card full">
            <h3>性能趋势图</h3>
            <div class="grid">
                {''.join(cards)}
            </div>
        </div>
        """

    def generate_report(self, summary_data: Dict, history_data: List[Dict], filename_prefix: str, root_cause_data: Dict = None):
        """生成简洁可读的 HTML 报告。"""
        decision = summary_data.get("score_result", {}) or {}
        root_cause_data = root_cause_data or {}
        diagnosis = root_cause_data.get("final_diagnosis", {}) or {}
        diagnosis_strength = self._normalize_evidence_strength(
            diagnosis.get("evidence_strength") or {}
        )
        decoder_summary = summary_data.get("decoder_stuck_summary", {}) or {}
        error_stats = summary_data.get("error_stats", {}) or {}
        process_failure_summary = summary_data.get("process_failure_summary", {}) or {}
        is_monitor_only = summary_data.get("test_mode") == "monitor_only"
        latency_probe = summary_data.get("tv_latency_probe", {}) or {}
        latency_excerpt = str(latency_probe.get("latency_output_excerpt", "") or "").strip()

        decision_text = str(decision.get("release_status", "") or "")
        if not decision_text:
            if decision.get("ready_to_release"):
                decision_text = "建议上线"
            elif str(decision.get("assessment", "") or "") == "observe":
                decision_text = "建议灰度观察"
            elif str(decision.get("assessment", "") or "") == "inconclusive":
                decision_text = "证据不足，暂不作上线结论"
            else:
                decision_text = "不建议上线"
        if decision_text == "建议上线":
            decision_color = "#16a34a"
        elif decision_text in {"建议灰度观察", "证据不足，暂不作上线结论"}:
            decision_color = "#d97706"
        else:
            decision_color = "#dc2626"

        def _safe(value):
            text = str(value if value is not None else "")
            return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

        def _list_html(items):
            items = list(items or [])
            if not items:
                return "<li>无</li>"
            return "".join(f"<li>{_safe(item)}</li>" for item in items)

        playback_stat = (
            "纯监控模式"
            if is_monitor_only
            else f"{summary_data.get('song_count', 0)} 首"
        )
        success_rate_text = (
            "不适用（纯监控模式）"
            if is_monitor_only
            else f"{float(summary_data.get('success_rate', 0) or 0):.1f}%"
        )
        avg_video_fps = float(summary_data.get("avg_video_fps", 0) or 0.0)
        fps_text = f"{avg_video_fps:.1f}" if avg_video_fps > 0 else "N/A"
        tv_jank_ratio = float(summary_data.get("tv_jank_sample_ratio_percent", 0) or 0.0)
        tv_big_jank_ratio = float(summary_data.get("tv_big_jank_sample_ratio_percent", 0) or 0.0)
        tv_frame_gap_p95 = float(summary_data.get("tv_frame_gap_p95_ms", 0) or 0.0)
        tv_frame_gap_p99 = float(summary_data.get("tv_frame_gap_p99_ms", 0) or 0.0)
        tv_micro_stall_ratio = float(summary_data.get("tv_micro_stall_sample_ratio_percent", 0) or 0.0)
        tv_perceptible_stall_ratio = float(summary_data.get("tv_perceptible_stall_sample_ratio_percent", 0) or 0.0)
        tv_severe_stall_ratio = float(summary_data.get("tv_severe_stall_sample_ratio_percent", 0) or 0.0)
        executive_statement = (
            summary_data.get("executive_statement")
            or diagnosis.get("conclusion")
            or "暂无明确结论"
        )
        latency_summary = (
            f"mode={latency_probe.get('latency_mode', 'unknown')} | "
            f"reason={latency_probe.get('probe_reason', 'unknown')} | "
            f"frame_count={latency_probe.get('latency_frame_count', 0)}"
        )
        coverage_ratio = float(summary_data.get("valid_sample_ratio", 0) or 0.0) * 100
        process_failure_timeline = process_failure_summary.get("timeline", []) or []
        process_failure_timeline_html = _list_html([
            f"[{item.get('timestamp', 'N/A')}] {item.get('type', 'UNKNOWN')}: {item.get('description', '')}"
            for item in process_failure_timeline
        ])
        process_failure_types = " / ".join(process_failure_summary.get("failure_types", []) or ["无"])
        process_failure_actions_html = _list_html(summary_data.get("process_failure_actions", []) or [])
        correlation_summary = summary_data.get("tv_process_correlation_summary", {}) or {}
        correlation_pairs_html = _list_html([
            f"卡顿[{item.get('stall_time', 'N/A')}] {item.get('stall_reason', '')} | "
            f"异常[{item.get('failure_time', 'N/A')}] {item.get('failure_type', 'UNKNOWN')} | "
            f"间隔 {item.get('delta_seconds', 0)}s"
            for item in (correlation_summary.get("pair_details", []) or [])
        ])
        responsibility_summary = summary_data.get("responsibility_summary", {}) or {}
        display_recommendation = summary_data.get("tv_display_recommendation", {}) or {}
        responsibility_evidence_html = _list_html([
            f"{item.get('label', '证据')}: {item.get('value', '')}"
            for item in (responsibility_summary.get("evidence_items", []) or [])
        ])
        
        tv_stall_events_html = self._render_tv_stall_events(summary_data)
        dev_priority_html = self._render_dev_priority_card(summary_data, root_cause_data)
        platform_support_html = self._render_platform_support_card(summary_data)
        evidence_coverage_html = self._render_evidence_coverage_card(summary_data)
        observer_overhead_html = self._render_observer_overhead_card(summary_data)
        playback_path_html = self._render_playback_path_card(summary_data)
        core_conclusion_html = self._render_core_conclusion_card(summary_data, root_cause_data, decision_text)
        release_controls_html = self._render_release_controls_card(summary_data, root_cause_data, decision_text)
        readable_summary = self._build_readable_report_summary(summary_data, root_cause_data, decision_text)
        readable_evidence_items = [
            item for item in list(readable_summary.get("evidence", []) or [])
            if not any(keyword in str(item) for keyword in ["优先", "查看", "日志", "排查"])
        ]
        readable_evidence_html = _list_html(readable_evidence_items)
        readable_notes_html = _list_html(readable_summary.get("notes", []))
        monitoring_scope = self._build_monitoring_scope_summary(summary_data)
        monitoring_scope_html = _list_html(monitoring_scope.get("items", []))
        try:
            pss_chart_b64 = self._generate_pss_chart(history_data or [])
        except Exception:
            pss_chart_b64 = ""
        try:
            cpu_chart_b64 = self._generate_cpu_chart(history_data or [])
        except Exception:
            cpu_chart_b64 = ""
        try:
            fps_chart_b64 = self._generate_fps_chart(history_data or [])
        except Exception:
            fps_chart_b64 = ""
        performance_trend_html = self._render_performance_trend_section(
            pss_chart_b64,
            cpu_chart_b64,
            fps_chart_b64,
        )

        html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>Android 播放器压测报告</title>
    <style>
        body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; background:#f6f8fb; color:#1f2937; margin:0; padding:24px; }}
        .wrap {{ max-width: 1080px; margin:0 auto; background:#fff; border-radius:16px; padding:28px; box-shadow:0 8px 30px rgba(15,23,42,.08); }}
        h1,h2,h3 {{ margin:0 0 12px; }}
        .muted {{ color:#6b7280; }}
        .hero {{ display:flex; justify-content:space-between; gap:24px; padding-bottom:20px; border-bottom:1px solid #e5e7eb; margin-bottom:24px; }}
        .decision {{ font-size:28px; font-weight:700; color:{decision_color}; }}
        .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }}
        .card {{ background:#f9fafb; border:1px solid #e5e7eb; border-radius:12px; padding:16px; }}
        .full {{ grid-column:1 / -1; }}
        ul {{ margin:8px 0 0 20px; }}
        .kv p {{ margin:6px 0; }}
        pre {{ white-space:pre-wrap; background:#0f172a; color:#cbd5e1; padding:12px; border-radius:8px; }}
        @media (max-width: 760px) {{ .grid {{ grid-template-columns:1fr; }} .hero {{ flex-direction:column; }} }}
    </style>
</head>
<body>
    <div class="wrap">
        <div class="hero">
            <div>
                <h1>Android 播放器压测报告</h1>
                <div class="muted">生成时间：{_safe(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</div>
                <div class="muted">测试设备：{_safe(summary_data.get('device_id', 'N/A'))}</div>
                <div class="muted">机顶盒 IP：{_safe(summary_data.get('device_ip') or '未获取')}</div>
                <div class="muted">固件版本：{_safe(summary_data.get('firmware_incremental') or '未获取')}</div>
                <div class="muted">测试包名：{_safe(summary_data.get('package_name', 'N/A'))}</div>
                <div class="muted">包名说明：{_safe(monitoring_scope.get('package_note', '测试包名用于锚定播放器进程。'))}</div>
            </div>
            <div style="text-align:right;">
                <div class="decision">{_safe(decision_text)}</div>
                <div class="muted">稳定性评分：{_safe(decision.get('score', 0))} / {_safe(decision.get('grade', 'N/A'))}</div>
                <div class="muted">测试时长：{_safe(summary_data.get('duration_str', 'N/A'))}</div>
            </div>
        </div>

        <div class="grid">
            {core_conclusion_html}
            {release_controls_html}
            <div class="card full">
                <h3>一句话结论</h3>
                <p><strong>{_safe(readable_summary.get('headline', executive_statement))}</strong></p>
                <p class="muted">这部分给测试、产品和管理层快速读结论；详细技术证据见后文。</p>
            </div>
            <div class="card">
                <h3>上线判断</h3>
                <p><strong>{_safe(readable_summary.get('release_status', decision_text))}</strong></p>
                <p class="muted">稳定性评分 {_safe(decision.get('score', 0))} / 100，体验观察：{_safe(summary_data.get('perceptual_stutter', {}).get('recommendation', '无'))}</p>
                <p class="muted">风险口径：{_safe(decision.get('risk_status', '无'))}；证据可信度：{_safe(decision.get('evidence_confidence_label', 'low'))} ({_safe(decision.get('evidence_confidence_score', 0))}/100)</p>
                <p class="muted">{_safe(decision.get('decision_rule', '准入结论优先由阻断项、稳定性评分和风险分级共同决定；体验观察只反映电视端流畅度，不单独决定是否上线。'))}</p>
            </div>
            <div class="card">
                <h3>谁先处理</h3>
                <p><strong>{_safe(readable_summary.get('owner', '待确认'))}</strong></p>
                <p>优先对象：{_safe(readable_summary.get('target', '无'))}</p>
                <p class="muted">拿到报告后建议先围绕这里排查，不必先读完整技术细节。</p>
            </div>
            <div class="card">
                <h3>为什么这么判断</h3>
                <ul>{readable_evidence_html}</ul>
            </div>
            <div class="card">
                <h3>注意事项</h3>
                <ul>{readable_notes_html}</ul>
            </div>
            <div class="card full">
                <h3>本次监控覆盖范围</h3>
                <p><strong>包名说明：</strong>{_safe(monitoring_scope.get('package_note', '测试包名用于锚定播放器进程。'))}</p>
                <ul>{monitoring_scope_html}</ul>
                <p class="muted">{_safe(monitoring_scope.get('conclusion', '当前报告采用整机与播放链路联合监控。'))}</p>
            </div>
            <div class="card kv">
                <h3>测试统计</h3>
                <p><strong>播放统计：</strong>{_safe(playback_stat)}</p>
                <p><strong>播放成功率：</strong>{_safe(success_rate_text)}</p>
                <p><strong>Crash / ANR：</strong>{_safe(error_stats.get('crash_count', 0))} / {_safe(error_stats.get('anr_count', 0))}</p>
                <p><strong>PID 重启 / 丢失：</strong>{_safe(summary_data.get('restart_count', 0))} / {_safe(summary_data.get('pid_loss_count', 0))}</p>
                <p><strong>有效采样：</strong>{_safe(summary_data.get('valid_samples', 0))}/{_safe(summary_data.get('duration_samples', 0))} ({coverage_ratio:.1f}%)</p>
            </div>
            <div class="card kv">
                <h3>播放器进程异常摘要</h3>
                <p><strong>是否异常：</strong>{'是' if process_failure_summary.get('has_player_failure') else '否'}</p>
                <p><strong>总次数：</strong>{_safe(process_failure_summary.get('total_failure_count', 0))}</p>
                <p><strong>类型分布：</strong>{_safe(process_failure_types)}</p>
                <p><strong>首次 / 最近：</strong>{_safe(process_failure_summary.get('first_failure_time', 'N/A'))} / {_safe(process_failure_summary.get('last_failure_time', 'N/A'))}</p>
            </div>
            <div class="card kv">
                <h3>电视端流畅度摘要</h3>
                <p><strong>目标 Display：</strong>{_safe(summary_data.get('tv_display_id', 'N/A'))}</p>
                <p><strong>Display 验证：</strong>{'已验证' if summary_data.get('tv_display_verified') else '未验证'} ({_safe(summary_data.get('tv_display_verification_reason', 'unknown'))})</p>
                <p><strong>推荐 Display：</strong>{_safe(display_recommendation.get('display_id', '?'))} ({_safe(display_recommendation.get('reason', 'unknown'))})</p>
                <p><strong>Surface 锁定：</strong>{'已锁定' if summary_data.get('tv_surface_locked') else '未锁定'}</p>
                <p><strong>视频 FPS：</strong>{fps_text}（样本 {_safe(summary_data.get('video_fps_samples', 0))}）</p>
                <p><strong>确认级卡顿 / 冻结：</strong>{_safe(summary_data.get('tv_stall_count', 0))} / {_safe(summary_data.get('tv_freeze_count', 0))}</p>
                <p><strong>底层抖动指标：</strong>Jank {tv_jank_ratio:.2f}% / Big Jank {tv_big_jank_ratio:.2f}%</p>
                <p><strong>人眼感知指标：</strong>微感 {tv_micro_stall_ratio:.2f}% / 明显停顿 {tv_perceptible_stall_ratio:.2f}% / 重度停顿 {tv_severe_stall_ratio:.2f}%</p>
                <p><strong>帧间隔 P95 / P99：</strong>{tv_frame_gap_p95:.0f} / {tv_frame_gap_p99:.0f} ms</p>
            </div>
            <div class="card full">
                <h3>术语说明</h3>
                <p><strong>电视端卡顿：</strong>已达到确认级的播放卡顿事件。</p>
                <p><strong>电视端冻结：</strong>画面一段时间无更新的冻结样本，不一定每次都等同于肉眼卡顿。</p>
                <p><strong>风险样本：</strong>已捕获异常信号，但证据还未闭环，不能直接当作最终定责依据。</p>
                <p><strong>底层抖动指标：</strong>{_safe(summary_data.get('tv_frame_gap_metric_note', '采样窗口级口径，用于补充确认级卡顿之外的细粒度流畅度变化。'))}</p>
                <p><strong>人眼感知指标：</strong>{_safe(summary_data.get('tv_perceptual_metric_note', '用于区分微感抖动、明显停顿和重度停顿，更贴近肉眼体验。'))}</p>
            </div>

            {self._render_decoder_stuck_summary(summary_data, diagnosis)}

            {dev_priority_html}
            {platform_support_html}
            {playback_path_html}
            {evidence_coverage_html}
            {performance_trend_html}
            {observer_overhead_html}
            {tv_stall_events_html}

            <div class="card full">
                <h3>总结性判断</h3>
                <p>{_safe(executive_statement)}</p>
                <p class="muted">Evidence Strength: {_safe(diagnosis_strength.get('label', 'Insufficient'))} | {_safe(diagnosis_strength.get('description', ''))}</p>
                <p class="muted">证据等级：{_safe(diagnosis.get('evidence_level', 'unknown'))} | 责任方向：{_safe(diagnosis.get('owner', '待确认'))} | 优先对象：{_safe(diagnosis.get('suspect_process', '无'))}</p>
            </div>

            <div class="card full">
                <h3>责任判定</h3>
                <p><strong>责任分类：</strong>{_safe(responsibility_summary.get('category', '暂不能定责'))}</p>
                <p><strong>置信度：</strong>{_safe(responsibility_summary.get('confidence', 'low'))}</p>
                <p><strong>{_safe('建议排查方向 / 风险对象' if '暂未确认主责' in str(responsibility_summary.get('category', '') or '') or str(responsibility_summary.get('confidence', 'low') or 'low').lower() in ['low', 'none'] else '责任方向 / 优先对象')}：</strong>{_safe(responsibility_summary.get('owner', '待确认'))} / {_safe(responsibility_summary.get('suspect_process', '无'))}</p>
                <p><strong>判定结论：</strong>{_safe(responsibility_summary.get('conclusion', '暂无明确结论'))}</p>
                <ul>{responsibility_evidence_html}</ul>
            </div>

            <div class="card full">
                <h3>最近进程异常时间线</h3>
                <ul>{process_failure_timeline_html}</ul>
            </div>

            <div class="card full">
                <h3>播放器异常排查建议</h3>
                <ul>{process_failure_actions_html}</ul>
            </div>

            <div class="card full">
                <h3>卡顿与播放器异常关联分析</h3>
                <p><strong>关联窗口：</strong>前后 {_safe(correlation_summary.get('window_seconds', 30))} 秒</p>
                <p><strong>卡顿总数 / 异常总数：</strong>{_safe(correlation_summary.get('total_tv_stall_count', 0))} / {_safe(correlation_summary.get('total_failure_event_count', 0))}</p>
                <p><strong>时间重合卡顿：</strong>{_safe('不适用' if int(correlation_summary.get('total_tv_stall_count', 0) or 0) <= 0 and int(correlation_summary.get('total_failure_event_count', 0) or 0) <= 0 else str(correlation_summary.get('matched_tv_stall_count', 0)) + ' 次')}（{float(correlation_summary.get('correlated_ratio', 0) or 0.0) * 100:.1f}%）</p>
                <p><strong>关联结论：</strong>{_safe(correlation_summary.get('conclusion', '暂无明确结论'))}</p>
                <ul>{correlation_pairs_html}</ul>
            </div>

            <div class="card">
                <h3>扣分 / 风险项</h3>
                <ul>{_list_html(decision.get('deductions', []))}</ul>
            </div>
            <div class="card">
                <h3>上线阻断 / 证据缺口</h3>
                <ul>{_list_html(decision.get('release_blockers', []))}</ul>
            </div>

            <div class="card">
                <h3>资源与热状态</h3>
                <p><strong>播放器平均 CPU：</strong>{_safe(summary_data.get('avg_player_cpu_percent', 0))}%</p>
                <p><strong>整机平均 / 峰值 CPU：</strong>{_safe(summary_data.get('avg_system_cpu_percent', 0))}% / {_safe(summary_data.get('max_system_cpu_percent', 0))}%</p>
                <p><strong>峰值 / 平均内存：</strong>{_safe(summary_data.get('max_pss_mb', 0))} MB / {_safe(summary_data.get('avg_pss_mb', 0))} MB</p>
                <p><strong>热降频采样：</strong>{_safe(summary_data.get('thermal_throttling_count', 0))}</p>
            </div>
            <div class="card">
                <h3>解码停顿诊断</h3>
                <p><strong>总样本：</strong>{_safe(summary_data.get('decoder_stuck_count', 0))}</p>
                <p><strong>确认样本：</strong>{_safe(summary_data.get('confirmed_decoder_stuck_count', 0))}</p>
                <p><strong>风险样本：</strong>{_safe(summary_data.get('decoder_stuck_risk_count', 0))}</p>
                <p><strong>关联 Decoder：</strong>{_safe(decoder_summary.get('decoder_name') or '未识别')}</p>
                <p><strong>最长持续：</strong>{_safe(decoder_summary.get('max_duration_sec', 0))}s</p>
            </div>

            <div class="card full">
                <h3>FPS 采集诊断</h3>
                <p><strong>不可用原因：</strong>{_safe(summary_data.get('video_fps_unavailable_reason', 'unknown'))}</p>
                <p><strong>候选 Surface：</strong>{_safe(summary_data.get('tv_surface_candidates', []))}</p>
                <p><strong>latency 探测：</strong>{_safe(latency_summary)}</p>
                <pre>{_safe(latency_excerpt or 'none')}</pre>
            </div>
        </div>
    </div>
</body>
</html>
        """

        filename = f"report_{filename_prefix}.html"
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, 'w', encoding='utf-8-sig') as f:
            f.write(html_content)

        return filepath

    def _build_dev_priority_plan(self, summary_data: Dict, root_cause_data: Dict) -> Dict:
        diagnosis = (root_cause_data or {}).get("final_diagnosis", {}) or {}
        cause_type = str(((root_cause_data or {}).get("most_confident_cause") or {}).get("root_cause_type", "") or "")
        suspect = str(diagnosis.get("suspect_process", "") or "N/A")
        owner = str(diagnosis.get("owner", "") or "待确认")
        evidence_strength = self._normalize_evidence_strength(diagnosis.get("evidence_strength") or {})
        decoder_summary = summary_data.get("decoder_stuck_summary", {}) or {}
        tv_stall_count = int(summary_data.get("tv_stall_count", 0) or 0)
        tv_stall_risk_count = int(summary_data.get("tv_stall_risk_count", 0) or 0)
        confirmed_decoder_stuck_count = int(summary_data.get("confirmed_decoder_stuck_count", 0) or 0)
        decoder_stuck_risk_count = int(summary_data.get("decoder_stuck_risk_count", 0) or 0)
        process_failure_summary = summary_data.get("process_failure_summary", {}) or {}
        has_player_failure = bool(process_failure_summary.get("has_player_failure", False))
        derived_display_jank_risk = bool(summary_data.get("derived_display_jank_risk", False))
        derived_display_jank_reason = str(summary_data.get("derived_display_jank_reason", "") or "")

        if (
            not cause_type
            and
            tv_stall_count <= 0
            and tv_stall_risk_count <= 0
            and confirmed_decoder_stuck_count <= 0
            and decoder_stuck_risk_count <= 0
            and not derived_display_jank_risk
            and not has_player_failure
        ):
            return {
                "target": "本轮无需专项排查",
                "owner": "无需归因",
                "strength": "N/A",
                "logs": [
                    "本轮未检测到电视端卡顿、播放器挂掉、Crash、ANR 或解码停顿",
                    "如肉眼也无异常，可直接将本轮结果作为通过样本归档",
                    "如怀疑偶发问题，优先延长监控时长或提高负载后再复测",
                ],
                "retest": "若需进一步验证稳定性，建议延长到 1 小时并在相同场景下重复 2 到 3 轮。",
            }

        if derived_display_jank_risk and not cause_type and tv_stall_count <= 0 and tv_stall_risk_count <= 0:
            return {
                "target": "显示/合成链路（优先看 SurfaceFlinger / HWC / mediaserver）",
                "owner": "待补证据",
                "strength": "Medium",
                "logs": [
                    derived_display_jank_reason or "细粒度帧间隔指标已出现异常，但尚未形成确认级电视端卡顿事件。",
                    "查看同时间窗的 SurfaceFlinger / composer / mediaserver 日志，确认是否存在瞬时调度抖动。",
                    "对照 Top 进程与电视端 frame gap 指标，确认是显示合成抖动还是系统瞬时 CPU 抢占。",
                ],
                "retest": "保持相同场景复测，并重点确认 P95/P99 frame gap、Jank/Big Jank 是否明显回落；若仍反复超阈值，应继续补抓显示链路证据。",
            }

        if cause_type == "CPU_CONTENTION":
            target = suspect or "Top CPU process"
            if target in {"mediaserver", "media.codec", "media.extractor"}:
                logs = [
                    f"{target} 的 media / codec / binder 相关日志",
                    "卡顿时整机 CPU / cpuinfo / process-count 快照",
                    "电视端卡顿时间线是否与 CPU 抢占、Surface/FPS 退化同步",
                ]
            else:
                logs = [
                    f"Top process list and repeated instances of {target}",
                    "System CPU / cpuinfo / process-count snapshots at stall time",
                    "TV stall timeline aligned with CPU spikes and surface evidence",
                ]
            retest = "Retest after cleanup and verify tv_stall_count drops, system CPU falls back, and suspect process count returns to normal."
        elif cause_type == "DECODER_STUCK":
            target = str(decoder_summary.get("decoder_name", "") or suspect or "MPP Hardware Decoder")
            logs = [
                f"Decoder or MPP logs around {target}",
                "MediaCodec / dequeue output timeout / codec error lines",
                "video_fps, decode_fps_estimate, decode_drop_ratio at the representative sample",
            ]
            retest = "Retest and verify decoder output resumes, video_fps and decode_fps_estimate recover together, and decoder error logs disappear."
        elif cause_type == "LOW_FPS_DEGRADATION":
            target = suspect or "SurfaceFlinger/Render"
            logs = [
                "SurfaceFlinger / composer service logs",
                "Display 1 FPS, max_frame_gap_ms, and latency output",
                "GPU or composition load at the same timestamp",
            ]
            retest = "Retest and verify Display 1 FPS stabilizes, surface stays locked, and max_frame_gap_ms falls back."
        else:
            target = suspect or "Primary suspect process"
            logs = [
                "Crash / ANR / codec / underrun related logs around the event",
                "TV stall, surface, FPS, and Top-process evidence at the same time",
                f"Module logs owned by {owner} in the 30s before and after the issue",
            ]
            retest = "Retest and verify TV-side stalls disappear, responsibility direction stays stable, and evidence strength is not downgraded."

        if owner == "待确认" and target not in {"", "N/A"}:
            owner = "系统/固件侧（待补硬证据）"

        return {
            "target": target,
            "owner": owner,
            "strength": evidence_strength.get("label", "Insufficient"),
            "logs": logs[:3],
            "retest": retest,
        }

    def _render_dev_priority_card(self, summary_data: Dict, root_cause_data: Dict) -> str:
        plan = self._build_dev_priority_plan(summary_data, root_cause_data)
        items = "".join(f"<li>{line}</li>" for line in (plan.get("logs") or []))
        return f"""
        <div class="card full">
            <h3>研发处理优先级</h3>
            <p><strong>先查谁：</strong>{plan.get('target', 'N/A')} (Owner: {plan.get('owner', '待确认')} | Evidence: {plan.get('strength', 'Insufficient')})</p>
            <p><strong>看什么日志：</strong></p>
            <ul>{items}</ul>
            <p><strong>怎么复测：</strong>{plan.get('retest', 'N/A')}</p>
        </div>
        """

    def _build_evidence_strength_note(self, summary_data: Dict, root_cause_data: Dict) -> str:
        diagnosis = (root_cause_data or {}).get("final_diagnosis", {}) or {}
        evidence_strength = self._normalize_evidence_strength(diagnosis.get("evidence_strength") or {})
        level = str(
            evidence_strength.get("level")
            or diagnosis.get("evidence_level")
            or evidence_strength.get("label")
            or "unknown"
        ).strip().lower()
        if level in {"confirmed", "blocker"}:
            return "当前已经形成多源闭环，可直接作为定责和阻断依据。"
        if level in {"strong", "high"}:
            return "当前证据较强，责任方向已比较明确，建议结合代表事件日志再复核一次。"
        if level in {"risk", "medium", "moderate"}:
            return "当前属于风险级证据，说明问题值得查，但还不建议把它当成最终责任裁决。"
        if level in {"low", "insufficient", "none", "unknown", "n/a"}:
            return "当前证据仍偏弱，更适合作为补证和延长监控的输入，不能单独一锤定音。"
        return "当前证据等级已输出，建议结合代表事件目录做交叉复核。"

    def _build_gray_release_guardrails(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> List[str]:
        if decision_text != "建议灰度观察":
            if decision_text == "建议上线":
                return [
                    "本轮未进入灰度熔断策略；若业务仍担心偶发问题，可在相同场景补跑 1 小时复核。",
                    "若上线后出现新增 Crash、ANR、PID 丢失或确认级电视端卡顿，建议立即回看本轮基线并补抓证据。",
                ]
            return ["当前不建议直接走灰度放量，请先修复问题或补齐关键证据后再评估。"]

        diagnosis = (root_cause_data or {}).get("final_diagnosis", {}) or {}
        suspect = str(
            diagnosis.get("suspect_process")
            or ((root_cause_data or {}).get("most_confident_cause") or {}).get("suspect_process")
            or (summary_data.get("responsibility_summary", {}) or {}).get("suspect_process")
            or "当前嫌疑链路"
        ).strip()
        has_player_failure = bool((summary_data.get("process_failure_summary", {}) or {}).get("has_player_failure"))
        rules = [
            "建议先按小流量灰度放量，并连续观察 1 到 3 天，确认同类场景不再持续放大。",
            f"若再次捕获 {suspect} 与电视端抖动同步共振，或线上千次播放卡顿率持续超过 0.5%，建议立即暂停放量并回滚。",
        ]
        if has_player_failure:
            rules.append("若灰度期间出现 Crash、ANR、PID 丢失或进程重启任一项，直接按阻断处理。")
        else:
            rules.append("若灰度期间新增 Crash、ANR、PID 丢失等播放器异常，也应直接升级为阻断问题。")
        return rules

    def _build_issue_tracking_summary(self, summary_data: Dict, root_cause_data: Dict) -> Dict:
        candidates = [
            summary_data.get("bug_tracking"),
            summary_data.get("issue_tracking"),
            summary_data.get("jira"),
            summary_data.get("ticket"),
            (root_cause_data or {}).get("bug_tracking"),
            (root_cause_data or {}).get("issue_tracking"),
            (root_cause_data or {}).get("jira"),
            (root_cause_data or {}).get("ticket"),
        ]
        bug_id = ""
        bug_url = ""
        for item in candidates:
            if isinstance(item, dict):
                bug_id = str(item.get("id", "") or item.get("key", "") or bug_id).strip()
                bug_url = str(item.get("url", "") or item.get("link", "") or bug_url).strip()
            elif isinstance(item, str) and item.strip():
                text = item.strip()
                if text.startswith("http://") or text.startswith("https://"):
                    bug_url = bug_url or text
                else:
                    bug_id = bug_id or text
        if bug_id and bug_url:
            display = f"{bug_id} | {bug_url}"
        elif bug_id:
            display = bug_id
        elif bug_url:
            display = bug_url
        else:
            suspect = str(
                ((root_cause_data or {}).get("final_diagnosis") or {}).get("suspect_process")
                or (summary_data.get("responsibility_summary", {}) or {}).get("suspect_process")
                or "系统/固件侧问题"
            ).strip()
            display = f"未填写（建议补挂 {suspect} 相关 Bug 单后回填）"
        return {"display": display, "id": bug_id, "url": bug_url}

    def _render_release_controls_card(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> str:
        evidence_note = self._build_evidence_strength_note(summary_data, root_cause_data)
        guardrails = self._build_gray_release_guardrails(summary_data, root_cause_data, decision_text)
        issue_tracking = self._build_issue_tracking_summary(summary_data, root_cause_data)
        guardrail_html = "".join(f"<li>{line}</li>" for line in (guardrails or []))
        return f"""
        <div class="card full">
            <h3>灰度与跟踪</h3>
            <p><strong>证据等级说明：</strong>{evidence_note}</p>
            <p><strong>灰度熔断条件：</strong></p>
            <ul>{guardrail_html}</ul>
            <p><strong>问题跟踪：</strong>{issue_tracking.get('display', '未填写')}</p>
        </div>
        """

    def _build_readable_report_summary(self, summary_data: Dict, root_cause_data: Dict, decision_text: str) -> Dict:
        diagnosis = (root_cause_data or {}).get("final_diagnosis", {}) or {}
        responsibility = summary_data.get("responsibility_summary", {}) or {}
        dev_priority = self._build_dev_priority_plan(summary_data, root_cause_data)
        process_failure = summary_data.get("process_failure_summary", {}) or {}
        platform_support = summary_data.get("platform_support_summary", {}) or {}

        tv_stall_count = int(summary_data.get("tv_stall_count", 0) or 0)
        tv_stall_risk_count = int(summary_data.get("tv_stall_risk_count", 0) or 0)
        decoder_confirmed = int(summary_data.get("confirmed_decoder_stuck_count", 0) or 0)
        decoder_risk = int(summary_data.get("decoder_stuck_risk_count", 0) or 0)
        avg_system_cpu = float(summary_data.get("avg_system_cpu_percent", 0) or 0.0)
        avg_player_cpu = float(summary_data.get("avg_player_cpu_percent", 0) or 0.0)
        avg_video_fps = float(summary_data.get("avg_video_fps", 0) or 0.0)
        effective_ratio = float(summary_data.get("effective_screen_anomaly_ratio_percent", 0) or 0.0)
        effective_duration_ms = int(summary_data.get("effective_screen_anomaly_duration_ms", 0) or 0)
        ignored_static_count = int(summary_data.get("tv_stall_ignored_count", 0) or 0)
        ignored_static_ratio = float(summary_data.get("ignored_static_scene_ratio_percent", 0) or 0.0)
        duration_sec = int(summary_data.get("duration_sec", 0) or 0)
        responsibility_category = str(responsibility.get("category", "") or "")
        responsibility_confidence = str(responsibility.get("confidence", "") or "").lower()

        target = str(dev_priority.get("target", "") or responsibility.get("suspect_process", "") or "无")
        owner = str(dev_priority.get("owner", "") or responsibility.get("owner", "") or "待确认")
        headline = str(
            summary_data.get("executive_statement")
            or diagnosis.get("conclusion")
            or responsibility.get("conclusion")
            or "暂无明确结论"
        )

        top_suspect_processes = list((root_cause_data or {}).get("top_suspect_processes") or [])
        top_suspect_name = ""
        top_suspect_count = 0
        if top_suspect_processes:
            first_suspect = top_suspect_processes[0] or {}
            top_suspect_name = str(first_suspect.get("process", "") or "").strip()
            top_suspect_count = int(first_suspect.get("count", 0) or 0)
            if (not target or "鏃" in target) and top_suspect_name and top_suspect_count >= 2:
                target = f"{top_suspect_name}（高频命中 {top_suspect_count} 次）"
                if (not owner) or ("寰" in owner):
                    owner = "系统/固件侧（待补证据）" if self._looks_like_system_process(top_suspect_name) else "待补证据"

        evidence = []
        if tv_stall_count > 0:
            evidence.append(f"电视端已确认卡顿 {tv_stall_count} 次。")
        elif tv_stall_risk_count > 0:
            evidence.append(f"电视端捕获风险样本 {tv_stall_risk_count} 次，但还没有升到确认级。")
        else:
            evidence.append("本轮未检测到确认级电视端卡顿事件。")

        if avg_system_cpu > 0:
            evidence.append(
                f"整机平均 CPU {avg_system_cpu:.1f}%，播放器平均 CPU {avg_player_cpu:.1f}%。"
            )

        if target and target != "无":
            if "暂未确认主责" in responsibility_category or responsibility_confidence in {"low", "none"}:
                evidence.append(f"当前高相关链路为 {target}，仅建议作为排查优先级参考。")
            else:
                evidence.append(f"当前优先排查链路为 {target}。")
        elif avg_video_fps > 0:
            evidence.append(f"电视端视频 FPS 当前约 {avg_video_fps:.1f}。")

        if decoder_confirmed > 0:
            evidence.append(f"已确认解码停顿样本 {decoder_confirmed} 次。")
        elif decoder_risk > 0:
            evidence.append(f"检测到解码风险样本 {decoder_risk} 次。")

        if effective_duration_ms > 0:
            evidence.append(
                f"鐪熸璁″叆缁撹鐨勭數瑙嗙寮傚父绱绾?{effective_duration_ms / 1000.0:.1f} 绉掞紝鍗犲叏绋?{effective_ratio:.2f}%銆?"
            )

        notes = []
        if ignored_static_count > 0:
            notes.append(
                f"鏈疆宸叉寜闈欐€佺敾闈?杞満璇垽杩囨护 {ignored_static_count} 娆★紝鍗犲叏绋嬪ぇ绾?{ignored_static_ratio:.2f}%锛岃繖閮ㄥ垎涓嶅啀鐩存帴璁″叆鐢佃绔崱椤跨粨璁恒€?"
            )
        if effective_ratio > 0 and effective_ratio < 10.0:
            notes.append("铏界劧椋庨櫓绐楀彛鍐呭崰姣斿彲鑳藉亸楂橈紝浣嗙湡姝ｅ浜庡紓甯哥殑鏃堕暱鍗犳瘮浠嶈緝浣庯紝浜虹溂鍙兘鍙細鎰熷埌鍋跺彂鎶栧姩銆?")
        if duration_sec and duration_sec < 3600:
            notes.append("本轮不足 1 小时，更适合快速筛查，不足以覆盖长时间积累问题。")
        if decision_text == "证据不足，暂不作上线结论":
            notes.append("当前报告明确提示了证据缺口，不能直接当作最终裁决。")
        if not summary_data.get("tv_surface_locked"):
            notes.append("当前未稳定锁定电视端视频 Surface，电视端结论可信度会下降。")
        if avg_video_fps <= 0:
            notes.append(
                f"当前未采集到稳定视频 FPS：{summary_data.get('video_fps_unavailable_reason', '需继续补采')}。"
            )
        if process_failure.get("has_player_failure"):
            notes.append("本轮出现播放器进程异常，建议优先看 Crash / ANR / PID 丢失时间线。")
        limitations = list(platform_support.get("limitations", []) or [])
        if limitations:
            notes.append(str(limitations[0]))
        if not notes:
            notes.append("本轮关键证据链相对完整，可直接作为研发排查输入。")

        return {
            "headline": headline,
            "release_status": decision_text,
            "owner": owner,
            "target": target,
            "evidence": evidence[:3],
            "notes": notes[:4],
        }

    def _render_platform_support_card(self, summary_data: Dict) -> str:
        plan = summary_data.get("platform_support_summary", {}) or {}
        capabilities_html = "".join(
            f"<li>{line}</li>" for line in (plan.get("capabilities") or ["当前仅保留基础资源与日志采样能力"])
        )
        limitations_html = "".join(
            f"<li>{line}</li>" for line in (plan.get("limitations") or ["当前未见明显平台级证据缺口"])
        )
        return f"""
        <div class="card full">
            <h3>平台支持等级</h3>
            <p><strong>当前平台：</strong>{plan.get('platform_label', '未识别平台')}</p>
            <p><strong>支持等级：</strong>{plan.get('grade', 'C')} ({plan.get('headline', '辅助级支持')})</p>
            <p><strong>结论：</strong>{plan.get('conclusion', '暂无明确结论')}</p>
            <p><strong>当前能力：</strong></p>
            <ul>{capabilities_html}</ul>
            <p><strong>当前边界：</strong></p>
            <ul>{limitations_html}</ul>
        </div>
        """

    def _render_evidence_coverage_card(self, summary_data: Dict) -> str:
        verified = bool(summary_data.get("tv_display_verified"))
        surface_locked = bool(summary_data.get("tv_surface_locked"))
        avg_video_fps = float(summary_data.get("avg_video_fps", 0) or 0.0)
        fps_samples = int(summary_data.get("video_fps_samples", 0) or 0)
        confirmed_decoder = int(summary_data.get("confirmed_decoder_stuck_count", 0) or 0)
        risk_decoder = int(summary_data.get("decoder_stuck_risk_count", 0) or 0)
        tv_stalls = int(summary_data.get("tv_stall_count", 0) or 0)
        tv_risks = int(summary_data.get("tv_stall_risk_count", 0) or 0)
        unavailable_reason = str(summary_data.get("video_fps_unavailable_reason", "") or "无")
        fps_sources = summary_data.get("video_fps_source_counts", {}) or {}

        if verified and surface_locked and avg_video_fps > 0:
            coverage = "高"
            conclusion = "本轮已经拿到 Display、Surface 和 FPS 直证，电视端结论可信度较高。"
        elif verified and avg_video_fps > 0:
            coverage = "中"
            conclusion = "已拿到 Display 与 FPS 证据，但 Surface 仍未完全锁定，结论可用于定位，发布前建议补足直证。"
        elif verified:
            coverage = "中-"
            conclusion = "已识别到电视端 Display，但缺少稳定 FPS 或 Surface 直证，适合做风险提示，不适合一锤定音。"
        else:
            coverage = "低"
            conclusion = "当前还没有完整电视端直证，报告更适合作为辅助线索。"

        return f"""
        <div class="card full">
            <h3>证据覆盖情况</h3>
            <p><strong>覆盖等级：</strong>{coverage}</p>
            <p><strong>Display 验证 / Surface 锁定：</strong>{'已验证' if verified else '未验证'} / {'已锁定' if surface_locked else '未锁定'}</p>
            <p><strong>视频 FPS：</strong>{avg_video_fps:.1f}（样本 {fps_samples}，来源 {fps_sources or '无'}）</p>
            <p><strong>电视端卡顿 / 风险：</strong>{tv_stalls} / {tv_risks}</p>
            <p><strong>解码停顿确认 / 风险：</strong>{confirmed_decoder} / {risk_decoder}</p>
            <p><strong>FPS 不可用原因：</strong>{unavailable_reason}</p>
            <p><strong>结论：</strong>{conclusion}</p>
        </div>
        """

    def _render_observer_overhead_card(self, summary_data: Dict) -> str:
        avg_cpu = float(summary_data.get("observer_avg_cpu_percent", 0) or 0.0)
        peak_cpu = float(summary_data.get("observer_peak_cpu_percent", 0) or 0.0)
        avg_memory = float(summary_data.get("observer_avg_memory_mb", 0) or 0.0)
        peak_memory = float(summary_data.get("observer_peak_memory_mb", 0) or 0.0)
        pid = summary_data.get("observer_pid", 0)
        mode = str(summary_data.get("observer_primary_sampling_mode", "unknown") or "unknown")
        if peak_cpu <= 5.0:
            conclusion = "工具自身 CPU 开销较低，当前未见明显带入误差风险。"
        elif peak_cpu <= 15.0:
            conclusion = "工具自身开销可接受，但建议结合采样模式一起评估极限场景结果。"
        else:
            conclusion = "工具自身 CPU 开销偏高，建议复核采样模式并确认是否对极限场景产生干扰。"
        return f"""
        <div class="card full">
            <h3>工具自身开销</h3>
            <p><strong>监控进程 PID：</strong>{pid}</p>
            <p><strong>平均 / 峰值 CPU：</strong>{avg_cpu:.2f}% / {peak_cpu:.2f}%</p>
            <p><strong>平均 / 峰值内存：</strong>{avg_memory:.2f} MB / {peak_memory:.2f} MB</p>
            <p><strong>采样模式：</strong>{mode}</p>
            <p><strong>结论：</strong>{conclusion}</p>
        </div>
        """

    def _render_playback_path_card(self, summary_data: Dict) -> str:
        info = summary_data.get("playback_path_summary", {}) or {}
        evidence_html = "".join(
            f"<li>{str(item)}</li>" for item in (info.get("evidence") or [])[:4]
        ) or "<li>暂无可展示依据</li>"
        transition_reason = str(summary_data.get("last_transition_reason", "") or "")
        transition_source = str(summary_data.get("last_transition_source", "") or "")
        transition_count = int(summary_data.get("ignore_transition_event_count", 0) or 0)
        ignore_total = float(summary_data.get("ignore_video_metrics_total_sec", 0) or 0.0)
        return f"""
        <div class="card full">
            <h3>播放通路识别</h3>
            <p><strong>当前判断：</strong>{str(info.get('route', '未识别'))}</p>
            <p><strong>可信度：</strong>{str(info.get('confidence', 'low'))}</p>
            <p><strong>结论：</strong>{str(info.get('summary', '当前还没有足够样本识别播放通路。'))}</p>
            <p><strong>转场豁免：</strong>{transition_count} 次 | 最近原因 {transition_reason or '无'} | 来源 {transition_source or '无'} | 累计忽略 {ignore_total:.1f} 秒</p>
            <ul>{evidence_html}</ul>
        </div>
        """

    def _build_tv_event_statement(self, event: Dict) -> Dict:
        event = event if isinstance(event, dict) else {}
        event_type = str(event.get("type", "TV_STALL") or "TV_STALL")
        reason = str(event.get("reason", "") or "")
        confirmed = bool(event.get("confirmed", False))
        confidence = str(event.get("confidence_level", "risk") or "risk")
        assessment_reason = str(event.get("assessment_reason", "") or "")
        signals = list(event.get("corroboration_signals", []) or [])
        gap_reasons = [
            str(item or "").strip()
            for item in (event.get("confirmation_gap_reasons") or [])
            if str(item or "").strip()
        ]
        promotion_hints = [
            str(item or "").strip()
            for item in (event.get("promotion_hints") or [])
            if str(item or "").strip()
        ]
        contention = event.get("cpu_contention") or {}
        candidate = contention.get("top_candidate") or {}
        cpu_detected = bool(contention.get("detected"))
        max_gap_ms = float(event.get("max_frame_gap_ms", 0) or 0.0)
        min_fps = float(event.get("min_fps", 0) or 0.0)
        video_fps = float(event.get("video_fps", 0) or 0.0)
        duration_sec = float(event.get("duration_sec", 0) or 0.0)

        if event_type == "TV_FREEZE":
            statement = "这条事件更像电视端画面冻结样本，说明画面在一段时间内没有继续推进。"
            basis = (
                assessment_reason
                or f"冻结时长 {duration_sec:.1f} 秒"
            )
            if signals:
                basis += f"；旁证信号 {', '.join(signals)}"
            if video_fps > 0:
                basis += f"；视频 FPS {video_fps:.1f}"
            if max_gap_ms > 0:
                basis += f"；最大帧间隔 {max_gap_ms:.0f} ms"
            next_action = "先看冻结样本前后 10 秒的 event.json、截图、Surface 指标和解码日志，确认是静态画面误判还是播放链路真的停住。"
            owner = "待继续定责"
            return {
                "statement": statement,
                "basis": basis,
                "next_action": next_action,
                "owner": owner,
                "confidence": "confirmed",
                "confirmed": True,
                "confirmation_gap_reasons": gap_reasons[:6],
                "promotion_hints": promotion_hints[:5],
            }

        if cpu_detected:
            process_name = str(candidate.get("process", "") or "高负载进程")
            statement = f"这条事件更像系统/固件侧 CPU 资源竞争，优先排查 {process_name}。"
            basis = f"卡顿时命中 CPU 资源竞争信号，高相关进程 {process_name}，最大帧间隔 {max_gap_ms:.0f} ms。"
            next_action = f"先看 {process_name} 的拉起/保活逻辑，再结合 top_before/top_after.txt 确认是否重复抢占 CPU。"
            owner = "系统/固件侧"
        elif (
            "decoder_confirmed" in signals
            or "decode_drop" in signals
            or "decoder" in reason.lower()
        ):
            statement = "这条事件更像播放器/解码链路停顿，优先排查硬件解码器、码流和驱动。"
            basis = f"事件带有解码侧互证信号（{', '.join(signals) if signals else 'decoder'}），最低 FPS {min_fps:.1f}。"
            next_action = "先看 event.json 与解码器相关日志，再核对码率、分辨率和芯片解码能力上限。"
            owner = "播放器/解码侧"
        elif confirmed:
            statement = "这条事件已确认是电视端卡顿，但当前还不能单独锁定到系统侧或播放器侧。"
            basis = f"事件已达确认级，最大帧间隔 {max_gap_ms:.0f} ms，最低 FPS {min_fps:.1f}。"
            next_action = "先看 event.json、截图和 cpu_during.jsonl，把时间线与播放器异常、系统负载一起交叉确认。"
            owner = "待继续定责"
        else:
            statement = "这条事件目前更像风险提示，还不能直接当成最终定责结论。"
            basis = assessment_reason or "当前只有局部异常信号，互证还不够完整。"
            if gap_reasons:
                basis = f"{basis} 还缺少：{'；'.join(gap_reasons[:3])}。"
            next_action = (
                '；'.join(promotion_hints[:3])
                if promotion_hints else
                "继续补齐 Surface、FPS、日志和 CPU 证据后再定责。"
            )
            owner = "待补证据"

        return {
            "statement": statement,
            "basis": basis,
            "next_action": next_action,
            "owner": owner,
            "confidence": confidence,
            "confirmed": confirmed,
            "confirmation_gap_reasons": gap_reasons[:6],
            "promotion_hints": promotion_hints[:5],
        }

    def _render_tv_stall_events(self, summary_data: Dict) -> str:
        events = []
        events.extend(list(summary_data.get("tv_stall_events_deduped", summary_data.get("tv_stall_events", [])) or []))
        events.extend(list(summary_data.get("tv_stall_risk_events_deduped", summary_data.get("tv_stall_risk_events", [])) or []))
        events.extend(list(summary_data.get("tv_freeze_events_deduped", summary_data.get("tv_freeze_events", [])) or []))
        if not events:
            return ""

        def _safe(value):
            text = str(value if value is not None else "")
            return (
                text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

        freeze_events = list(summary_data.get("tv_freeze_events_deduped", summary_data.get("tv_freeze_events", [])) or [])
        freeze_confirmed = sum(1 for event in freeze_events if isinstance(event, dict) and str(event.get("confidence_level", "") or "").lower() == "confirmed")
        freeze_risk = sum(1 for event in freeze_events if isinstance(event, dict) and str(event.get("confidence_level", "") or "").lower() in {"risk", "low"})
        freeze_ignored = max(0, len(freeze_events) - freeze_confirmed - freeze_risk)

        cards = []
        def _sort_key(item):
            if not isinstance(item, dict):
                return ""
            return str(item.get("start_time") or item.get("timestamp") or "")

        for event in sorted(events, key=_sort_key)[-12:]:
            if not isinstance(event, dict):
                continue
            diagnosis = self._build_tv_event_statement(event)
            auto_analysis = _load_event_auto_analysis(str(event.get("evidence_dir", "") or ""))
            auto_title = str(auto_analysis.get("diagnosis_title", "") or "")
            auto_detail = str(auto_analysis.get("diagnosis_detail", "") or "")
            contention = event.get("cpu_contention") or {}
            candidate = contention.get("top_candidate") or {}
            process_name = str(candidate.get("process", "") or "无")
            process_cpu = float(candidate.get("peak_cpu_percent", 0) or 0.0)
            confidence_badge = "确认级" if diagnosis.get("confirmed") else "风险级"
            event_type = str(event.get("type", "TV_STALL") or "TV_STALL")
            trigger_summary = []
            signals = list(event.get("corroboration_signals", []) or [])
            if signals:
                trigger_summary.append(f"命中信号: {', '.join(str(s) for s in signals[:4])}")
            gaps = list(event.get("confirmation_gap_reasons", []) or [])
            if gaps and not diagnosis.get("confirmed"):
                trigger_summary.append(f"未闭环原因: {'；'.join(str(g) for g in gaps[:2])}")
            if not trigger_summary:
                trigger_summary.append("当前事件已具备基础说明，详见关键依据。")
            evidence_dir = str(event.get("evidence_dir", "") or "")
            display_evidence_dir = evidence_dir
            anchor = "data\\"
            lower_dir = evidence_dir.lower()
            anchor_pos = lower_dir.find(anchor)
            if anchor_pos >= 0:
                display_evidence_dir = ".\\" + evidence_dir[anchor_pos:].replace("/", "\\")
            cards.append(
                f"""
                <div class="card" style="background:#ffffff;">
                    <p><strong>时间 / 时长：</strong>{_safe(event.get('start_time', event.get('timestamp', 'N/A')))} / {_safe(event.get('duration_ms', event.get('duration_sec', 0)))}{' ms' if event.get('duration_ms') is not None else ''}</p>
                    <p><strong>事件类型：</strong>{_safe(event_type)} | <strong>责任方向：</strong>{_safe(diagnosis.get('owner', '待确认'))} | <strong>等级：</strong>{_safe(confidence_badge)}</p>
                    <p><strong>研发一句话结论：</strong>{_safe(diagnosis.get('statement', ''))}</p>
                    <p><strong>关键依据：</strong>{_safe(diagnosis.get('basis', ''))}</p>
                    <p><strong>触发条件说明：</strong>{_safe(' | '.join(trigger_summary))}</p>
                    <p><strong>建议动作：</strong>{_safe(diagnosis.get('next_action', ''))}</p>
                    <p><strong>最大帧间隔 / 最低FPS：</strong>{float(event.get('max_frame_gap_ms', 0) or 0.0):.0f} ms / {float(event.get('min_fps', event.get('video_fps', 0)) or 0.0):.1f}</p>
                    <p><strong>CPU高相关进程：</strong>{_safe(process_name)} ({process_cpu:.1f}%)</p>
                    <p><strong>证据目录：</strong>{_safe(display_evidence_dir)}</p>
                </div>
                """
            )

        duplicate_stats = summary_data.get("duplicate_event_stats", {}) or {}
        return (
            "<div class='card full'>"
            "<h3>电视端异常事件明细</h3>"
            f"<p class='muted'>以下明细按事件粒度展示确认级卡顿、风险样本和冻结样本，方便研发直接定位“为什么报、证据差什么、先查哪里”。静态画面过滤：已忽略 {int(summary_data.get('tv_stall_ignored_count', 0) or 0)} 次，累计约 {int(summary_data.get('tv_stall_ignored_duration_ms', 0) or 0) / 1000.0:.1f} 秒。冻结分级：确认 {freeze_confirmed} / 风险 {freeze_risk} / 已忽略 {freeze_ignored}。细粒度指标：Jank {float(summary_data.get('tv_jank_sample_ratio_percent', 0) or 0.0):.2f}% / Big Jank {float(summary_data.get('tv_big_jank_sample_ratio_percent', 0) or 0.0):.2f}% / 帧间隔 P95-P99 {float(summary_data.get('tv_frame_gap_p95_ms', 0) or 0.0):.0f}-{float(summary_data.get('tv_frame_gap_p99_ms', 0) or 0.0):.0f} ms。重复事件清洗：卡顿去重 {((duplicate_stats.get('tv_stall') or {}).get('duplicate_count', 0))} 条，风险去重 {((duplicate_stats.get('tv_stall_risk') or {}).get('duplicate_count', 0))} 条，冻结去重 {((duplicate_stats.get('tv_freeze') or {}).get('duplicate_count', 0))} 条。</p>"
            + "".join(cards)
            + "</div>"
        )

    def _render_resource_diagnostics(self, summary_data: Dict) -> str:
        thermal_detected = bool(
            summary_data.get("thermal_throttling_detected", False)
        )
        thermal_class = "result-fail" if thermal_detected else "result-pass"
        thermal_text = "检测到热降频" if thermal_detected else "未检测到热降频"
        thermal_available = int(
            summary_data.get("thermal_available_count", 0) or 0
        ) > 0
        min_ratio = float(summary_data.get("min_cpu_frequency_ratio", 0) or 0.0)
        ratio_text = (
            f"{min_ratio * 100:.1f}%"
            if thermal_available and min_ratio > 0
            else "未采集"
        )
        temperature_text = (
            f"{float(summary_data.get('max_temperature_c', 0) or 0.0):.1f} °C"
            if thermal_available
            else "设备节点不支持或无读取权限"
        )
        if not thermal_available:
            thermal_class = ""
            thermal_text = "未采集，无法判断"
        return f"""
        <div class="section">
            <h3 class="section-title">🧭 资源与热状态诊断</h3>
            <div class="degradation">
                <div><strong>解码吞吐明显下降</strong> {int(summary_data.get('decode_slowdown_count', 0) or 0)} 次</div>
                <div><strong>播放器平均 CPU</strong> {float(summary_data.get('avg_player_cpu_percent', 0) or 0.0):.1f}%</div>
                <div><strong>整机平均 / 峰值 CPU</strong> {float(summary_data.get('avg_system_cpu_percent', 0) or 0.0):.1f}% / {float(summary_data.get('max_system_cpu_percent', 0) or 0.0):.1f}%</div>
                <div><strong>最高温度</strong> {temperature_text}</div>
                <div><strong>最低 CPU 频率比例</strong> {ratio_text}</div>
                <div><strong>热降频判定</strong> <span class="{thermal_class}">{thermal_text}（{int(summary_data.get('thermal_throttling_count', 0) or 0)} 个采样）</span></div>
            </div>
        </div>
        """

    def _normalize_evidence_strength(self, evidence_strength: Dict) -> Dict:
        if not isinstance(evidence_strength, dict):
            evidence_strength = {}
        label = str(evidence_strength.get("label", "") or "").strip() or "Insufficient"
        level = str(evidence_strength.get("level", "") or "").strip() or "insufficient"
        description = str(evidence_strength.get("description", "") or "").strip()
        confidence = float(evidence_strength.get("confidence", 0.0) or 0.0)
        return {
            "label": label,
            "level": level,
            "description": description,
            "confidence": confidence,
        }

    def _render_decoder_stuck_summary(self, summary_data: Dict, diagnosis: Dict = None) -> str:
        decoder_summary = summary_data.get("decoder_stuck_summary") or {}
        count = int(decoder_summary.get("count", 0) or 0)
        if count <= 0:
            return ""
        diagnosis = diagnosis or {}
        diagnosis_strength = self._normalize_evidence_strength(
            diagnosis.get("evidence_strength") or {}
        )

        decoder_name = str(decoder_summary.get("decoder_name", "") or "")
        decoder_names = decoder_summary.get("decoder_names") or []
        decoder_display = decoder_name or ", ".join(
            str(name) for name in decoder_names[:3] if str(name).strip()
        ) or "未识别"
        log_lines = [
            str(line).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            for line in (decoder_summary.get("log_lines") or [])[:3]
            if str(line).strip()
        ]
        diagnostic_lines = [
            str(line).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            for line in (decoder_summary.get("diagnostic_lines") or [])[:3]
            if str(line).strip()
        ]
        log_html = (
            "<div class='log-snippet'>" + "<br/>".join(log_lines) + "</div>"
            if log_lines else ""
        )
        diagnostic_html = (
            "<div class='log-snippet'>" + "<br/>".join(diagnostic_lines) + "</div>"
            if diagnostic_lines else ""
        )
        diagnosis_html = ""
        if diagnosis:
            diagnosis_html = (
                f"<div><strong>Evidence Strength</strong> "
                f"{diagnosis_strength.get('label', 'Insufficient')} "
                f"({diagnosis.get('evidence_level', 'unknown')})</div>"
                f"<div><strong>Conclusion</strong> {diagnosis.get('conclusion', 'N/A')}</div>"
                f"<div><strong>Owner / Target</strong> {diagnosis.get('owner', 'N/A')} / "
                f"{diagnosis.get('suspect_process', decoder_display or 'N/A')}</div>"
            )
        return f"""
        <div class="section">
            <h3 class="section-title">解码输出停顿详解</h3>
            <div class="degradation" style="border-color: #fecaca; background: #fff7ed;">
                {diagnosis_html}
                <div><strong>定义</strong> 活跃 decoder 实例仍存在，但连续至少 1 秒没有产出新帧，表现为 `MPP work_count` 未增长。</div>
                <div><strong>停顿次数 / 最长单次</strong> {count} 次 / {float(decoder_summary.get('max_duration_sec', 0) or 0.0):.2f}s</div>
                <div><strong>涉及 Decoder</strong> {decoder_display}</div>
                <div><strong>代表样本时间</strong> {decoder_summary.get('sample_timestamp', 'N/A')}</div>
                <div><strong>代表样本指标</strong> Video FPS {float(decoder_summary.get('video_fps', 0) or 0.0):.1f} / Decode FPS {float(decoder_summary.get('decode_fps_estimate', 0) or 0.0):.1f} / Expected FPS {float(decoder_summary.get('expected_stream_fps', 0) or 0.0):.1f} / Decode Drop {float(decoder_summary.get('decode_drop_ratio', 0) or 0.0) * 100:.1f}%</div>
                <div><strong>代表样本 CPU</strong> Player {float(decoder_summary.get('player_cpu_percent', 0) or 0.0):.1f}% / System {float(decoder_summary.get('system_cpu_percent', 0) or 0.0):.1f}%</div>
                {f"<div><strong>Decoder Diagnostics</strong>{diagnostic_html}</div>" if diagnostic_html else ""}
                {f"<div><strong>关联 Decoder 日志</strong>{log_html}</div>" if log_html else ""}
            </div>
        </div>
        """

    def _build_executive_summary(self, summary_data: Dict, root_cause_data: Dict) -> str:
        diagnosis = (root_cause_data or {}).get("final_diagnosis") or {}
        tv_stall_count = int(summary_data.get("tv_stall_count", 0) or 0)
        avg_system_cpu = float(summary_data.get("avg_system_cpu_percent", 0) or 0.0)
        avg_player_cpu = float(summary_data.get("avg_player_cpu_percent", 0) or 0.0)
        evidence_level = str(diagnosis.get("evidence_level", "") or "")
        conclusion = str(diagnosis.get("conclusion", "") or "")
        owner = str(diagnosis.get("owner", "") or "")

        suspect = ""
        process_risk_summary = (root_cause_data or {}).get("process_risk_summary") or []
        top_suspect_processes = (root_cause_data or {}).get("top_suspect_processes") or []

        if process_risk_summary:
            top_process = process_risk_summary[0] or {}
            process_name = str(top_process.get("process", "") or "")
            instance_count = int(top_process.get("max_instance_count", 0) or 0)
            if process_name:
                suspect = (
                    f"{process_name} x{instance_count}"
                    if instance_count > 1 and " x" not in process_name
                    else process_name
                )

        if not suspect and top_suspect_processes:
            first_suspect = top_suspect_processes[0] or {}
            if isinstance(first_suspect, dict):
                suspect = str(first_suspect.get("process", "") or "")
            elif isinstance(first_suspect, (list, tuple)) and first_suspect:
                suspect = str(first_suspect[0] or "")

        if not suspect:
            suspect = str(diagnosis.get("suspect_process", "") or "")

        if not owner:
            owner = self._infer_issue_owner_from_process(suspect)

        if (
            tv_stall_count > 0
            and avg_system_cpu >= 85.0
            and avg_player_cpu <= 15.0
            and owner in ("系统/固件侧", "应用/系统联合排查")
        ):
            process_suffix = (
                f"。当前最高优先级高相关进程为 {suspect}"
                if suspect else ""
            )
            return (
                "本轮问题已经明确为：电视端确实发生卡顿，"
                "主因是整机 CPU 长时间被高负载进程竞争，"
                f"问题方向偏{owner}，不是播放器单进程本身性能不足{process_suffix}。"
            ).strip()

        if (
            conclusion
            and evidence_level == "risk"
            and suspect in {"mediaserver", "media.codec", "media.extractor"}
        ):
            return (
                f"本轮播放器 App 自身稳定，但 {suspect} 在多次抖动样本中出现高 CPU 共振。"
                " 当前更应优先按系统媒体服务 / 解码链路方向排查，"
                "只是还缺少解码丢帧或 underrun 等硬证据来升为确认级。"
            )

        if conclusion and evidence_level in ("confirmed", "strong"):
            return conclusion

        if tv_stall_count > 0:
            return (
                f"本轮已确认电视端卡顿 {tv_stall_count} 次，"
                "建议结合根因分析继续锁定责任进程。"
            )

        return ""

    def _infer_issue_owner_from_process(self, process_name: str) -> str:
        process = str(process_name or "").lower()
        if not process:
            return ""
        if (
            process.startswith("/system/bin/")
            or "surfaceflinger" in process
            or "mediaserver" in process
            or "composer" in process
            or "audioserver" in process
        ):
            return "系统/固件侧"
        if "com.thunder.ktv" in process or "player" in process:
            return "应用侧"
        return "应用/系统联合排查"

    def _build_dev_validation_hint(self, cause_type: str) -> str:
        cause = str(cause_type or "").upper()
        if cause == "CPU_CONTENTION":
            return "复测时重点看 tv_stall_count 是否下降，同时确认整机 CPU 明显回落，且高相关进程实例数或占用恢复正常。"
        if cause == "DECODER_STUCK":
            return "复测时重点看解码输出是否恢复连续，video_fps 与 decode_fps_estimate 是否同步恢复，并核对具体 decoder 错误日志是否消失。"
        if cause == "LOW_FPS_DEGRADATION":
            return "复测时重点看 Display 1 的 max_frame_gap_ms 是否回落，电视端 FPS 是否稳定，同时排查 composer / SurfaceFlinger 相关负载。"
        if cause == "AV_SYNC_ISSUE":
            return "复测时重点看 droppedFrames / underrun / buffer starvation 日志是否消失，并确认电视端卡顿事件不再增长。"
        if cause == "THERMAL_THROTTLING":
            return "复测时重点看温度是否下降、CPU 频率是否恢复，且高温阶段不再伴随电视端卡顿或整机 CPU 异常。"
        return "复测时请同时核对电视端卡顿事件、关键日志、整机 CPU 和高相关进程变化，确认问题已收敛。"

    def _render_root_cause(self, summary_data: Dict, root_cause_data: Dict, process_chart_b64: str, timeline_chart_b64: str) -> str:
        if not isinstance(root_cause_data, dict) or not root_cause_data:
            return ""

        total = int(root_cause_data.get("total_stutter_events", 0) or 0)
        identified = int(root_cause_data.get("identified_causes", 0) or 0)
        confirmed = int(root_cause_data.get("confirmed_playback_causes", 0) or 0)
        resource_risks = int(root_cause_data.get("resource_risk_events", 0) or 0)
        log_signals = int(root_cause_data.get("log_signal_events", 0) or 0)
        most = root_cause_data.get("most_confident_cause") or {}
        top_suspect = ""
        top_label = "首要风险候选"
        if isinstance(most, dict):
            t = str(most.get("root_cause_type", "") or "")
            s = str(most.get("suspect_process", "") or "")
            c = most.get("confidence", 0)
            evidence = most.get("evidence") or {}
            signal_only = bool(
                isinstance(evidence, dict)
                and evidence.get("signal_only", False)
            )
            resource_only = bool(
                isinstance(evidence, dict)
                and evidence.get("resource_only", False)
            )
            if signal_only:
                top_label = "首要日志信号"
            elif resource_only:
                top_label = "首要资源风险候选"
            if t or s:
                top_suspect = f"{t} | {s} (风险评分: {c})"

        top_suspect_items = []
        for item in (root_cause_data.get("top_suspect_processes") or [])[:3]:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            top_suspect_items.append(
                f"<li><strong>{item[0]}</strong><span>命中 {item[1]} 次根因候选</span></li>"
            )
        top_suspect_html = (
            "<div style='margin-top: 16px;'>"
            "<h4>根因高相关对象 Top</h4>"
            "<ul class='suspect-list'>"
            + "".join(top_suspect_items)
            + "</ul></div>"
            if top_suspect_items
            else ""
        )
        diagnosis = root_cause_data.get("final_diagnosis") or {}
        diagnosis_html = ""
        if isinstance(diagnosis, dict) and diagnosis:
            diagnosis_strength = self._normalize_evidence_strength(
                diagnosis.get("evidence_strength") or {}
            )
            action_items = []
            for action in (diagnosis.get("actions") or [])[:3]:
                action_items.append(f"<li>{action}</li>")
            suspect_process = str(diagnosis.get("suspect_process", "") or "N/A")
            cause_type = str((most or {}).get("root_cause_type", "") or "")
            validation_hint = self._build_dev_validation_hint(cause_type)
            confidence_text = ""
            if diagnosis.get("confidence") not in (None, ""):
                confidence_text = f" | 置信度 {float(diagnosis.get('confidence', 0) or 0.0):.1f}"
            diagnosis_html = (
                "<div class='degradation' style='margin-top: 16px; border-color: #c7d2fe; background: #eef2ff;'>"
                f"<div><strong>问题判断</strong> {diagnosis.get('title', 'N/A')}</div>"
                f"<div><strong>定性结论</strong> {diagnosis.get('conclusion', 'N/A')}</div>"
                f"<div><strong>证据等级 / 建议责任方向</strong> {diagnosis.get('evidence_level', 'unknown')} / {diagnosis.get('owner', '待确认')}{confidence_text}</div>"
                f"<div><strong>优先处理对象</strong> {suspect_process}</div>"
                + (
                    "<div style='margin-top: 8px;'><strong>研发处理建议（按优先级）</strong><ul>"
                    + "".join(action_items)
                    + "</ul></div>"
                    if action_items else ""
                )
                + f"<div style='margin-top: 8px;'><strong>回归验证标准</strong> {validation_hint}</div>"
                + "</div>"
            )

        rows = []
        detail_cards = []
        timeline_links = []
        causes = root_cause_data.get("all_causes") or []
        start_ts = causes[0].get("timestamp") if causes else None
        start_sec = self._to_epoch_seconds(start_ts) if start_ts else None
        for idx, c in enumerate(causes, start=1):
            if not isinstance(c, dict):
                continue
            ts = str(c.get("timestamp", "") or "")
            rct = str(c.get("root_cause_type", "") or "")
            sp = str(c.get("suspect_process", "") or "")
            conf = c.get("confidence", 0)
            sug = str(c.get("suggestion", "") or "")
            evidence = c.get("evidence") or {}
            kind = "风险候选"
            if isinstance(evidence, dict):
                if evidence.get("signal_only", False):
                    kind = "辅助日志信号"
                elif evidence.get("resource_only", False):
                    kind = "资源风险候选"
            card_id = f"rc-event-{idx}"
            offset_label = ""
            sec = self._to_epoch_seconds(ts)
            if sec is not None and start_sec is not None:
                offset_label = f"T+{((sec - start_sec) / 60.0):.1f} min"
            evidence_html = self._format_root_cause_evidence(evidence)
            rows.append(f"<tr><td><a href='#{card_id}' data-target-card='{card_id}'>#{idx}</a><br/>{ts}</td><td>{kind}</td><td>{rct}</td><td>{sp}</td><td>{conf}</td><td>{evidence_html}</td><td>{sug}</td></tr>")
            detail_cards.append(self._render_root_cause_detail_card(c, idx, card_id))
            timeline_links.append(
                f"<a class='timeline-link' href='#{card_id}' data-target-card='{card_id}'>"
                f"<strong>#{idx} {rct or 'UNKNOWN'}</strong>"
                f"<small>{ts}{(' | ' + offset_label) if offset_label else ''}</small>"
                f"<small>{sp or 'N/A'}</small>"
                "</a>"
            )
        table_html = "".join(rows) if rows else "<tr><td colspan='7'>无有效风险候选记录</td></tr>"
        timeline_links_html = "".join(timeline_links)
        process_rows = []
        for item in (root_cause_data.get("process_risk_summary") or [])[:10]:
            if not isinstance(item, dict):
                continue
            process_rows.append(
                "<tr>"
                f"<td>{item.get('process', '')}</td>"
                f"<td>{item.get('max_instance_count', 1)}</td>"
                f"<td>{item.get('primary_event_count', item.get('event_count', 0))} / {item.get('appearance_count', item.get('event_count', 0))}</td>"
                f"<td>{item.get('avg_cpu_percent', 0)}%</td>"
                f"<td>{item.get('peak_cpu_percent', 0)}%</td>"
                f"<td>{item.get('max_system_cpu_percent', 0)}%</td>"
                "</tr>"
            )
        process_table_html = "".join(process_rows)

        charts_html = ""
        if process_chart_b64:
            charts_html += f"""
            <div class="chart-box" style="margin-top: 16px;">
                <h4>进程CPU对比（基线 vs 卡顿时）</h4>
                <img src="data:image/png;base64,{process_chart_b64}" alt="Process Comparison"/>
            </div>
            """
        if timeline_chart_b64:
            charts_html += f"""
            <div class="chart-box" style="margin-top: 16px;">
                <h4>卡顿时间线（CPU）</h4>
                <img src="data:image/png;base64,{timeline_chart_b64}" alt="Stutter Timeline"/>
                {f"<div class='timeline-links'>{timeline_links_html}</div>" if timeline_links_html else ""}
            </div>
            """

        return f"""
        <div class="section">
            <h3 class="section-title">🔍 异常触发与根因分析 (V3.1)</h3>
            <div class="degradation">
                <div><strong>异常触发快照</strong> {total} 次</div>
                <div><strong>根因 / 风险候选</strong> {identified} 次</div>
                <div><strong>已确认播放退化 / 资源风险 / 日志信号</strong> {confirmed} / {resource_risks} / {log_signals}</div>
                <div><strong>{top_label}</strong> {top_suspect if top_suspect else 'N/A'}</div>
                <div>日志命中仅作为辅助信号，不等同于人眼可见卡顿；需结合电视 Surface 帧时间、解码吞吐和 CPU 证据判断。</div>
            </div>
            {diagnosis_html}
            {top_suspect_html}
            <div style="margin-top: 12px;">
                <table class="rc-table">
                    <thead>
                        <tr><th>时间</th><th>证据类型</th><th>风险类型</th><th>高相关进程</th><th>风险评分</th><th>关键证据</th><th>建议</th></tr>
                    </thead>
                    <tbody>
                        {table_html}
                    </tbody>
                </table>
            </div>
            {f'''
            <div style="margin-top: 16px;">
                <h4>单次卡顿证据明细</h4>
                <div class="rc-detail-grid">{''.join(detail_cards)}</div>
            </div>
            ''' if detail_cards else ''}
            {f'''
            <div style="margin-top: 20px;">
                <h4>重复 / 高负载进程聚合榜</h4>
                <table>
                    <thead>
                        <tr><th>进程</th><th>最多实例</th><th>命中采样</th><th>合计CPU均值</th><th>合计CPU峰值</th><th>整机CPU峰值</th></tr>
                    </thead>
                    <tbody>{process_table_html}</tbody>
                </table>
                <div class="header-meta" style="margin-top: 8px;">
                    同名实例 CPU 为聚合值，例如 37 个进程各约 0.3%，合计约 11.1%。
                </div>
            </div>
            ''' if process_table_html else ''}
            {charts_html}
        </div>
        """

    def _format_root_cause_evidence(self, evidence: Dict) -> str:
        if not isinstance(evidence, dict):
            return "-"
        snapshot = evidence.get("event_snapshot") or {}
        chips = []
        if isinstance(snapshot, dict):
            fps = float(snapshot.get("video_fps", 0) or 0.0)
            if fps > 0:
                chips.append(f"FPS {fps:.1f}")
            decode_fps = float(snapshot.get("decode_fps_estimate", 0) or 0.0)
            if decode_fps > 0:
                chips.append(f"解码 {decode_fps:.1f}fps")
            drop_ratio = float(snapshot.get("decode_drop_ratio", 0) or 0.0)
            chips.append(f"丢帧率 {drop_ratio * 100:.1f}%")
            cpu = float(snapshot.get("system_cpu_percent", 0) or 0.0)
            if cpu > 0:
                chips.append(f"整机CPU {cpu:.1f}%")
            log_delta = int(snapshot.get("log_stutter_delta", 0) or 0)
            if log_delta > 0:
                chips.append(f"日志+{log_delta}")
        top_processes = evidence.get("top_processes") or []
        if isinstance(top_processes, list) and top_processes:
            first = top_processes[0]
            if isinstance(first, dict):
                chips.append(
                    f"Top {first.get('process', '')} {float(first.get('cpu_percent', 0) or 0.0):.1f}%"
                )
        if not chips:
            return "-"
        return "<div class='evidence-list'>" + "".join(
            f"<span class='evidence-chip'>{chip}</span>" for chip in chips[:6]
        ) + "</div>"

    def _render_root_cause_detail_card(self, cause: Dict, index: int, card_id: str) -> str:
        if not isinstance(cause, dict):
            return ""
        evidence = cause.get("evidence") or {}
        snapshot = evidence.get("event_snapshot") or {}
        top_processes = evidence.get("top_processes") or []
        log_events = evidence.get("log_stutter_events") or []
        decoder_events = evidence.get("decoder_log_events") or []
        decoder_diagnostics = evidence.get("decoder_diagnostics") or {}
        top_html = ""
        if isinstance(top_processes, list) and top_processes:
            formatted = []
            for item in top_processes[:3]:
                if not isinstance(item, dict):
                    continue
                formatted.append(
                    f"{item.get('process', '')} {float(item.get('cpu_percent', 0) or 0.0):.1f}%"
                )
            if formatted:
                top_html = f"<p><strong>Top Processes</strong> {' | '.join(formatted)}</p>"
        log_html = ""
        if isinstance(log_events, list) and log_events:
            lines = []
            for item in log_events[:3]:
                if not isinstance(item, dict):
                    continue
                pattern = str(item.get("pattern", "") or "")
                line = str(item.get("line", "") or "")
                if not line:
                    continue
                if pattern:
                    lines.append(f"[{pattern}] {line}")
                else:
                    lines.append(line)
            if lines:
                escaped = "<br/>".join(
                    str(line)
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    for line in lines
                )
                log_html = f"<div class='log-snippet'>{escaped}</div>"
        decoder_html = ""
        decoder_name = str(
            evidence.get("decoder_name", "")
            or (decoder_diagnostics.get("decoder_name", "") if isinstance(decoder_diagnostics, dict) else "")
            or cause.get("suspect_process", "")
            or ""
        )
        decoder_lines = []
        if decoder_name:
            decoder_lines.append(f"<p><strong>Decoder</strong> {decoder_name}</p>")
        decoder_source_lines = []
        if isinstance(decoder_events, list) and decoder_events:
            decoder_source_lines = [
                str(item.get("line", "") or "")
                for item in decoder_events[:3]
                if isinstance(item, dict) and str(item.get("line", "") or "")
            ]
        elif isinstance(decoder_diagnostics, dict):
            decoder_source_lines = [
                str(line)
                for line in (decoder_diagnostics.get("codec_lines") or [])[:3]
                if str(line).strip()
            ]
        if decoder_source_lines:
            escaped_decoder = "<br/>".join(
                str(line)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                for line in decoder_source_lines
            )
            decoder_lines.append(f"<div class='log-snippet'>{escaped_decoder}</div>")
        if decoder_lines:
            decoder_html = "".join(decoder_lines)
        return (
            f"<div class='rc-detail-card' id='{card_id}'>"
            f"<h5>#{index} {cause.get('timestamp', '')} | {cause.get('root_cause_type', '')}</h5>"
            f"<p><strong>Suspect</strong> {cause.get('suspect_process', '') or 'N/A'}</p>"
            f"<p><strong>System CPU / Player CPU</strong> {float(snapshot.get('system_cpu_percent', 0) or 0.0):.1f}% / {float(snapshot.get('player_cpu_percent', 0) or 0.0):.1f}%</p>"
            f"<p><strong>Video FPS / Decode FPS</strong> {float(snapshot.get('video_fps', 0) or 0.0):.1f} / {float(snapshot.get('decode_fps_estimate', 0) or 0.0):.1f}</p>"
            f"<p><strong>Expected FPS / Decode Drop</strong> {float(snapshot.get('expected_stream_fps', 0) or 0.0):.1f} / {float(snapshot.get('decode_drop_ratio', 0) or 0.0) * 100:.1f}%</p>"
            f"<p><strong>Log Delta / TV Stall / Thermal</strong> +{int(snapshot.get('log_stutter_delta', 0) or 0)} / {'Y' if snapshot.get('tv_stutter_detected', False) else 'N'} / {'Y' if snapshot.get('thermal_throttling', False) else 'N'}</p>"
            f"{top_html}"
            f"{log_html}"
            f"{decoder_html}"
            f"<p><strong>Suggestion</strong> {cause.get('suggestion', '')}</p>"
            "</div>"
        )
    def _generate_process_comparison_chart(self, root_cause_data: Dict) -> str:
        if not isinstance(root_cause_data, dict) or not root_cause_data:
            return ""

        causes = root_cause_data.get("all_causes") or []
        if not isinstance(causes, list) or not causes:
            return ""

        proc_stats: Dict[str, Dict[str, float]] = {}
        for c in causes:
            if not isinstance(c, dict):
                continue
            if str(c.get("root_cause_type", "") or "") != "CPU_CONTENTION":
                continue
            proc = str(c.get("suspect_process", "") or "")
            if not proc:
                continue
            ev = c.get("evidence") or {}
            if not isinstance(ev, dict):
                ev = {}
            stutter_cpu = float(ev.get("stutter_cpu", 0) or 0.0)
            baseline_cpu = float(ev.get("baseline_cpu", 0) or 0.0)
            if stutter_cpu <= 0:
                continue
            current = proc_stats.get(proc) or {"stutter": 0.0, "baseline": 0.0}
            current["stutter"] = max(float(current["stutter"]), stutter_cpu)
            current["baseline"] = max(float(current["baseline"]), baseline_cpu)
            proc_stats[proc] = current

        if not proc_stats:
            return ""

        items = []
        for proc, v in proc_stats.items():
            delta = float(v["stutter"]) - float(v["baseline"])
            items.append((proc, float(v["baseline"]), float(v["stutter"]), delta))
        items.sort(key=lambda x: x[3], reverse=True)
        items = items[:8]

        names = [x[0] for x in items][::-1]
        baseline_vals = [x[1] for x in items][::-1]
        stutter_vals = [x[2] for x in items][::-1]
        deltas = [x[3] for x in items][::-1]

        plt.figure(figsize=(10, max(3.0, 0.6 * len(names) + 1.8)))
        y = list(range(len(names)))
        bar_h = 0.35
        plt.barh([i - bar_h / 2 for i in y], baseline_vals, height=bar_h, color="#60a5fa", label="Baseline")
        plt.barh([i + bar_h / 2 for i in y], stutter_vals, height=bar_h, color="#f87171", label="Stutter")
        plt.yticks(y, names)
        plt.xlabel("CPU %")
        plt.title("Process CPU Comparison")
        plt.grid(True, axis="x", linestyle="--", alpha=0.4)
        plt.legend(loc="lower right")

        max_x = max(stutter_vals + baseline_vals) if (stutter_vals or baseline_vals) else 0.0
        for i, d in enumerate(deltas):
            x = max(stutter_vals[i], baseline_vals[i]) + max_x * 0.02
            plt.text(x, y[i], f"+{d:.1f}", va="center", fontsize=8, color="#b91c1c")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight")
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")

    def _generate_stutter_timeline_chart(self, history: List[Dict], root_cause_data: Dict) -> str:
        if not history:
            return ""

        data = history[-3600:] if len(history) > 3600 else history
        start_ts = data[0].get("timestamp")
        start_sec = self._to_epoch_seconds(start_ts)

        x_axis = []
        y_axis = []
        colors = []
        labels = []
        for i, pt in enumerate(data):
            sec = self._to_epoch_seconds(pt.get("timestamp"))
            if sec is not None and start_sec is not None:
                x_axis.append((sec - start_sec) / 60.0)
            else:
                x_axis.append(i / 60.0)
            y_axis.append(float(
                pt.get("system_cpu_percent", pt.get("cpu_percent", 0)) or 0.0
            ))

            rct = str(pt.get("root_cause_type", "") or "")
            is_stutter = bool(rct) and rct != "UNKNOWN"
            if not is_stutter:
                is_stutter = bool(pt.get("decoder_stuck", False)) or bool(pt.get("is_perceptual_jank", False)) or (float(pt.get("decode_drop_ratio", 0) or 0.0) > 0.1)

            colors.append("#ef4444" if is_stutter else "#22c55e")
            labels.append(rct if rct else "")

        if not any(c == "#ef4444" for c in colors):
            return ""

        plt.figure(figsize=(10, 4))
        plt.scatter(x_axis, y_axis, c=colors, s=18, alpha=0.85)
        plt.title("Stutter Timeline (CPU)")
        plt.xlabel("Time (minutes)")
        plt.ylabel("CPU %")
        plt.ylim(0, 100)
        plt.grid(True, linestyle="--", alpha=0.4)

        for i, lab in enumerate(labels):
            if not lab:
                continue
            plt.text(x_axis[i], y_axis[i] + 2.0, lab, fontsize=7, color="#991b1b", rotation=25)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight")
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")
    
    def generate_comparison(self, json_path_a: str, json_path_b: str, filename_prefix: str):
        with open(json_path_a, 'r', encoding='utf-8') as fa:
            data_a = json.load(fa)
        with open(json_path_b, 'r', encoding='utf-8') as fb:
            data_b = json.load(fb)
        meta_a = data_a.get('meta', {})
        meta_b = data_b.get('meta', {})
        decision_a = data_a.get('decision', {})
        decision_b = data_b.get('decision', {})
        stats_a = data_a.get('stats', {})
        stats_b = data_b.get('stats', {})
        counts_a = decision_a.get('counts', {})
        counts_b = decision_b.get('counts', {})
        def val(d, k, default=0):
            return d.get(k, default)
        rows = [
            ("包名", meta_a.get('package_name',''), meta_b.get('package_name','')),
            ("开始时间", meta_a.get('start_time',''), meta_b.get('start_time','')),
            ("结束时间", meta_a.get('end_time',''), meta_b.get('end_time','')),
            ("时长(秒)", str(meta_a.get('duration_sec',0)), str(meta_b.get('duration_sec',0))),
            ("评分", f"{decision_a.get('score',0)} ({decision_a.get('grade','')})", f"{decision_b.get('score',0)} ({decision_b.get('grade','')})"),
            ("建议", "建议上线" if decision_a.get('ready_to_release') else "不建议上线", "建议上线" if decision_b.get('ready_to_release') else "不建议上线"),
            ("平均PSS(MB)", str(stats_a.get('avg_pss_mb',0)), str(stats_b.get('avg_pss_mb',0))),
            ("峰值PSS(MB)", str(stats_a.get('max_pss_mb',0)), str(stats_b.get('max_pss_mb',0))),
            ("总丢帧", str(stats_a.get('total_gfx_jank',0)), str(stats_b.get('total_gfx_jank',0))),
            ("总渲染帧数", str(stats_a.get('total_frames_delta',0)), str(stats_b.get('total_frames_delta',0))),
            ("平均视频帧率(FPS)", str(stats_a.get('avg_video_fps','N/A')), str(stats_b.get('avg_video_fps','N/A'))),
            ("解码估算丢帧", str(stats_a.get('decode_drop_estimate_total',0)), str(stats_b.get('decode_drop_estimate_total',0))),
            ("解码估算丢帧率", str(stats_a.get('decode_drop_ratio',0)), str(stats_b.get('decode_drop_ratio',0))),
            ("卡顿日志次数", str(stats_a.get('final_log_stutter_count',0)), str(stats_b.get('final_log_stutter_count',0))),
            ("UI丢帧率(GFX Jank)", str(stats_a.get('avg_jank_percent',0)), str(stats_b.get('avg_jank_percent',0))),
            ("重启次数", str(stats_a.get('restart_count',0)), str(stats_b.get('restart_count',0))),
            ("崩溃数", str(stats_a.get('crash_count',0)), str(stats_b.get('crash_count',0))),
            ("ANR数", str(stats_a.get('anr_count',0)), str(stats_b.get('anr_count',0))),
            ("屏幕异常", str(counts_a.get('screen_anomaly',0)), str(counts_b.get('screen_anomaly',0)))
        ]
        table_rows = "".join([f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>" for r in rows])
        html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>对比报告</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }}
        th {{ background-color: #f8f9fa; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Android 播放器压测对比报告</h1>
        <p>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <table>
            <thead>
                <tr>
                    <th>指标</th>
                    <th>A</th>
                    <th>B</th>
                </tr>
            </thead>
            <tbody>
                {table_rows}
            </tbody>
        </table>
    </div>
</body>
</html>
        """
        filename = f"compare_{filename_prefix}.html"
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)
        return filepath

    def _render_deductions(self, deductions):
        if not deductions:
            return ""
        
        items = "".join([f"<li style='margin: 6px 0;'>{d}</li>" for d in deductions])
        return f"""
        <div class="section" style="background: #fffbeb; padding: 16px; border-radius: 8px; border-left: 4px solid #f59e0b;">
            <h3 class="section-title" style="color: #b45309; margin-top: 0;">⚠️ 扣分/风险项</h3>
            <ul style="margin: 0; padding-left: 20px;">{items}</ul>
        </div>
        """

    def _render_error_table(self, summary):
        rows = ""
        # 1. Failed Sessions
        for item in summary.get('failed_sessions', []):
            rows += f"""
            <tr>
                <td><span class="error-tag bg-orange">播放失败</span></td>
                <td>{item.get('time')}</td>
                <td>{item.get('song')}</td>
                <td>{item.get('reason')}</td>
            </tr>
            """
        
        # 2. System Errors
        for evt in summary.get('error_events', []):
            bg = "bg-red" if evt['type'] in ['CRASH', 'ANR'] else "bg-orange"
            rows += f"""
            <tr>
                <td><span class="error-tag {bg}">{evt['type']}</span></td>
                <td>{evt['time']}</td>
                <td>System</td>
                <td>{evt['message']} <br/><small>{evt.get('log_file','')}</small></td>
            </tr>
            """
            
        # 3. PID Events
        for evt in summary.get('pid_events', []):
            if evt['type'] in ["PID_RESTART", "PID_LOST"]:
                rows += f"""
                <tr>
                    <td><span class="error-tag bg-red">PID重启</span></td>
                    <td>{evt['timestamp']}</td>
                    <td>Process</td>
                    <td>{evt['description']} (T+{evt.get('elapsed_min', 0)}m)</td>
                </tr>
                """

        if not rows:
            return "<p class=\"no-errors\">✅ 太棒了！本次测试未发现明显错误。</p>"
            
        return f"""
        <table>
            <thead>
                <tr>
                    <th style="width: 100px;">类型</th>
                    <th style="width: 180px;">时间</th>
                    <th style="width: 200px;">对象</th>
                    <th>详情</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
        """

    def _render_degradation(self, summary):
        deg = summary.get('degradation_analysis', {})
        if deg.get("status") == "insufficient_data":
            return "数据不足，无法分析退化趋势。"
        
        return f"""
        内存增长速率: <strong>{deg.get('mem_growth_rate_mb_per_hour', 0)} MB/h</strong><br/>
        CPU 变化幅度: {deg.get('cpu_change_percent', 0)}%<br/>
        (首段均值: {deg.get('first_avg_pss', 0)} MB -> 末段均值: {deg.get('last_avg_pss', 0)} MB)
        """

    def _generate_pss_chart(self, history: List[Dict]) -> str:
        """生成 PSS 曲线 Base64"""
        if not history: return ""
        
        # 只取最近 1 小时的数据 (假设 3600 个点，如果采样间隔 1s)
        # 或者全部数据，视需求而定。用户说 "最近 1 小时"，这里简单处理取最后 3600 点
        data = history[-3600:] if len(history) > 3600 else history
        
        timestamps = [x.get('timestamp') for x in data]
        start_ts = data[0].get('timestamp')
        start_sec = self._to_epoch_seconds(start_ts)
        x_axis = []
        for i, t in enumerate(timestamps):
            sec = self._to_epoch_seconds(t)
            if sec is not None and start_sec is not None:
                x_axis.append((sec - start_sec) / 60.0)
            else:
                x_axis.append(i / 60.0)
        y_axis = [x.get('pss_mb', 0) for x in data]
        
        plt.figure(figsize=(10, 4))
        plt.plot(x_axis, y_axis, color='#2196F3', linewidth=1.5)
        plt.title("Memory Usage (PSS)")
        plt.xlabel("Time (minutes)")
        plt.ylabel("MB")
        plt.grid(True, linestyle='--', alpha=0.5)
        
        # 标记重启点
        for i, pt in enumerate(data):
            if pt.get('is_restarted'):
                plt.axvline(x=x_axis[i], color='red', linestyle='--', alpha=0.8)
                plt.text(x_axis[i], max(y_axis)*0.9, 'Restart', color='red', fontsize=8)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    def _generate_cpu_chart(self, history: List[Dict]) -> str:
        """生成 CPU 曲线 Base64"""
        if not history: return ""
        data = history[-3600:] if len(history) > 3600 else history
        
        timestamps = [x.get('timestamp') for x in data]
        start_ts = data[0].get('timestamp')
        start_sec = self._to_epoch_seconds(start_ts)
        x_axis = []
        for i, t in enumerate(timestamps):
            sec = self._to_epoch_seconds(t)
            if sec is not None and start_sec is not None:
                x_axis.append((sec - start_sec) / 60.0)
            else:
                x_axis.append(i / 60.0)
        player_cpu = [
            float(x.get("player_cpu_percent", x.get("cpu_percent", 0)) or 0.0)
            for x in data
        ]
        system_cpu = [
            float(x.get("system_cpu_percent", 0) or 0.0)
            for x in data
        ]
        
        plt.figure(figsize=(10, 4))
        plt.plot(
            x_axis,
            player_cpu,
            color="#4CAF50",
            linewidth=1.0,
            label="Player Process",
        )
        if any(value > 0 for value in system_cpu):
            plt.plot(
                x_axis,
                system_cpu,
                color="#ef4444",
                linewidth=1.2,
                label="System Total",
            )
        plt.title("CPU Usage: Player vs System")
        plt.xlabel("Time (minutes)")
        plt.ylabel("%")
        plt.ylim(0, 100)
        plt.grid(True, linestyle='--', alpha=0.5)
        plt.legend(loc="upper right")
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    def _generate_fps_chart(self, history: List[Dict]) -> str:
        """生成视频 FPS 曲线 Base64（仅当有有效 FPS 数据时）"""
        if not history:
            return ""
        data = history[-3600:] if len(history) > 3600 else history
        fps_values = [x.get('video_fps', 0) for x in data]
        if not any(v > 0 for v in fps_values):
            return ""
        timestamps = [x.get('timestamp') for x in data]
        start_ts = data[0].get('timestamp')
        start_sec = self._to_epoch_seconds(start_ts)
        x_axis = []
        for i, t in enumerate(timestamps):
            sec = self._to_epoch_seconds(t)
            if sec is not None and start_sec is not None:
                x_axis.append((sec - start_sec) / 60.0)
            else:
                x_axis.append(i / 60.0)
        plt.figure(figsize=(10, 4))
        plt.plot(x_axis, fps_values, color='#8b5cf6', linewidth=1.5)
        plt.title("Video FPS")
        plt.xlabel("Time (minutes)")
        plt.ylabel("FPS")
        plt.ylim(0, max(max(fps_values) * 1.1, 30))
        plt.grid(True, linestyle='--', alpha=0.5)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    def _to_epoch_seconds(self, ts):
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                return dt.timestamp()
            except Exception:
                return None
        return None
