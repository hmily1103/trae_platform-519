import time
import random
import requests
import json
import logging
from .adb_manager import AdbManager

logger = logging.getLogger(__name__)


class PlayerController:
    # Android KeyCodes
    KEYCODE_MEDIA_PLAY_PAUSE = 85
    KEYCODE_MEDIA_NEXT = 87
    KEYCODE_MEDIA_PREVIOUS = 88
    KEYCODE_MEDIA_STOP = 86
    
    def __init__(self, adb: AdbManager, package_name: str, activity_name: str = None, http_config: dict = None):
        self.adb = adb
        self.package_name = package_name
        self.activity_name = activity_name
        self.http_config = http_config or {}

    def launch(self):
        """启动播放器"""
        logger.info("正在启动播放器...")
        self.adb.start_app(self.package_name, self.activity_name)
        time.sleep(5) # 等待启动
        
        # 移除自动点歌逻辑，由 Runner 根据模式决定是否点歌

    def _has_http_config(self):
        return self.http_config.get('server_ip') and self.http_config.get('music_list')

    def _get_next_music_no(self):
        """获取下一首要播放的歌曲编号"""
        raw_list = self.http_config.get('music_list', "")
        if not raw_list:
            return None
            
        # 兼容处理: 如果已经是列表则直接使用，如果是字符串则解析
        if isinstance(raw_list, list):
            songs = raw_list
        else:
            # 解析歌曲列表 (支持中英文逗号)
            songs = [s.strip() for s in str(raw_list).replace("，", ",").split(",") if s.strip()]
            
        if not songs:
            return None
            
        # 简单轮询
        if not hasattr(self, '_song_index'):
            self._song_index = 0
        else:
            self._song_index = (self._song_index + 1) % len(songs)
            
        return songs[self._song_index]

    def vod_song(self):
        """通过 HTTP 接口点歌"""
        ip = self.http_config.get('server_ip')
        stb_ip = self.http_config.get('stb_ip', "192.168.16.100")
        music_no = self._get_next_music_no()
        
        if not ip or not music_no:
            logger.debug("跳过点歌: 未配置 IP 或 歌曲列表为空")
            return None # 改为返回 None 表示失败
            
        url = f"http://{ip}:8008/song/vod"
        # 构造 Body
        # roominfo 格式: 86f02338_{STB_IP}
        
        payload = {
            "roominfo": f"86f02338_{stb_ip}",
            "musicinfo": [{"musicno": music_no, "musicname": ""}],
            "parm": "{\"vip\":0}",
            "userid": "123",
            "appid": "32432424"
        }
        
        try:
            logger.info("正在通过 HTTP 点歌: %s -> %s (STB: %s)", music_no, url, stb_ip)
            resp = requests.post(url, json=payload, timeout=3)
            if resp.status_code == 200:
                logger.info("点歌请求成功: %s", resp.text)
                return str(music_no) # 返回歌曲编号
            else:
                logger.warning("点歌请求失败: %s", resp.status_code)
                return None
        except Exception as e:
            logger.warning("点歌异常: %s", e)
            return None

    def ensure_playing(self):
        """尝试确保播放状态 (发送 Play 键)"""
        # 注意：如果不确定当前状态，发送 KEYCODE_MEDIA_PLAY_PAUSE 可能会暂停
        # 很多播放器处理 KEYCODE_MEDIA_PLAY 自动识别
        # 这里假设发送 PLAY_PAUSE 来触发
        # 更好的做法是检查 dumpsys audio 输出，但这比较复杂。
        # 简单策略：仅在需要切歌或显式操作时交互
        pass

    def next_song(self):
        """切歌 (强制切歌)"""
        # 优先使用 HTTP 接口切歌
        if self.http_cut_song():
            return

        logger.info("执行切歌操作 (ADB)...")
        self.adb.send_key_event(self.KEYCODE_MEDIA_NEXT)

    def http_cut_song(self):
        """通过 HTTP 接口切歌"""
        stb_ip = self.http_config.get('stb_ip', "192.168.16.100")
        if not stb_ip:
            return False

        url = f"http://{stb_ip}:2007/playcontrol/cutsong"
        try:
            logger.info("正在通过 HTTP 切歌: %s", url)
            resp = requests.get(url, timeout=3)
            if resp.status_code == 200:
                logger.info("切歌请求成功: %s", resp.text)
                return True
            else:
                logger.warning("切歌请求失败: %s", resp.status_code)
                return False
        except Exception as e:
            logger.warning("切歌异常: %s", e)
            return False

    def pause_resume(self):
        """暂停/恢复"""
        logger.info("执行暂停/恢复...")
        self.adb.send_key_event(self.KEYCODE_MEDIA_PLAY_PAUSE)

    def stop(self):
        """停止应用"""
        self.adb.stop_app(self.package_name)
