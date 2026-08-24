#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


KEYWORD_GROUPS = {
    "mpp_decoder": [
        r"\bmpp\b",
        r"\brk_mpp\b",
        r"decoder",
        r"dequeue output timeout",
        r"get buffer timeout",
        r"output first frame",
    ],
    "audio_pipeline": [
        r"AudioFlinger",
        r"AudioTrack",
        r"audioserver",
        r"underrun",
        r"buffer starvation",
        r"audio decoder EOS",
    ],
    "binder_or_service": [
        r"binder",
        r"IPCThreadState",
        r"mediaserver",
        r"media\.codec",
        r"media\.extractor",
        r"service",
    ],
    "tv_osd": [
        r"tvservice",
        r"DISPLAY_TV",
        r"MARQUEE",
        r"WX_QRCODE",
    ],
    "error_or_timeout": [
        r"timeout",
        r"error",
        r"failed",
        r"fatal",
        r"exception",
        r"ANR",
    ],
}

FOCUS_PROCESSES = [
    "mediaserver",
    "media.codec",
    "media.extractor",
    "surfaceflinger",
    "android.hardware.graphics.composer3-service.rockchip",
    "composer3-service",
    "com.thunder.ktv",
    "com.thunder.ktv:media",
    "com.thunder.ktv:tvservice",
    "audioserver",
]


@dataclass
class ProcessHit:
    name: str
    cpu_percent: float
    source: str
    timestamp: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze a TV stall evidence directory.")
    parser.add_argument(
        "event_dir",
        nargs="?",
        default="latest",
        help="Event evidence directory path, or 'latest' to auto-pick the newest event.",
    )
    parser.add_argument(
        "--root",
        default=r"D:\trae-code\trae_platform\data\reports\player_stress\tv_stall_events",
        help="Root directory containing TV stall evidence folders.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write auto_analysis.txt and auto_analysis.json into the event directory.",
    )
    return parser.parse_args()


def choose_event_dir(root: Path, value: str) -> Path:
    if value != "latest":
        return Path(value)
    candidates = [p for p in root.iterdir() if p.is_dir() and re.match(r"^\d{8}_\d{6}_\d+$", p.name)]
    if not candidates:
        raise FileNotFoundError(f"No event directories found under {root}")
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8", errors="ignore"))


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def normalize_process_name(name: str) -> str:
    name = (name or "").strip()
    if name.startswith("media.") or name == "mediaserver":
        return "mediaserver"
    if "composer3-service" in name:
        return "composer3-service"
    if name == "surfaceflinger":
        return "surfaceflinger"
    if name.startswith("com.thunder.ktv:tvservice"):
        return "tvservice"
    if name.startswith("com.thunder.ktv"):
        return "com.thunder.ktv"
    return name


def extract_top_hits(rows: Iterable[Dict], source: str) -> List[ProcessHit]:
    hits: List[ProcessHit] = []
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ts = row.get("time", "")
        for process in row.get("top_processes", []) or []:
            name = process.get("name", "")
            if any(token in name for token in ("grep", "logcat", "top -b", "sh -c")):
                continue
            hits.append(
                ProcessHit(
                    name=name,
                    cpu_percent=float(process.get("cpu_percent", 0.0) or 0.0),
                    source=source,
                    timestamp=ts,
                )
            )
    return hits


def collect_keyword_lines(text: str, max_per_group: int = 12) -> Dict[str, List[str]]:
    matches: Dict[str, List[str]] = {}
    lines = text.splitlines()
    for group, patterns in KEYWORD_GROUPS.items():
        compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        bucket: List[str] = []
        for line in lines:
            if any(pattern.search(line) for pattern in compiled):
                bucket.append(line.strip())
            if len(bucket) >= max_per_group:
                break
        matches[group] = bucket
    return matches


def summarize_decoder_window(decoder_text: str) -> Dict[str, object]:
    lines = [line.strip() for line in decoder_text.splitlines() if line.strip()]
    codec_names: Counter[str] = Counter()
    eos_count = 0
    timeout_count = 0
    first_frame_count = 0
    for line in lines:
        codec_match = re.search(r"codec\(name=([^/\s,]+)", line)
        if codec_match:
            codec_names[codec_match.group(1)] += 1
        if "audio decoder EOS" in line:
            eos_count += 1
        if "timeout" in line.lower():
            timeout_count += 1
        if "output first frame" in line.lower():
            first_frame_count += 1
    return {
        "codec_names": dict(codec_names),
        "audio_decoder_eos_count": eos_count,
        "timeout_count": timeout_count,
        "first_frame_count": first_frame_count,
        "line_count": len(lines),
    }


def summarize_process_hits(hits: List[ProcessHit]) -> Dict[str, Dict[str, object]]:
    summary: Dict[str, Dict[str, object]] = defaultdict(lambda: {"count": 0, "peak_cpu": 0.0, "examples": []})
    for hit in hits:
        key = normalize_process_name(hit.name)
        item = summary[key]
        item["count"] += 1
        item["peak_cpu"] = max(float(item["peak_cpu"]), hit.cpu_percent)
        examples: List[str] = item["examples"]
        if len(examples) < 4:
            examples.append(f"{hit.timestamp} | {hit.source} | {hit.name} | CPU {hit.cpu_percent:.1f}%")
    return dict(summary)


def infer_diagnosis(
    event: Dict,
    proc_summary: Dict[str, Dict[str, object]],
    keyword_lines: Dict[str, List[str]],
    decoder_summary: Dict[str, object],
) -> Tuple[str, str, List[str]]:
    max_gap = float(event.get("max_frame_gap_ms", 0.0) or 0.0)
    mediaserver_peak = float(proc_summary.get("mediaserver", {}).get("peak_cpu", 0.0) or 0.0)
    surface_peak = float(proc_summary.get("surfaceflinger", {}).get("peak_cpu", 0.0) or 0.0)
    composer_peak = float(proc_summary.get("composer3-service", {}).get("peak_cpu", 0.0) or 0.0)
    eos_count = int(decoder_summary.get("audio_decoder_eos_count", 0) or 0)
    timeout_count = int(decoder_summary.get("timeout_count", 0) or 0)
    hard_errors = len(keyword_lines.get("error_or_timeout", []))

    findings: List[str] = []
    if mediaserver_peak >= 80.0:
        findings.append(f"mediaserver 在窗口内峰值 CPU {mediaserver_peak:.1f}%")
    if surface_peak >= 30.0:
        findings.append(f"surfaceflinger 峰值 CPU {surface_peak:.1f}%")
    if composer_peak >= 20.0:
        findings.append(f"composer3-service 峰值 CPU {composer_peak:.1f}%")
    if eos_count > 0:
        findings.append(f"decoder window 中出现 {eos_count} 次 audio decoder EOS")
    if timeout_count > 0 or hard_errors > 0:
        findings.append("日志中出现 timeout/error 关键字")

    if mediaserver_peak >= 80.0 and max_gap >= 1500.0:
        title = "高概率为系统媒体服务 CPU 竞争"
        detail = (
            "电视端风险窗口与 mediaserver 高 CPU 同步出现，优先怀疑系统媒体服务/解码链路被抢占；"
            "当前还需要结合 mpp timeout、underrun 或 decode drop 证据做最后闭环。"
        )
    elif (surface_peak >= 30.0 or composer_peak >= 20.0) and max_gap >= 1000.0:
        title = "高概率为显示合成链路抖动"
        detail = (
            "风险窗口内 surfaceflinger 或 composer3-service 占用偏高，"
            "更像是显示合成链路短时拥堵，而不是播放器 App 自身崩溃。"
        )
    elif eos_count >= 3:
        title = "怀疑音频/解码链路切换抖动"
        detail = (
            "decoder window 中重复出现 audio decoder EOS，"
            "说明窗口内存在音频链路切换或结束重建，建议重点复核切歌/转场阶段。"
        )
    else:
        title = "证据不足，暂列为风险级抖动"
        detail = (
            "当前能确认存在较大的帧间隔异常，但 CPU 共振和硬解日志证据还不够硬，"
            "建议继续结合 top 快照和 time_window_logcat 做二次复核。"
        )

    return title, detail, findings


def build_text_report(
    event_dir: Path,
    event: Dict,
    proc_summary: Dict[str, Dict[str, object]],
    decoder_summary: Dict[str, object],
    keyword_lines: Dict[str, List[str]],
    diagnosis_title: str,
    diagnosis_detail: str,
    findings: List[str],
) -> str:
    lines: List[str] = []
    lines.append("=== TV Stall Event Auto Analysis ===")
    lines.append(f"事件目录: {event_dir}")
    lines.append(f"事件ID: {event.get('event_id', event_dir.name)}")
    lines.append(f"事件类型: {event.get('type', 'UNKNOWN')}")
    lines.append(f"开始/结束: {event.get('start_time', '')} -> {event.get('end_time', '')}")
    lines.append(f"持续时长: {event.get('duration_ms', 0)} ms")
    lines.append(f"Display / Surface: Display {event.get('display_id', '?')} | {event.get('surface_name', 'N/A')}")
    lines.append(
        f"最大帧间隔 / 最低FPS: {float(event.get('max_frame_gap_ms', 0.0) or 0.0):.1f} ms / {float(event.get('min_fps', 0.0) or 0.0):.2f}"
    )
    lines.append("")
    lines.append("【自动诊断】")
    lines.append(f"1. 结论: {diagnosis_title}")
    lines.append(f"2. 说明: {diagnosis_detail}")
    if findings:
        lines.append("3. 关键证据:")
        for item in findings:
            lines.append(f"   - {item}")
    else:
        lines.append("3. 关键证据: 暂未提炼出强共振信号")

    lines.append("")
    lines.append("【进程共振摘要】")
    if proc_summary:
        ranked = sorted(proc_summary.items(), key=lambda item: (-float(item[1]["peak_cpu"]), -int(item[1]["count"])))
        for index, (name, data) in enumerate(ranked[:8], start=1):
            lines.append(
                f"{index}. {name} | 命中 {int(data['count'])} 次 | 峰值CPU {float(data['peak_cpu']):.1f}%"
            )
            for example in data["examples"]:
                lines.append(f"   - {example}")
    else:
        lines.append("无进程快照数据")

    lines.append("")
    lines.append("【Decoder / Log 证据】")
    codec_names = decoder_summary.get("codec_names", {})
    lines.append(f"1. Decoder 日志行数: {decoder_summary.get('line_count', 0)}")
    lines.append(f"2. Codec 命中: {codec_names or '无'}")
    lines.append(f"3. audio decoder EOS: {decoder_summary.get('audio_decoder_eos_count', 0)}")
    lines.append(f"4. timeout 关键字: {decoder_summary.get('timeout_count', 0)}")
    for group, title in [
        ("mpp_decoder", "MPP/Decoder"),
        ("audio_pipeline", "Audio"),
        ("binder_or_service", "Binder/Service"),
        ("tv_osd", "TV OSD"),
        ("error_or_timeout", "Error/Timeout"),
    ]:
        matches = keyword_lines.get(group, [])
        if not matches:
            continue
        lines.append(f"5.{group} {title} 命中 {len(matches)} 条:")
        for line in matches[:6]:
            lines.append(f"   - {line}")

    lines.append("")
    lines.append("【研发优先排查】")
    lines.append("1. 先看 top_before.txt / top_after.txt，确认 mediaserver、surfaceflinger、composer3-service 是否同时抬头。")
    lines.append("2. 再看 time_window_logcat.txt，搜索 mpp / underrun / buffer starvation / timeout / AudioFlinger。")
    lines.append("3. 如果窗口内出现 audio decoder EOS 或 mpp output first frame，重点复核切歌/转场是否触发了解码链路重建。")
    lines.append("4. 如果仍无法闭环，再去比对同时间点的 tvservice / OSD 日志，确认是否叠加层刷新放大了抖动。")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    root = Path(args.root)
    event_dir = choose_event_dir(root, args.event_dir)

    event = read_json(event_dir / "event.json", {})
    cpu_before = read_json(event_dir / "cpu_before.json", [])
    cpu_after = read_json(event_dir / "cpu_after.json", [])
    cpu_during = []
    cpu_during_path = event_dir / "cpu_during.jsonl"
    if cpu_during_path.exists():
        with cpu_during_path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    cpu_during.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    decoder_text = read_text(event_dir / "decoder_window.txt")
    logcat_text = read_text(event_dir / "time_window_logcat.txt")

    hits = []
    hits.extend(extract_top_hits(cpu_before, "before"))
    hits.extend(extract_top_hits(cpu_during, "during"))
    hits.extend(extract_top_hits(cpu_after, "after"))

    proc_summary = summarize_process_hits(hits)
    keyword_lines = collect_keyword_lines("\n".join([decoder_text, logcat_text]))
    decoder_summary = summarize_decoder_window(decoder_text)
    diagnosis_title, diagnosis_detail, findings = infer_diagnosis(
        event, proc_summary, keyword_lines, decoder_summary
    )

    text_report = build_text_report(
        event_dir,
        event,
        proc_summary,
        decoder_summary,
        keyword_lines,
        diagnosis_title,
        diagnosis_detail,
        findings,
    )
    print(text_report)

    if args.write:
        (event_dir / "auto_analysis.txt").write_text(text_report, encoding="utf-8")
        (event_dir / "auto_analysis.json").write_text(
            json.dumps(
                {
                    "event_dir": str(event_dir),
                    "diagnosis_title": diagnosis_title,
                    "diagnosis_detail": diagnosis_detail,
                    "key_findings": findings,
                    "process_summary": proc_summary,
                    "decoder_summary": decoder_summary,
                    "keyword_matches": keyword_lines,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
