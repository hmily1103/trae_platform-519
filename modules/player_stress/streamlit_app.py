#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
KTV播放器压测 - Streamlit Web界面
版本: V2.3.1 - 优化版
功能: 可视化配置、实时监控、报告查看
"""

import streamlit as st
import subprocess
import json
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime
import pandas as pd

# 添加项目路径
SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from trae_platform.modules.player_stress.core.runner import TestRunner
    from trae_platform.modules.player_stress.core.adb_manager import AdbManager
    from trae_platform.modules.player_stress.core.monitor import PerformanceMonitor
    from trae_platform.modules.player_stress.core.rk_monitor import RkMonitor
except ImportError:
    sys.path.insert(0, str(SCRIPT_DIR))
    from core.runner import TestRunner
    from core.adb_manager import AdbManager
    from core.monitor import PerformanceMonitor
    from core.rk_monitor import RkMonitor

# 页面配置
st.set_page_config(
    page_title="KTV播放器电视端监控系统",
    page_icon="📺",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# 全局状态初始化
if 'test_running' not in st.session_state:
    st.session_state.test_running = False
if 'test_runner' not in st.session_state:
    st.session_state.test_runner = None
if 'test_thread' not in st.session_state:
    st.session_state.test_thread = None
if 'log_buffer' not in st.session_state:
    st.session_state.log_buffer = []
if 'last_refresh_time' not in st.session_state:
    st.session_state.last_refresh_time = time.time()
if 'auto_refresh_interval' not in st.session_state:
    st.session_state.auto_refresh_interval = 3  # 3秒自动刷新

# 工具函数
def get_connected_devices():
    """获取已连接的设备列表"""
    return AdbManager.list_devices()

def check_root_permission():
    """检查Root权限"""
    try:
        result = subprocess.run(
            ["adb", "shell", "su", "0", "id"],
            capture_output=True,
            text=True,
            timeout=3
        )
        return "uid=0" in result.stdout
    except Exception:
        return False

def detect_tv_displays():
    """检测电视端Display ID"""
    try:
        result = subprocess.run(
            ["adb", "shell", "dumpsys", "display"],
            capture_output=True,
            text=True,
            timeout=5
        )
        displays = []
        for line in result.stdout.splitlines():
            if "Display id=1" in line or "mDisplayId=1" in line:
                displays.append(1)
            if "Display id=2" in line or "mDisplayId=2" in line:
                displays.append(2)
        return list(set(displays)) if displays else [1]
    except Exception:
        return [1]

def logger_callback(log_entry):
    """日志回调函数"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    log_msg = f"[{timestamp}] {log_entry}"
    st.session_state.log_buffer.append(log_msg)
    # 只保留最近200条日志
    if len(st.session_state.log_buffer) > 200:
        st.session_state.log_buffer.pop(0)

def run_test_background(config):
    """后台运行测试"""
    try:
        logger_callback("📋 正在准备测试环境...")
        
        # 确保报告目录存在
        report_dir = Path(config.get('report', {}).get('output_dir', 'reports'))
        if not report_dir.is_absolute():
            report_dir = SCRIPT_DIR / report_dir
        report_dir.mkdir(parents=True, exist_ok=True)
        config['report']['output_dir'] = str(report_dir)
        logger_callback(f"📁 报告目录: {report_dir}")
        
        logger_callback("🔌 正在连接设备...")
        runner = TestRunner(config, logger_callback=logger_callback)
        st.session_state.test_runner = runner
        logger_callback("✅ 设备连接成功，开始监控...")
        
        runner.run()
        logger_callback("✅ 测试运行完成，正在生成报告...")
        
        # 确保报告已生成
        if hasattr(runner, 'last_summary_file') and runner.last_summary_file:
            logger_callback(f"📄 报告已生成: {runner.last_summary_file}")
        if hasattr(runner, 'last_csv_file') and runner.last_csv_file:
            logger_callback(f"📊 CSV数据已生成: {runner.last_csv_file}")
            
    except Exception as e:
        logger_callback(f"❌ 测试运行失败: {e}")
        import traceback
        error_trace = traceback.format_exc()
        logger_callback(f"错误详情:\n{error_trace}")
    finally:
        # 确保状态更新
        st.session_state.test_running = False
        logger_callback("🏁 测试已结束")
        logger_callback("💡 提示: 切换到'报告查看'标签页查看最新报告")

# ==================== 主界面 ====================
st.title("📺 KTV播放器电视端监控系统")
st.caption("版本: V2.3.1 - 优化版 | 电视端专项卡顿检测")

# 顶部状态栏
status_col1, status_col2, status_col3, status_col4 = st.columns([2, 1, 1, 1])

with status_col1:
    if st.session_state.test_running:
        st.success("🟢 **测试运行中** - 请查看监控标签页")
    else:
        st.info("⚪ **测试未运行** - 请先在配置标签页启动测试")

with status_col2:
    devices = get_connected_devices()
    if devices:
        st.success(f"✅ {len(devices)} 个设备已连接")
    else:
        st.warning("⚠️ 未检测到设备")

with status_col3:
    if st.session_state.test_runner:
        st.info("📊 测试实例已就绪")
    else:
        st.caption("等待测试启动")

with status_col4:
    if st.button("🔄 手动刷新"):
        st.rerun()

st.divider()

# 使用标签页组织功能
tab1, tab2, tab3, tab4 = st.tabs(["⚙️ 配置与启动", "📊 实时监控", "📄 报告查看", "ℹ️ 使用说明"])

# ==================== 标签页1: 配置与启动 ====================
with tab1:
    st.header("📝 测试配置")
    
    config_col1, config_col2 = st.columns([1, 1])
    
    with config_col1:
        st.subheader("🔌 设备与应用")
        devices = get_connected_devices()
        if devices:
            selected_device = st.selectbox(
                "选择设备",
                options=devices,
                index=0,
                help="选择要监控的设备"
            )
        else:
            st.warning("⚠️ 未检测到设备，请确保设备已连接")
            selected_device = None
        
        package_name = st.text_input(
            "播放器包名",
            value="com.thunder.ktv:media",
            help="例如: com.thunder.ktv:media"
        )
        
        # 环境检查
        st.subheader("🔍 环境检查")
        env_col1, env_col2 = st.columns(2)
        
        with env_col1:
            root_available = check_root_permission()
            if root_available:
                st.success("✅ Root权限可用")
            else:
                st.warning("⚠️ Root权限不可用")
        
        with env_col2:
            available_displays = detect_tv_displays()
            if available_displays:
                st.info(f"📺 Display {', '.join(map(str, available_displays))}")
            else:
                st.warning("⚠️ 未检测到电视端")
        
        # 监控目标屏
        display_options = {0: "Display 0 (点歌屏)", 1: "Display 1 (电视屏)"}
        if 2 in available_displays:
            display_options[2] = "Display 2 (电视屏2)"
        
        target_displays = st.multiselect(
            "监控目标屏",
            options=list(display_options.keys()),
            default=[1] if 1 in available_displays else available_displays[:1],
            format_func=lambda x: display_options[x],
            help="选择要监控的显示屏，建议只监控电视屏（Display 1/2）"
        )
    
    with config_col2:
        st.subheader("⚡ 监控模式")
        
        performance_mode = st.radio(
            "监控强度",
            options=["极低功耗", "标准模式", "深度压测"],
            index=0,
            help="""
            - **极低功耗** (推荐): 仅监控RK硬件节点，**零性能影响**
            - **标准模式**: 增加FPS采集和30s间隔截图
            - **深度压测**: 高频截图+模拟点歌，**仅用于复现特定问题**
            """
        )
        
        # 根据性能模式设置参数
        if performance_mode == "极低功耗":
            interval_seconds = 5
            enable_screenshot = False
            enable_fps = False
            st.success("✅ **纯净监控模式**: 零性能影响，100%排除工具干扰")
        elif performance_mode == "标准模式":
            interval_seconds = st.slider("采样间隔（秒）", 1, 10, 5, 1, help="1秒可捕捉瞬时卡顿，建议排查问题时使用")
            enable_screenshot = True
            enable_fps = True
            st.info("⚠️ **标准模式**: 已启用FPS和截图")
        else:
            interval_seconds = 1
            enable_screenshot = True
            enable_fps = True
            st.warning("🚨 **深度压测**: 高频监控，可能影响性能")
        
        st.subheader("📋 测试策略")
        
        test_mode = st.selectbox(
            "测试模式",
            options=["monitor_only", "fixed_skip", "loop_playback"],
            index=0,
            format_func=lambda x: {
                "monitor_only": "纯监控（不干扰播放）",
                "fixed_skip": "固定间隔切歌",
                "loop_playback": "循环播放"
            }[x]
        )
        
        duration_minutes = st.slider(
            "监控时长（分钟）",
            min_value=10,
            max_value=1440,
            value=60,
            step=10
        )
        
        if test_mode != "monitor_only":
            skip_interval = st.slider("切歌间隔（秒）", 30, 600, 300, 30)
        else:
            skip_interval = 300
    
    # HTTP VOD配置（可选）
    with st.expander("🌐 HTTP点歌配置（可选）", expanded=False):
        enable_http_vod = st.checkbox("启用HTTP点歌", value=False)
        if enable_http_vod:
            vod_col1, vod_col2 = st.columns(2)
            with vod_col1:
                server_ip = st.text_input("服务器IP", value="192.168.1.100")
                stb_ip = st.text_input("机顶盒IP", value="192.168.1.101")
            with vod_col2:
                music_list = st.text_input("歌曲ID列表", value="1001,1002,1003", help="逗号分隔")
    
    st.divider()
    
    # 启动/停止控制
    st.subheader("🚀 测试控制")
    control_col1, control_col2, control_col3 = st.columns([1, 1, 2])
    
    with control_col1:
        start_disabled = st.session_state.test_running or not selected_device or not package_name
        if st.button("▶️ 开始监控", type="primary", disabled=start_disabled, use_container_width=True):
            # 构建配置
            config = {
                "device_id": selected_device,
                "target_app": {"package_name": package_name},
                "test_strategy": {
                    "mode": test_mode,
                    "duration_minutes": duration_minutes,
                    "skip_interval_seconds": skip_interval
                },
                "monitor": {
                    "interval_seconds": interval_seconds,
                    "enable_screenshot": enable_screenshot,
                    "enable_fps": enable_fps
                },
                "report": {"output_dir": "reports"}
            }
            
            if enable_http_vod:
                config["http_vod"] = {
                    "server_ip": server_ip,
                    "stb_ip": stb_ip,
                    "music_list": music_list.split(",")
                }
            
            # 启动测试线程
            st.session_state.test_running = True
            logger_callback("🚀 正在启动测试...")
            logger_callback(f"设备: {selected_device}, 包名: {package_name}")
            logger_callback(f"监控模式: {performance_mode}, 采样间隔: {interval_seconds}秒")
            
            test_thread = threading.Thread(
                target=run_test_background,
                args=(config,),
                daemon=True
            )
            test_thread.start()
            st.session_state.test_thread = test_thread
            st.success("✅ 测试正在启动，请切换到'实时监控'标签页查看状态...")
            st.rerun()
    
    with control_col2:
        if st.button("⏹️ 停止测试", disabled=not st.session_state.test_running, use_container_width=True):
            if st.session_state.test_runner:
                if hasattr(st.session_state.test_runner, 'stop_flag'):
                    st.session_state.test_runner.stop_flag = True
                if hasattr(st.session_state.test_runner, 'stop'):
                    st.session_state.test_runner.stop()
            st.session_state.test_running = False
            logger_callback("用户手动停止测试")
            st.warning("⏸️ 正在停止测试...")
            st.rerun()
    
    with control_col3:
        if st.session_state.test_running:
            st.caption("💡 测试运行中，请切换到'实时监控'标签页查看详情")
        else:
            st.caption("💡 配置完成后点击'开始监控'启动测试")

# ==================== 标签页2: 实时监控 ====================
with tab2:
    if not st.session_state.test_running:
        st.info("⚠️ 请先在'配置与启动'标签页启动测试")
    else:
        st.header("📊 实时监控状态")
        
        # 实时状态看板
        if st.session_state.test_runner:
            monitor = st.session_state.test_runner.monitor
            if monitor and monitor.history:
                latest_snapshot = monitor.history[-1]
                
                # 关键指标
                metric_cols = st.columns(4)
                
                with metric_cols[0]:
                    video_fps = latest_snapshot.get('video_fps', 0)
                    st.metric("视频FPS", f"{video_fps:.1f}" if video_fps > 0 else "N/A")
                
                with metric_cols[1]:
                    work_count = latest_snapshot.get('mpp_work_count', 0)
                    st.metric("解码器工作计数", work_count)
                
                with metric_cols[2]:
                    decoder_stuck = latest_snapshot.get('decoder_stuck', False)
                    st.metric("解码器状态", "⚠️ 卡死" if decoder_stuck else "✅ 正常")
                
                with metric_cols[3]:
                    tv_stutter = latest_snapshot.get('tv_stutter_detected', False)
                    st.metric("电视端卡顿", "🚨 检测到" if tv_stutter else "✅ 正常")
                
                # 趋势图
                if len(monitor.history) > 1:
                    st.subheader("📈 趋势图")
                    df = pd.DataFrame(monitor.history[-50:])
                    
                    chart_cols = st.columns(2)
                    with chart_cols[0]:
                        if 'video_fps' in df.columns:
                            fps_data = df[df['video_fps'] > 0]['video_fps']
                            if not fps_data.empty:
                                st.line_chart(fps_data, use_container_width=True)
                                st.caption("视频FPS趋势")
                    
                    with chart_cols[1]:
                        if 'mpp_work_count' in df.columns:
                            st.line_chart(df['mpp_work_count'], use_container_width=True)
                            st.caption("解码器工作计数趋势")
            else:
                st.info("⏳ 等待数据采集...")
        
        # 实时日志
        st.subheader("📝 实时日志")
        log_container = st.container(height=400)
        with log_container:
            if st.session_state.log_buffer:
                for log in st.session_state.log_buffer[-50:]:  # 显示最近50条
                    st.text(log)
            else:
                st.info("暂无日志")
        
        # 自动刷新（测试运行中时）
        if st.session_state.test_running:
            time.sleep(st.session_state.auto_refresh_interval)
            st.rerun()

# ==================== 标签页3: 报告查看 ====================
with tab3:
    st.header("📄 测试报告")
    
    # 刷新按钮
    col_refresh, col_info = st.columns([1, 4])
    with col_refresh:
        if st.button("🔄 刷新报告列表", use_container_width=True):
            st.rerun()
    
    with col_info:
        reports_dir = SCRIPT_DIR / "reports"
        if reports_dir.exists():
            st.caption(f"报告目录: {reports_dir}")
        else:
            st.caption("报告目录不存在，测试完成后会自动创建")
    
    # 列出可用报告
    reports_dir = SCRIPT_DIR / "reports"
    if reports_dir.exists():
        txt_files = sorted(
            reports_dir.glob("summary_*.txt"),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        html_files = sorted(
            reports_dir.glob("report_*.html"),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        csv_files = sorted(
            reports_dir.glob("report_*.csv"),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        
        if txt_files or html_files or csv_files:
            st.subheader(f"📊 报告列表 (共 {len(txt_files)} 个文本报告)")
            
            # 显示文本报告
            if txt_files:
                for i, report_file in enumerate(txt_files[:10]):
                    file_time = datetime.fromtimestamp(report_file.stat().st_mtime)
                    file_size = report_file.stat().st_size / 1024  # KB
                    
                    with st.expander(f"📄 {report_file.stem} - {file_time.strftime('%Y-%m-%d %H:%M:%S')} ({file_size:.1f} KB)"):
                        try:
                            with open(report_file, 'r', encoding='utf-8') as f:
                                report_content = f.read()
                            st.text_area("报告内容", report_content, height=300, key=f"report_content_{i}")
                            
                            col_dl1, col_dl2, col_dl3 = st.columns(3)
                            with col_dl1:
                                st.download_button(
                                    "📥 下载TXT",
                                    report_content,
                                    file_name=report_file.name,
                                    mime="text/plain",
                                    key=f"download_txt_{i}"
                                )
                            
                            # 查找对应的HTML和CSV文件
                            base_name = report_file.stem.replace('summary_', 'report_')
                            html_file = reports_dir / f"{base_name}.html"
                            csv_file = reports_dir / f"{base_name}.csv"
                            
                            with col_dl2:
                                if html_file.exists():
                                    with open(html_file, 'rb') as f:
                                        st.download_button(
                                            "📥 下载HTML",
                                            f.read(),
                                            file_name=html_file.name,
                                            mime="text/html",
                                            key=f"download_html_{i}"
                                        )
                            
                            with col_dl3:
                                if csv_file.exists():
                                    with open(csv_file, 'rb') as f:
                                        st.download_button(
                                            "📥 下载CSV",
                                            f.read(),
                                            file_name=csv_file.name,
                                            mime="text/csv",
                                            key=f"download_csv_{i}"
                                        )
                        except Exception as e:
                            st.error(f"读取报告失败: {e}")
        else:
            st.info("📭 暂无报告文件")
            st.caption("提示: 运行测试后会自动生成报告")
    else:
        st.warning("📁 报告目录不存在")
        if st.button("创建报告目录"):
            reports_dir.mkdir(parents=True, exist_ok=True)
            st.success("报告目录已创建，请刷新")
            st.rerun()

# ==================== 标签页4: 使用说明 ====================
with tab4:
    st.header("ℹ️ 使用说明")
    
    st.markdown("""
    ## 📖 快速开始
    
    ### 1. 配置测试
    - 在 **"配置与启动"** 标签页中选择设备、输入包名
    - 选择监控模式（推荐：极低功耗）
    - 设置测试时长
    
    ### 2. 启动监控
    - 点击 **"开始监控"** 按钮
    - 切换到 **"实时监控"** 标签页查看状态
    
    ### 3. 查看报告
    - 测试结束后，切换到 **"报告查看"** 标签页
    - 点击报告名称查看详情
    - 可以下载报告文件
    
    ---
    
    ## ⚡ 监控模式说明
    
    ### 极低功耗（推荐）
    - 仅监控RK硬件节点
    - 零性能影响
    - 100%排除工具干扰
    
    ### 标准模式
    - 增加FPS采集和30s间隔截图
    - 轻微性能影响
    
    ### 深度压测
    - 高频截图+模拟点歌
    - 仅用于复现特定问题
    
    ---
    
    ## 🔍 故障排查
    
    - **设备连接失败**: 检查 `adb devices` 是否能检测到设备
    - **Root权限不可用**: 检查设备是否已Root
    - **没有报告**: 确保测试正常结束（不要强制关闭）
    - **页面不刷新**: 点击右上角的"手动刷新"按钮
    
    ---
    
    ## 📞 技术支持
    
    如有问题，请查看日志区域获取详细错误信息。
    """)

# 自动刷新逻辑（仅在测试运行时）
if st.session_state.test_running:
    # 检查测试线程是否还在运行
    if st.session_state.test_thread and not st.session_state.test_thread.is_alive():
        # 测试线程已结束，更新状态
        st.session_state.test_running = False
        st.rerun()
