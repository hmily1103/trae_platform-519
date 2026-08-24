@echo off
chcp 65001 >nul
set "PLATFORM=%~dp0..\.."
cd /d "%PLATFORM%" || exit /b 1

where py >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python 启动器 py。请先安装 Python 3，并勾选 "Add python.exe to PATH"。
  echo 下载: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

echo [信息] 创建虚拟环境 .venv ...
py -3 -m venv .venv || exit /b 1

call ".venv\Scripts\activate.bat" || exit /b 1
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [警告] pip 安装返回非零。部分包 ^(如 weasyprint^) 在 Windows 可能需要额外系统依赖，请查看项目文档。
  pause
  exit /b 1
)

echo.
echo [完成] Python 依赖已安装到 trae_platform\.venv
echo 下一步：开始菜单 -^> "Trae Code" -^> "2 启动 Trae Platform"
pause
