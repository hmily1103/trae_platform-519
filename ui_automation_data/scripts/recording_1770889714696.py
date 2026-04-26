"""
自动生成的UI自动化脚本
录制ID: recording_1770889714696
设备ID: 192.168.16.105:8787
应用包名: 
创建时间: 2026-02-12T17:48:34.700142
描述: 
"""

# Fix for "Invalid version: ''" error
try:
    import packaging.version
    original_parse = packaging.version.parse
    def safe_parse(version):
        if not version or version == "":
            return original_parse("0.0.0")
        return original_parse(version)
    packaging.version.parse = safe_parse
except ImportError:
    pass

import uiautomator2 as u2
import time
import re
import os

d = u2.connect("192.168.16.105:8787")

# 截图保存路径
screenshot_dir = os.path.join(os.getcwd(), "trae_platform", "static", "ui_automation", "screenshots")
if not os.path.exists(screenshot_dir):
    os.makedirs(screenshot_dir)

# 设置全局隐式等待 (10秒)
d.implicitly_wait(10.0)

# 开始执行
try:
    # Step 1: 点击 (197, 264) (Error: 'UISelector' object has no attribute 'text')
    d.click(197, 264)
    time.sleep(1.0)

    print("脚本执行完成")
except Exception as e:
    print(f"脚本执行失败: {e}")
    # 尝试截图
    try:
        timestamp = int(time.time())
        filename = f"error_{timestamp}_192.168.16.105_8787.jpg"
        filepath = os.path.join(screenshot_dir, filename)
        d.screenshot(filepath)
        print(f"ERROR_SCREENSHOT: {{filename}}")
    except Exception as s_e:
        print(f"截图失败: {s_e}")
    raise