@echo off
chcp 65001 >nul
set "PLATFORM=%~dp0..\.."
cd /d "%PLATFORM%" || exit /b 1

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到 .venv。请先运行「1 安装依赖」快捷方式或执行 post_install.bat
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat" || exit /b 1
python app.py
pause
