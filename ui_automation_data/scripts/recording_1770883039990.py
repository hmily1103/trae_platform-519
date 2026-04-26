"""
自动生成的UI自动化脚本
录制ID: recording_1770883039990
设备ID: 192.168.16.105:8787
应用包名: 
创建时间: 2026-02-12T15:57:20.038876
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
    # Step 1: 点击控件 'com.thunder.ktv:id/add_to_top'
    try:
        d(resourceId="com.thunder.ktv:id/add_to_top").click()
    except Exception:
        try:
            d(text="置顶").click()
        except Exception:
            d.click(634, 177)
    time.sleep(1.0)

    # Step 2: 点击控件 'com.thunder.ktv:id/textview_by_singer'
    try:
        d(resourceId="com.thunder.ktv:id/textview_by_singer").click()
    except Exception:
        try:
            d(text="张芸京").click()
        except Exception:
            d.click(207, 174)
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