"""
视频流处理
处理scrcpy视频流并通过WebSocket推送到前端
"""
import subprocess
import threading
import time
import os
import tempfile
from typing import Optional, Callable, Dict, Any
from utils.logger import setup_logger

logger = setup_logger('video_stream')


class VideoStreamManager:
    """视频流管理器"""
    
    def __init__(self, scrcpy_path: str = "scrcpy"):
        """
        初始化视频流管理器
        
        :param scrcpy_path: scrcpy可执行文件路径
        """
        self.scrcpy_path = scrcpy_path
        self.streams: dict = {}  # {device_id: {process, thread, callbacks, last_frame, last_frame_payload}}
        self.lock = threading.Lock()
    
    def start_stream(self, device_id: str, callback: Callable[[bytes], None]) -> bool:
        """
        启动视频流
        
        :param device_id: 设备ID
        :param callback: 视频帧回调函数
        :return: 是否成功
        """
        with self.lock:
            if device_id in self.streams:
                # 已存在，添加回调
                self.streams[device_id]['callbacks'].append(callback)
                return True
            
            try:
                # 使用scrcpy的截图方式（简化版，后续可优化为真正的视频流）
                stream_info = {
                    'device_id': device_id,
                    'callbacks': [callback],
                    'running': True,
                    'thread': None,
                    'last_frame': None,  # 兼容：仅 image base64
                    'last_frame_payload': None,
                }
                
                # 启动截图线程
                thread = threading.Thread(
                    target=self._screenshot_loop,
                    args=(device_id,),
                    daemon=True
                )
                thread.start()
                stream_info['thread'] = thread
                
                self.streams[device_id] = stream_info
                logger.info(f"视频流已启动: {device_id}")
                return True
                
            except Exception as e:
                logger.error(f"启动视频流失败: {e}", exc_info=True)
                return False
    
    def stop_stream(self, device_id: str) -> bool:
        """
        停止视频流
        
        :param device_id: 设备ID
        :return: 是否成功
        """
        with self.lock:
            if device_id not in self.streams:
                return False
            
            stream_info = self.streams[device_id]
            stream_info['running'] = False
            
            # 等待线程结束
            if stream_info['thread']:
                stream_info['thread'].join(timeout=2)
            
            del self.streams[device_id]
            logger.info(f"视频流已停止: {device_id}")
            return True
    
    def remove_callback(self, device_id: str, callback: Callable):
        """移除回调函数"""
        with self.lock:
            if device_id in self.streams:
                callbacks = self.streams[device_id]['callbacks']
                if callback in callbacks:
                    callbacks.remove(callback)
                
                # 如果没有回调了，停止流
                if not callbacks:
                    self.stop_stream(device_id)

    def get_last_frame(self, device_id: str) -> Optional[str]:
        """获取最新一帧（仅 image base64）"""
        with self.lock:
            if device_id in self.streams:
                payload = self.streams[device_id].get('last_frame_payload') or {}
                return payload.get('image') or self.streams[device_id].get('last_frame')
        return None

    def get_last_frame_payload(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            if device_id in self.streams:
                payload = self.streams[device_id].get('last_frame_payload')
                if payload:
                    return dict(payload)
                frame = self.streams[device_id].get('last_frame')
                if frame:
                    return {'image': frame}
            # 即使流未启动，也可能被外部 set 过缓存
            cached = getattr(self, '_external_frame_cache', {}).get(device_id)
            return dict(cached) if cached else None

    def set_last_frame_payload(self, device_id: str, payload: Dict[str, Any]) -> None:
        if not payload or not payload.get('image'):
            return
        with self.lock:
            if not hasattr(self, '_external_frame_cache'):
                self._external_frame_cache = {}
            self._external_frame_cache[device_id] = dict(payload)
            if device_id in self.streams:
                self.streams[device_id]['last_frame_payload'] = dict(payload)
                self.streams[device_id]['last_frame'] = payload.get('image')

    def invalidate_frame_cache(self, device_id: str) -> None:
        """控制操作后清缓存，避免预览一直显示旧画面。"""
        with self.lock:
            if hasattr(self, '_external_frame_cache'):
                self._external_frame_cache.pop(device_id, None)
            if device_id in self.streams:
                self.streams[device_id]['last_frame'] = None
                self.streams[device_id]['last_frame_payload'] = None
    
    def _screenshot_loop(self, device_id: str):
        """截图循环（压缩预览帧，避免大图 SSE 卡死浏览器）"""
        from .device_controller import DeviceController
        from .preview_image import encode_preview_payload
        
        controller = DeviceController(device_id)
        fps = 1  # 降低频率，减少与手动预览/点击的锁竞争
        interval = 1.0 / fps
        dw, dh = controller.get_display_size()
        
        while True:
            with self.lock:
                if device_id not in self.streams:
                    break
                stream_info = self.streams[device_id]
                if not stream_info['running']:
                    break
                callbacks = stream_info['callbacks'].copy()
            
            try:
                png = controller.screenshot_png_bytes(timeout=20)
                if png:
                    payload = encode_preview_payload(png, device_width=dw, device_height=dh)
                    if payload and payload.get('image'):
                        image_base64 = payload['image']
                        with self.lock:
                            if device_id in self.streams:
                                self.streams[device_id]['last_frame'] = image_base64
                                self.streams[device_id]['last_frame_payload'] = payload
                        for callback in callbacks:
                            try:
                                callback(image_base64)
                            except Exception as e:
                                logger.error(f"回调执行失败: {e}")
                
            except Exception as e:
                logger.error(f"截图循环错误: {e}", exc_info=True)
            
            time.sleep(interval)
        
        logger.debug(f"截图循环结束: {device_id}")
