import threading
import subprocess
import time
import re
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class LogMonitor:
    def __init__(self, adb_manager, device_id, log_callback=None, song_change_patterns=None):
        self.adb = adb_manager
        self.device_id = device_id
        self.log_callback = log_callback
        self.running = False
        self.process = None
        self.log_dir = os.path.join(os.getcwd(), "crash_logs")
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
            
        # 卡顿统计
        self.stutter_count = 0
        self.stutter_logs = []
        
        # 错误统计
        self.crash_count = 0
        self.anr_count = 0
        self.error_events = [] # [{time, type, message, log_file}]
        
        # 纯监控模式：歌曲切换检测（用户可配置 Logcat 关键字）
        self.song_change_patterns = song_change_patterns or []
        self.song_change_events = []
        self._last_song_change_time = 0
        self._song_change_debounce_sec = 3  # 3 秒内只计一次

    def get_song_change_events(self):
        """获取并清空歌曲切换事件（供 monitor_only 模式使用）"""
        events = list(self.song_change_events)
        self.song_change_events.clear()
        return events

    def get_stutter_count(self):
        return self.stutter_count
        
    def get_error_stats(self):
        return {
            "crash_count": self.crash_count,
            "anr_count": self.anr_count,
            "error_events": self.error_events
        }

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
            except Exception as e:
                logger.debug("terminate logcat 进程时忽略异常: %s", e)

    def get_lifecycle_events(self):
        """获取并清空当前的生命周期事件"""
        if not hasattr(self, 'lifecycle_events'):
            return []
        events = list(self.lifecycle_events)
        self.lifecycle_events.clear()
        return events

    def _monitor_loop(self):
        cmd = ["adb"]
        if self.device_id:
            cmd.extend(["-s", self.device_id])
        cmd.extend(["logcat", "-v", "time"])

        # 清除之前的日志缓冲区
        try:
            cmd_clear = ["adb"]
            if self.device_id:
                cmd_clear.extend(["-s", self.device_id])
            cmd_clear.extend(["logcat", "-c"])
            subprocess.run(cmd_clear, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0, timeout=5)
        except Exception as e:
            logger.debug("logcat -c 清除缓冲区失败(可忽略): %s", e)

        try:
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='ignore',
                creationflags=creationflags
            )

            # 关键错误特征
            # FATAL EXCEPTION
            # ANR in
            patterns = [
                r"FATAL EXCEPTION",
                r"ANR in"
            ]
            
            # 卡顿/丢帧特征 (常见播放器关键字，含自研引擎变体)
            stutter_patterns = [
                r"Skipped \d+ frames",     # Choreographer (UI)
                r"missed \d+ frames",      # SurfaceFlinger/HWComposer
                r"droppedFrames",          # ExoPlayer/Ijk
                r"late by \d+ ms",         # MediaCodec/Player
                r"buffer starvation",      # Buffering
                r"AudioTrack.*underrun",   # Audio glitches (Strong indicator of stutter)
                r"AudioFlinger.*underrun",  # Audio glitches
                r"underrun",               # 通用缓冲区下溢（部分引擎不写 AudioTrack 前缀）
                r"hard loss",              # Audio packet loss
                r"av_get_frame_hold",      # IjkPlayer decoding block
                r"decoder.*too slow",       # Generic decoder warning
                r"Video render too late",   # 自研引擎：视频渲染延迟
                r"render.*too late",        # 渲染延迟变体
                r"Buffer usage\s*>\s*\d+", # 缓冲区压力（如 Buffer usage > 90）
                r"frame drop",             # 丢帧
                r"dropped frame",          # 丢帧单数
                r"stutter",                # 卡顿
                r"jank",                   # 卡顿/掉帧
            ]
            
            # 播放器生命周期特征 (V2 状态机信号)
            # 涵盖: ExoPlayer, MediaPlayer, IjkPlayer 常见日志
            lifecycle_patterns = {
                "PREPARING": [r"onPrepared", r"prepareAsync", r"msg=2\(STARTED\)"], 
                "STARTED": [r"onStart", r"start\(\)", r"cmp=.*\.START"],
                "FIRST_FRAME": [r"MEDIA_INFO_VIDEO_RENDERING_START", r"first frame rendered", r"onInfo\(3\)"],
                "COMPLETED": [r"onCompletion", r"MEDIA_PLAYBACK_COMPLETE", r"End of playback"],
                "ERROR": [r"onError", r"MEDIA_ERROR"]
            }

            buffer_lines = []
            MAX_BUFFER = 100
            
            # 状态事件队列 (Thread-safe list)
            self.lifecycle_events = []
            
            while self.running:
                line = self.process.stdout.readline()
                if not line:
                    break
                
                # 维护一个小的上下文 buffer
                buffer_lines.append(line)
                if len(buffer_lines) > MAX_BUFFER:
                    buffer_lines.pop(0)

                # 1. 检测 Crash/ANR
                for pattern in patterns:
                    if re.search(pattern, line):
                        if "ANR" in pattern:
                            self.anr_count += 1
                        else:
                            self.crash_count += 1
                            
                        self._handle_crash(line, buffer_lines, pattern)
                        buffer_lines = []
                        
                # 2. 检测卡顿 (Stutter)
                for pattern in stutter_patterns:
                    if re.search(pattern, line, re.IGNORECASE):
                        self.stutter_count += 1
                        if len(self.stutter_logs) < 10:
                            self.stutter_logs.append(line.strip())
                        break
                
                # 3. 检测生命周期事件 (V2)
                for event_type, pats in lifecycle_patterns.items():
                    for pat in pats:
                        if re.search(pat, line):
                            self.lifecycle_events.append({
                                "time": time.time(),
                                "type": event_type,
                                "line": line.strip()
                            })
                            break
                    else:
                        continue
                    break
                
                # 4. 纯监控模式：检测歌曲切换（用户配置的关键字）
                if self.song_change_patterns:
                    now = time.time()
                    if now - self._last_song_change_time >= self._song_change_debounce_sec:
                        for pat in self.song_change_patterns:
                            try:
                                if re.search(pat, line):
                                    self.song_change_events.append({"time": now, "line": line.strip()})
                                    self._last_song_change_time = now
                                    break
                            except re.error:
                                pass
                        
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"日志监控异常: {e}", "ERROR")
        finally:
            if self.process:
                self.process.terminate()

    def _handle_crash(self, trigger_line, context_lines, pattern):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"crash_{pattern.replace(' ', '_')}_{timestamp}.txt"
        filepath = os.path.join(self.log_dir, filename)
        
        content = "".join(context_lines)
        # 继续读取一些行以获取完整堆栈 (简单的策略: 再读 100 行)
        try:
            for _ in range(100):
                line = self.process.stdout.readline()
                if not line: break
                content += line
        except (BrokenPipeError, OSError) as e:
            logger.debug("读取 crash 堆栈时中断: %s", e)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"Triggered by: {trigger_line}\n")
            f.write("-" * 50 + "\n")
            f.write(content)
            
        # 记录详细错误事件
        self.error_events.append({
            "time": timestamp, # 格式: YYYYMMDD_HHMMSS
            "type": "ANR" if "ANR" in pattern else "CRASH",
            "message": trigger_line.strip(),
            "log_file": filename
        })
            
        msg = f"检测到异常 ({pattern})! 已保存日志: {filename}"
        if self.log_callback:
            self.log_callback(msg, "ERROR")
            
        # 尝试截图
        self._take_screenshot(timestamp)

    def _take_screenshot(self, timestamp):
        try:
            filename = f"crash_screenshot_{timestamp}.png"
            local_path = os.path.join(self.log_dir, filename)
            remote_path = f"/sdcard/{filename}"
            
            # 截图
            self.adb._run_command(["shell", "screencap", "-p", remote_path])
            # 拉取
            self.adb._run_command(["pull", remote_path, local_path])
            # 清理
            self.adb._run_command(["shell", "rm", remote_path])
            
            if self.log_callback:
                self.log_callback(f"已保存现场截图: {filename}", "INFO")
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"截图失败: {e}", "WARNING")
