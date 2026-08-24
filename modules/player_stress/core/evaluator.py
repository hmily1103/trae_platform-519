from typing import Dict, List, Optional
import time


class PlayStateEvaluator:
    """
    V2 裁决模块（Referee）。
    职责：收集会话事实，并给出最终判定。
    """

    STATE_INIT = "INIT"
    STATE_STARTED = "STARTED"
    STATE_FINISHED = "FINISHED"
    STATE_INTERRUPTED = "INTERRUPTED"
    STATE_FAILED = "FAILED"

    def __init__(self):
        self.session_id = "init"
        self.events = []

        self.has_pid_restart = False
        self.has_crash_anr = False
        self.has_first_frame = False
        self.is_interrupted = False
        self.is_finished = False

        self.start_time = 0.0
        self.first_frame_time = None
        self.end_time = None

        self.session_results = []
        self.screen_anomaly_count = 0

    def start_new_session(self, session_id: str):
        self.session_id = session_id
        self.events = []

        self.has_pid_restart = False
        self.has_crash_anr = False
        self.has_first_frame = False
        self.is_interrupted = False
        self.is_finished = False

        self.start_time = 0.0
        self.first_frame_time = None
        self.end_time = None

    def on_play_command_issued(self):
        self.start_time = time.time()
        self.events.append({"time": self.start_time, "event": "PLAY_CMD"})

    def on_first_frame(self, source: str = "unknown"):
        if not self.has_first_frame:
            self.has_first_frame = True
            self.first_frame_time = time.time()
            self.events.append(
                {
                    "time": self.first_frame_time,
                    "event": "FIRST_FRAME",
                    "source": source,
                }
            )

    def on_pid_event(self, event_type: str):
        self.events.append({"time": time.time(), "event": f"PID_{event_type}"})
        if event_type in ["RESTART", "LOST", "CHANGED"]:
            self.has_pid_restart = True

    def on_fatal_error(self, error_type: str, msg: str = ""):
        self.events.append(
            {"time": time.time(), "event": f"FATAL_{error_type}", "msg": msg}
        )
        self.has_crash_anr = True

    def on_play_end(self):
        self.is_finished = True
        self.end_time = time.time()
        self.events.append({"time": self.end_time, "event": "PLAY_END"})

    def on_interrupt(self, reason: str):
        self.is_interrupted = True
        self.end_time = time.time()
        self.events.append(
            {"time": self.end_time, "event": "INTERRUPT", "reason": reason}
        )

    def on_screen_anomaly(self):
        self.screen_anomaly_count += 1
        self.events.append({"time": time.time(), "event": "SCREEN_ANOMALY"})

    def finalize(self):
        current_time = time.time()
        final_state = self.STATE_INIT
        fail_reason = None

        if self.has_crash_anr:
            final_state = self.STATE_FAILED
            fail_reason = "Crash/ANR"
        elif self.has_pid_restart:
            final_state = self.STATE_FAILED
            fail_reason = "PID Restart/Lost"
        elif self.is_interrupted:
            final_state = self.STATE_INTERRUPTED
            fail_reason = "Interrupted"
        elif self.is_finished:
            final_state = self.STATE_FINISHED
        elif self.start_time > 0:
            final_state = self.STATE_STARTED

        duration = 0.0
        first_frame_latency = 0.0
        if self.start_time > 0:
            end = self.end_time if self.end_time else current_time
            duration = end - self.start_time
            if self.first_frame_time:
                first_frame_latency = (self.first_frame_time - self.start_time) * 1000

        result = {
            "session_id": self.session_id,
            "final_state": final_state,
            "fail_reason": fail_reason,
            "pid_restart": self.has_pid_restart,
            "crash_anr": self.has_crash_anr,
            "first_frame_latency_ms": int(first_frame_latency),
            "duration_sec": round(duration, 2),
            "events": self.events,
        }
        self.session_results.append(result)
        return result

    def evaluate_global_score(self, summary_stats: Dict) -> Dict:
        score = 100
        deductions: List[str] = []

        total_restarts = sum(1 for r in self.session_results if r["pid_restart"])
        total_crashes = sum(1 for r in self.session_results if r["crash_anr"])
        total_sessions = len(self.session_results)
        success_sessions = sum(
            1 for r in self.session_results if r["final_state"] == self.STATE_FINISHED
        )

        pid_loss_count = int(summary_stats.get("pid_loss_count", 0) or 0)
        tv_stall_count = int(summary_stats.get("tv_stall_count", 0) or 0)
        tv_stall_risk_count = int(summary_stats.get("tv_stall_risk_count", 0) or 0)
        tv_freeze_count = int(summary_stats.get("tv_freeze_count", 0) or 0)
        confirmed_decoder_stuck_count = int(
            summary_stats.get("confirmed_decoder_stuck_count", 0) or 0
        )
        decoder_stuck_risk_count = int(
            summary_stats.get("decoder_stuck_risk_count", 0) or 0
        )
        surface_locked = bool(summary_stats.get("tv_surface_locked", False))
        display_verified = bool(summary_stats.get("tv_display_verified", False))
        video_fps_samples = int(summary_stats.get("video_fps_samples", 0) or 0)
        avg_video_fps = float(summary_stats.get("avg_video_fps", 0) or 0.0)
        valid_sample_ratio = float(summary_stats.get("valid_sample_ratio", 1.0) or 1.0)
        target_process_lost = bool(summary_stats.get("target_process_lost", False))
        avg_system_cpu = float(summary_stats.get("avg_system_cpu_percent", 0) or 0.0)
        max_system_cpu = float(summary_stats.get("max_system_cpu_percent", 0) or 0.0)
        tv_jank_ratio = float(summary_stats.get("tv_jank_sample_ratio_percent", 0) or 0.0)
        tv_big_jank_ratio = float(
            summary_stats.get("tv_big_jank_sample_ratio_percent", 0) or 0.0
        )
        tv_micro_stall_ratio = float(
            summary_stats.get("tv_micro_stall_sample_ratio_percent", 0) or 0.0
        )
        tv_perceptible_stall_ratio = float(
            summary_stats.get("tv_perceptible_stall_sample_ratio_percent", 0) or 0.0
        )
        tv_severe_stall_ratio = float(
            summary_stats.get("tv_severe_stall_sample_ratio_percent", 0) or 0.0
        )
        tv_frame_gap_p95_ms = float(summary_stats.get("tv_frame_gap_p95_ms", 0) or 0.0)
        tv_frame_gap_p99_ms = float(summary_stats.get("tv_frame_gap_p99_ms", 0) or 0.0)

        screen_anomaly_count = max(
            int(self.screen_anomaly_count or 0),
            tv_stall_count + tv_freeze_count,
        )

        if total_restarts > 0:
            score -= 41
            deductions.append(f"PID 重启/丢失 {total_restarts} 次 (-41) [阻断项]")
        if total_crashes > 0:
            score -= 41
            deductions.append(f"Crash / ANR {total_crashes} 次 (-41) [阻断项]")
        if pid_loss_count > 0 and total_restarts == 0:
            score -= 41
            deductions.append(f"目标播放器进程丢失 {pid_loss_count} 次 (-41) [阻断项]")

        if total_sessions > 0:
            success_rate = success_sessions / total_sessions
            if success_rate < 0.95:
                deduction = min(int((1.0 - success_rate) * 100), 30)
                score -= deduction
                deductions.append(f"播放成功率偏低 ({success_rate * 100:.1f}%) (-{deduction})")

        if screen_anomaly_count > 0:
            deduction = min(40, screen_anomaly_count * 20)
            score -= deduction
            if screen_anomaly_count == 1:
                deductions.append(f"屏幕异常 1 次 (-{deduction}，同类封顶40)")
            else:
                deductions.append(
                    f"有效屏幕异常 {screen_anomaly_count} 次 (-{deduction}，同类封顶40)"
                )

        degradation = summary_stats.get("degradation_analysis", {}) or {}
        mem_growth = float(degradation.get("mem_growth_rate_mb_per_hour", 0) or 0.0)
        if mem_growth > 50.0:
            score -= 20
            deductions.append(f"内存增长过快 ({mem_growth:.1f}MB/h) (-20)")

        cpu_overloaded = avg_system_cpu >= 85.0 or max_system_cpu >= 95.0
        if avg_system_cpu >= 85.0:
            score -= 25
            deductions.append(f"整机平均CPU过高 ({avg_system_cpu:.1f}%) (-25)")
        elif avg_system_cpu >= 75.0:
            score -= 15
            deductions.append(f"整机平均CPU偏高 ({avg_system_cpu:.1f}%) (-15)")
        elif max_system_cpu >= 95.0:
            score -= 10
            deductions.append(f"整机峰值CPU过高 ({max_system_cpu:.1f}%) (-10)")

        moderate_jank = (
            tv_frame_gap_p99_ms >= 300.0
            and (
                tv_perceptible_stall_ratio >= 0.3
                or tv_big_jank_ratio >= 3.0
                or tv_jank_ratio >= 8.0
            )
        )
        jank_pressure = (
            moderate_jank
            or tv_perceptible_stall_ratio >= 0.5
            or tv_severe_stall_ratio >= 0.2
            or tv_frame_gap_p99_ms >= 300.0
            or tv_frame_gap_p95_ms >= 180.0
            or (tv_big_jank_ratio >= 5.0 and tv_perceptible_stall_ratio >= 0.3)
            or (tv_jank_ratio >= 10.0 and tv_frame_gap_p99_ms >= 250.0)
        )
        strong_jank = (
            tv_frame_gap_p95_ms >= 2000.0
            or tv_frame_gap_p99_ms >= 2500.0
            or tv_severe_stall_ratio >= 1.0
            or (tv_big_jank_ratio >= 35.0 and tv_perceptible_stall_ratio >= 2.0)
        )
        severe_jank = (
            tv_perceptible_stall_ratio >= 1.0
            or tv_severe_stall_ratio >= 0.5
            or tv_frame_gap_p99_ms >= 1000.0
            or tv_frame_gap_p95_ms >= 500.0
        )
        if strong_jank:
            score -= 20
            deductions.append(
                f"检测到电视端视频帧时间严重异常 (底层Jank {tv_jank_ratio:.2f}%, Big Jank {tv_big_jank_ratio:.2f}%, P95/P99 {tv_frame_gap_p95_ms:.0f}/{tv_frame_gap_p99_ms:.0f}ms) (-20)"
            )
        elif severe_jank:
            score -= 20
            deductions.append(
                f"检测到电视端明显停顿风险 (微 {tv_micro_stall_ratio:.2f}%, 感知 {tv_perceptible_stall_ratio:.2f}%, 严重 {tv_severe_stall_ratio:.2f}%, P99 {tv_frame_gap_p99_ms:.0f}ms) (-20)"
            )
        elif jank_pressure:
            score -= 10
            deductions.append(
                f"电视端流畅度存在抖动风险 (感知 {tv_perceptible_stall_ratio:.2f}%, P99 {tv_frame_gap_p99_ms:.0f}ms) (-10)"
            )
        if strong_jank and deductions:
            deductions[-1] = (
                f"检测到电视端视频帧时间严重异常 (底层Jank {tv_jank_ratio:.2f}%, "
                f"Big Jank {tv_big_jank_ratio:.2f}%, P95/P99 {tv_frame_gap_p95_ms:.0f}/{tv_frame_gap_p99_ms:.0f}ms) (-20)"
            )
        elif severe_jank and deductions:
            deductions[-1] = (
                f"检测到电视端明显停顿风险 (微 {tv_micro_stall_ratio:.2f}%, "
                f"感知 {tv_perceptible_stall_ratio:.2f}%, 严重 {tv_severe_stall_ratio:.2f}%, "
                f"P99 {tv_frame_gap_p99_ms:.0f}ms) (-20)"
            )
        elif moderate_jank and deductions:
            score -= 5
            deductions[-1] = (
                f"检测到电视端视频帧时间抖动 (底层Jank {tv_jank_ratio:.2f}%, "
                f"Big Jank {tv_big_jank_ratio:.2f}%, P99 {tv_frame_gap_p99_ms:.0f}ms) (-15)"
            )
        elif jank_pressure and deductions:
            deductions[-1] = (
                f"检测到电视端流畅度抖动风险 (感知 {tv_perceptible_stall_ratio:.2f}%, "
                f"P99 {tv_frame_gap_p99_ms:.0f}ms) (-10)"
            )
        root_cause_data = summary_stats.get("root_cause_analysis", {}) or {}
        identified_causes = (
            int(root_cause_data.get("identified_causes", 0) or 0)
            if isinstance(root_cause_data, dict)
            else 0
        )
        most_confident = (
            root_cause_data.get("most_confident_cause") or {}
            if isinstance(root_cause_data, dict)
            else {}
        )
        cause_type = str(most_confident.get("root_cause_type", "") or "")
        cause_evidence = (
            most_confident.get("evidence", {}) if isinstance(most_confident, dict) else {}
        ) or {}
        resource_only = bool(cause_evidence.get("resource_only", False))
        signal_only = bool(cause_evidence.get("signal_only", False))
        if identified_causes > 0 and cause_type != "AV_SYNC_ISSUE" and not resource_only and not signal_only:
            if cause_type == "CPU_CONTENTION":
                score -= 15
                deductions.append("多进程CPU竞争导致卡顿 (-15)")
            elif cause_type == "DECODER_STUCK":
                score -= 25
                deductions.append("确认解码链路停顿 (-25)")
            elif cause_type == "MEMORY_PRESSURE":
                score -= 10
                deductions.append("内存压力导致播放风险 (-10)")
            else:
                score -= 5
                deductions.append(f"检测到根因候选 {identified_causes} 次 (-5)")

        score = max(0, int(score))
        if score >= 90:
            grade = "S"
        elif score >= 80:
            grade = "A"
        elif score >= 60:
            grade = "B"
        elif score >= 40:
            grade = "C"
        else:
            grade = "D"

        release_blockers: List[str] = []
        if target_process_lost or pid_loss_count > 0:
            release_blockers.append(f"目标播放器进程丢失 {max(1, pid_loss_count)} 次")
        if valid_sample_ratio < 0.8:
            release_blockers.append(f"有效采样覆盖率不足 {valid_sample_ratio * 100:.1f}%")
        if tv_stall_count > 0:
            release_blockers.append(f"确认电视端卡顿 {tv_stall_count} 次")
        if confirmed_decoder_stuck_count > 0:
            release_blockers.append(f"确认解码停顿 {confirmed_decoder_stuck_count} 次")
        if cpu_overloaded and (tv_stall_count > 0 or severe_jank):
            release_blockers.append("整机CPU持续高负载并已影响电视端流畅度")
        if strong_jank:
            release_blockers.append(
                f"电视端视频帧时间异常已达阻断级别 (底层Jank {tv_jank_ratio:.2f}%, Big Jank {tv_big_jank_ratio:.2f}%, P95/P99 {tv_frame_gap_p95_ms:.0f}/{tv_frame_gap_p99_ms:.0f}ms)"
            )

        evidence_confidence_score = 20
        if display_verified:
            evidence_confidence_score += 15
        if surface_locked:
            evidence_confidence_score += 25
        if video_fps_samples >= 10 and avg_video_fps > 0:
            evidence_confidence_score += 20
        if tv_stall_count > 0 or confirmed_decoder_stuck_count > 0:
            evidence_confidence_score += 20
        elif tv_stall_risk_count > 0 or decoder_stuck_risk_count > 0 or jank_pressure:
            evidence_confidence_score += 10
        evidence_confidence_score = max(0, min(100, int(evidence_confidence_score)))
        if evidence_confidence_score >= 80:
            evidence_confidence_label = "high"
        elif evidence_confidence_score >= 55:
            evidence_confidence_label = "medium"
        else:
            evidence_confidence_label = "low"

        risk_gate_reasons: List[str] = []
        if tv_stall_risk_count >= 5:
            risk_gate_reasons.append(f"电视端风险样本 {tv_stall_risk_count} 次，建议灰度观察")
        if decoder_stuck_risk_count >= 3:
            risk_gate_reasons.append(f"解码风险样本 {decoder_stuck_risk_count} 次，建议灰度观察")
        if screen_anomaly_count >= 5 and tv_stall_count <= 0 and confirmed_decoder_stuck_count <= 0:
            risk_gate_reasons.append(f"屏幕异常累计 {screen_anomaly_count} 次，建议灰度观察")
        if tv_freeze_count >= 3 and tv_stall_count <= 0:
            risk_gate_reasons.append(f"冻结样本 {tv_freeze_count} 次，建议灰度观察")
        if (strong_jank or severe_jank) and not release_blockers:
            risk_gate_reasons.append(
                f"视频帧时间抖动已明显超阈值 (底层Jank {tv_jank_ratio:.2f}%, Big Jank {tv_big_jank_ratio:.2f}%, P95/P99 {tv_frame_gap_p95_ms:.0f}/{tv_frame_gap_p99_ms:.0f}ms)，建议灰度观察"
            )

        evidence_incomplete = bool(
            (not surface_locked)
            or (valid_sample_ratio < 0.8)
            or (
                (tv_stall_risk_count > 0 or decoder_stuck_risk_count > 0)
                and evidence_confidence_label == "low"
            )
        )
        ready_to_release = (
            score >= 60
            and total_restarts == 0
            and total_crashes == 0
            and not release_blockers
            and not risk_gate_reasons
            and not evidence_incomplete
        )
        hard_failure = bool(
            target_process_lost
            or pid_loss_count > 0
            or total_restarts > 0
            or total_crashes > 0
            or tv_stall_count > 0
            or confirmed_decoder_stuck_count > 0
            or strong_jank
            or score < 60
        )

        if ready_to_release:
            assessment = "pass"
            release_status = "建议上线"
            risk_status = "风险可接受"
        elif hard_failure:
            assessment = "fail"
            release_status = "不建议上线"
            risk_status = "存在阻断性问题"
        elif risk_gate_reasons:
            assessment = "observe"
            release_status = "建议灰度观察"
            risk_status = "存在明显风险，建议先灰度观察"
        elif evidence_incomplete:
            assessment = "inconclusive"
            release_status = "证据不足，暂不作上线结论"
            risk_status = "证据覆盖不足"
        else:
            assessment = "fail"
            release_status = "不建议上线"
            risk_status = "存在未闭环风险"

        decision_rule = (
            "准入结论由稳定性门禁、电视端流畅性门禁和证据完整度共同决定；"
            "其中流畅性结论可以单独阻断上线。"
        )
        if assessment == "observe":
            decision_rule = (
                "稳定性未触发硬阻断，但电视端流畅性风险已达到灰度观察阈值；"
                "建议先小流量观察，并优先排查高相关链路后再决定是否放量。"
            )
        elif assessment == "fail" and strong_jank:
            decision_rule = (
                "虽然本轮未必出现 Crash/ANR/进程丢失，但电视端流畅性门禁已触发阻断；"
                "当前应先修复明显停顿问题，再重新验证。"
            )

        return {
            "score": score,
            "grade": grade,
            "deductions": deductions,
            "ready_to_release": ready_to_release,
            "release_blockers": release_blockers,
            "risk_gate_reasons": risk_gate_reasons,
            "release_status": release_status,
            "risk_status": risk_status,
            "evidence_confidence_score": evidence_confidence_score,
            "evidence_confidence_label": evidence_confidence_label,
            "decision_rule": decision_rule,
            "assessment": assessment,
            "counts": {
                "restart": total_restarts,
                "crash": total_crashes,
                "screen_anomaly": screen_anomaly_count,
                "pid_loss": pid_loss_count,
                "decoder_stuck_risk": decoder_stuck_risk_count,
                "tv_stall_risk": tv_stall_risk_count,
                "tv_freeze": tv_freeze_count,
            },
        }

    def get_one_sentence_summary(
        self,
        duration_str: str,
        song_count: int,
        score_result: Dict,
        root_cause_info: Optional[Dict] = None,
    ) -> str:
        score = score_result["score"]
        grade = score_result["grade"]
        ready = score_result["ready_to_release"]
        counts = score_result.get("counts", {}) or {}
        assessment = score_result.get("assessment", "")

        issues: List[str] = []
        if counts.get("crash", 0) > 0:
            issues.append(f"{counts['crash']} 次 Crash/ANR")
        if counts.get("restart", 0) > 0:
            issues.append(f"{counts['restart']} 次 PID 重启")
        if counts.get("pid_loss", 0) > 0:
            issues.append(f"{counts['pid_loss']} 次进程丢失")
        if counts.get("screen_anomaly", 0) > 0:
            issues.append(f"{counts['screen_anomaly']} 次屏幕异常")
        issue_str = f"发生{'、'.join(issues)}。" if issues else "运行稳定。"

        blockers = list(score_result.get("release_blockers", []) or [])
        blocker_str = ""
        if blockers:
            blocker_str = f"阻断项：{'；'.join(blockers)}。"

        root_cause_str = ""
        if isinstance(root_cause_info, dict):
            diagnosis = str(root_cause_info.get("summary_title", "") or "").strip()
            evidence_level = str(root_cause_info.get("evidence_level", "") or "").strip()
            if diagnosis and evidence_level in {"confirmed", "strong"}:
                root_cause_str = f"{diagnosis}。"

        if ready:
            recommendation = "建议上线。"
        elif assessment == "observe":
            recommendation = "建议灰度观察。"
        elif assessment == "inconclusive":
            recommendation = "本轮证据不足，暂不作上线结论。"
        else:
            recommendation = "不建议上线。"

        return (
            f"本次压测持续 {duration_str}，共播放 {song_count} 首歌曲。"
            f"稳定性评分 {score} ({grade})。"
            f"{root_cause_str}{issue_str}{blocker_str}{recommendation}"
        )
