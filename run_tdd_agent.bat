@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 每次构建/开发新功能时调用 TDD Agent
REM 用法: run_tdd_agent.bat "需求描述" [模块路径]
REM 示例: run_tdd_agent.bat "新增 run_has_performance_monitor 函数" shared.unified.orchestrator
set FEATURE=%~1
set MODULE=%~2
if "%MODULE%"=="" set MODULE=shared.unified.orchestrator
if "%FEATURE%"=="" (
  echo 用法: run_tdd_agent.bat "需求描述" [模块路径]
  echo 示例: run_tdd_agent.bat "新增 run_has_performance_monitor" shared.unified.orchestrator
  exit /b 1
)
python -m tools.tdd_agent "%FEATURE%" --module %MODULE%
exit /b %ERRORLEVEL%
