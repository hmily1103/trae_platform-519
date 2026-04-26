"""
自动生成的UI自动化脚本
录制ID: recording_1769507087062
设备ID: 192.168.16.114:8787
应用包名: 
创建时间: 2026-01-27T17:44:47.062577
描述: 
"""

import uiautomator2 as u2
import time

d = u2.connect("192.168.16.114:8787")

# 开始执行
try:
    # Step 1: 点击 (448, 356)
    try:
        d(resourceId="com.thunder.ktv:id/singer_rv").click()
    except Exception:
        d.click(448, 356)
    time.sleep(1.0)

    # Step 2: 点击 (308, 220)
    try:
        d(resourceId="com.thunder.ktv:id/singer_rv").click()
    except Exception:
        d.click(308, 220)
    time.sleep(1.0)

    print("脚本执行完成")
except Exception as e:
    print(f"脚本执行失败: {e}")
    raise