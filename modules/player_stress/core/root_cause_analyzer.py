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
            "decoder_stuck_confirmed": bool(
                stutter_snapshot.get("decoder_stuck_confirmed", False)
            ),
            "decode_fps_estimate": float(stutter_snapshot.get("decode_fps_estimate", 0) or 0.0),
            "decode_slowdown_detected": bool(
                stutter_snapshot.get("decode_slowdown_detected", False)
            ),
            "decode_drop_ratio": float(stutter_snapshot.get("decode_drop_ratio", 0) or 0.0),
            "gfx_jank_percent": float(stutter_snapshot.get("gfx_jank_percent", 0) or 0.0),
            "tv_stutter_detected": bool(
                stutter_snapshot.get("tv_stutter_detected", False)
            ),
            "pss_mb": float(stutter_snapshot.get("pss_mb", 0) or 0.0),
            "top_processes": top_processes,
            "top_consumers_raw": str(top_consumers_raw or ""),
            "log_stutter_count": int(stutter_snapshot.get("log_stutter_count", 0) or 0),
            "log_stutter_delta": int(stutter_snapshot.get("log_stutter_delta", 0) or 0),
            "log_stutter_events": list(stutter_snapshot.get("log_stutter_events") or []),
            "decoder_log_events": list(stutter_snapshot.get("decoder_log_events") or []),
            "decoder_diagnostics": dict(stutter_snapshot.get("decoder_diagnostics") or {}),
            "audio_active": bool(stutter_snapshot.get("audio_active", False)),
            "system_cpu_pressure": bool(
                stutter_snapshot.get("system_cpu_pressure", False)
            ),
            "max_temperature_c": float(
                stutter_snapshot.get("max_temperature_c", 0) or 0.0
            ),
            "min_cpu_frequency_ratio": float(
                stutter_snapshot.get("min_cpu_frequency_ratio", 0) or 0.0
            ),
            "thermal_throttling": bool(
                stutter_snapshot.get("thermal_throttling", False)
            ),
            "tv_surface_locked": bool(stutter_snapshot.get("tv_surface_name", "")),
        }

        self.stutter_events.append(event)
        candidate = self._classify_event(event)
        if candidate:
            candidate = self._attach_common_event_evidence(candidate, event)
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

    @staticmethod
    def normalize_evidence_level(level: str) -> str:
        value = str(level or "").strip().lower()
        mapping = {
            "confirmed": "confirmed",
            "strong": "strong",
            "weak": "risk",
            "risk": "risk",
            "insufficient": "insufficient",
            "none": "insufficient",
            "unknown": "insufficient",
        }
        return mapping.get(value, "insufficient")

    @classmethod
    def build_evidence_strength(cls, level: str, confidence: float = 0.0) -> Dict:
        normalized = cls.normalize_evidence_level(level)
        labels = {
            "confirmed": "Confirmed",
            "strong": "Strong",
            "risk": "Risk",
            "insufficient": "Insufficient",
        }
        descriptions = {
            "confirmed": "多源证据已闭环，可直接用于定责或阻断发布。",
            "strong": "证据已很强，但还缺一条更直接的播放侧实锤。",
            "risk": "当前只有风险信号，不能当作最终结论。",
            "insufficient": "证据覆盖不足，当前不能一锤定音。",
        }
        return {
            "level": normalized,
            "label": labels.get(normalized, "Insufficient"),
            "description": descriptions.get(normalized, descriptions["insufficient"]),
            "confidence": round(float(confidence or 0.0), 1),
        }

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
        cause_type_stats: Dict[str, Dict] = {}
        for c in self.cause_candidates:
            t = str(c.get("root_cause_type", "UNKNOWN") or "UNKNOWN")
            breakdown[t] = breakdown.get(t, 0) + 1
            sp = str(c.get("suspect_process", "") or "")
            if sp:
                suspect_counts[sp] = suspect_counts.get(sp, 0) + 1
            if t != "UNKNOWN":
                confidence = float(c.get("confidence", 0) or 0.0)
                evidence = c.get("evidence") or {}
                stats = cause_type_stats.setdefault(t, {
                    "count": 0,
                    "confidence_total": 0.0,
                    "max_confidence": 0.0,
                    "best_candidate": None,
                    "playback_confirmed": False,
                })
                stats["count"] += 1
                stats["confidence_total"] += confidence
                if confidence >= float(stats["max_confidence"]):
                    stats["max_confidence"] = confidence
                    stats["best_candidate"] = c
                if isinstance(evidence, dict) and not evidence.get("resource_only", False) and not evidence.get("signal_only", False):
                    stats["playback_confirmed"] = True

        top_suspects = sorted(suspect_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        most_confident = self.cause_candidates[0] if self.cause_candidates else None
        representative_cause = most_confident
        if cause_type_stats:
            representative_cause = max(
                (
                    stats.get("best_candidate")
                    for stats in cause_type_stats.values()
                    if isinstance(stats.get("best_candidate"), dict)
                ),
                key=lambda candidate: (
                    1 if cause_type_stats.get(str(candidate.get("root_cause_type", "") or ""), {}).get("playback_confirmed", False) else 0,
                    int(cause_type_stats.get(str(candidate.get("root_cause_type", "") or ""), {}).get("count", 0) or 0),
                    float(cause_type_stats.get(str(candidate.get("root_cause_type", "") or ""), {}).get("max_confidence", 0.0) or 0.0),
                    float(
                        cause_type_stats.get(str(candidate.get("root_cause_type", "") or ""), {}).get("confidence_total", 0.0) or 0.0
                    ) / max(
                        1,
                        int(cause_type_stats.get(str(candidate.get("root_cause_type", "") or ""), {}).get("count", 0) or 0),
                    ),
                ),
                default=most_confident,
            )
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
            "most_confident_cause": representative_cause,
            "final_diagnosis": self._build_final_diagnosis(
                representative_cause,
                process_risk_summary,
                confirmed_playback_causes,
                resource_risk_events,
                log_signal_events,
            ),
            "all_causes": list(self.cause_candidates),
        }

    def _build_final_diagnosis(
        self,
        most_confident: Optional[Dict],
        process_risk_summary: List[Dict],
        confirmed_playback_causes: int,
        resource_risk_events: int,
        log_signal_events: int,
    ) -> Dict:
        if not isinstance(most_confident, dict) or not most_confident:
            return {
                "title": "暂无法定责",
                "conclusion": "当前证据不足，暂时无法一锤定音定位具体问题。",
                "evidence_level": "insufficient",
                "evidence_strength": self.build_evidence_strength("insufficient", 0.0),
                "owner": "待补证据",
                "actions": [
                    "延长监控时长并补充更多卡顿样本",
                    "同步保留电视端卡顿事件目录与 Top 证据",
                ],
            }

        cause_type = str(most_confident.get("root_cause_type", "") or "UNKNOWN")
        confidence = float(most_confident.get("confidence", 0) or 0.0)
        suspect = str(most_confident.get("suspect_process", "") or "")
        evidence = (most_confident.get("evidence") or {}) if isinstance(most_confident, dict) else {}
        resource_only = bool(evidence.get("resource_only", False))
        signal_only = bool(evidence.get("signal_only", False))
        matching_process_risk = {}
        if suspect:
            for item in process_risk_summary:
                if str(item.get("process", "") or "") == suspect:
                    matching_process_risk = item
                    break
        top_process_risk = matching_process_risk or (process_risk_summary[0] if process_risk_summary else {})
        issue_process = str(suspect or top_process_risk.get("process", ""))
        owner = self._guess_owner(issue_process or suspect, cause_type)

        if cause_type == "CPU_CONTENTION":
            issue_process = issue_process or suspect or "未知进程"
            instance_count = int(top_process_risk.get("max_instance_count", evidence.get("instance_count", 1)) or 1)
            event_count = int(top_process_risk.get("event_count", 0) or 0)
            proliferation = bool(
                top_process_risk.get("process_proliferation", False)
                or evidence.get("process_proliferation", False)
            )
            evidence_level = "confirmed"
            if resource_only and confirmed_playback_causes <= 0:
                evidence_level = "risk"
            if signal_only:
                evidence_level = "risk"
            title = (
                "可判定为 CPU 资源竞争导致电视端卡顿"
                if evidence_level == "confirmed"
                else "检测到 CPU 资源竞争风险，但电视端直证仍不足"
            )
            conclusion = (
                f"可判定本轮电视端卡顿主要由 {issue_process} 引发的 CPU 资源竞争导致。"
                if evidence_level == "confirmed"
                else f"检测到 {issue_process} 存在明显 CPU 资源竞争风险，但当前缺少足够的电视端 Surface/帧时间直证，建议先按高风险问题继续复核。"
            )
            if proliferation:
                conclusion += f" 该进程存在多实例并发迹象，峰值达到 {instance_count} 个实例。"
            elif event_count > 0:
                conclusion += f" 该进程在卡顿样本中重复命中 {event_count} 次。"
            actions = [
                f"优先排查 {issue_process} 的启动/保活/循环拉起逻辑",
                "将卡顿时的整机 CPU、嫌疑进程实例数和电视端事件时间线一起交叉确认",
            ]
            return {
                "title": title,
                "conclusion": conclusion,
                "evidence_level": evidence_level,
                "evidence_strength": self.build_evidence_strength(evidence_level, confidence),
                "owner": owner,
                "actions": actions,
                "suspect_process": issue_process,
                "confidence": round(confidence, 1),
            }

        if cause_type == "DECODER_STUCK":
            confirmed_decoder_stuck = not resource_only and bool(evidence.get("decoder_stuck_confirmed", True))
            return {
                "title": "可判定为硬件解码链路停滞",
                "conclusion": (
                    "卡顿时硬件解码吞吐明显停顿，可优先归因到解码链路异常，而不是单纯 CPU 抢占。"
                    if confirmed_decoder_stuck
                    else "检测到解码链路停顿风险，但当前缺少电视端 Surface 或硬错误日志直证，需结合实机观察复核。"
                ),
                "evidence_level": "confirmed" if confirmed_decoder_stuck else "risk",
                "evidence_strength": self.build_evidence_strength(
                    "confirmed" if confirmed_decoder_stuck else "risk",
                    confidence,
                ),
                "owner": "播放器/解码侧",
                "actions": [
                    "检查 MPP 驱动日志与输入流状态",
                    "核对码率、分辨率和芯片能力上限",
                ],
                "suspect_process": suspect or "MPP Hardware Decoder",
                "confidence": round(confidence, 1),
            }

        if cause_type == "THERMAL_THROTTLING":
            return {
                "title": "可判定为热降频导致播放退化",
                "conclusion": "卡顿与温度/限频同时出现，更像是热降频触发后的系统性性能下降。",
                "evidence_level": "confirmed",
                "evidence_strength": self.build_evidence_strength("confirmed", confidence),
                "owner": "系统/硬件侧",
                "actions": [
                    "检查散热条件与温控策略",
                    "对比冷机与热机下的同场景表现",
                ],
                "suspect_process": suspect or "CPU Thermal Governor",
                "confidence": round(confidence, 1),
            }
        if cause_type == "MEMORY_PRESSURE":
            return {
                "title": "较大概率为内存压力导致播放退化",
                "conclusion": "播放器或关联进程 PSS 明显高于基线，存在内存压力或泄漏风险。",
                "evidence_level": "strong" if confidence >= 70 else "weak",
                "evidence_strength": self.build_evidence_strength(
                    "strong" if confidence >= 70 else "weak",
                    confidence,
                ),
                "owner": owner,
                "actions": [
                    "对比基线版本内存曲线",
                    "重点检查大对象、纹理缓存和 GC 抖动",
                ],
                "suspect_process": suspect,
                "confidence": round(confidence, 1),
            }
        if cause_type == "LOW_FPS_DEGRADATION":
            return {
                "title": "较大概率为渲染/合成链路压力导致低帧率",
                "conclusion": "解码吞吐基本正常，但电视端显示帧率持续偏低，问题更偏向渲染/合成链路。",
                "evidence_level": "risk",
                "evidence_strength": self.build_evidence_strength("risk", confidence),
                "owner": "系统显示链路/渲染侧",
                "actions": [
                    "检查 SurfaceFlinger、composer 服务与 GPU 占用",
                    "排查 UI 动画、弹层和合成开销",
                ],
                "suspect_process": suspect or "SurfaceFlinger/Render",
                "confidence": round(confidence, 1),
            }
        if cause_type == "AV_SYNC_ISSUE":
            evidence_level = "weak" if signal_only else "strong"
            return {
                "title": "较大概率为播放器内部音画同步/缓冲抖动",
                "conclusion": "日志出现卡顿信号，但解码、显示和系统资源没有同步恶化，更偏向播放器内部缓冲或时钟同步问题。",
                "evidence_level": evidence_level,
                "evidence_strength": self.build_evidence_strength(evidence_level, confidence),
                "owner": "播放器侧",
                "actions": [
                    "重点核对 buffer underrun、时钟漂移和音轨切换日志",
                    "复查播放器内部队列深度与同步策略",
                ],
                "suspect_process": suspect or self.main_package_name or self.package_name,
                "confidence": round(confidence, 1),
            }
        if resource_risk_events > 0 and confirmed_playback_causes <= 0:
            return {
                "title": "存在明显资源风险，但证据仍偏辅助",
                "conclusion": "当前已看到整机资源竞争或负载异常，但还缺少足够强的电视端退化同步证据。",
                "evidence_level": "strong",
                "evidence_strength": self.build_evidence_strength("strong", confidence),
                "owner": owner,
                "actions": [
                    "继续保留电视端卡顿事件目录",
                    "增加同场景复测次数，确认是否稳定复现",
                ],
                "suspect_process": issue_process or suspect,
                "confidence": round(confidence, 1),
            }
        if log_signal_events > 0:
            return {
                "title": "仅发现日志信号，暂不能直接定责",
                "conclusion": "当前只有播放器相关日志异常，仍不足以直接判定具体责任方。",
                "evidence_level": "weak",
                "evidence_strength": self.build_evidence_strength("weak", confidence),
                "owner": "待补证据",
                "actions": [
                    "补充电视端 Surface 帧时间和 Top 进程证据",
                    "延长监控并提高卡顿时段采样密度",
                ],
                "suspect_process": suspect,
                "confidence": round(confidence, 1),
            }

        return {
            "title": "暂无法定责",
            "conclusion": "已捕获异常，但现有证据还不足以直接锁定具体问题。",
            "evidence_level": "insufficient",
            "evidence_strength": self.build_evidence_strength("insufficient", confidence),
            "owner": "待补证据",
            "actions": [
                "继续采集更多卡顿样本",
                "补充播放器日志、Top 和电视端 Surface 证据",
            ],
            "suspect_process": suspect,
            "confidence": round(confidence, 1),
        }

    def _guess_owner(self, suspect_process: str, cause_type: str) -> str:
        suspect = str(suspect_process or "").lower()
        if cause_type == "AV_SYNC_ISSUE":
            return "播放器侧"
        if cause_type == "DECODER_STUCK":
            return "播放器/解码侧"
        if cause_type == "THERMAL_THROTTLING":
            return "系统/硬件侧"
        if (
            suspect.startswith("/system/bin/")
            or "surfaceflinger" in suspect
            or "mediaserver" in suspect
            or "composer" in suspect
            or "audioserver" in suspect
        ):
            return "系统/固件侧"
        if self.main_package_name and self.main_package_name.lower() in suspect:
            return "播放器侧"
        return "应用/系统联合排查"

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

    def _attach_common_event_evidence(self, candidate: Dict, event: Dict) -> Dict:
        if not isinstance(candidate, dict):
            return candidate
        evidence = dict(candidate.get("evidence") or {})
        top_processes = event.get("top_processes") or []
        evidence.setdefault(
            "event_snapshot",
            {
                "system_cpu_percent": round(
                    float(event.get("system_cpu_percent", 0) or 0.0), 2
                ),
                "player_cpu_percent": round(
                    float(event.get("player_cpu_percent", 0) or 0.0), 2
                ),
                "video_fps": round(float(event.get("video_fps", 0) or 0.0), 2),
                "video_fps_source": str(event.get("video_fps_source", "") or ""),
                "decode_fps_estimate": round(
                    float(event.get("decode_fps_estimate", 0) or 0.0), 2
                ),
                "expected_stream_fps": round(
                    float(event.get("expected_stream_fps", 0) or 0.0), 2
                ),
                "decode_drop_ratio": round(
                    float(event.get("decode_drop_ratio", 0) or 0.0), 4
                ),
                "gfx_jank_percent": round(
                    float(event.get("gfx_jank_percent", 0) or 0.0), 2
                ),
                "log_stutter_delta": int(event.get("log_stutter_delta", 0) or 0),
                "audio_active": bool(event.get("audio_active", False)),
                "system_cpu_pressure": bool(
                    event.get("system_cpu_pressure", False)
                ),
                "thermal_throttling": bool(
                    event.get("thermal_throttling", False)
                ),
                "tv_stutter_detected": bool(
                    event.get("tv_stutter_detected", False)
                ),
                "tv_surface_locked": bool(
                    event.get("tv_surface_locked", False)
                ),
                "decoder_stuck_confirmed": bool(
                    event.get("decoder_stuck_confirmed", False)
                ),
            },
        )
        if top_processes:
            evidence.setdefault(
                "top_processes",
                [
                    {"process": str(name), "cpu_percent": round(float(cpu), 2)}
                    for name, cpu in top_processes[:5]
                ],
            )
        if event.get("top_consumers_raw"):
            evidence.setdefault(
                "top_consumers_raw", str(event.get("top_consumers_raw") or "")
            )
        stutter_events = event.get("log_stutter_events") or []
        if stutter_events:
            normalized_events = []
            for item in stutter_events[:3]:
                if not isinstance(item, dict):
                    continue
                normalized_events.append({
                    "pattern": str(item.get("pattern", "") or ""),
                    "line": str(item.get("line", "") or ""),
                })
            if normalized_events:
                evidence.setdefault("log_stutter_events", normalized_events)
        decoder_events = event.get("decoder_log_events") or []
        if decoder_events:
            normalized_decoder_events = []
            for item in decoder_events[:5]:
                if not isinstance(item, dict):
                    continue
                normalized_decoder_events.append({
                    "pattern": str(item.get("pattern", "") or ""),
                    "line": str(item.get("line", "") or ""),
                })
            if normalized_decoder_events:
                evidence.setdefault("decoder_log_events", normalized_decoder_events)
        decoder_diagnostics = event.get("decoder_diagnostics") or {}
        if isinstance(decoder_diagnostics, dict) and decoder_diagnostics:
            evidence.setdefault("decoder_diagnostics", dict(decoder_diagnostics))
        candidate["evidence"] = evidence
        return candidate

    def _infer_decoder_name(self, event: Dict) -> str:
        diagnostics = event.get("decoder_diagnostics") or {}
        if isinstance(diagnostics, dict):
            name = str(diagnostics.get("decoder_name", "") or "").strip()
            if name:
                return name

        decoder_logs = event.get("decoder_log_events") or []
        for item in decoder_logs:
            if not isinstance(item, dict):
                continue
            found = self._extract_decoder_name_from_text(str(item.get("line", "") or ""))
            if found:
                return found

        log_events = event.get("log_stutter_events") or []
        for item in log_events:
            if not isinstance(item, dict):
                continue
            found = self._extract_decoder_name_from_text(str(item.get("line", "") or ""))
            if found:
                return found
        return ""

    @staticmethod
    def _extract_decoder_name_from_text(text: str) -> str:
        raw = str(text or "")
        patterns = [
            r"(OMX\.[\w\.\-]+)",
            r"(c2\.[\w\.\-]+)",
            r"(rk[\w\.\-]*decoder[\w\.\-]*)",
            r"(rkvdec[\w\.\-]*)",
            r"(vdec[\w\.\-]*)",
            r"(MediaCodec[\w\.\-:/]*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw, re.IGNORECASE)
            if match:
                return str(match.group(1)).strip()
        return ""

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
        log_stutter_delta = int(event.get("log_stutter_delta", 0) or 0)
        system_cpu = float(event.get("system_cpu_percent", 0) or 0.0)
        player_cpu = float(event.get("player_cpu_percent", 0) or 0.0)
        vf = float(event.get("video_fps", 0) or 0.0)
        expected_stream_fps = float(event.get("expected_stream_fps", 0) or 0.0)
        decode_fps = float(event.get("decode_fps_estimate", 0) or 0.0)
        decode_drop_ratio = float(event.get("decode_drop_ratio", 0) or 0.0)
        audio_active = bool(event.get("audio_active", False))
        system_cpu_pressure = bool(event.get("system_cpu_pressure", False))
        video_fps_source = str(event.get("video_fps_source", "") or "")
        playback_degradation = bool(
            event.get("decoder_stuck_confirmed", False)
            or event.get("decode_slowdown_detected", False)
            or (
                decode_drop_ratio >= 0.10
                and bool(event.get("tv_surface_locked", False))
            )
            or event.get("tv_stutter_detected", False)
            or (
                video_fps_source.startswith(
                    "surfaceflinger"
                )
                and vf < 20
            )
        )

        if event.get("decoder_stuck") and int(event.get("mpp_active", 0) or 0) > 0:
            decoder_name = self._infer_decoder_name(event)
            confirmed_decoder_stuck = bool(
                event.get("decoder_stuck_confirmed", False)
            )
            candidates.append({
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "DECODER_STUCK",
                "suspect_process": decoder_name or "MPP Hardware Decoder",
                "confidence": 90.0 if confirmed_decoder_stuck else 72.0,
                "evidence": {
                    "mpp_active_instances": int(event.get("mpp_active", 0) or 0),
                    "mpp_work_count_delta": int(event.get("mpp_work_count_delta", 0) or 0),
                    "video_fps": float(event.get("video_fps", 0) or 0.0),
                    "decoder_name": decoder_name,
                    "resource_only": not confirmed_decoder_stuck,
                    "surface_locked": bool(event.get("tv_surface_locked", False)),
                    "decoder_stuck_confirmed": confirmed_decoder_stuck,
                },
                "suggestion": "硬件解码器输出停顿。建议检查码率、分辨率是否超出芯片能力，输入流是否异常，并结合 MPP 驱动日志继续定位。",
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
                "suggestion": "PSS 明显高于基线，存在内存压力或泄漏风险。建议检查大对象、纹理缓存、队列积压和 GC 相关日志。",
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
                "suggestion": "卡顿时检测到高温或 CPU 明显限频，建议优先排查散热、温控策略和持续高负载，避免误判为普通应用抢占 CPU。",
            })

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
                        f"{proc_name} 在高负载时 CPU {cpu_pct:.1f}%（基线约 {base_cpu:.1f}%）。"
                        + (
                            "同时检测到播放退化信号，建议优先排查它与媒体链路之间的算力竞争。"
                            if playback_degradation
                            else "当前还没有足够的电视端退化直证，先作为资源风险候选保留。"
                        )
                    ),
                })

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
                    "suggestion": "解码吞吐基本正常，但显示帧率持续偏低，问题更像合成链路压力或 UI/渲染线程阻塞。建议检查 SurfaceFlinger、GPU 占用和界面动画/弹层。",
                })

        strong_non_avsync_cause = any(
            str(candidate.get("root_cause_type", "") or "") in (
                "DECODER_STUCK",
                "THERMAL_THROTTLING",
                "CPU_CONTENTION",
                "LOW_FPS_DEGRADATION",
                "MEMORY_PRESSURE",
            )
            and float(candidate.get("confidence", 0) or 0.0) >= 70.0
            for candidate in candidates
        )
        decode_pipeline_healthy = bool(
            not event.get("decoder_stuck", False)
            and not event.get("decode_slowdown_detected", False)
            and decode_drop_ratio <= 0.03
            and (
                decode_fps <= 0
                or expected_stream_fps <= 0
                or decode_fps >= expected_stream_fps * 0.75
            )
        )
        render_fps_degraded = bool(
            vf > 0
            and expected_stream_fps > 0
            and vf < max(20.0, expected_stream_fps * 0.7)
        )
        resource_pressure = bool(
            system_cpu_pressure
            or system_cpu >= 85.0
            or player_cpu >= 35.0
            or event.get("thermal_throttling", False)
        )
        av_sync_confirmed = bool(
            log_stutter_delta > 0
            and audio_active
            and decode_pipeline_healthy
            and not resource_pressure
            and not render_fps_degraded
            and not strong_non_avsync_cause
        )
        if av_sync_confirmed:
            confidence = 52.0
            if log_stutter_delta >= 2:
                confidence += 6.0
            if decode_fps > 0 and expected_stream_fps > 0 and decode_fps >= expected_stream_fps * 0.9:
                confidence += 4.0
            if vf > 0 and expected_stream_fps > 0 and vf >= expected_stream_fps * 0.85:
                confidence += 3.0
            if system_cpu < 70.0:
                confidence += 3.0
            candidates.append({
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "AV_SYNC_ISSUE",
                "suspect_process": self.main_package_name or self.package_name or "Player",
                "confidence": min(68.0, confidence),
                "evidence": {
                    "log_stutter_delta": log_stutter_delta,
                    "log_stutter_total": int(event.get("log_stutter_count", 0) or 0),
                    "audio_active": audio_active,
                    "decode_pipeline_healthy": decode_pipeline_healthy,
                    "decode_fps_estimate": decode_fps,
                    "expected_stream_fps": expected_stream_fps,
                    "video_fps": vf,
                    "system_cpu_percent": system_cpu,
                    "signal_only": False,
                },
                "suggestion": "卡顿日志在增加，但解码吞吐、显示帧率和系统资源没有同步恶化，更像缓冲波动、音画同步或播放器内部队列抖动。建议重点核对 buffer underrun、时钟漂移和音轨切换日志。",
            })

        if log_stutter_delta > 0 and not candidates:
            candidates.append({
                "timestamp": event.get("timestamp", ""),
                "root_cause_type": "AV_SYNC_ISSUE",
                "suspect_process": self.main_package_name or self.package_name or "Player",
                "confidence": 40.0,
                "evidence": {
                    "log_stutter_delta": log_stutter_delta,
                    "log_stutter_total": int(event.get("log_stutter_count", 0) or 0),
                    "audio_active": audio_active,
                    "signal_only": True,
                },
                "suggestion": "仅检测到播放器相关卡顿日志，缺少电视端画面、解码吞吐或系统资源退化的同步证据，当前仅作为日志信号保留。",
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
                "suggestion": "当前数据不足以确定根因。建议延长采样、补充播放器层日志，或提高异常时段的 Top 进程采样密度继续对比。",
            }

        candidates.sort(key=lambda x: float(x.get("confidence", 0) or 0), reverse=True)
        return candidates[0]
