param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$specPath = Join-Path $ProjectRoot "packaging\IPDK_plus.spec"
$iconPath = Join-Path $ProjectRoot "assets\IPDK_plus.ico"

if (-not (Test-Path $specPath)) {
    throw "PyInstaller spec 파일을 찾지 못했습니다: $specPath"
}

if (-not (Test-Path $iconPath)) {
    throw "아이콘 파일을 찾지 못했습니다: $iconPath"
}

Write-Host "[build] Project root: $ProjectRoot"
Write-Host "[build] Using icon: $iconPath"
Write-Host "[build] Running PyInstaller..."

Push-Location $ProjectRoot
try {
    uv run --group build pyinstaller $specPath --clean --noconfirm
    Write-Host "[build] PyInstaller build complete: dist\\IPDK_plus"

    $iscc = Get-Command ISCC -ErrorAction SilentlyContinue
    if ($null -ne $iscc) {
        Write-Host "[build] Inno Setup detected. Building installer..."
        & $iscc.Source (Join-Path $ProjectRoot "packaging\IPDK_plus.iss")
        Write-Host "[build] Installer build complete: dist\\installer"
    }
    else {
        Write-Warning "ISCC(Inno Setup)가 PATH에 없어 installer 빌드는 건너뛰었습니다."
        Write-Warning "수동 빌드: ISCC .\\packaging\\IPDK_plus.iss"
    }
}
finally {
    Pop-Location
}
