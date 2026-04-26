@echo off
chcp 65001 >nul
echo.
echo ============================================
echo   KTV播放器电视端监控系统 - Web界面启动
echo   版本: V2.3
echo ============================================
echo.

REM 检查Streamlit是否安装
python -c "import streamlit" 2>nul
if errorlevel 1 (
    echo [提示] 正在安装Streamlit...
    pip install streamlit pandas
)

echo.
echo [提示] 正在启动Web界面...
echo [提示] 浏览器将自动打开，如果没有自动打开，请访问:
echo        http://localhost:8501
echo.
echo [提示] 按 Ctrl+C 可停止服务器
echo.

cd /d "%~dp0"
streamlit run streamlit_app.py

pause
