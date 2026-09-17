param(
    [string]$Python = "python",
    [string]$Version = "1.17.0"
)

$ErrorActionPreference = "Stop"
$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $PackagingDir
$BuildDir = Join-Path $Root "build"
$DistDir = Join-Path $Root "dist"
$PortableDir = Join-Path $DistDir "DocumentRegistry"
$ReleaseDir = Join-Path $Root "release"
$ReleaseZip = Join-Path $ReleaseDir ("DocumentRegistry_v{0}_portable.zip" -f $Version)
$SpecFile = Join-Path $PackagingDir "DocumentRegistry.spec"

function Require-File([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing ${Label}: $Path"
    }
}

# Remove obsolete build helpers and old informational text files from earlier
# development packages.  The final build flow uses this script only.
Remove-Item (Join-Path $PackagingDir "build_portable.ps1") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $PackagingDir "build_release.ps1") -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $Root -File -Filter "RELEASE_NOTES_*.txt" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem -LiteralPath $Root -File -Filter "BUILD_RELEASE_*.txt" -ErrorAction SilentlyContinue | Remove-Item -Force

Write-Host "=== DocumentRegistry v$Version build ==="
Write-Host "Project: $Root"

Require-File (Join-Path $Root "verify_project.py") "project verifier"
Require-File $SpecFile "PyInstaller spec"
Require-File (Join-Path $Root "assets\app_icon.ico") "Windows application icon"
Require-File (Join-Path $Root "assets\app_icon.png") "application icon"
Require-File (Join-Path $Root "models\paddle\official_models\PP-OCRv5_mobile_det_onnx\inference.onnx") "OCR detection model"
Require-File (Join-Path $Root "models\paddle\official_models\eslav_PP-OCRv5_mobile_rec_onnx\inference.onnx") "OCR recognition model"

Write-Host "[1/5] Verifying project..."
Push-Location $Root
try {
    & $Python "verify_project.py" "--full"
    if ($LASTEXITCODE -ne 0) {
        throw "verify_project.py --full failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "[2/5] Cleaning previous build output..."
Remove-Item $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $DistDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $ReleaseDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[3/5] Building Windows portable application..."
& $Python -m PyInstaller --noconfirm --clean $SpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Require-File (Join-Path $PortableDir "DocumentRegistry.exe") "frozen executable"

$ModelSource = Join-Path $Root "models"
Copy-Item $ModelSource (Join-Path $PortableDir "models") -Recurse -Force

New-Item -ItemType Directory -Path (Join-Path $PortableDir "data") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $PortableDir "logs") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $PortableDir "backups") -Force | Out-Null
New-Item -ItemType File -Path (Join-Path $PortableDir "portable.flag") -Force | Out-Null

Write-Host "[4/5] Running frozen self-check..."
$ExePath = Join-Path $PortableDir "DocumentRegistry.exe"
& $ExePath --self-check
if ($LASTEXITCODE -ne 0) {
    throw "Frozen self-check failed with exit code $LASTEXITCODE"
}

Write-Host "[5/5] Creating final ZIP..."
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
Compress-Archive -Path $PortableDir -DestinationPath $ReleaseZip -CompressionLevel Optimal -Force
Require-File $ReleaseZip "release ZIP"

# build\ and dist\ are temporary. Keep them only when the build fails so they
# can be inspected. Once the final ZIP exists, remove both directories and leave
# release\ as the only build output. Retry briefly because antivirus/indexing
# software can hold freshly created files for a moment on Windows.
Write-Host "[cleanup] Removing temporary build and dist folders..."
foreach ($TempDir in @($BuildDir, $DistDir)) {
    if (Test-Path -LiteralPath $TempDir) {
        $Removed = $false
        for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
            try {
                Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction Stop
                $Removed = $true
                break
            }
            catch {
                if ($Attempt -lt 3) {
                    Start-Sleep -Milliseconds 700
                }
                else {
                    Write-Warning ("Could not remove temporary folder: {0}. {1}" -f $TempDir, $_.Exception.Message)
                }
            }
        }
    }
}

Write-Host ""
Write-Host "BUILD OK" -ForegroundColor Green
Write-Host "Final file: $ReleaseZip"
