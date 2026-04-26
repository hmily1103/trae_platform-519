"""
自动生成的UI自动化脚本
录制ID: recording_1768301814732
设备ID: 192.168.16.105:8787
应用包名: 
创建时间: 2026-01-13T18:56:54.987985
描述: 
"""

import uiautomator2 as u2
import time

d = u2.connect("192.168.16.105:8787")

# 开始执行
try:
    # Step 1: 点击 (75, 767)
    d.click(75, 767)
    time.sleep(1.0)

    # Step 2: 点击 (72, 58)
    d.click(72, 58)
    time.sleep(1.0)

    # Step 3: 点击 (472, 414)
    d.click(472, 414)
    time.sleep(1.0)

    print("脚本执行完成")
except Exception as e:
    print(f"脚本执行失败: {e}")
    raise