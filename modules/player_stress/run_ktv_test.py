#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
KTV播放器压测 - 一键启动脚本
版本: V2.3 - 电视端专项卡顿检测
功能: 自动检查环境、检测Display配置、启动压测
"""

import json
import os
import sys
import subprocess
import time
from pathlib import Path

# 添加项目路径到 sys.path
SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 导入核心模块
try:
    from trae_platform.modules.player_stress.core.runner import TestRunner
    from trae_platform.modules.player_stress.core.adb_manager import AdbManager
except ImportError:
    # 如果导入失败，尝试相对导入
    sys.path.insert(0, str(SCRIPT_DIR))
    from core.runner import TestRunner
    from core.adb_manager import AdbManager


def print_section(title):
    """打印分节标题"""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def check_adb():
    """检查 ADB 环境"""
    print("\n[1/6] 检查 ADB 环境...")
    try:
        result = subprocess.run(
            ["adb", "version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            print(f"  ✅ ADB 已安装: {version_line}")
            return True
        else:
            print("  ❌ ADB 命令执行失败")
            return False
    except FileNotFoundError:
        print("  ❌ 未找到 ADB，请确保 ADB 已添加到 PATH 环境变量")
        return False
    except Exception as e:
        print(f"  ❌ ADB 检查失败: {e}")
        return False


def check_devices():
    """检查设备连接"""
    print("\n[2/6] 检查设备连接...")
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            print("  ❌ 无法获取设备列表")
            return False
        
        lines = result.stdout.strip().split('\n')[1:]  # 跳过第一行标题
        devices = [line.split('\t')[0] for line in lines if line.strip() and '\t' in line]
        
        if not devices:
            print("  ❌ 未检测到已连接的设备")
            print("  提示: 请确保设备已通过 USB 或网络 ADB 连接")
            return False
        
        print(f"  ✅ 检测到 {len(devices)} 个设备:")
        for device in devices:
            print(f"     - {device}")
        return True
    except Exception as e:
        print(f"  ❌ 设备检查失败: {e}")
        return False


def check_root_permission():
    """检查 Root 权限（用于硬件解码监控）"""
    print("\n[3/6] 检查 Root 权限（用于硬件解码监控）...")
    try:
        # 方法1: 尝试 su 0 id
        result = subprocess.run(
            ["adb", "shell", "su", "0", "id"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if "uid=0" in result.stdout:
            print("  ✅ Root 权限确认成功（su 0）")
            return True
        
        # 方法2: 尝试 su -c id
        result = subprocess.run(
            ["adb", "shell", "su", "-c", "id"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if "uid=0" in result.stdout:
            print("  ✅ Root 权限确认成功（su -c）")
            return True
        
        # 方法3: 尝试直接访问 MPP 节点
        result = subprocess.run(
            ["adb", "shell", "su", "0", "ls", "/sys/kernel/debug/mpp_service/stats"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and "No such file" not in result.stdout:
            print("  ✅ Root 权限确认成功（可访问 MPP 节点）")
            return True
        
        print("  ⚠️  设备未开启 Root 权限或不支持 'su 0'")
        print("  提示: 硬件解码监控（work_count 增量检测）将无法使用")
        print("  建议: 开启 Root 权限以获得完整的卡顿检测能力")
        return False
    except Exception as e:
        print(f"  ⚠️  Root 权限检查失败: {e}")
        print("  提示: 硬件解码监控可能无法使用")
        return False


def detect_tv_displays():
    """自动检测电视端 Display ID"""
    print("\n[4/6] 自动检测电视端 Display ID...")
    try:
        result = subprocess.run(
            ["adb", "shell", "dumpsys", "display"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            print("  ⚠️  无法获取 Display 信息")
            return []
        
        import re
        display_ids = set()
        
        # 查找所有 Display ID
        for line in result.stdout.split('\n'):
            # 匹配 "Display id=1" 或 "Display 1:" 或 "mDisplayId=1" 等格式
            matches = re.findall(
                r'Display\s+(?:id=)?(\d+)|mDisplayId[=:](\d+)',
                line,
                re.IGNORECASE
            )
            for match in matches:
                display_id = int(match[0] or match[1])
                if display_id > 0:  # 排除 Display 0（点歌屏）
                    display_ids.add(display_id)
        
        if display_ids:
            tv_displays = sorted(display_ids)
            print(f"  ✅ 检测到电视端 Display ID: {tv_displays}")
            return tv_displays
        else:
            print("  ⚠️  未检测到电视端 Display（Display 1/2）")
            print("  提示: 系统将使用默认 Display ID: 1")
            return [1]
    except Exception as e:
        print(f"  ⚠️  Display 检测失败: {e}")
        print("  提示: 系统将使用默认 Display ID: 1")
        return [1]


def check_surfaceflinger():
    """检查 SurfaceFlinger 配置（用于 FPS 获取）"""
    print("\n[5/6] 检查 SurfaceFlinger 配置（用于 FPS 获取）...")
    try:
        # 检查 SurfaceFlinger 列表
        result = subprocess.run(
            ["adb", "shell", "dumpsys", "SurfaceFlinger", "--list"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0 and result.stdout:
            surfaces = result.stdout.split('\n')
            print(f"  ✅ SurfaceFlinger 可用，检测到 {len(surfaces)} 个 Surface")
            
            # 查找可能的视频 Surface
            video_keywords = ['video', 'media', 'SurfaceView', 'TextureView', 'Secondary', 'External', 'HDMI']
            found_video_surfaces = []
            for surface in surfaces:
                surface_lower = surface.lower()
                if any(keyword.lower() in surface_lower for keyword in video_keywords):
                    found_video_surfaces.append(surface.strip())
            
            if found_video_surfaces:
                print(f"  ✅ 检测到可能的视频 Surface ({len(found_video_surfaces)} 个):")
                for surface in found_video_surfaces[:3]:  # 只显示前3个
                    print(f"     - {surface[:80]}...")
            else:
                print("  ⚠️  未检测到明显的视频 Surface")
                print("  提示: 如果无法获取视频 FPS，可能需要调整 Surface 过滤关键字")
        else:
            print("  ⚠️  SurfaceFlinger 查询失败")
    except Exception as e:
        print(f"  ⚠️  SurfaceFlinger 检查失败: {e}")


def load_config():
    """加载配置文件"""
    print("\n[6/6] 加载配置文件...")
    
    config_path = SCRIPT_DIR / "config.json"
    example_path = SCRIPT_DIR / "config.json.example"
    
    if not config_path.exists():
        if example_path.exists():
            print(f"  ⚠️  配置文件不存在: {config_path}")
            print(f"  提示: 请复制 {example_path} 为 config.json 并修改参数")
            print(f"  命令: copy {example_path} {config_path}")
            return None
        else:
            print(f"  ❌ 配置文件不存在: {config_path}")
            return None
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        print(f"  ✅ 配置文件加载成功: {config_path}")
        return config
    except json.JSONDecodeError as e:
        print(f"  ❌ 配置文件格式错误: {e}")
        return None
    except Exception as e:
        print(f"  ❌ 配置文件加载失败: {e}")
        return None


def main():
    """主函数"""
    print_section("KTV播放器压测系统 - 一键启动脚本")
    print("版本: V2.3 (电视端专项卡顿检测)")
    print("=" * 60)
    
    # 环境检查
    if not check_adb():
        print("\n❌ 环境检查失败，请先安装 ADB")
        sys.exit(1)
    
    if not check_devices():
        print("\n❌ 设备检查失败，请确保设备已连接")
        sys.exit(1)
    
    root_available = check_root_permission()
    tv_displays = detect_tv_displays()
    check_surfaceflinger()
    
    # 加载配置
    config = load_config()
    if not config:
        print("\n❌ 配置文件加载失败")
        sys.exit(1)
    
    # 如果没有指定设备ID，自动选择第一个设备
    if not config.get('device_id'):
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')[1:]
                devices = [line.split('\t')[0] for line in lines if line.strip() and '\t' in line]
                if devices:
                    config['device_id'] = devices[0]
                    print(f"\n✅ 自动选择设备: {config['device_id']}")
                else:
                    print("\n⚠️  未找到可用设备")
            else:
                print("\n⚠️  无法获取设备列表")
        except Exception as e:
            print(f"\n⚠️  自动选择设备失败: {e}")
            print("  提示: 请在 config.json 中手动指定 device_id")
    
    # 显示配置摘要
    print_section("配置摘要")
    print(f"设备ID: {config.get('device_id', '未指定')}")
    print(f"包名: {config.get('target_app', {}).get('package_name', '未指定')}")
    print(f"测试模式: {config.get('test_strategy', {}).get('mode', '未指定')}")
    print(f"测试时长: {config.get('test_strategy', {}).get('duration_minutes', 0)} 分钟")
    print(f"采集间隔: {config.get('monitor', {}).get('interval_seconds', 2)} 秒")
    print(f"电视端 Display: {tv_displays}")
    print(f"Root 权限: {'可用' if root_available else '不可用'}")
    
    # 确认启动
    print_section("准备启动")
    print("按 Enter 键开始测试，或按 Ctrl+C 取消...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n\n测试已取消")
        sys.exit(0)
    
    # 启动测试
    print_section("开始测试")
    print("提示: 按 Ctrl+C 可随时停止测试")
    print("=" * 60)
    
    try:
        runner = TestRunner(config, logger_callback=lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}"))
        runner.run()
    except KeyboardInterrupt:
        print("\n\n正在停止测试...")
        if 'runner' in locals():
            runner.stop()
        print("测试已手动停止，正在生成报告...")
    except Exception as e:
        print(f"\n❌ 测试运行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    print_section("测试完成")
    report_dir = config.get('report', {}).get('output_dir', 'reports')
    print(f"报告已生成，请查看: {SCRIPT_DIR / report_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
