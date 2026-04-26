@echo off
chcp 65001 >nul
echo ========================================
echo Trae Platform 启动脚本
echo ========================================
cd /d %~dp0
echo 当前目录: %CD%
echo.
echo 正在启动服务器...
echo.
python app.py
if errorlevel 1 (
    echo.
    echo ========================================
    echo 启动失败！请检查错误信息
    echo ========================================
    pause
)

