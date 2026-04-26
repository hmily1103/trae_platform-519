"""
自动生成的UI自动化脚本
录制ID: recording_1769508565085
设备ID: 192.168.16.114:8787
应用包名: 
创建时间: 2026-01-27T18:09:25.087491
描述: 
"""

import uiautomator2 as u2
import time

d = u2.connect("192.168.16.114:8787")

# 开始执行
try:
    # Step 1: 点击 (527, 480)
    d.click(527, 480)
    time.sleep(1.0)

    # Step 2: 点击 (212, 570)
    d.click(212, 570)
    time.sleep(1.0)

    # Step 3: 点击 (493, 582)
    d.click(493, 582)
    time.sleep(1.0)

    # Step 4: 点击 (583, 580)
    d.click(583, 580)
    time.sleep(1.0)

    print("脚本执行完成")
except Exception as e:
    print(f"脚本执行失败: {e}")
    raise