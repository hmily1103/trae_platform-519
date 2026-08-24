# Build TraeCode.msi using WiX Toolset v3 (heat, candle, light).
# Install WiX v3 from https://wixtoolset.org/docs/wix3/ or GitHub releases.
# Optionally set env WIX to the WiX install root (contains bin\heat.exe).

$ErrorActionPreference = "Stop"

$MsiDir = $PSScriptRoot
$TraeCodeRoot = (Resolve-Path (Join-Path $MsiDir "..\..\..")).Path
$OutMsi = Join-Path $MsiDir "TraeCode.msi"

function Find-WixBin {
    if ($env:WIX) {
        $b = Join-Path $env:WIX "bin"
        if (Test-Path (Join-Path $b "heat.exe")) { return $b }
    }
    $candidates = @(
        "${env:ProgramFiles(x86)}\WiX Toolset v3.14\bin",
        "${env:ProgramFiles(x86)}\WiX Toolset v3.13\bin",
        "${env:ProgramFiles(x86)}\WiX Toolset v3.11\bin"
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "heat.exe")) { return $c }
    }
    return $null
}

$wixBin = Find-WixBin
if (-not $wixBin) {
    Write-Host "ERROR: WiX Toolset v3 not found (heat.exe). Install WiX v3 or set WIX to install root."
    Write-Host "See: https://github.com/wixtoolset/wix3/releases"
    exit 1
}

$heat = Join-Path $wixBin "heat.exe"
$candle = Join-Path $wixBin "candle.exe"
$light = Join-Path $wixBin "light.exe"

Write-Host "WiX bin: $wixBin"
Write-Host "Source (trae-code root): $TraeCodeRoot"
Write-Host "Output MSI: $OutMsi"

$filesWxs = Join-Path $MsiDir "Files.wxs"
$fragDir = Join-Path $MsiDir "obj"
New-Item -ItemType Directory -Force -Path $fragDir | Out-Null

Write-Host "Running heat.exe ..."
& $heat dir $TraeCodeRoot `
    -cg AppFiles `
    -dr INSTALLFOLDER `
    -gg -sfrag -ke -scom -sreg -srd `
    -template fragment `
    -var var.SourceDir `
    -out $filesWxs

Write-Host "Running candle.exe ..."
$candleOutDir = $fragDir + "\"
& $candle -nologo -arch x64 `
    "-dSourceDir=$TraeCodeRoot" `
    -ext WixUIExtension `
    -out $candleOutDir `
    (Join-Path $MsiDir "Product.wxs") `
    $filesWxs

Write-Host "Running light.exe ..."
& $light -nologo `
    -ext WixUIExtension `
    -out $OutMsi `
    (Join-Path $fragDir "Product.wixobj") `
    (Join-Path $fragDir "Files.wixobj")

Write-Host "Done."
Get-Item -LiteralPath $OutMsi | Format-List FullName, Length, LastWriteTime
