@echo off
chcp 65001 >nul
echo.
echo ============================================
echo   KTV播放器电视端监控系统 - Web界面启动
echo   版本: V2.3
echo ============================================
echo.

cd /d "%~dp0"

echo [检查] 正在检查 Streamlit...
python -c "import streamlit" 2>nul
if errorlevel 1 (
    echo [错误] Streamlit 未安装，正在安装...
    pip install streamlit pandas
    if errorlevel 1 (
        echo [错误] 安装失败，请手动运行: pip install streamlit pandas
        pause
        exit /b 1
    )
)

echo [检查] 正在检查模块导入...
python -c "import sys; sys.path.insert(0, '..'); from core.runner import TestRunner; print('OK')" 2>nul
if errorlevel 1 (
    echo [警告] 模块导入检查失败，但将继续启动...
)

echo.
echo [启动] 正在启动 Streamlit Web 界面...
echo [提示] 浏览器将自动打开，如果没有自动打开，请访问:
echo        http://localhost:8501
echo.
echo [提示] 按 Ctrl+C 可停止服务器
echo.

streamlit run streamlit_app.py --server.port 8501

pause
