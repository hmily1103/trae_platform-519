@echo off
chcp 65001 >nul
echo.
echo ============================================
echo   KTV播放器电视端监控系统 - Web界面启动
echo   版本: V2.3 (修复版)
echo ============================================
echo.

cd /d "%~dp0"

echo [1/4] 检查 Streamlit...
python -c "import streamlit" 2>nul
if errorlevel 1 (
    echo [错误] Streamlit 未安装
    echo [安装] 正在安装 Streamlit 和 pandas...
    pip install streamlit pandas
    if errorlevel 1 (
        echo [错误] 安装失败
        pause
        exit /b 1
    )
) else (
    echo [成功] Streamlit 已安装
)

echo.
echo [2/4] 检查端口占用...
netstat -ano | findstr :8501 >nul
if not errorlevel 1 (
    echo [警告] 端口 8501 已被占用，正在清理...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8501 ^| findstr LISTENING') do (
        taskkill /F /PID %%a >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
)

echo.
echo [3/4] 检查模块导入...
python -c "import sys; sys.path.insert(0, '..'); from core.runner import TestRunner; print('[成功] 模块导入正常')" 2>nul
if errorlevel 1 (
    echo [警告] 模块导入检查失败，但将继续启动...
)

echo.
echo [4/4] 启动 Streamlit Web 界面...
echo.
echo ============================================
echo   访问地址: http://localhost:8501
echo   按 Ctrl+C 可停止服务器
echo ============================================
echo.

streamlit run streamlit_app.py --server.port 8501 --server.address localhost

pause
