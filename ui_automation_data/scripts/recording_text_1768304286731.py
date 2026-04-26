"""
自动生成的UI自动化脚本
录制ID: recording_text_1768304286731
设备ID: 192.168.16.105:8787
应用包名: 
创建时间: 2026-01-13T19:38:06.986533
描述: selector check
"""

import uiautomator2 as u2
import time

d = u2.connect("192.168.16.105:8787")

# 开始执行
try:
    # Step 1: probe
    d.click(360, 640)
    time.sleep(1.0)

    print("脚本执行完成")
except Exception as e:
    print(f"脚本执行失败: {e}")
    raise