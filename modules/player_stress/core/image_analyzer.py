import time
import os
import logging
from PIL import Image, ImageStat, ImageChops

logger = logging.getLogger(__name__)


class ScreenAnalyzer:
    """
    屏幕状态分析器 (基于 PIL)
    功能:
    1. 黑屏检测 (Black Screen)
    2. 画面冻结检测 (Static Screen / Freeze)
    """
    
    def __init__(self, adb_manager, temp_dir="reports/screenshots"):
        self.adb = adb_manager
        self.temp_dir = os.path.join(os.getcwd(), temp_dir)
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            
        self.last_screenshot_paths = {} # {display_id: path}
        self.last_screenshot_time = 0
        # V2.3: 电视端 Display ID 缓存
        self._tv_display_ids_cache = None
        self._tv_display_ids_cache_time = 0
        self._tv_display_ids_cache_ttl = 300.0  # 缓存5分钟

    def check_video_freeze(self, interval_seconds: float = 3.0, threshold: int = 5) -> bool:
        """
        专用视频卡顿检测
        原理: 连续抓取两帧 (间隔较短)，判断画面是否变化
        Returns: True (卡顿/画面静止), False (画面在动)
        """
        t1 = time.time()
        path1 = os.path.join(self.temp_dir, f"freeze_1_{int(t1)}.png")
        
        try:
            # 1. 第一帧
            self.adb.take_screenshot(path1)
            
            # 2. 等待间隔
            time.sleep(interval_seconds)
            
            # 3. 第二帧
            t2 = time.time()
            path2 = os.path.join(self.temp_dir, f"freeze_2_{int(t2)}.png")
            self.adb.take_screenshot(path2)
            
            if not os.path.exists(path1) or not os.path.exists(path2):
                return False # 截图失败，无法判断
            
            # 4. 对比
            img1 = Image.open(path1).convert("RGB")
            
            # 复用 _is_same_image 逻辑，但这里传入路径
            is_frozen = self._is_same_image(img1, path2, diff_threshold=threshold)
            
            # 5. 清理
            try:
                os.remove(path1)
                os.remove(path2)
            except:
                pass
                
            return is_frozen
            
        except Exception as e:
            logger.warning("[VideoFreeze] Check failed: %s", e)
            return False

    def check_screen_status(self) -> dict:
        """
        检测当前屏幕状态 (支持 Display 0 和自动检测的电视端 Display)
        Returns: {
            0: {"status": "NORMAL", "path": "..."},
            1: {"status": "BLACK_SCREEN", "path": "..."}  # 或 2，取决于检测到的电视端
        }
        """
        current_time = time.time()
        results = {}
        
        # V2.3: 自动检测电视端 Display ID
        tv_display_ids = self._detect_tv_displays()
        
        # 检测 Display 0 (Main/点歌屏) 和所有检测到的电视端 Display
        display_ids = [0] + tv_display_ids
        
        for display_id in display_ids:
            filename = f"screen_d{display_id}_{int(current_time)}.png"
            local_path = os.path.join(self.temp_dir, filename)
            
            status = "UNKNOWN"
            try:
                self.adb.take_screenshot(local_path, display_id=display_id)
                
                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    try:
                        img = Image.open(local_path).convert("RGB")
                        
                        # A. 黑屏检测
                        if self._is_black_screen(img):
                            status = "BLACK_SCREEN"
                        else:
                            # B. 冻结检测 (已禁用，因为误报率太高)
                            # 用户反馈：即使肉眼看有变化，也容易被误判为静止。
                            # 且音乐播放场景本身变化就小。
                            status = "NORMAL"
                            
                            # last_path = self.last_screenshot_paths.get(display_id)
                            # if last_path and os.path.exists(last_path):
                            #     if self._is_same_image(img, last_path):
                            #         status = "STATIC_SCREEN"
                            
                        # 更新上一帧记录
                        self.last_screenshot_paths[display_id] = local_path
                        
                    except Exception as e:
                        logger.warning("图像分析异常 (Display %s): %s", display_id, e)
                        status = "ANALYSIS_ERROR"
                else:
                    # 如果文件不存在或为空，可能是该 Display 不存在
                    status = "NO_SIGNAL" if display_id == 1 else "CAPTURE_FAILED"
                    if os.path.exists(local_path):
                        try: os.remove(local_path)
                        except Exception:
                            pass
                    local_path = "" # 不记录路径
            except Exception as e:
                logger.warning("截图失败 (Display %s): %s", display_id, e)
                status = "ERROR"
                local_path = ""
                
            results[display_id] = {"status": status, "path": local_path}
            
        self.last_screenshot_time = current_time
        return results
    
    def _detect_tv_displays(self) -> list:
        """
        V2.3: 自动检测电视端 Display ID 列表
        返回: [1] 或 [1, 2] 等，取决于设备配置
        """
        # 使用缓存
        current_time = time.time()
        if (self._tv_display_ids_cache is not None and 
            current_time - self._tv_display_ids_cache_time < self._tv_display_ids_cache_ttl):
            return self._tv_display_ids_cache
        
        tv_displays = []
        try:
            # 方法1: 通过 dumpsys display 检测
            display_output = self.adb._run_command(["shell", "dumpsys", "display"], timeout=2)
            
            if display_output and "Error:" not in display_output:
                import re
                display_ids = set()
                for line in display_output.splitlines():
                    matches = re.findall(r'Display\s+(?:id=)?(\d+)|mDisplayId[=:](\d+)', line, re.IGNORECASE)
                    for match in matches:
                        display_id = int(match[0] or match[1])
                        if display_id > 0:  # 排除 Display 0
                            display_ids.add(display_id)
                
                if display_ids:
                    tv_displays = sorted(display_ids)
                    self._tv_display_ids_cache = tv_displays
                    self._tv_display_ids_cache_time = current_time
                    logger.info("[ScreenAnalyzer] 检测到电视端 Display IDs: %s", tv_displays)
                    return tv_displays
            
            # 方法2: 尝试测试 Display 1 和 2
            for test_id in [1, 2]:
                try:
                    # 简单测试：尝试查询该 Display 的信息
                    result = self.adb._run_command(
                        ["shell", "dumpsys", "SurfaceFlinger", "--display-id", str(test_id), "--list"], 
                        timeout=1
                    )
                    # 如果命令成功（没有明确的错误），认为 Display 存在
                    if result and "Error:" not in result and "not found" not in result.lower():
                        tv_displays.append(test_id)
                except Exception:
                    continue
            
            # 如果检测到任何 Display，使用它们
            if tv_displays:
                self._tv_display_ids_cache = tv_displays
                self._tv_display_ids_cache_time = current_time
                return tv_displays
            
            # 默认：假设 Display 1 存在（最常见的配置）
            logger.info("[ScreenAnalyzer] 无法自动检测，使用默认 Display ID: 1")
            self._tv_display_ids_cache = [1]
            self._tv_display_ids_cache_time = current_time
            return [1]
            
        except Exception as e:
            logger.warning("[ScreenAnalyzer] Display 检测失败: %s，使用默认 Display ID: 1", e)
            self._tv_display_ids_cache = [1]
            self._tv_display_ids_cache_time = current_time
            return [1]

    def _is_black_screen(self, img: Image.Image, threshold: int = 5) -> bool:
        """
        判断是否黑屏
        threshold: 平均亮度阈值 (0-255)，调低阈值以减少误报
        """
        # 转换为灰度
        gray_img = img.convert("L")
        stat = ImageStat.Stat(gray_img)
        avg_brightness = stat.mean[0]
        
        # 如果平均亮度极低，认为是黑屏
        return avg_brightness < threshold

    def _is_same_image(self, current_img: Image.Image, last_img_path: str, diff_threshold: int = 1.0) -> bool:
        """
        判断两张图片是否几乎一致
        diff_threshold: 1.0 (极低阈值，只有几乎完全一样才算静止)
        """
        try:
            last_img = Image.open(last_img_path).convert("RGB")
            
            # 尺寸必须一致
            if current_img.size != last_img.size:
                return False
                
            # 计算差异
            diff = ImageChops.difference(current_img, last_img)
            stat = ImageStat.Stat(diff)
            
            # 平均差异值
            avg_diff = sum(stat.mean) / len(stat.mean)
            
            # Debug: 打印差异值，方便调试
            # print(f"[ImageDiff] Diff: {avg_diff:.2f} (Threshold: {diff_threshold})")
            
            # 如果差异极小，认为是同一画面
            return avg_diff < diff_threshold
        except Exception as e:
            logger.warning("[ImageDiff] Error: %s", e)
            return False
