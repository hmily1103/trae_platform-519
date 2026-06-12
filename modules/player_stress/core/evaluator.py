from typing import Dict, List, Optional, Tuple
import time

class PlayStateEvaluator:
    """
    V2 裁决模块 (Referee)
    职责: 收敛事实 -> 给出最终判定
    """
    
    # States
    STATE_INIT = "INIT"
    STATE_STARTED = "STARTED"
    STATE_FINISHED = "FINISHED"
    STATE_INTERRUPTED = "INTERRUPTED"
    STATE_FAILED = "FAILED"

    def __init__(self):
        # Current Session State
        self.session_id = "init"
        self.events = []
        
        # Priority Flags
        self.has_pid_restart = False
        self.has_crash_anr = False
        self.has_first_frame = False
        self.is_interrupted = False
        self.is_finished = False
        
        # Timings
        self.start_time = 0
        self.first_frame_time = None
        self.end_time = None
        
        # Global History (for scoring)
        self.session_results = []
        self.screen_anomaly_count = 0 # Accumulated

    def start_new_session(self, session_id: str):
        """Step 1: Start a new session"""
        self.session_id = session_id
        self.events = []
        
        self.has_pid_restart = False
        self.has_crash_anr = False
        self.has_first_frame = False
        self.is_interrupted = False
        self.is_finished = False
        
        self.start_time = 0
        self.first_frame_time = None
        self.end_time = None
        
    def on_play_command_issued(self):
        """Step 2: Play command issued"""
        self.start_time = time.time()
        self.events.append({"time": self.start_time, "event": "PLAY_CMD"})

    def on_first_frame(self, source: str = "unknown"):
        """Step 3: First frame detected (Log or Audio)"""
        if not self.has_first_frame:
            self.has_first_frame = True
            self.first_frame_time = time.time()
            self.events.append({"time": self.first_frame_time, "event": "FIRST_FRAME", "source": source})

    def on_pid_event(self, event_type: str):
        """Step 3: PID event detected"""
        self.events.append({"time": time.time(), "event": f"PID_{event_type}"})
        if event_type in ["RESTART", "LOST", "CHANGED"]:
            self.has_pid_restart = True

    def on_fatal_error(self, error_type: str, msg: str = ""):
        """Step 3: Fatal error detected"""
        self.events.append({"time": time.time(), "event": f"FATAL_{error_type}", "msg": msg})
        self.has_crash_anr = True

    def on_play_end(self):
        """Step 4: Play ended naturally"""
        self.is_finished = True
        self.end_time = time.time()
        self.events.append({"time": self.end_time, "event": "PLAY_END"})

    def on_interrupt(self, reason: str):
        """Step 4: Play interrupted"""
        self.is_interrupted = True
        self.end_time = time.time()
        self.events.append({"time": self.end_time, "event": "INTERRUPT", "reason": reason})

    def on_screen_anomaly(self, status: str):
        """Step 3: Screen anomaly detected"""
        self.screen_anomaly_count += 1
        self.events.append({"time": time.time(), "event": "SCREEN_ANOMALY", "status": status})

    def finalize(self) -> Dict:
        """Step 5: Final Verdict (The Whistle)"""
        # Priority Logic
        final_state = self.STATE_FINISHED
        fail_reason = None
        
        if self.has_pid_restart:
            final_state = self.STATE_FAILED
            fail_reason = "PID Restart/Lost"
        elif self.has_crash_anr:
            final_state = self.STATE_FAILED
            fail_reason = "Crash/ANR Detected"
        elif not self.has_first_frame:
            final_state = self.STATE_FAILED
            fail_reason = "No First Frame"
        elif self.is_interrupted:
            final_state = self.STATE_INTERRUPTED
            fail_reason = "Interrupted"
        elif self.is_finished:
            final_state = self.STATE_FINISHED
        else:
            # Fallback for undefined states
            if self.has_first_frame:
                # If it started but didn't finish or interrupt, and we are finalizing...
                # It likely timed out or was cut short without explicit interrupt.
                # Assume INTERRUPTED if we are finalizing now.
                final_state = self.STATE_INTERRUPTED
                fail_reason = "Timeout/Incomplete"
            else:
                final_state = self.STATE_FAILED
                fail_reason = "Did not start"

        # Calculate durations
        duration = 0
        first_frame_latency = 0
        current_time = time.time()
        
        if self.start_time > 0:
            end = self.end_time if self.end_time else current_time
            duration = end - self.start_time
            
            if self.first_frame_time:
                first_frame_latency = (self.first_frame_time - self.start_time) * 1000 # ms

        result = {
            "session_id": self.session_id,
            "final_state": final_state,
            "fail_reason": fail_reason,
            "pid_restart": self.has_pid_restart,
            "crash_anr": self.has_crash_anr,
            "first_frame_latency_ms": int(first_frame_latency),
            "duration_sec": round(duration, 2),
            "events": self.events
        }
        
        self.session_results.append(result)
        return result

    # --- Global Scoring Logic (Report Consumption) ---

    def evaluate_global_score(self, summary_stats: Dict) -> Dict:
        """
        基于 session_results 计算全局评分
        """
        score = 100
        deductions = []
        
        # 1. PID / Crash (Zero Tolerance)
        total_restarts = sum(1 for r in self.session_results if r["pid_restart"])
        total_crashes = sum(1 for r in self.session_results if r["crash_anr"])
        
        if total_restarts > 0:
            ded = 41
            score -= ded
            deductions.append(f"PID重启 {total_restarts} 次 (-{ded}) [零容忍]")
            
        if total_crashes > 0:
            ded = 41
            score -= ded
            deductions.append(f"崩溃/ANR {total_crashes} 次 (-{ded}) [零容忍]")
            
        # 2. Success Rate
        total_sessions = len(self.session_results)
        success_sessions = sum(1 for r in self.session_results if r["final_state"] == self.STATE_FINISHED)
        # INTERRUPTED counts as...? If manually skipped, maybe okay? 
        # But in automation, usually we want FINISHED.
        # Let's count FINISHED as Success.
        
        if total_sessions > 0:
            success_rate = success_sessions / total_sessions
            if success_rate < 0.95:
                ded = int((1.0 - success_rate) * 100) # Simple deduction
                ded = min(ded, 30)
                score -= ded
                deductions.append(f"成功率低 ({success_rate*100:.1f}%) (-{ded})")
        
        # 3. Screen Anomalies
        if self.screen_anomaly_count > 0:
            ded = min(40, self.screen_anomaly_count * 20)
            score -= ded
            deductions.append(
                f"有效屏幕异常 {self.screen_anomaly_count} 次 (-{ded}，同类封顶40)"
            )

        # 4. Resource Degradation (from summary_stats)
        degradation = summary_stats.get("degradation_analysis", {})
        growth = degradation.get("mem_growth_rate_mb_per_hour", 0)
        if growth > 50:
            score -= 20
            deductions.append(f"内存严重泄露 ({growth}MB/h) (-20)")

        avg_system_cpu = float(
            summary_stats.get("avg_system_cpu_percent", 0) or 0.0
        )
        max_system_cpu = float(
            summary_stats.get("max_system_cpu_percent", 0) or 0.0
        )
        cpu_overloaded = avg_system_cpu >= 85.0 or max_system_cpu >= 95.0
        if avg_system_cpu >= 85.0:
            score -= 25
            deductions.append(f"整机平均CPU过高 ({avg_system_cpu:.1f}%) (-25)")
        elif avg_system_cpu >= 75.0:
            score -= 15
            deductions.append(f"整机平均CPU偏高 ({avg_system_cpu:.1f}%) (-15)")
        elif max_system_cpu >= 95.0:
            score -= 10
            deductions.append(f"整机CPU峰值过高 ({max_system_cpu:.1f}%) (-10)")

        root_cause_data = summary_stats.get("root_cause_analysis", {}) or {}
        identified_causes = int(root_cause_data.get("identified_causes", 0) or 0) if isinstance(root_cause_data, dict) else 0
        most_confident = (root_cause_data.get("most_confident_cause") or {}) if isinstance(root_cause_data, dict) else {}
        cause_type = str(most_confident.get("root_cause_type", "") or "")

        cause_evidence = (
            most_confident.get("evidence", {})
            if isinstance(most_confident, dict)
            else {}
        ) or {}
        resource_only = bool(cause_evidence.get("resource_only", False))

        if (
            identified_causes > 0
            and cause_type != "AV_SYNC_ISSUE"
            and not resource_only
        ):
            if cause_type == "CPU_CONTENTION":
                ded = 15
                score -= ded
                deductions.append(f"多进程CPU竞争导致卡顿 (-{ded})")
            elif cause_type == "DECODER_STUCK":
                ded = 25
                score -= ded
                deductions.append(f"硬件解码器卡死 (-{ded})")
            elif cause_type == "MEMORY_PRESSURE":
                ded = 10
                score -= ded
                deductions.append(f"内存压力异常 (-{ded})")
            else:
                ded = 5
                score -= ded
                deductions.append(f"检测到卡顿根因候选 {identified_causes} 次 (-{ded})")
            
        score = max(0, int(score))
        
        grade = "D"
        if score >= 90: grade = "S"
        elif score >= 80: grade = "A"
        elif score >= 60: grade = "B"
        elif score >= 40: grade = "C"
        
        tv_stall_count = int(summary_stats.get("tv_stall_count", 0) or 0)
        decoder_stuck_count = int(
            summary_stats.get("decoder_stuck_count", 0) or 0
        )
        surface_locked = bool(summary_stats.get("tv_surface_locked", False))
        release_blockers = []
        if cpu_overloaded:
            release_blockers.append("整机CPU持续高负载")
        if tv_stall_count > 0:
            release_blockers.append(f"确认电视端卡顿 {tv_stall_count} 次")
        if decoder_stuck_count > 0:
            release_blockers.append(f"解码器卡死 {decoder_stuck_count} 次")
        if not surface_locked:
            release_blockers.append("未锁定电视端视频Surface，证据覆盖不足")

        ready_to_release = (
            score >= 60
            and total_restarts == 0
            and total_crashes == 0
            and not release_blockers
        )

        return {
            "score": score,
            "grade": grade,
            "deductions": deductions,
            "ready_to_release": ready_to_release,
            "release_blockers": release_blockers,
            "assessment": (
                "pass"
                if ready_to_release
                else ("inconclusive" if not surface_locked else "fail")
            ),
            "counts": {
                "restart": total_restarts,
                "crash": total_crashes,
                "screen_anomaly": self.screen_anomaly_count
            }
        }

    def get_one_sentence_summary(self, duration_str: str, song_count: int, score_result: Dict, root_cause_info: Optional[Dict] = None) -> str:
        score = score_result['score']
        grade = score_result['grade']
        ready = score_result['ready_to_release']
        counts = score_result.get('counts', {})
        
        issues = []
        if counts.get('crash', 0) > 0: issues.append(f"{counts['crash']}次崩溃")
        if counts.get('restart', 0) > 0: issues.append(f"{counts['restart']}次重启")
        if counts.get('screen_anomaly', 0) > 0: issues.append(f"{counts['screen_anomaly']}次屏幕异常")
        
        issue_str = f"发生 {'、'.join(issues)}，" if issues else "运行稳定，"
        blockers = list(score_result.get("release_blockers", []) or [])
        assessment = str(score_result.get("assessment", "") or "")
        if ready:
            recommendation = "建议上线"
        elif assessment == "inconclusive":
            recommendation = "本轮证据不足，不给出上线结论"
        else:
            recommendation = "不建议上线"

        root_cause_str = ""
        if isinstance(root_cause_info, dict):
            top_cause = root_cause_info.get("most_confident_cause") or {}
            evidence = (
                top_cause.get("evidence", {})
                if isinstance(top_cause, dict) else {}
            ) or {}
            if (
                isinstance(top_cause, dict)
                and top_cause.get("root_cause_type")
                and not evidence.get("signal_only", False)
                and not evidence.get("resource_only", False)
                and float(top_cause.get("confidence", 0) or 0) >= 60
            ):
                cause_type = str(top_cause.get("root_cause_type", "") or "")
                suspect = str(top_cause.get("suspect_process", "") or "")
                type_cn = {
                    "CPU_CONTENTION": "CPU竞争",
                    "DECODER_STUCK": "解码器卡死",
                    "MEMORY_PRESSURE": "内存压力",
                    "LOW_FPS_DEGRADATION": "持续低帧",
                    "AV_SYNC_ISSUE": "音画同步/缓冲",
                }.get(cause_type, cause_type)
                if suspect:
                    root_cause_str = f"主要根因为{type_cn}（{suspect}），"
                else:
                    root_cause_str = f"主要根因为{type_cn}，"
        
        blocker_str = (
            f"阻断项：{'；'.join(blockers)}。"
            if blockers else ""
        )
        return (f"本次压测持续 {duration_str}，共播放 {song_count} 首歌曲。"
                f"稳定性评分 {score} ({grade})。"
                f"{root_cause_str}{issue_str}{blocker_str}{recommendation}。")
