import re
import time
from datetime import datetime

class VoiceCommandTracker:
    """语音指令状态追踪器"""
    
    def __init__(self):
        self.pending_commands = {}  # 待确认的指令 {command_text: record}
        self.current_song = None     # 当前播放的歌曲
        self.command_history = []    # 指令历史
        self.last_ts_str = ""

        # 正则预编译
        self.VOICE_RULES = {
            # 识别到语音（放宽匹配条件）
            'VOICE_DETECTED': re.compile(
                r'(?:XunfeiVoiceEngine|VoiceResultHandler).*?(?:extracted=\'([^\']+)\'|RecognitionResult\{.*?command=\'([^\']+)\')'
            ),
            # 指令被执行
            'VOICE_EXECUTED': re.compile(
                r'AiVoiceService:\s*(?:onCommand|executeLocalInstruction):\s*name=([^,]+).*?(?:response=(.+?))?(?:\s|$)'
            ),
            # 指令被忽略
            'VOICE_NOT_HIT': re.compile(
                r'AiVoiceService:\s*(?:handleNotHit|onNotHit):\s*(?:ignored,\s*text=([^,\s]+)|([^,\s]+))'
            ),
            # 歌曲信息更新
            'SONG_PLAYING': re.compile(
                r'IdleVideoPlayManager.*path\s+/[^/]+/[^/]+/Song-[^/]+/(\d+)\.ts.*category\s+(\w+)'
            ),
        }

    def _extract_timestamp(self, log_line: str) -> str:
        # e.g. "03-29 20:05:14.991"
        if len(log_line) > 18:
            potential_ts = log_line[:18]
            if potential_ts[0].isdigit() and potential_ts[2] == '-':
                self.last_ts_str = potential_ts
        return self.last_ts_str

    def process_log(self, log_line: str):
        """解析单行日志，更新状态机"""
        # 性能优化：快速预过滤，不包含这些关键字的直接跳过，避免跑正则
        # 放宽条件：包括 BusinessManagerImpl 或 com.thunder.ktv，因为执行指令可能在这里
        if not any(k in log_line for k in ['XunfeiVoiceEngine', 'VoiceResultHandler', 'AiVoiceService', 'IdleVideoPlayManager', 'BusinessManagerImpl', 'com.thunder.ktv']):
            return

        ts = self._extract_timestamp(log_line)
        
        # 1. 歌曲播放更新
        if 'IdleVideoPlayManager' in log_line and 'path' in log_line:
            m_song = self.VOICE_RULES['SONG_PLAYING'].search(log_line)
            if m_song:
                self.current_song = {
                    'id': m_song.group(1),
                    'category': m_song.group(2)
                }
            return

        # 2. 识别到语音
        if 'XunfeiVoiceEngine' in log_line or 'VoiceResultHandler' in log_line:
            m_detect = self.VOICE_RULES['VOICE_DETECTED'].search(log_line)
            if m_detect:
                command = m_detect.group(1) or m_detect.group(2)
                if not command: return
                record = {
                    'id': str(int(time.time() * 1000)),
                    'timestamp': ts,
                    'command': command,
                    'status': 'detected',
                    'response': '',
                    'song': self.current_song,
                    'raw_logs': [log_line]
                }
                self.pending_commands[command] = record
                self.command_history.append(record)
                self._trim_history()
                return

        # 3. 指令执行或忽略
        if 'AiVoiceService' in log_line or 'com.thunder.ktv' in log_line:
            # 尝试匹配忽略
            m_ignored = self.VOICE_RULES['VOICE_NOT_HIT'].search(log_line)
            if m_ignored:
                command = m_ignored.group(1) or m_ignored.group(2)
                if command and command in self.pending_commands:
                    self.pending_commands[command]['status'] = 'ignored'
                    self.pending_commands[command]['raw_logs'].append(log_line)
                    del self.pending_commands[command]
                return

            # 尝试匹配执行
            m_exec = self.VOICE_RULES['VOICE_EXECUTED'].search(log_line)
            if m_exec:
                command = m_exec.group(1)
                response = m_exec.group(2) or ''
                if command in self.pending_commands:
                    self.pending_commands[command]['status'] = 'executed'
                    self.pending_commands[command]['response'] = response
                    self.pending_commands[command]['raw_logs'].append(log_line)
                    del self.pending_commands[command]
                return

        # 兜底追加日志
        if self.command_history:
            self.command_history[-1]['raw_logs'].append(log_line)

    def _trim_history(self):
        if len(self.command_history) > 200:
            self.command_history = self.command_history[-200:]

    def get_history(self):
        return self.command_history
