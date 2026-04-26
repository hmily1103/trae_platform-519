param(
    [string]$OutputRoot = "",
    [switch]$KeepSnapshots
)

$ErrorActionPreference = "Stop"

$ModuleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ModuleDir "..\..")).Path

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $RepoRoot "dist\prd_audit_clone"
}

$OutputRoot = (Resolve-Path (New-Item -ItemType Directory -Force -Path $OutputRoot)).Path
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$stagingDir = Join-Path $OutputRoot "prd_audit_clone_bundle"
$zipPath = Join-Path $OutputRoot "prd_audit_clone_bundle_$timestamp.zip"

if (Test-Path $stagingDir) {
    Remove-Item -Recurse -Force $stagingDir
}
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

New-Item -ItemType Directory -Force -Path (Join-Path $stagingDir "modules\prd_audit_clone") | Out-Null
robocopy (Join-Path $RepoRoot "modules\prd_audit_clone") (Join-Path $stagingDir "modules\prd_audit_clone") /E /R:1 /W:1 /XD dist __pycache__ .git /XF *.pyc > $null
Copy-Item -Recurse -Force (Join-Path $RepoRoot "utils") (Join-Path $stagingDir "utils")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "static") (Join-Path $stagingDir "static")
Copy-Item -Force (Join-Path $RepoRoot "requirements.txt") (Join-Path $stagingDir "requirements.txt")

$targetTplDir = Join-Path $stagingDir "templates"
New-Item -ItemType Directory -Force -Path $targetTplDir | Out-Null
# 独立包使用与 standalone_app 相同的 PRD-only 壳（无运维平台侧栏）
Copy-Item -Force (Join-Path $RepoRoot "modules\prd_audit_clone\standalone_templates\base.html") (Join-Path $targetTplDir "base.html")

$cloneLearning = Join-Path $stagingDir "modules\prd_audit_clone\learning_repo"
if (Test-Path $cloneLearning) {
    Get-ChildItem -Recurse -File -Path (Join-Path $cloneLearning "snapshots") -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    if (-not $KeepSnapshots) {
        Set-Content -Path (Join-Path $cloneLearning "index.json") -Value "{`n  `"snapshots`": []`n}" -Encoding UTF8
        Set-Content -Path (Join-Path $cloneLearning "learning_actions.json") -Value "[]" -Encoding UTF8
        Get-ChildItem -File -Path $cloneLearning -Filter "*.backup*.json" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
        Get-ChildItem -File -Path $cloneLearning -Filter "rule_candidates.json" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
        Get-ChildItem -File -Path $cloneLearning -Filter "*.draft.json" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
        Get-ChildItem -File -Path $cloneLearning -Filter "*.applied.json" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

$runPs1 = @"
`$ErrorActionPreference = "Stop"
`$root = Split-Path -Parent `$(Resolve-Path `$(Join-Path `$PSScriptRoot "."))
Set-Location `$root
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}
`$env:PRD_AUDIT_CLONE_HOST = "127.0.0.1"
if (-not `$env:PRD_AUDIT_CLONE_PORT) { `$env:PRD_AUDIT_CLONE_PORT = "5010" }
`$env:PRD_AUDIT_CLONE_DEBUG = "1"
.\.venv\Scripts\python.exe -m modules.prd_audit_clone.standalone_app
"@
Set-Content -Path (Join-Path $stagingDir "run_clone.ps1") -Value $runPs1 -Encoding UTF8

$runBat = @"
@echo off
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  call .venv\Scripts\python.exe -m pip install --upgrade pip
  call .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if "%PRD_AUDIT_CLONE_PORT%"=="" set PRD_AUDIT_CLONE_PORT=5010
set PRD_AUDIT_CLONE_HOST=127.0.0.1
set PRD_AUDIT_CLONE_DEBUG=1
call .venv\Scripts\python.exe -m modules.prd_audit_clone.standalone_app
endlocal
"@
Set-Content -Path (Join-Path $stagingDir "run_clone.bat") -Value $runBat -Encoding ASCII

$note = @"
prd_audit_clone 一键包

启动方式：
1) 双击 run_clone.bat
2) 或在 PowerShell 执行 .\run_clone.ps1

访问地址：
http://127.0.0.1:5010/prd_audit_clone/
"@
Set-Content -Path (Join-Path $stagingDir "README_QUICK_START.txt") -Value $note -Encoding UTF8

if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path (Join-Path $stagingDir "*") -DestinationPath $zipPath -CompressionLevel Optimal

Write-Host "PACK_OK"
Write-Host "Staging: $stagingDir"
Write-Host "Zip: $zipPath"
