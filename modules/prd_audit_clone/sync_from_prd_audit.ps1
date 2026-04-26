# 将 modules/prd_audit 同步到本目录（维护用）。
# 保留 learning_repo 用户数据、以及 clone 独立文件不被覆盖。
# 同步后请检查：__init__.py、views.py 中的 clone 补丁（见 STANDALONE_README.md）。

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Src = Join-Path $RepoRoot "modules\prd_audit"
$Dst = Join-Path $RepoRoot "modules\prd_audit_clone"

$Keep = @(
    "standalone_app.py",
    "package_clone.ps1",
    "sync_from_prd_audit.ps1",
    "STANDALONE_README.md",
    "bundle_base.html",
    "__init__.py"
)
$bak = Join-Path $env:TEMP "prd_audit_clone_sync_$(Get-Date -Format yyyyMMddHHmmss)"
New-Item -ItemType Directory -Force -Path $bak | Out-Null
foreach ($f in $Keep) {
    $p = Join-Path $Dst $f
    if (Test-Path $p) {
        Copy-Item -Force $p (Join-Path $bak $f)
    }
}

robocopy $Src $Dst /E /XD __pycache__ learning_repo /NFL /NDL /NJH /NJS /nc /ns /np
if ($LASTEXITCODE -ge 8) { throw "robocopy failed: $LASTEXITCODE" }

foreach ($f in $Keep) {
    $from = Join-Path $bak $f
    if (Test-Path $from) {
        Copy-Item -Force $from (Join-Path $Dst $f)
    }
}

# 模板 URL：/prd_audit -> /prd_audit_clone，并修复双重替换
Get-ChildItem -Path (Join-Path $Dst "templates") -Filter "*.html" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
    $c = [System.IO.File]::ReadAllText($_.FullName)
    $n = $c -replace '/prd_audit','/prd_audit_clone'
    $n = $n -replace '/prd_audit_clone_clone','/prd_audit_clone'
    if ($n -ne $c) { [System.IO.File]::WriteAllText($_.FullName, $n) }
}

Write-Host "SYNC_DONE. 请手动将 modules/prd_audit_clone/views.py 与主仓库中已适配的 clone 版本对齐（LLM 路径、prepare_save_to_cases 等），或从本分支复制 views.py。"
Write-Host "Backup of kept files: $bak"
