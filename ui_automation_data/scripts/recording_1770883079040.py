"""
自动生成的UI自动化脚本
录制ID: recording_1770883079040
设备ID: 192.168.16.105:8787
应用包名: 
创建时间: 2026-02-12T15:57:59.060915
描述: 
"""

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