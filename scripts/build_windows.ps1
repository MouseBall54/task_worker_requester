param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$specPath = Join-Path $ProjectRoot "packaging\IPDK_plus.spec"
$iconPath = Join-Path $ProjectRoot "assets\IPDK_plus.ico"
$vcRedistPath = Join-Path $ProjectRoot "packaging\prereqs\vc_redist.x64.exe"
$bundleRoot = Join-Path $ProjectRoot "dist\IPDK_plus\_internal"

function Assert-PathExists {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue,
        [Parameter(Mandatory = $true)][string]$ErrorMessage
    )

    if (-not (Test-Path $PathValue)) {
        throw $ErrorMessage
    }
}

function Assert-BundleRuntimeFiles {
    param(
        [Parameter(Mandatory = $true)][string]$InternalRoot
    )

    $required = @(
        "PySide6\QtGui.pyd",
        "PySide6\Qt6Core.dll",
        "PySide6\Qt6Gui.dll",
        "PySide6\plugins\platforms\qwindows.dll",
        "VCRUNTIME140.dll",
        "VCRUNTIME140_1.dll",
        "PySide6\MSVCP140.dll",
        "PySide6\MSVCP140_1.dll",
        "PySide6\MSVCP140_2.dll"
    )

    $missing = @()
    foreach ($relativePath in $required) {
        $fullPath = Join-Path $InternalRoot $relativePath
        if (-not (Test-Path $fullPath)) {
            $missing += $relativePath
        }
    }

    if ($missing.Count -gt 0) {
        $missingList = ($missing | ForEach-Object { " - $_" }) -join "`n"
        throw "Required Qt/VC runtime files are missing from PyInstaller output.`n$missingList"
    }
}

Assert-PathExists -PathValue $specPath -ErrorMessage "PyInstaller spec file not found: $specPath"
Assert-PathExists -PathValue $iconPath -ErrorMessage "Application icon file not found: $iconPath"
Assert-PathExists -PathValue $vcRedistPath -ErrorMessage (
    "VC++ redistributable not found: $vcRedistPath`n" +
    "Download from https://aka.ms/vs/17/release/vc_redist.x64.exe and place it at the path above."
)

Write-Host "[build] Project root: $ProjectRoot"
Write-Host "[build] Using icon: $iconPath"
Write-Host "[build] Using VC++ redistributable: $vcRedistPath"
Write-Host "[build] Running PyInstaller..."

Push-Location $ProjectRoot
try {
    uv run --group build pyinstaller $specPath --clean --noconfirm
    Write-Host "[build] PyInstaller build complete: dist\\IPDK_plus"
    Assert-BundleRuntimeFiles -InternalRoot $bundleRoot
    Write-Host "[build] Bundle runtime validation passed."

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
