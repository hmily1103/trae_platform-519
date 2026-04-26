#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键修复 ADB 权限和环境脚本
用于解决 Root 权限和 Display 检测问题
"""

import subprocess
import sys
import platform
import time

def run_adb_command(cmd, device_id=None, timeout=10):
    """执行 ADB 命令"""
    adb_cmd = ["adb"]
    if device_id:
        adb_cmd.extend(["-s", device_id])
    adb_cmd.extend(cmd)
    
    creation_flags = 0
    if platform.system() == 'Windows':
        creation_flags = subprocess.CREATE_NO_WINDOW
    
    try:
        result = subprocess.run(
            adb_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            errors='ignore',
            creationflags=creation_flags,
            shell=False
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return False, "", str(e)

def list_devices():
    """列出所有设备"""
    success, stdout, stderr = run_adb_command(["devices", "-l"])
    if success:
        devices = []
        for line in stdout.splitlines()[1:]:
            if line.strip() and "\tdevice" in line:
                device_id = line.split()[0]
                devices.append(device_id)
        return devices
    return []

def fix_adb_root(device_id):
    """尝试激活 ADB Root 模式"""
    print(f"\n[1/4] 尝试激活 ADB Root 模式 (设备: {device_id})...")
    
    # 方法1: adb root
    success, stdout, stderr = run_adb_command(["root"], device_id=device_id, timeout=5)
    if success:
        print("  [OK] adb root 执行成功")
        # 等待设备重启 ADB 服务
        print("  等待设备重启 ADB 服务（5秒）...")
        time.sleep(5)
        
        # 重新连接
        if ":" in device_id:
            ip, port = device_id.split(":")
            run_adb_command(["connect", f"{ip}:{port}"], timeout=3)
        
        return True
    else:
        print(f"  [WARN] adb root 失败: {stderr or stdout}")
        return False

def fix_adb_remount(device_id):
    """尝试重新挂载系统分区为可写"""
    print(f"\n[2/4] 尝试重新挂载系统分区 (设备: {device_id})...")
    
    success, stdout, stderr = run_adb_command(["remount"], device_id=device_id, timeout=5)
    if success:
        print("  [OK] adb remount 执行成功")
        return True
    else:
        print(f"  [WARN] adb remount 失败: {stderr or stdout} (可能不需要)")
        return False

def test_root_permission(device_id):
    """测试 Root 权限"""
    print(f"\n[3/4] 测试 Root 权限 (设备: {device_id})...")
    
    # 方法1: su 0 id
    success, stdout, stderr = run_adb_command(["shell", "su", "0", "id"], device_id=device_id, timeout=8)
    if success and "uid=0" in (stdout + stderr):
        print("  [OK] Root 权限可用 (方法1: su 0 id)")
        return True
    
    # 方法2: su -c id
    success, stdout, stderr = run_adb_command(["shell", "su", "-c", "id"], device_id=device_id, timeout=5)
    if success and "uid=0" in (stdout + stderr):
        print("  [OK] Root 权限可用 (方法2: su -c id)")
        return True
    
    print(f"  [FAIL] Root 权限不可用")
    print(f"     stdout: {stdout[:200]}")
    print(f"     stderr: {stderr[:200]}")
    print(f"\n  提示:")
    print(f"    1. 请检查设备是否已 Root")
    print(f"    2. 首次运行时，设备屏幕可能会弹出权限确认对话框，请点击'允许'")
    print(f"    3. 可以手动执行: adb -s {device_id} shell su")
    return False

def test_display_detection(device_id):
    """测试 Display 检测"""
    print(f"\n[4/4] 测试 Display 检测 (设备: {device_id})...")
    
    # 方法1: dumpsys display
    success, stdout, stderr = run_adb_command(["shell", "dumpsys", "display"], device_id=device_id, timeout=10)
    if success:
        import re
        displays = []
        for line in stdout.splitlines():
            matches = re.findall(r'(?:Display|display)[\s_]*id[\s=:]*(\d+)', line, re.IGNORECASE)
            for match in matches:
                did = int(match)
                if did > 0:
                    displays.append(did)
        
        if displays:
            print(f"  [OK] 检测到 Display IDs: {sorted(set(displays))}")
            return True
        else:
            print("  [WARN] 未在 dumpsys display 中检测到 Display ID")
    else:
        print(f"  [WARN] dumpsys display 失败: {stderr[:200]}")
    
    # 方法2: 直接测试 Display 1
    print("  尝试直接测试 Display 1...")
    success, stdout, stderr = run_adb_command(
        ["shell", "dumpsys", "SurfaceFlinger", "--display-id", "1"],
        device_id=device_id,
        timeout=3
    )
    if success:
        print("  [OK] Display 1 可用")
        return True
    else:
        print(f"  [WARN] Display 1 测试失败: {stderr[:200] if stderr else stdout[:200]}")
        print(f"\n  提示:")
        print(f"    1. 电视端 Display 可能未激活，请确保 KTV 应用已启动")
        print(f"    2. 某些设备使用自定义 Display ID，可以手动指定")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("ADB 权限和环境修复工具")
    print("=" * 60)
    
    # 获取设备ID
    device_id = None
    if len(sys.argv) > 1:
        device_id = sys.argv[1]
    else:
        # 自动检测设备
        devices = list_devices()
        if not devices:
            print("[FAIL] 未检测到任何设备")
            print("   请确保设备已连接，或手动指定设备ID:")
            print("   python fix_adb_permissions.py <device_id>")
            return
        
        # 优先选择 IP 格式的设备
        ip_devices = [d for d in devices if "." in d and ":" in d]
        if ip_devices:
            device_id = ip_devices[0]
            print(f"自动选择设备: {device_id}")
        elif len(devices) == 1:
            device_id = devices[0]
            print(f"自动使用唯一设备: {device_id}")
        else:
            print(f"检测到多个设备: {devices}")
            print("请手动指定设备ID:")
            print(f"   python fix_adb_permissions.py <device_id>")
            return
    
    print(f"\n目标设备: {device_id}")
    print("-" * 60)
    
    # 1. 尝试激活 ADB Root
    fix_adb_root(device_id)
    
    # 2. 尝试重新挂载
    fix_adb_remount(device_id)
    
    # 3. 测试 Root 权限
    root_ok = test_root_permission(device_id)
    
    # 4. 测试 Display 检测
    display_ok = test_display_detection(device_id)
    
    # 总结
    print("\n" + "=" * 60)
    print("修复结果总结:")
    print("=" * 60)
    print(f"Root 权限: {'[OK] 可用' if root_ok else '[FAIL] 不可用'}")
    print(f"Display 检测: {'[OK] 可用' if display_ok else '[FAIL] 不可用'}")
    
    if not root_ok:
        print("\n[WARN] Root 权限不可用时，仍可使用'极低功耗模式'进行监控")
        print("   （仅监控 FPS 和日志卡顿，不需要 Root 权限）")
    
    if not display_ok:
        print("\n[WARN] Display 检测失败时，可以手动选择 Display 1 进行测试")

if __name__ == "__main__":
    main()
