import math
import re
from typing import Dict, List, Tuple, Optional


class RootCauseAnalyzer:
    def __init__(self, package_name: str = ""):
        self.package_name = package_name or ""
        self.main_package_name = (self.package_name.split(":")[0] if self.package_name else "")
        self.baseline_snapshots: List[Dict] = []
        self.stutter_events: List[Dict] = []
        self.cause_candidates: List[Dict] = []

    def record_baseline(self, snapshot: Dict) -> None:
        if not snapshot:
            return
        if snapshot.get("ignore_video_metrics", False):
            return
        if snapshot.get("decoder_stuck", False):
            return
        if snapshot.get("is_perceptual_jank", False):
            return
        if float(snapshot.get("gfx_jank_percent", 0) or 0) > 15.0:
            return
        if float(snapshot.get("decode_drop_ratio", 0) or 0) > 0.1:
            return

        self.baseline_snapshots.append(snapshot)
        if len(self.baseline_snapshots) > 20:
            self.baseline_snapshots = self.baseline_snapshots[-20:]

    def record_stutter_event(self, stutter_snapshot: Dict, top_consumers_raw: str) -> Optional[Dict]:
        if not stutter_snapshot:
            return None

        if stutter_snapshot.get("ignore_video_metrics", False):
            return None

        top_processes = self._parse_top_consumers(top_consumers_raw or "")
        event = {
            "timestamp": stutter_snapshot.get("timestamp", ""),
            "player_cpu_percent": float(
                stutter_snapshot.get(
                    "player_cpu_percent",
                    stutter_snapshot.get("cpu_percent", 0),
                ) or 0.0
            ),
            "system_cpu_percent": float(
                stutter_snapshot.get("system_cpu_percent", 0) or 0.0
            ),
            "video_fps": float(stutter_snapshot.get("video_fps", 0) or 0.0),
            "video_fps_source": str(stutter_snapshot.get("video_fps_source", "") or ""),
            "expected_stream_fps": float(stutter_snapshot.get("expected_stream_fps", 0) or 0.0),
            "mpp_active": int(stutter_snapshot.get("mpp_active", 0) or 0),
            "mpp_work_count_delta": int(stutter_snapshot.get("mpp_work_count_delta", 0) or 0),
            "decoder_stuck": bool(stutter_snapshot.get("decoder_stuck", False)),
            "decode_fps_estimate": float(stutter_snapshot.get("decode_fps_estimate", 0) or 0.0),
            "decode_slowdown_detected": bool(
                stutter_snapshot.get("decode_slowdown_detected", False)
            ),
            "decode_drop_ratio": float(stutter_snapshot.get("decode_drop_ratio", 0) or 0.0),
            "gfx_jank_percent": float(stutter_snapshot.get("gfx_jank_percent", 0) or 0.0),
            "pss_mb": float(stutter_snapshot.get("pss_mb", 0) or 0.0),
            "top_processes": top_processes,
            "log_stutter_count": int(stutter_snapshot.get("log_stutter_count", 0) or 0),
            "log_stutter_delta": int(stutter_snapshot.get("log_stutter_delta", 0) or 0),
            "max_temperature_c": float(
                stutter_snapshot.get("max_temperature_c", 0) or 0.0
            ),
            "min_cpu_frequency_ratio": float(
                stutter_snapshot.get("min_cpu_frequency_ratio", 0) or 0.0
            ),
            "thermal_throttling": bool(
                stutter_snapshot.get("thermal_throttling", False)
            ),
        }

        self.stutter_events.append(event)
        candidate = self._classify_event(event)
        if candidate:
            self.cause_candidates.append(candidate)
        return candidate

    def analyze(self) -> List[Dict]:
        if not self.cause_candidates and self.stutter_events:
            for evt in self.stutter_events:
                candidate = self._classify_event(evt)
                if candidate:
                    self.cause_candidates.append(candidate)
        self.cause_candidates.sort(key=lambda x: float(x.get("confidence", 0) or 0), reverse=True)
        return list(self.cause_candidates)

    def get_summary(self) -> Dict:
        self.analyze()
        breakdown = {
            "CPU_CONTENTION": 0,
            "DECODER_STUCK": 0,
            "MEMORY_PRESSURE": 0,
            "THERMAL_THROTTLING": 0,
            "LOW_FPS_DEGRADATION": 0,
            "AV_SYNC_ISSUE": 0,
            "UNKNOWN": 0,
        }
        suspect_counts: Dict[str, int] = {}
        for c in self.cause_candidates:
            t = str(c.get("root_cause_type", "UNKNOWN") or "UNKNOWN")
            breakdown[t] = breakdown.get(t, 0) + 1
            sp = str(c.get("suspect_process", "") or "")
            if sp:
                suspect_counts[sp] = suspect_counts.get(sp, 0) + 1

        top_suspects = sorted(suspect_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        most_confident = self.cause_candidates[0] if self.cause_candidates else None
        process_risk_stats: Dict[str, Dict] = {}
        for cause in self.cause_candidates:
            if str(cause.get("root_cause_type", "") or "") != "CPU_CONTENTION":
                continue
            process_name = str(cause.get("suspect_process", "") or "")
            if not process_name:
                continue
            evidence = cause.get("evidence") or {}
            current = process_risk_stats.setdefault(process_name, {
                "process": process_name,
                "event_count": 0,
                "max_instance_count": 1,
                "peak_cpu_percent": 0.0,
                "cpu_total": 0.0,
                "max_system_cpu_percent": 0.0,
                "process_proliferation": False,
            })
            cpu_percent = float(evidence.get("stutter_cpu", 0) or 0.0)
            current["event_count"] += 1
            current["max_instance_count"] = max(
                int(current["max_instance_count"]),
                int(evidence.get("instance_count", 1) or 1),
            )
            current["peak_cpu_percent"] = max(
                float(current["peak_cpu_percent"]),
                cpu_percent,
            )
            current["cpu_total"] += cpu_percent
            current["max_system_cpu_percent"] = max(
                float(current["max_system_cpu_percent"]),
                float(evidence.get("system_cpu_percent", 0) or 0.0),
            )
            current["process_proliferation"] = bool(
                current["process_proliferation"]
                or evidence.get("process_proliferation", False)
            )
        process_risk_summary = []
        for item in process_risk_stats.values():
            count = int(item.get("event_count", 0) or 0)
            process_risk_summary.append({
                "process": item["process"],
                "event_count": count,
                "max_instance_count": int(item["max_instance_count"]),
                "avg_cpu_percent": round(
                    float(item["cpu_total"]) / count if count else 0.0,
                    2,
                ),
                "peak_cpu_percent": round(float(item["peak_cpu_percent"]), 2),
                "max_system_cpu_percent": round(
                    float(item["max_system_cpu_percent"]),
                    2,
                ),
                "process_proliferation": bool(
                    item["process_proliferation"]
                ),
            })
        process_risk_summary.sort(
            key=lambda item: (
                bool(item.get("process_proliferation", False)),
                int(item.get("max_instance_count", 1) or 1),
                int(item.get("event_count", 0) or 0),
                float(item.get("peak_cpu_percent", 0) or 0.0),
            ),
            reverse=True,
        )
        confirmed_playback_causes = 0
        resource_risk_events = 0
        log_signal_events = 0
        identified_causes = 0
        for cause in self.cause_candidates:
            cause_type = str(cause.get("root_cause_type", "") or "")
            evidence = cause.get("evidence") or {}
            if cause_type == "UNKNOWN":
                continue
            identified_causes += 1
            if isinstance(evidence, dict) and evidence.get("resource_only", False):
                resource_risk_events += 1
            elif isinstance(evidence, dict) and evidence.get("signal_only", False):
                log_signal_events += 1
            else:
                confirmed_playback_causes += 1
        return {
            "total_stutter_events": int(len(self.stutter_events)),
            "identified_causes": int(identified_causes),
            "confirmed_playback_causes": int(confirmed_playback_causes),
            "resource_risk_events": int(resource_risk_events),
            "log_signal_events": int(log_signal_events),
            "cause_breakdown": breakdown,
            "top_suspect_processes": top_suspects,
            "process_risk_summary": process_risk_summary[:10],
            "most_confident_cause": most_confident,
            "all_causes": list(self.cause_candidates),
        }

    def _parse_top_consumers(self, raw_str: str) -> List[Tuple[str, float]]:
        if not raw_str:
            return []
        s = raw_str.strip()
        if not s or s.lower().startswith("error"):
            return []
        parts = [p.strip() for p in s.split("|") if p.strip()]
        out: List[Tuple[str, float]] = []
        for p in parts:
            m = re.match(r"^(.*)\(([\d\.]+)%\)$", p)
            if not m:
                continue
            name = (m.group(1) or "").strip()
            try:
                cpu = float(m.group(2))
            except (TypeError, ValueError):
                continue
            if not name:
                continue
            out.append((name, cpu))
        return out

    def _calculate_baseline(self) -> Dict:
        snaps = self.baseline_snapshots or []
        cpu_list: List[float] = []
        system_cpu_list: List[float] = []
        pss_list: List[float] = []
        fps_list: List[float] = []
        proc_cpu_totals: Dict[str, float] = {}
        proc_cpu_counts: Dict[str, int] = {}

        for s in snaps:
            cpu_list.append(float(s.get("cpu_percent", 0) or 0.0))
            system_cpu = float(s.get("system_cpu_percent", 0) or 0.0)
            if system_cpu > 0:
                system_cpu_list.append(system_cpu)
            pss_list.append(float(s.get("pss_mb", 0) or 0.0))
            fps = float(s.get("video_fps", 0) or 0.0)
            if fps > 0:
                fps_list.append(fps)

            top_raw = str(s.get("top_consumers", "") or "")
            for name, cpu in self._parse_top_consumers(top_raw):
                proc_cpu_totals[name] = proc_cpu_totals.get(name, 0.0) + float(cpu)
                proc_cpu_counts[name] = proc_cpu_counts.get(name, 0) + 1

        def avg(arr: List[float]) -> float:
            return (sum(arr) / len(arr)) if arr else 0.0

        cpu_p95 = 0.0
        if cpu_list:
            sorted_cpu = sorted(cpu_list)
            idx = int(math.ceil(0.95 * len(sorted_cpu))) - 1
            idx = max(0, min(idx, len(sorted_cpu) - 1))
            cpu_p95 = float(sorted_cpu[idx])

        proc_baseline: Dict[str, float] = {}
        for name, total in proc_cpu_totals.items():
            c = proc_cpu_counts.get(name, 0)
            if c > 0:
                proc_baseline[name] = total / c

        return {
            "avg_cpu": avg(cpu_list),
            "avg_system_cpu": avg(system_cpu_list),
            "avg_pss": avg(pss_list),
            "avg_fps": avg(fps_list),
            "cpu_p95": cpu_p95,
            "process_cpu_baseline": proc_baseline,
            "sample_count": int(len(snaps)),
        }

    def _classify_event(self, event: Dict) -> Dict:
        baseline = self._calculate_baseline()
        candidates: List[Dict] = []
        playback_degradation = bool(
            event.get("decoder_stuck", False)
            or event.get("decode_slowdown_detected", False)
            or float(event.get("decode_drop_ratio", 0) or 0.0) >= 0.10
            or int(event.get("log_stutter_delta", 0) or 0) > 0
            or (
                str(event.get("video_fps_source", "") or "").startswith(
                    "surfaceflinger"
                )
                and float(event.get("video_fps", 0) or 0.0) < 20
            )
        )

        if event.get("decoder_stuck") and int(event.get("mpp_active", 0) or 0) > 0:
            candidates.append({
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "DECODER_STUCK",
                "suspect_process": "MPP Hardware Decoder",
                "confidence": 90.0,
                "evidence": {
                    "mpp_active_instances": int(event.get("mpp_active", 0) or 0),
                    "mpp_work_count_delta": int(event.get("mpp_work_count_delta", 0) or 0),
                    "video_fps": float(event.get("video_fps", 0) or 0.0),
                },
                "suggestion": "硬件解码器输出停滞。建议检查码率/分辨率是否超出芯片能力、输入流是否损坏，或抓取MPP驱动日志定位。",
            })

        avg_pss = float(baseline.get("avg_pss", 0) or 0.0)
        cur_pss = float(event.get("pss_mb", 0) or 0.0)
        if avg_pss > 0 and (cur_pss - avg_pss) > 200:
            candidates.append({
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "MEMORY_PRESSURE",
                "suspect_process": self.main_package_name or self.package_name or "APP",
                "confidence": 75.0,
                "evidence": {
                    "current_pss_mb": cur_pss,
                    "baseline_pss_mb": avg_pss,
                    "growth_mb": round(cur_pss - avg_pss, 1),
                },
                "suggestion": "PSS显著高于基线，存在内存压力/泄漏风险。建议检查大对象、纹理、缓存与GC相关日志。",
            })

        max_temperature = float(event.get("max_temperature_c", 0) or 0.0)
        min_frequency_ratio = float(
            event.get("min_cpu_frequency_ratio", 0) or 0.0
        )
        if event.get("thermal_throttling", False):
            candidates.append({
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "THERMAL_THROTTLING",
                "suspect_process": "CPU Thermal Governor",
                "confidence": 98.0 if max_temperature >= 80 else 96.0,
                "evidence": {
                    "max_temperature_c": max_temperature,
                    "min_cpu_frequency_ratio": min_frequency_ratio,
                    "system_cpu_percent": float(
                        event.get("system_cpu_percent", 0) or 0.0
                    ),
                    "decode_fps_estimate": float(
                        event.get("decode_fps_estimate", 0) or 0.0
                    ),
                    "expected_stream_fps": float(
                        event.get("expected_stream_fps", 0) or 0.0
                    ),
                },
                "suggestion": "卡顿时检测到高温或CPU明显限频，优先排查散热、温控策略和持续高负载，避免误判为三方应用单独抢占CPU。",
            })

        system_cpu = float(event.get("system_cpu_percent", 0) or 0.0)
        player_cpu = float(event.get("player_cpu_percent", 0) or 0.0)
        proc_baseline = baseline.get("process_cpu_baseline", {}) or {}
        for proc_name, cpu_pct in (event.get("top_processes") or []):
            name_lower = str(proc_name).lower()
            instance_match = re.search(r"\sx(\d+)$", str(proc_name))
            instance_count = (
                int(instance_match.group(1))
                if instance_match else 1
            )
            if "top" in name_lower:
                continue
            if "screencap" in name_lower or "screen_temp_" in name_lower:
                continue
            if name_lower in ("system_server", "surfaceflinger", "audioserver", "cameraserver"):
                continue
            if self.main_package_name and self.main_package_name in proc_name:
                continue
            if self.package_name and self.package_name in proc_name:
                continue

            base_cpu = float(proc_baseline.get(proc_name, 0) or 0.0)
            if system_cpu <= 0:
                continue
            cpu_contention = bool(
                cpu_pct > 20
                and (cpu_pct - base_cpu) > 15
                and cpu_pct > system_cpu * 0.25
            )
            process_proliferation = bool(
                instance_count >= 10
                and cpu_pct >= 1
                and system_cpu >= 80
            )
            if cpu_contention or process_proliferation:
                confidence = (
                    min(
                        85.0,
                        55.0
                        + min(20.0, max(0, instance_count - 5) * 0.75)
                        + min(10.0, cpu_pct),
                    )
                    if process_proliferation
                    else min(95.0, 60.0 + (cpu_pct - base_cpu) * 1.5)
                )
                candidates.append({
                    "timestamp": event.get("timestamp", ""),
                    "root_cause_type": "CPU_CONTENTION",
                    "suspect_process": proc_name,
                    "confidence": float(confidence),
                    "evidence": {
                        "stutter_cpu": float(cpu_pct),
                        "baseline_cpu": float(base_cpu),
                        "cpu_surge": round(float(cpu_pct - base_cpu), 1),
                        "system_cpu_percent": system_cpu,
                        "instance_count": instance_count,
                        "process_proliferation": process_proliferation,
                        "player_cpu_percent": player_cpu,
                        "baseline_system_cpu_percent": float(
                            baseline.get("avg_system_cpu", 0) or 0.0
                        ),
                        "video_fps_at_moment": float(event.get("video_fps", 0) or 0.0),
                        "resource_only": not playback_degradation,
                    },
                    "suggestion": (
                        f"{proc_name} 在资源高负载时CPU {cpu_pct:.1f}%"
                        f"（基线约{base_cpu:.1f}%）。"
                        + (
                            "已同时检测到播放退化信号，建议排查其与媒体链路的算力竞争。"
                            if playback_degradation
                            else "当前没有电视画面卡顿证据，仅作为资源风险候选。"
                        )
                    ),
                })

        vf = float(event.get("video_fps", 0) or 0.0)
        expected_stream_fps = float(event.get("expected_stream_fps", 0) or 0.0)
        decode_fps = float(event.get("decode_fps_estimate", 0) or 0.0)
        decode_drop_ratio = float(event.get("decode_drop_ratio", 0) or 0.0)
        if vf > 0 and vf < 20 and not event.get("decoder_stuck") and decode_fps > 0:
            if expected_stream_fps <= 0:
                expected_stream_fps = max(vf, decode_fps)
            if expected_stream_fps > 0 and decode_fps >= expected_stream_fps * 0.8 and decode_drop_ratio <= 0.02:
                candidates.append({
                    "timestamp": event.get("timestamp", ""),
                    "root_cause_type": "LOW_FPS_DEGRADATION",
                    "suspect_process": "SurfaceFlinger/Render",
                    "confidence": 65.0,
                    "evidence": {
                        "video_fps": vf,
                        "expected_stream_fps": expected_stream_fps,
                        "decode_fps_estimate": decode_fps,
                        "decode_drop_ratio": decode_drop_ratio,
                    },
                    "suggestion": "解码吞吐基本正常但显示帧率持续偏低，可能是合成/渲染链路压力或UI线程阻塞。建议检查surfaceflinger负载、GPU占用、UI动画/弹层。",
                })

        if int(event.get("log_stutter_delta", 0) or 0) > 0:
            candidates.append({
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "AV_SYNC_ISSUE",
                "suspect_process": self.main_package_name or self.package_name or "Player",
                "confidence": 40.0,
                "evidence": {
                    "log_stutter_delta": int(event.get("log_stutter_delta", 0) or 0),
                    "log_stutter_total": int(event.get("log_stutter_count", 0) or 0),
                    "signal_only": True,
                },
                "suggestion": "检测到卡顿相关日志计数增长。建议结合日志关键字（buffer underrun/av sync）进一步确认是网络/缓冲/时钟同步问题。",
            })

        if not candidates:
            return {
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "UNKNOWN",
                "suspect_process": "",
                "confidence": 20.0,
                "evidence": {
                    "baseline_sample_count": int(baseline.get("sample_count", 0) or 0),
                },
                "suggestion": "数据不足以确定根因。建议延长采样、补充播放器层日志或开启更高频的Top采集进行对比。",
            }

        candidates.sort(key=lambda x: float(x.get("confidence", 0) or 0), reverse=True)
        return candidates[0]
