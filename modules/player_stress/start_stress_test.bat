@echo off
REM ============================================
REM KTV播放器压测 - 自动化启动脚本 (Windows)
REM 版本: V2.3 - 电视端专项卡顿检测
REM ============================================

chcp 65001 >nul
echo.
echo ============================================
echo   KTV播放器压测系统 - 启动脚本
echo   版本: V2.3 (电视端专项卡顿检测)
echo ============================================
echo.

REM 检查Python环境
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到Python环境，请先安装Python 3.7+
    pause
    exit /b 1
)

echo [1/5] 检查Python环境...
python --version

REM 检查ADB环境
adb version >nul 2>&1
if errorlevel 1 (
    echo [警告] 未检测到ADB，请确保ADB已添加到PATH环境变量
    echo 继续执行...
) else (
    echo [2/5] 检查ADB环境...
    adb version | findstr "Version"
)

REM 检查设备连接
echo.
echo [3/5] 检查设备连接...
adb devices
echo.

REM 检查Root权限（可选，用于硬件解码监控）
echo [4/5] 检查Root权限（用于硬件解码监控）...
adb shell "su -c 'ls /sys/kernel/debug/mpp_service/stats 2>&1'" >nul 2>&1
if errorlevel 1 (
    echo [提示] 设备可能未开启Root权限，硬件解码监控可能无法使用
    echo         建议：开启Root权限以获得完整的卡顿检测能力
) else (
    echo [成功] Root权限检测通过，硬件解码监控可用
)

REM 检查Display配置
echo.
echo [5/5] 检查Display配置...
echo 正在检测电视端Display ID...
adb shell "dumpsys display | grep -i 'Display id=' | head -5"
echo.

REM 切换到项目目录
cd /d "%~dp0"
cd ..\..\..

REM 启动Flask服务器
echo ============================================
echo   正在启动压测系统...
echo ============================================
echo.
echo [提示] 服务器启动后，请在浏览器中访问：
echo        http://localhost:5000/player_stress/
echo.
echo [提示] 按 Ctrl+C 可停止服务器
echo.

python trae_platform\app.py

pause
