import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.player_stress.core.report_generator import ReportGenerator


ZH = {
    "title": "\u64ad\u653e\u5668\u538b\u6d4b\u62a5\u544a",
    "recommend_release": "\u5efa\u8bae\u4e0a\u7ebf",
    "recommend_inconclusive": "\u8bc1\u636e\u4e0d\u8db3\uff0c\u6682\u4e0d\u4f5c\u4e0a\u7ebf\u7ed3\u8bba",
    "recommend_reject": "\u4e0d\u5efa\u8bae\u4e0a\u7ebf",
    "monitor_only": "\u7eaf\u76d1\u63a7\u6a21\u5f0f\uff0c\u4e0d\u4e3b\u52a8\u70b9\u6b4c\u6216\u7edf\u8ba1\u6b4c\u66f2\u6210\u529f\u7387",
    "playback_na": "\u4e0d\u9002\u7528\uff08\u7eaf\u76d1\u63a7\u6a21\u5f0f\uff09",
    "surface_unavailable": "\u672a\u8bc6\u522b\u5230\u7535\u89c6\u7aef\u89c6\u9891 Surface",
}


def format_duration(duration_sec: int) -> str:
    duration_sec = max(0, int(duration_sec or 0))
    hours = duration_sec // 3600
    minutes = (duration_sec % 3600) // 60
    seconds = duration_sec % 60
    return f"{hours}\u5c0f\u65f6 {minutes}\u5206\u949f {seconds}\u79d2"


def perceptual_from_score(score: int) -> Dict:
    score = int(score or 0)
    if score >= 80:
        level = "\u4e25\u91cd\u5361\u987f"
        recommendation = "\u4e0d\u5efa\u8bae\u4e0a\u7ebf\uff0c\u5b58\u5728\u660e\u663e\u5361\u987f\u95ee\u9898"
    elif score >= 50:
        level = "\u660e\u663e\u5361\u987f"
        recommendation = "\u9700\u8981\u4f18\u5316\uff0c\u7528\u6237\u53ef\u660e\u663e\u611f\u77e5\u5361\u987f"
    elif score >= 30:
        level = "\u8f7b\u5fae\u5361\u987f"
        recommendation = "\u5efa\u8bae\u4f18\u5316\uff0c\u90e8\u5206\u7528\u6237\u53ef\u80fd\u611f\u77e5\u5230\u5361\u987f"
    elif score >= 15:
        level = "\u5fae\u5361\u987f"
        recommendation = "\u53ef\u63a5\u53d7\uff0c\u4f46\u5efa\u8bae\u5173\u6ce8"
    else:
        level = "\u6d41\u7545"
        recommendation = "\u64ad\u653e\u6d41\u7545\uff0c\u53ef\u4ee5\u4e0a\u7ebf"
    severity = "low"
    if score >= 50:
        severity = "high"
    elif score >= 15:
        severity = "medium"
    return {
        "score": score,
        "level": level,
        "recommendation": recommendation,
        "human_perceptible": score >= 15,
        "severity": severity,
    }


def clean_deductions(stats: Dict, decision: Dict) -> List[str]:
    items: List[str] = []
    counts = decision.get("counts") or {}
    restart = int(counts.get("restart", 0) or 0)
    crash = int(counts.get("crash", 0) or 0)
    pid_loss = int(stats.get("pid_loss_count", 0) or 0)
    screen_anomaly = int(counts.get("screen_anomaly", 0) or 0)
    avg_system_cpu = float(stats.get("avg_system_cpu_percent", 0) or 0.0)
    max_system_cpu = float(stats.get("max_system_cpu_percent", 0) or 0.0)
    confirmed_decoder_stuck = int(stats.get("confirmed_decoder_stuck_count", 0) or 0)
    tv_stall_count = int(stats.get("tv_stall_count", 0) or 0)
    avg_player_cpu = float(stats.get("avg_player_cpu_percent", 0) or 0.0)

    if restart > 0:
        items.append(f"PID\u91cd\u542f {restart} \u6b21 (-41) [\u96f6\u5bb9\u5fcd]")
    if crash > 0:
        items.append(f"\u5d29\u6e83/ANR {crash} \u6b21 (-41) [\u96f6\u5bb9\u5fcd]")
    if pid_loss > 0 and restart == 0:
        items.append(f"\u76ee\u6807\u64ad\u653e\u5668\u8fdb\u7a0b\u4e22\u5931 {pid_loss} \u6b21 (-41) [\u96f6\u5bb9\u5fcd]")
    if screen_anomaly > 0:
        ded = min(40, screen_anomaly * 20)
        items.append(f"\u6709\u6548\u5c4f\u5e55\u5f02\u5e38 {screen_anomaly} \u6b21 (-{ded}\uff0c\u540c\u7c7b\u5c01\u987640)")
    if avg_system_cpu >= 90.0:
        items.append(f"\u6574\u673a\u5e73\u5747CPU\u8fc7\u9ad8 ({avg_system_cpu:.1f}%) (-25)")
    elif avg_system_cpu >= 75.0:
        items.append(f"\u6574\u673a\u5e73\u5747CPU\u504f\u9ad8 ({avg_system_cpu:.1f}%) (-15)")
    elif max_system_cpu >= 95.0:
        items.append(f"\u6574\u673aCPU\u5cf0\u503c\u8fc7\u9ad8 ({max_system_cpu:.1f}%) (-10)")
    if confirmed_decoder_stuck > 0:
        items.append("\u786c\u4ef6\u89e3\u7801\u8f93\u51fa\u505c\u987f (-25)")
    elif tv_stall_count > 0 and avg_system_cpu >= 85.0 and avg_player_cpu <= 15.0:
        items.append("\u591a\u8fdb\u7a0bCPU\u7ade\u4e89\u5bfc\u81f4\u5361\u987f (-15)")
    return items


def clean_blockers(stats: Dict) -> List[str]:
    blockers: List[str] = []
    pid_loss = int(stats.get("pid_loss_count", 0) or 0)
    if bool(stats.get("target_process_lost", False)) or pid_loss > 0:
        blockers.append(f"\u76ee\u6807\u64ad\u653e\u5668\u8fdb\u7a0b\u4e22\u5931 {max(1, pid_loss)} \u6b21\uff0c\u6d4b\u8bd5\u94fe\u8def\u4e2d\u65ad")
    valid_ratio = float(stats.get("valid_sample_ratio", 1.0) or 0.0)
    if valid_ratio < 0.8:
        blockers.append(f"\u6709\u6548\u76d1\u63a7\u8986\u76d6\u7387\u4ec5 {valid_ratio * 100:.1f}%")
    avg_system_cpu = float(stats.get("avg_system_cpu_percent", 0) or 0.0)
    if avg_system_cpu >= 90.0:
        blockers.append("\u6574\u673aCPU\u6301\u7eed\u9ad8\u8d1f\u8f7d")
    tv_stall_count = int(stats.get("tv_stall_count", 0) or 0)
    if tv_stall_count > 0:
        blockers.append(f"\u786e\u8ba4\u7535\u89c6\u7aef\u5361\u987f {tv_stall_count} \u6b21")
    confirmed_decoder_stuck = int(stats.get("confirmed_decoder_stuck_count", 0) or 0)
    if confirmed_decoder_stuck > 0:
        blockers.append(f"\u89e3\u7801\u8f93\u51fa\u505c\u987f {confirmed_decoder_stuck} \u6b21")
    if not bool(stats.get("tv_surface_locked", False)):
        blockers.append("\u672a\u9501\u5b9a\u7535\u89c6\u7aef\u89c6\u9891Surface\uff0c\u8bc1\u636e\u8986\u76d6\u4e0d\u8db3")
    return blockers


def infer_diagnosis(stats: Dict) -> Dict:
    tv_stall_count = int(stats.get("tv_stall_count", 0) or 0)
    confirmed_decoder_stuck = int(stats.get("confirmed_decoder_stuck_count", 0) or 0)
    decoder_risk = int(stats.get("decoder_stuck_risk_count", 0) or 0)
    avg_system_cpu = float(stats.get("avg_system_cpu_percent", 0) or 0.0)
    avg_player_cpu = float(stats.get("avg_player_cpu_percent", 0) or 0.0)
    surface_locked = bool(stats.get("tv_surface_locked", False))
    decoder_summary = stats.get("decoder_stuck_summary") or {}
    decoder_name = str(decoder_summary.get("decoder_name", "") or "\u672a\u8bc6\u522b")

    if tv_stall_count > 0 and avg_system_cpu >= 85.0 and avg_player_cpu <= 15.0:
        return {
            "title": "CPU \u8d44\u6e90\u7ade\u4e89\u5bfc\u81f4\u7535\u89c6\u7aef\u5361\u987f",
            "conclusion": "\u53ef\u5224\u5b9a\u672c\u8f6e\u7535\u89c6\u7aef\u5361\u987f\u4e3b\u8981\u7531\u6574\u673a CPU \u8d44\u6e90\u7ade\u4e89\u5bfc\u81f4\uff0c\u95ee\u9898\u65b9\u5411\u504f\u7cfb\u7edf/\u56fa\u4ef6\u4fa7\uff0c\u4e0d\u662f\u64ad\u653e\u5668\u5355\u8fdb\u7a0b\u672c\u8eab\u6027\u80fd\u4e0d\u8db3\u3002",
            "evidence_level": "confirmed",
            "owner": "\u7cfb\u7edf/\u56fa\u4ef6\u4fa7",
            "suspect_process": "\u5f85\u7ed3\u5408 CPU \u8bc1\u636e\u76ee\u5f55\u786e\u8ba4",
            "actions": [
                "\u4f18\u5148\u67e5\u770b\u5361\u987f\u65f6\u523b\u7684 CPU Top \u8fdb\u7a0b\u548c\u5b9e\u4f8b\u6570\u53d8\u5316",
                "\u5bf9\u6bd4\u6574\u673a CPU\u3001\u7535\u89c6\u7aef\u5361\u987f\u4e8b\u4ef6\u3001\u89e3\u7801\u8f93\u51fa\u505c\u987f\u662f\u5426\u53d1\u751f\u5728\u540c\u4e00\u65f6\u95f4\u7a97",
            ],
        }
    if confirmed_decoder_stuck > 0:
        return {
            "title": "\u89e3\u7801\u8f93\u51fa\u505c\u987f",
            "conclusion": f"\u5df2\u786e\u8ba4\u5b58\u5728\u89e3\u7801\u8f93\u51fa\u505c\u987f\uff0c\u5efa\u8bae\u4f18\u5148\u6392\u67e5\u89e3\u7801\u94fe\u8def\u4e0e {decoder_name} \u7684\u5f02\u5e38\u72b6\u6001\u3002",
            "evidence_level": "confirmed",
            "owner": "\u5e94\u7528/\u7cfb\u7edf\u8054\u5408\u6392\u67e5",
            "suspect_process": decoder_name,
            "actions": [
                "\u67e5\u770b\u505c\u987f\u6837\u672c\u65f6\u95f4\u70b9\u524d\u540e\u7684\u89e3\u7801\u5668\u65e5\u5fd7",
                "\u786e\u8ba4 work_count\u3001video_fps\u3001decode_drop_ratio \u662f\u5426\u540c\u6b65\u6076\u5316",
            ],
        }
    if tv_stall_count > 0:
        return {
            "title": "\u7535\u89c6\u7aef\u5df2\u786e\u8ba4\u5361\u987f",
            "conclusion": "\u5df2\u786e\u8ba4\u7535\u89c6\u7aef\u53d1\u751f\u5361\u987f\uff0c\u4f46\u5f53\u524d\u8bc1\u636e\u8fd8\u4e0d\u8db3\u4ee5\u552f\u4e00\u9501\u5b9a\u6839\u56e0\uff0c\u9700\u8981\u7ed3\u5408 CPU \u8bc1\u636e\u4e0e\u89e3\u7801\u94fe\u8def\u7ee7\u7eed\u6392\u67e5\u3002",
            "evidence_level": "strong",
            "owner": "\u5e94\u7528/\u7cfb\u7edf\u8054\u5408\u6392\u67e5",
            "suspect_process": "\u5f85\u786e\u8ba4",
            "actions": [
                "\u4f18\u5148\u67e5\u770b\u5361\u987f\u4e8b\u4ef6\u76ee\u5f55\u4e2d\u7684\u622a\u56fe\u3001CPU Top \u548c\u65e5\u5fd7\u65f6\u95f4\u7ebf",
            ],
        }
    if decoder_risk > 0 and not surface_locked:
        return {
            "title": "\u8bc1\u636e\u4e0d\u8db3\uff0c\u5b58\u5728\u89e3\u7801\u98ce\u9669\u6837\u672c",
            "conclusion": "\u68c0\u6d4b\u5230\u8f83\u591a\u89e3\u7801\u98ce\u9669\u6837\u672c\uff0c\u4f46\u7531\u4e8e\u672a\u9501\u5b9a\u7535\u89c6\u7aef\u89c6\u9891Surface\uff0c\u5f53\u524d\u53ea\u80fd\u4f5c\u4e3a\u98ce\u9669\u63d0\u793a\uff0c\u4e0d\u80fd\u76f4\u63a5\u5224\u5b9a\u4e3a\u8089\u773c\u5361\u987f\u3002",
            "evidence_level": "risk",
            "owner": "\u5f85\u786e\u8ba4",
            "suspect_process": decoder_name,
            "actions": [
                "\u4f18\u5148\u89e3\u51b3\u7535\u89c6\u7aef Surface \u9501\u5b9a\u4e0e FPS \u91c7\u96c6\u95ee\u9898",
                "\u590d\u6d4b\u65f6\u91cd\u70b9\u770b tv_stall_count\u3001video_fps\u3001decode_drop_ratio \u662f\u5426\u540c\u6b65\u5f02\u5e38",
            ],
        }
    if not surface_locked:
        return {
            "title": "\u8bc1\u636e\u8986\u76d6\u4e0d\u8db3",
            "conclusion": "\u5f53\u524d\u672a\u9501\u5b9a\u7535\u89c6\u7aef\u89c6\u9891Surface\uff0c\u62a5\u544a\u53ef\u7528\u4e8e\u98ce\u9669\u6392\u67e5\uff0c\u4f46\u4e0d\u80fd\u4f5c\u4e3a\u7535\u89c6\u7aef\u6d41\u7545\u5ea6\u7684\u6700\u7ec8\u7ed3\u8bba\u3002",
            "evidence_level": "inconclusive",
            "owner": "\u5f85\u786e\u8ba4",
            "suspect_process": "\u65e0",
            "actions": [
                "\u4f18\u5148\u4fee\u590d Surface \u8bc6\u522b\u4e0e FPS \u91c7\u96c6\uff0c\u8865\u9f50\u7535\u89c6\u7aef\u76f4\u63a5\u8bc1\u636e",
            ],
        }
    return {
        "title": "\u6682\u65e0\u660e\u786e\u7ed3\u8bba",
        "conclusion": "\u5f53\u524d\u672a\u8bc6\u522b\u5230\u8db3\u591f\u5f3a\u7684\u5f02\u5e38\u8bc1\u636e\u3002",
        "evidence_level": "unknown",
        "owner": "\u5f85\u786e\u8ba4",
        "suspect_process": "\u65e0",
        "actions": [],
    }


def build_executive_statement(stats: Dict, diagnosis: Dict) -> str:
    tv_stall_count = int(stats.get("tv_stall_count", 0) or 0)
    avg_system_cpu = float(stats.get("avg_system_cpu_percent", 0) or 0.0)
    avg_player_cpu = float(stats.get("avg_player_cpu_percent", 0) or 0.0)
    if tv_stall_count > 0 and avg_system_cpu >= 85.0 and avg_player_cpu <= 15.0:
        return "\u672c\u8f6e\u95ee\u9898\u5df2\u7ecf\u660e\u786e\u4e3a\uff1a\u7535\u89c6\u7aef\u786e\u5b9e\u53d1\u751f\u5361\u987f\uff0c\u4e3b\u56e0\u662f\u6574\u673a CPU \u957f\u65f6\u95f4\u88ab\u9ad8\u8d1f\u8f7d\u8fdb\u7a0b\u7ade\u4e89\uff0c\u95ee\u9898\u65b9\u5411\u504f\u7cfb\u7edf/\u56fa\u4ef6\u4fa7\uff0c\u4e0d\u662f\u64ad\u653e\u5668\u5355\u8fdb\u7a0b\u672c\u8eab\u6027\u80fd\u4e0d\u8db3\u3002"
    return str(diagnosis.get("conclusion", "") or "")


def build_clean_text(meta: Dict, summary: Dict, score_result: Dict, perceptual_result: Dict, diagnosis: Dict) -> str:
    duration_str = summary["duration_str"]
    duration_sec = int(meta.get("duration_sec", 0) or 0)
    screen_anomaly = int((score_result.get("counts") or {}).get("screen_anomaly", 0) or 0)
    issue_text = "\u8fd0\u884c\u7a33\u5b9a\uff0c" if screen_anomaly == 0 else f"\u53d1\u751f{screen_anomaly}\u6b21\u5c4f\u5e55\u5f02\u5e38\uff0c"
    blockers = list(score_result.get("release_blockers", []) or [])
    blocker_joined = "\uff1b".join(blockers)
    blocker_text = f"\u963b\u65ad\u9879\uff1a{blocker_joined}\u3002" if blockers else ""
    if score_result.get("ready_to_release"):
        release_status = ZH["recommend_release"]
    elif score_result.get("assessment") == "inconclusive":
        release_status = ZH["recommend_inconclusive"]
    else:
        release_status = ZH["recommend_reject"]
    executive_statement = build_executive_statement(summary, diagnosis)
    decoder_summary = summary.get("decoder_stuck_summary", {}) or {}
    latency_probe = summary.get("tv_latency_probe", {}) or {}
    unknown_text = "\u672a\u83b7\u53d6"
    no_text = "\u65e0"
    diagnosis_title = str(diagnosis.get("title") or "\u6682\u65e0\u660e\u786e\u7ed3\u8bba")
    diagnosis_conclusion = str(diagnosis.get("conclusion") or "\u6682\u65e0\u660e\u786e\u7ed3\u8bba")
    diagnosis_owner = str(diagnosis.get("owner") or "\u5f85\u786e\u8ba4")
    diagnosis_suspect = str(diagnosis.get("suspect_process") or no_text)
    display_status = "\u5df2\u9a8c\u8bc1" if summary.get("tv_display_verified") else "\u672a\u9a8c\u8bc1"
    surface_status = "\u5df2\u9501\u5b9a" if summary.get("tv_surface_locked") else "\u672a\u9501\u5b9a"
    decoder_name = str(decoder_summary.get("decoder_name") or "\u672a\u8bc6\u522b")
    device_id = meta.get("device_id") or "N/A"
    device_ip = meta.get("device_ip") or unknown_text
    firmware = meta.get("firmware_incremental") or unknown_text
    package_name = meta.get("package_name") or "N/A"
    perceptual_level = perceptual_result.get("level") or "unknown"
    perceptual_recommendation = perceptual_result.get("recommendation") or no_text
    unavailable_reason = summary.get("video_fps_unavailable_reason") or ZH["surface_unavailable"]
    summary_time = meta.get("end_time") or datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        f"=== Android {ZH['title']} (V2\u6807\u51c6) ===",
        f"\u751f\u6210\u65f6\u95f4: {summary_time}",
        f"\u6d4b\u8bd5\u5305\u540d: {package_name}",
        f"\u6d4b\u8bd5\u8bbe\u5907: {device_id}",
        f"\u673a\u9876\u76d2 IP: {device_ip}",
        f"\u56fa\u4ef6\u7248\u672c: {firmware}",
        "-" * 30,
        "\u3010\u6d4b\u8bd5\u7ed3\u8bba (Decision)\u3011",
        (
            f"\u672c\u6b21\u538b\u6d4b\u6301\u7eed {duration_str}\uff0c\u5171\u64ad\u653e 0 \u9996\u6b4c\u66f2\u3002"
            f"\u7a33\u5b9a\u6027\u8bc4\u5206 {score_result.get('score', 0)} ({score_result.get('grade', 'N/A')})\u3002"
            f"{diagnosis_title}\u3002"
            f"{issue_text}{blocker_text}{release_status}\u3002"
        ),
    ]
    if executive_statement:
        lines.append(f"\u603b\u7ed3\u6027\u5224\u65ad: {executive_statement}")
    if duration_sec < 3600:
        lines.append("[\u8986\u76d6\u5ea6\u63d0\u793a] \u672c\u8f6e\u4e0d\u8db31\u5c0f\u65f6\uff0c\u53ef\u9a8c\u8bc1\u57fa\u7840\u6d41\u7545\u5ea6\uff1b\u5185\u5b58\u79ef\u7d2f\u3001\u540e\u53f0CPU\u7ade\u4e89\u548c\u70ed\u964d\u9891\u5efa\u8bae\u81f3\u5c11\u8fd0\u884c1\u5c0f\u65f6\u3002")
    lines.extend([
        "-" * 30,
        f"\u7a33\u5b9a\u6027\u8bc4\u5206: {score_result.get('score', 0)} / 100 (\u7b49\u7ea7: {score_result.get('grade', 'N/A')})",
        f"\u7535\u89c6\u7aef\u5361\u987f\u98ce\u9669\u5206: {perceptual_result.get('score', 0)} / 100 (\u7b49\u7ea7: {perceptual_level})",
        f"\u611f\u77e5\u5efa\u8bae: {perceptual_recommendation}",
        f"\u51c6\u5165\u7ed3\u8bba: {release_status}",
    ])
    if score_result.get("deductions"):
        lines.append("\u6263\u5206/\u98ce\u9669\u9879:")
        lines.extend([f"  - {item}" for item in score_result.get("deductions", [])])
    if blockers:
        lines.append("\u4e0a\u7ebf\u963b\u65ad\u9879 / \u8bc1\u636e\u7f3a\u53e3:")
        lines.extend([f"  - {item}" for item in blockers])
    lines.extend([
        "-" * 30,
        "\u3010\u6d4b\u8bd5\u6267\u884c\u7edf\u8ba1\u3011",
        f"1. \u5b9e\u9645\u8fd0\u884c\u65f6\u957f: {duration_str}",
        f"2. \u64ad\u653e\u7edf\u8ba1: {ZH['monitor_only']}",
        f"3. \u64ad\u653e\u6210\u529f\u7387: {ZH['playback_na']}",
        f"4. \u6027\u80fd\u91c7\u6837\u70b9\u6570: {summary.get('duration_samples', 0)}",
        f"5. \u6709\u6548\u91c7\u6837\u8986\u76d6\u7387: {summary.get('valid_samples', 0)}/{summary.get('duration_samples', 0)} ({float(summary.get('valid_sample_ratio', 0) or 0) * 100:.1f}%)",
        "-" * 30,
        "\u3010\u9519\u8bef\u6c47\u603b (Errors)\u3011",
        f"1. \u5d29\u6e83 (Crash/Exception): {int((score_result.get('counts') or {}).get('crash', 0) or 0)} \u6b21",
        "2. \u65e0\u54cd\u5e94 (ANR): 0 \u6b21",
        f"3. \u8fdb\u7a0b\u5f02\u5e38\u91cd\u542f: {summary.get('restart_count', 0)} \u6b21",
        f"4. \u76ee\u6807\u8fdb\u7a0b\u4e22\u5931: {summary.get('pid_loss_count', 0)} \u6b21",
        "-" * 30,
        "\u3010\u6838\u5fc3\u7a33\u5b9a\u6027\u6307\u6807\u3011",
        f"1. \u5cf0\u503c\u5185\u5b58(PSS): {summary.get('max_pss_mb', 0)} MB",
        f"2. \u5e73\u5747\u5185\u5b58(PSS): {summary.get('avg_pss_mb', 0)} MB",
        f"3. \u64ad\u653e\u5668\u5e73\u5747CPU / \u6574\u673a\u5e73\u5747CPU / \u6574\u673a\u5cf0\u503cCPU: {summary.get('avg_player_cpu_percent', 0)}% / {summary.get('avg_system_cpu_percent', 0)}% / {summary.get('max_system_cpu_percent', 0)}%",
        f"4. \u7535\u89c6\u7aef\u5361\u987f / \u51bb\u7ed3: {summary.get('tv_stall_count', 0)} / {summary.get('tv_freeze_count', 0)}",
        f"5. \u89e3\u7801\u505c\u987f\u603b\u6837\u672c / \u786e\u8ba4\u6837\u672c / \u98ce\u9669\u6837\u672c: {summary.get('decoder_stuck_count', 0)} / {summary.get('confirmed_decoder_stuck_count', 0)} / {summary.get('decoder_stuck_risk_count', 0)}",
        "-" * 30,
        "\u3010\u6839\u56e0\u5206\u6790 (V3.0)\u3011",
        f"1. \u603b\u7ed3: {diagnosis_title}",
        f"2. \u7ed3\u8bba: {diagnosis_conclusion}",
        f"3. \u8bc1\u636e\u7b49\u7ea7 / \u8d23\u4efb\u65b9\u5411 / \u4f18\u5148\u5bf9\u8c61: {diagnosis.get('evidence_level', 'unknown')} / {diagnosis_owner} / {diagnosis_suspect}",
    ])
    actions = list(diagnosis.get("actions", []) or [])
    if actions:
        lines.append("4. \u5efa\u8bae\u52a8\u4f5c:")
        lines.extend([f"   - {item}" for item in actions])
    lines.extend([
        "-" * 30,
        "\u3010\u7535\u89c6\u7aef\u6d41\u7545\u5ea6\u8bc1\u636e\u3011",
        f"1. Display: {summary.get('tv_display_id', 'N/A')} | \u9a8c\u8bc1\u72b6\u6001: {display_status} ({summary.get('tv_display_verification_reason', 'unknown')})",
        f"2. Surface \u9501\u5b9a: {surface_status}",
        f"3. \u89c6\u9891 FPS: {summary.get('avg_video_fps', 0)} | \u6837\u672c {summary.get('video_fps_samples', 0)} | \u6765\u6e90 {summary.get('video_fps_source_counts', {})}",
        f"4. \u89e3\u7801\u4f30\u7b97\u4e22\u5e27: {summary.get('decode_drop_estimate_total', 0)} / {summary.get('decode_expected_frames_estimate', 0)} (\u6bd4\u4f8b {float(summary.get('decode_drop_ratio', 0) or 0) * 100:.2f}%)",
        f"5. \u505c\u987f\u6837\u672c\u8be6\u60c5: \u6700\u5927\u6301\u7eed {decoder_summary.get('max_duration_sec', 0)}s | Decoder {decoder_name} | \u6837\u672c\u65f6\u95f4 {decoder_summary.get('sample_timestamp', 'N/A')}",
    ])
    if (not summary.get("tv_surface_locked")) or float(summary.get("avg_video_fps", 0) or 0.0) <= 0:
        lines.append("6. FPS \u91c7\u96c6\u8bca\u65ad:")
        lines.append(f"   - \u4e0d\u53ef\u7528\u539f\u56e0: {unavailable_reason}")
        lines.append(f"   - \u5019\u9009 Surface: {summary.get('tv_surface_candidates', [])}")
        lines.append(
            f"   - latency \u63a2\u6d4b: mode={latency_probe.get('latency_mode', 'unknown')} | reason={latency_probe.get('probe_reason', 'unknown')} | frame_count={latency_probe.get('latency_frame_count', 0)}"
        )
        excerpt = str(latency_probe.get("latency_output_excerpt", "") or "").strip()
        if excerpt:
            lines.append("   - latency \u8f93\u51fa\u6458\u5f55:")
            for row in excerpt.splitlines()[:6]:
                lines.append(f"     {row}")
    prefix = meta.get("start_time") or "unknown"
    lines.extend([
        "-" * 30,
        "\u3010\u539f\u59cb\u6570\u636e\u3011",
        f"CSV \u8be6\u7ec6\u62a5\u544a: report_{prefix}.csv",
        f"HTML \u62a5\u544a: report_{prefix}.html",
        f"JSON \u6458\u8981: summary_{prefix}.json",
    ])
    return "\n".join(lines) + "\n"


def repair_file(json_path: Path) -> None:
    data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    meta = data.get("meta") or {}
    raw_decision = data.get("decision") or {}
    stats = data.get("stats") or {}
    metrics = data.get("metrics") or {}

    perceptual_score = int((((metrics.get("perceptual_stutter") or {}).get("score")) or 0))
    perceptual_result = perceptual_from_score(perceptual_score)
    score_result = {
        "score": int(raw_decision.get("score", 0) or 0),
        "grade": raw_decision.get("grade", "N/A"),
        "ready_to_release": bool(raw_decision.get("ready_to_release", False)),
        "assessment": str(raw_decision.get("assessment", "") or ""),
        "counts": raw_decision.get("counts") or {},
        "deductions": clean_deductions(stats, raw_decision),
        "release_blockers": clean_blockers(stats),
    }
    diagnosis = infer_diagnosis(stats)

    summary = dict(stats)
    summary["duration_str"] = format_duration(int(meta.get("duration_sec", 0) or 0))
    summary["package_name"] = meta.get("package_name")
    summary["device_id"] = meta.get("device_id")
    summary["device_ip"] = meta.get("device_ip")
    summary["firmware_incremental"] = meta.get("firmware_incremental")
    summary["score_result"] = score_result
    summary["error_stats"] = {
        "crash_count": int((score_result.get("counts") or {}).get("crash", 0) or 0),
        "anr_count": 0,
    }
    summary["test_mode"] = "monitor_only"
    summary["executive_statement"] = build_executive_statement(summary, diagnosis)
    if "video_fps_samples" not in summary:
        source_counts = summary.get("video_fps_source_counts") or {}
        total = 0
        for value in source_counts.values():
            if isinstance(value, (int, float)):
                total += int(value)
        summary["video_fps_samples"] = total
    if "avg_video_fps" not in summary:
        summary["avg_video_fps"] = float((summary.get("decoder_stuck_summary") or {}).get("video_fps", 0) or 0.0)
    summary.setdefault("tv_surface_candidates", [])
    summary.setdefault("tv_latency_probe", {})

    prefix = meta.get("start_time") or json_path.stem.replace("summary_", "")
    report_dir = json_path.parent

    txt_path = report_dir / f"summary_{prefix}.txt"
    txt_path.write_text(
        build_clean_text(meta, summary, score_result, perceptual_result, diagnosis),
        encoding="utf-8-sig",
    )

    generator = ReportGenerator(str(report_dir))
    generator.generate_report(summary, [], prefix, root_cause_data={"final_diagnosis": diagnosis})

    repaired = {
        "meta": meta,
        "decision": score_result,
        "stats": stats,
        "metrics": {"perceptual_stutter": perceptual_result},
        "repair": {
            "repaired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_json": json_path.name,
        },
    }
    json_path.write_text(json.dumps(repaired, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"repaired: {prefix}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair old player stress report files.")
    parser.add_argument("--report-dir", default=str(ROOT / "data" / "reports" / "player_stress"))
    parser.add_argument("--prefix", help="Only repair one report prefix, e.g. 20260617_180357")
    parser.add_argument("--latest", action="store_true", help="Repair latest report only")
    parser.add_argument("--all", action="store_true", help="Repair all summary json files")
    args = parser.parse_args()

    report_dir = Path(args.report_dir)
    json_files = sorted(report_dir.glob("summary_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if args.prefix:
        json_files = [report_dir / f"summary_{args.prefix}.json"]
    elif args.latest and json_files:
        json_files = [json_files[0]]
    elif not args.all and json_files:
        json_files = [json_files[0]]

    for path in json_files:
        if path.exists():
            repair_file(path)


if __name__ == "__main__":
    main()
