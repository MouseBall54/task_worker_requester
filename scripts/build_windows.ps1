param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$specPath = Join-Path $ProjectRoot "packaging\IPDK_plus.spec"
$iconPath = Join-Path $ProjectRoot "assets\IPDK_plus.ico"
$pngIconPath = Join-Path $ProjectRoot "assets\IPDK_plus.png"
$vcRedistPath = Join-Path $ProjectRoot "packaging\prereqs\vc_redist.x64.exe"
$bundleRoot = Join-Path $ProjectRoot "dist\IPDK_plus\_internal"
$buildOutputPath = Join-Path $ProjectRoot "build\IPDK_plus"
$distOutputPath = Join-Path $ProjectRoot "dist\IPDK_plus"
$installerOutputPath = Join-Path $ProjectRoot "dist\installer"

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

function Remove-BuildOutput {
    param(
        [Parameter(Mandatory = $true)][string]$PathValue
    )

    $resolvedProjectRoot = (Resolve-Path $ProjectRoot).Path
    $parent = Split-Path -Parent $PathValue
    if (-not (Test-Path $parent)) {
        return
    }

    $resolvedParent = (Resolve-Path $parent).Path
    if (-not $resolvedParent.StartsWith($resolvedProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean outside project root: $PathValue"
    }

    if (Test-Path $PathValue) {
        Write-Host "[build] Cleaning: $PathValue"
        Remove-Item -LiteralPath $PathValue -Recurse -Force
    }
}

function Remove-InstallerArtifacts {
    if (-not (Test-Path $installerOutputPath)) {
        return
    }

    $resolvedProjectRoot = (Resolve-Path $ProjectRoot).Path
    $resolvedInstallerDir = (Resolve-Path $installerOutputPath).Path
    if (-not $resolvedInstallerDir.StartsWith($resolvedProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean installer artifacts outside project root: $installerOutputPath"
    }

    Get-ChildItem -LiteralPath $installerOutputPath -Filter "IPDK_plusSetup*.exe" -File |
        Remove-Item -Force
}

function Remove-BundledRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$InternalRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $rootPath = (Resolve-Path $InternalRoot).Path
    $targetPath = Join-Path $InternalRoot $RelativePath
    if (-not (Test-Path $targetPath)) {
        return
    }

    $resolvedTarget = (Resolve-Path $targetPath).Path
    if (-not $resolvedTarget.StartsWith($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to prune outside bundle root: $targetPath"
    }

    Write-Host "[build] Pruning optional bundle file: $RelativePath"
    Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
}

function Remove-OptionalBundleFiles {
    param(
        [Parameter(Mandatory = $true)][string]$InternalRoot
    )

    $optionalPaths = @(
        "PySide6\Qt6Pdf.dll",
        "PySide6\Qt6Qml.dll",
        "PySide6\Qt6QmlMeta.dll",
        "PySide6\Qt6QmlModels.dll",
        "PySide6\Qt6QmlWorkerScript.dll",
        "PySide6\Qt6Quick.dll",
        "PySide6\Qt6VirtualKeyboard.dll",
        "PySide6\plugins\generic\qtuiotouchplugin.dll",
        "PySide6\plugins\imageformats\qpdf.dll",
        "PySide6\plugins\imageformats\qicns.dll",
        "PySide6\plugins\imageformats\qtga.dll",
        "PySide6\plugins\imageformats\qwbmp.dll",
        "PySide6\plugins\imageformats\qwebp.dll",
        "PySide6\plugins\platforminputcontexts\qtvirtualkeyboardplugin.dll",
        "PySide6\plugins\platforms\qdirect2d.dll",
        "PySide6\plugins\platforms\qminimal.dll",
        "PySide6\plugins\platforms\qoffscreen.dll"
    )

    foreach ($relativePath in $optionalPaths) {
        Remove-BundledRelativePath -InternalRoot $InternalRoot -RelativePath $relativePath
    }

    $translationsDir = Join-Path $InternalRoot "PySide6\translations"
    if (Test-Path $translationsDir) {
        $keepTranslations = @("qtbase_en.qm", "qtbase_ko.qm", "qt_en.qm", "qt_ko.qm")
        Get-ChildItem -LiteralPath $translationsDir -Filter "*.qm" -File |
            Where-Object { $keepTranslations -notcontains $_.Name } |
            ForEach-Object {
                Write-Host "[build] Pruning optional translation: $($_.Name)"
                Remove-Item -LiteralPath $_.FullName -Force
            }
    }
}

Assert-PathExists -PathValue $specPath -ErrorMessage "PyInstaller spec file not found: $specPath"
Assert-PathExists -PathValue $iconPath -ErrorMessage "Application icon file not found: $iconPath"
Assert-PathExists -PathValue $pngIconPath -ErrorMessage "Application PNG icon file not found: $pngIconPath"
Assert-PathExists -PathValue $vcRedistPath -ErrorMessage (
    "VC++ redistributable not found: $vcRedistPath`n" +
    "Download from https://aka.ms/vs/17/release/vc_redist.x64.exe and place it at the path above."
)

Write-Host "[build] Project root: $ProjectRoot"
Write-Host "[build] Using icon: $iconPath"
Write-Host "[build] Using PNG icon: $pngIconPath"
Write-Host "[build] Using VC++ redistributable: $vcRedistPath"
Remove-BuildOutput -PathValue $buildOutputPath
Remove-BuildOutput -PathValue $distOutputPath
Remove-InstallerArtifacts
Write-Host "[build] Running PyInstaller..."

Push-Location $ProjectRoot
try {
    uv run --group build pyinstaller $specPath --clean --noconfirm
    Write-Host "[build] PyInstaller build complete: dist\\IPDK_plus"
    Remove-OptionalBundleFiles -InternalRoot $bundleRoot
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
