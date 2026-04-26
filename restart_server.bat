@echo off
echo 正在停止现有服务器进程...
taskkill /F /FI "WINDOWTITLE eq *python*app.py*" 2>nul
taskkill /F /FI "IMAGENAME eq python.exe" /FI "COMMANDLINE eq *app.py*" 2>nul
timeout /t 2 /nobreak >nul

echo.
echo 正在启动服务器...
cd /d %~dp0
python app.py
pause
