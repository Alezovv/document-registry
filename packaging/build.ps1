param(
    [string]$Python = "python",
    [string]$Version = "1.28.7",
    [string]$PublishTo = "",
    [string]$ReleaseNotes = "DocumentRegistry 1.28.7: подсветка и поиск дубликатов, компактный вид реестра (8-10 строк), ограничение истории до 2000 записей.",
    [string]$PackageFileId = "",
    [switch]$PublishExisting,
    [switch]$ManifestOnly
)

$ErrorActionPreference = "Stop"
$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $PackagingDir

if (-not $Version) {
    $InitFile = Join-Path $Root "app\__init__.py"
    if (Test-Path -LiteralPath $InitFile -PathType Leaf) {
        $InitSource = Get-Content -LiteralPath $InitFile -Raw
        if ($InitSource -match '__version__\s*=\s*["'']([^"'']+)["'']') {
            $Version = [string]$Matches[1]
            Write-Host "Auto-detected version from app\__init__.py: $Version" -ForegroundColor Cyan
        }
    }
}

$BuildDir = Join-Path $Root "build"
$DistDir = Join-Path $Root "dist"
$PortableDir = Join-Path $DistDir "DocumentRegistry"
$UpdaterBuildExe = Join-Path $DistDir "DocumentRegistryUpdater.exe"
$ReleaseDir = Join-Path $Root "release"
$ReleaseZip = Join-Path $ReleaseDir ("DocumentRegistry_v{0}_portable.zip" -f $Version)
$SpecFile = Join-Path $PackagingDir "DocumentRegistry.spec"
$SchemaFile = Join-Path $Root "app\db_migrations.py"
if (-not (Test-Path -LiteralPath $SchemaFile -PathType Leaf)) {
    throw "Missing database migration definition: $SchemaFile"
}
$SchemaSource = Get-Content -LiteralPath $SchemaFile -Raw
if ($SchemaSource -notmatch 'CURRENT_SCHEMA_VERSION\s*=\s*(\d+)') {
    throw "Could not determine CURRENT_SCHEMA_VERSION from app\db_migrations.py"
}
$DatabaseSchema = [int]$Matches[1]

$UpdateConfigFile = Join-Path $Root "app\update_config.py"
$DriveManifestFileId = "1DAYJCCB1eO0zSvqNeWsWJREXbyMoSse8"
if (Test-Path -LiteralPath $UpdateConfigFile -PathType Leaf) {
    $ConfigSource = Get-Content -LiteralPath $UpdateConfigFile -Raw
    if ($ConfigSource -match 'UPDATE_MANIFEST_FILE_ID\s*=\s*["'']([^"'']+)["'']') {
        $DriveManifestFileId = [string]$Matches[1]
    }
}

function Require-File([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing ${Label}: $Path"
    }
}


function Assert-AppVersion([string]$ExpectedVersion) {
    $VersionFile = Join-Path $Root "app\__init__.py"
    Require-File $VersionFile "application version source"
    $VersionSource = Get-Content -LiteralPath $VersionFile -Raw
    if ($VersionSource -notmatch ('__version__\s*=\s*["'']' + [regex]::Escape($ExpectedVersion) + '["'']')) {
        throw "Release version $ExpectedVersion does not match app\__init__.py"
    }
}

function Assert-PortableVersion([string]$PortableSource, [string]$ExpectedVersion) {
    $VersionFile = Join-Path $PortableSource "version.json"
    Require-File $VersionFile "portable version.json"
    try {
        $VersionInfo = Get-Content -LiteralPath $VersionFile -Raw | ConvertFrom-Json
    }
    catch {
        throw "Could not read portable version.json: $($_.Exception.Message)"
    }
    $ActualVersion = [string]$VersionInfo.version
    if ($ActualVersion -ne $ExpectedVersion) {
        throw "Portable version $ActualVersion does not match requested release version $ExpectedVersion"
    }
}

function Assert-UpdatePackageVersion([string]$PackagePath, [string]$ExpectedVersion) {
    Require-File $PackagePath "existing update package"
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($PackagePath)
    try {
        $Entry = $Archive.GetEntry("update_manifest.json")
        if ($null -eq $Entry) {
            throw "Update package does not contain update_manifest.json"
        }
        $Stream = $Entry.Open()
        $Reader = [System.IO.StreamReader]::new($Stream, [System.Text.Encoding]::UTF8, $true)
        try {
            $InternalManifest = $Reader.ReadToEnd() | ConvertFrom-Json
        }
        finally {
            $Reader.Dispose()
            $Stream.Dispose()
        }
        $ActualVersion = [string]$InternalManifest.version
        if ($ActualVersion -ne $ExpectedVersion) {
            throw "Update package version $ActualVersion does not match requested manifest version $ExpectedVersion"
        }
    }
    finally {
        $Archive.Dispose()
    }
}



function Write-JsonUtf8NoBom([object]$Value, [string]$Path, [int]$Depth = 6) {
    $Json = $Value | ConvertTo-Json -Depth $Depth
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Json + [Environment]::NewLine, $Utf8NoBom)
}

function ConvertTo-ReleaseEntryObject([object]$Item) {
    if ($null -eq $Item) {
        return $null
    }
    $ItemVersion = [string]$Item.version
    $ItemPackage = [string]$Item.package
    $ItemHash = [string]$Item.sha256
    $ItemSize = 0L
    try {
        $ItemSize = [int64]$Item.size
    }
    catch {
        $ItemSize = 0L
    }
    if ([string]::IsNullOrWhiteSpace($ItemVersion) -or [string]::IsNullOrWhiteSpace($ItemPackage) -or [string]::IsNullOrWhiteSpace($ItemHash) -or $ItemSize -le 0) {
        return $null
    }
    $ItemSchema = $DatabaseSchema
    if ($null -ne $Item.database_schema) {
        try {
            $ItemSchema = [int]$Item.database_schema
        }
        catch {
            $ItemSchema = $DatabaseSchema
        }
    }
    $ItemNotes = [string]$Item.notes
    if ([string]::IsNullOrWhiteSpace($ItemNotes) -and $null -ne $Item.release_notes) {
        $ItemNotes = [string]$Item.release_notes
    }
    $ItemPublishedAt = [string]$Item.published_at
    if ([string]::IsNullOrWhiteSpace($ItemPublishedAt)) {
        $ItemPublishedAt = [DateTimeOffset]::Now.ToString("o")
    }
    return [ordered]@{
        version = $ItemVersion.Trim()
        database_schema = $ItemSchema
        package = $ItemPackage.Trim()
        package_file_id = ([string]$Item.package_file_id).Trim()
        sha256 = $ItemHash.Trim().ToLowerInvariant()
        size = $ItemSize
        notes = $ItemNotes
        published_at = $ItemPublishedAt
    }
}

function Add-ManifestEntriesToHistory([object]$ManifestObj, [hashtable]$Table, [string]$SkipVersion) {
    if ($null -eq $ManifestObj) {
        return
    }
    $TopEntry = ConvertTo-ReleaseEntryObject $ManifestObj
    if ($null -ne $TopEntry -and $TopEntry.version -ne $SkipVersion) {
        if (-not $Table.ContainsKey($TopEntry.version) -or [string]::IsNullOrWhiteSpace($Table[$TopEntry.version].package_file_id)) {
            $Table[$TopEntry.version] = $TopEntry
        }
    }
    if ($null -ne $ManifestObj.versions) {
        foreach ($OldItem in $ManifestObj.versions) {
            $ParsedOld = ConvertTo-ReleaseEntryObject $OldItem
            if ($null -ne $ParsedOld -and $ParsedOld.version -ne $SkipVersion) {
                if (-not $Table.ContainsKey($ParsedOld.version) -or [string]::IsNullOrWhiteSpace($Table[$ParsedOld.version].package_file_id)) {
                    $Table[$ParsedOld.version] = $ParsedOld
                }
            }
        }
    }
}

function Get-RemoteGoogleDriveManifest() {
    if ([string]::IsNullOrWhiteSpace($DriveManifestFileId)) {
        return $null
    }
    $Url = "https://drive.google.com/uc?export=download&id=$DriveManifestFileId"
    try {
        $Response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        $ContentText = [System.Text.Encoding]::UTF8.GetString($Response.RawContentStream.ToArray())
        $ContentText = $ContentText.TrimStart([char]0xFEFF)
        return ($ContentText | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        Write-Warning ("Could not fetch current manifest.json from Google Drive (offline or restricted): {0}" -f $_.Exception.Message)
        return $null
    }
}

function Write-PublicUpdateManifest(
    [string]$Destination,
    [string]$PublishVersion,
    [string]$Notes,
    [string]$GoogleDriveFileId
) {
    $VersionedPackageName = "DocumentRegistry_{0}_update.zip" -f $PublishVersion
    $VersionedPackageTarget = Join-Path $Destination $VersionedPackageName
    $LegacyPackageName = "DocumentRegistry_update.zip"
    $LegacyPackageTarget = Join-Path $Destination $LegacyPackageName

    if (Test-Path -LiteralPath $VersionedPackageTarget -PathType Leaf) {
        $PackageName = $VersionedPackageName
        $PackageTarget = $VersionedPackageTarget
    }
    else {
        $PackageName = $LegacyPackageName
        $PackageTarget = $LegacyPackageTarget
    }

    Require-File $PackageTarget "existing update package"
    Assert-UpdatePackageVersion $PackageTarget $PublishVersion

    $Hash = (Get-FileHash -LiteralPath $PackageTarget -Algorithm SHA256).Hash.ToLowerInvariant()
    $Size = (Get-Item -LiteralPath $PackageTarget).Length
    if ([string]::IsNullOrWhiteSpace($Notes)) {
        $Notes = "DocumentRegistry v$PublishVersion"
    }
    $NowIso = [DateTimeOffset]::Now.ToString("o")

    $ManifestTarget = Join-Path $Destination "manifest.json"
    $PreviousEntriesByVersion = @{}

    # 1. Read local manifest.json in publish folder if it exists
    if (Test-Path -LiteralPath $ManifestTarget -PathType Leaf) {
        try {
            $ExistingLocal = Get-Content -LiteralPath $ManifestTarget -Raw | ConvertFrom-Json
            Add-ManifestEntriesToHistory $ExistingLocal $PreviousEntriesByVersion $PublishVersion
        }
        catch {
            Write-Warning ("Could not read local manifest.json: {0}" -f $_.Exception.Message)
        }
    }

    # 2. Also fetch current live manifest.json from Google Drive so previously published
    # versions (and their Google Drive package_file_id) are preserved automatically!
    $RemoteManifest = Get-RemoteGoogleDriveManifest
    if ($null -ne $RemoteManifest) {
        Add-ManifestEntriesToHistory $RemoteManifest $PreviousEntriesByVersion $PublishVersion
    }

    $VersionsList = @()
    foreach ($VerKey in ($PreviousEntriesByVersion.Keys | Sort-Object {
        try { [version]($_ -replace '^v', '') } catch { [version]"0.0.0" }
    } -Descending)) {
        $EntryObj = $PreviousEntriesByVersion[$VerKey]
        $LocalOldFile = Join-Path $Destination $EntryObj.package
        if (-not [string]::IsNullOrWhiteSpace($EntryObj.package_file_id) -or (Test-Path -LiteralPath $LocalOldFile -PathType Leaf)) {
            $VersionsList += $EntryObj
        }
    }

    $Manifest = [ordered]@{
        format_version = 1
        version = $PublishVersion
        database_schema = $DatabaseSchema
        package = $PackageName
        package_file_id = $GoogleDriveFileId
        sha256 = $Hash
        size = $Size
        notes = $Notes
        published_at = $NowIso
        versions = $VersionsList
    }
    $ManifestUploading = Join-Path $Destination "manifest.json.uploading"
    Remove-Item $ManifestUploading -Force -ErrorAction SilentlyContinue
    Write-JsonUtf8NoBom $Manifest $ManifestUploading 6
    Move-Item $ManifestUploading $ManifestTarget -Force
    return $ManifestTarget
}

function Invoke-FrozenSelfCheck(
    [string]$Executable,
    [string]$WorkingDirectory,
    [string]$Label,
    [string[]]$Arguments = @("--self-check"),
    [string]$ReportPath = ""
) {
    # PyInstaller executables are built with console=False. Write diagnostic
    # self-check output to JSON because stdout/stderr of a GUI executable are
    # not reliably visible from PowerShell.
    if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
        Remove-Item -LiteralPath $ReportPath -Force -ErrorAction SilentlyContinue
        $Arguments = @($Arguments) + @("--self-check-output", $ReportPath)
    }

    # Start-Process -Wait gives us an explicit process handle and guarantees
    # that the build does not start archiving a still-running frozen app.
    $Process = Start-Process `
        -FilePath $Executable `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -PassThru `
        -Wait
    try {
        if ($Process.ExitCode -ne 0) {
            if (-not [string]::IsNullOrWhiteSpace($ReportPath) -and (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
                Write-Host ""
                Write-Host "$Label details:" -ForegroundColor Yellow
                try {
                    $Report = Get-Content -LiteralPath $ReportPath -Raw | ConvertFrom-Json
                    foreach ($Item in $Report.items) {
                        $Status = if ($Item.ok) { "OK" } else { "FAILED" }
                        Write-Host ("  [{0}] {1}: {2}" -f $Status, $Item.name, $Item.details)
                    }
                }
                catch {
                    Get-Content -LiteralPath $ReportPath | ForEach-Object { Write-Host "  $_" }
                }
            }
            throw "$Label failed with exit code $($Process.ExitCode)"
        }
    }
    finally {
        $Process.Dispose()
    }
}

function Wait-ReadableFile(
    [string]$Path,
    [int]$Attempts = 60,
    [int]$DelayMilliseconds = 500
) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        $Stream = $null
        try {
            # Wait until the file can be opened exclusively. After a frozen
            # self-check Windows Defender / SmartScreen can keep the EXE or
            # base_library.zip open for a few seconds even though the process
            # itself has already exited. Compress-Archive fails immediately in
            # that situation, so we explicitly wait for every important file.
            $Stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::None
            )
            return
        }
        catch [System.IO.IOException] {
            if ($Attempt -ge $Attempts) {
                throw "Timed out waiting for file to be released: $Path"
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
        catch [System.UnauthorizedAccessException] {
            if ($Attempt -ge $Attempts) {
                throw "Timed out waiting for file to be released: $Path"
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
        finally {
            if ($null -ne $Stream) {
                $Stream.Dispose()
            }
        }
    }
}

function New-ReleaseZipWithRetry(
    [string]$SourceDirectory,
    [string]$DestinationZip,
    [int]$Attempts = 8
) {
    $ExeProbe = Join-Path $SourceDirectory "DocumentRegistry.exe"
    $UpdaterProbe = Join-Path $SourceDirectory "updater\DocumentRegistryUpdater.exe"
    $LibraryProbe = Join-Path $SourceDirectory "_internal\base_library.zip"

    foreach ($Probe in @($ExeProbe, $UpdaterProbe, $LibraryProbe)) {
        Wait-ReadableFile $Probe
    }

    # Do not ZIP directly from dist after executing the frozen application.
    # First copy the finished portable tree to a fresh, never-executed staging
    # directory. This avoids transient Windows/antivirus locks on the executable
    # from breaking the final archive operation.
    $ZipStageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("DocumentRegistry_zip_" + [guid]::NewGuid().ToString("N"))
    $ZipStagePortable = Join-Path $ZipStageRoot "DocumentRegistry"

    try {
        for ($CopyAttempt = 1; $CopyAttempt -le $Attempts; $CopyAttempt++) {
            try {
                Remove-Item -LiteralPath $ZipStageRoot -Recurse -Force -ErrorAction SilentlyContinue
                New-Item -ItemType Directory -Path $ZipStageRoot -Force | Out-Null
                Copy-Item -LiteralPath $SourceDirectory -Destination $ZipStagePortable -Recurse -Force -ErrorAction Stop
                break
            }
            catch {
                Remove-Item -LiteralPath $ZipStageRoot -Recurse -Force -ErrorAction SilentlyContinue
                if ($CopyAttempt -ge $Attempts) {
                    throw
                }
                Write-Warning ("Staging copy attempt {0}/{1} failed because files are still busy. Retrying..." -f $CopyAttempt, $Attempts)
                Start-Sleep -Milliseconds (1000 * $CopyAttempt)
                foreach ($Probe in @($ExeProbe, $UpdaterProbe, $LibraryProbe)) {
                    Wait-ReadableFile $Probe
                }
            }
        }

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
            try {
                Remove-Item -LiteralPath $DestinationZip -Force -ErrorAction SilentlyContinue
                [System.IO.Compression.ZipFile]::CreateFromDirectory(
                    $ZipStageRoot,
                    $DestinationZip,
                    [System.IO.Compression.CompressionLevel]::Optimal,
                    $false
                )
                return
            }
            catch {
                Remove-Item -LiteralPath $DestinationZip -Force -ErrorAction SilentlyContinue
                if ($Attempt -ge $Attempts) {
                    throw
                }
                Write-Warning ("ZIP attempt {0}/{1} failed. Retrying from staging directory..." -f $Attempt, $Attempts)
                Start-Sleep -Milliseconds (1000 * $Attempt)
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $ZipStageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Assert-PortableRuntimeClean([string]$PortableSource) {
    $ForbiddenDirs = @("tests", "tools", "gold", "gold_dataset")
    foreach ($Name in $ForbiddenDirs) {
        $Candidate = Join-Path $PortableSource $Name
        if (Test-Path -LiteralPath $Candidate) {
            throw "Developer-only content leaked into portable runtime: $Candidate"
        }
    }

    $ForbiddenPatterns = @("*gold_dataset*.json", "*gold_regression*.json", "*test_*.py")
    foreach ($Pattern in $ForbiddenPatterns) {
        $Hit = Get-ChildItem -LiteralPath $PortableSource -Recurse -File -Filter $Pattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $Hit) {
            throw "Developer/test artifact leaked into portable runtime: $($Hit.FullName)"
        }
    }
}

function Publish-UpdatePackage(
    [string]$PortableSource,
    [string]$Destination,
    [string]$PublishVersion,
    [string]$Notes
) {
    Require-File (Join-Path $PortableSource "DocumentRegistry.exe") "portable executable"
    Require-File (Join-Path $PortableSource "updater\DocumentRegistryUpdater.exe") "portable updater"
    Assert-PortableVersion $PortableSource $PublishVersion
    if (-not (Test-Path -LiteralPath (Join-Path $PortableSource "_internal") -PathType Container)) {
        throw "Missing portable _internal directory: $PortableSource"
    }

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $PublishTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("DocumentRegistry_publish_" + [guid]::NewGuid().ToString("N"))
    $Stage = Join-Path $PublishTemp "stage"
    $Payload = Join-Path $Stage "payload"
    $PayloadUpdater = Join-Path $Payload "updater"
    $LocalPackage = Join-Path $PublishTemp ("DocumentRegistry_{0}_update.zip" -f $PublishVersion)
    try {
        New-Item -ItemType Directory -Path $Payload -Force | Out-Null
        New-Item -ItemType Directory -Path $PayloadUpdater -Force | Out-Null

        Copy-Item (Join-Path $PortableSource "DocumentRegistry.exe") (Join-Path $Payload "DocumentRegistry.exe") -Force
        Copy-Item (Join-Path $PortableSource "_internal") (Join-Path $Payload "_internal") -Recurse -Force
        Copy-Item (Join-Path $PortableSource "updater\DocumentRegistryUpdater.exe") (Join-Path $PayloadUpdater "DocumentRegistryUpdater.exe") -Force

        $VersionFile = Join-Path $PortableSource "version.json"
        if (Test-Path -LiteralPath $VersionFile -PathType Leaf) {
            Copy-Item $VersionFile (Join-Path $Payload "version.json") -Force
        }

        if ([string]::IsNullOrWhiteSpace($Notes)) {
            $Notes = "DocumentRegistry v$PublishVersion"
        }

        $InternalManifest = [ordered]@{
            format_version = 1
            version = $PublishVersion
            database_schema = $DatabaseSchema
            notes = $Notes
            created_at = [DateTimeOffset]::Now.ToString("o")
        }
        Write-JsonUtf8NoBom $InternalManifest (Join-Path $Stage "update_manifest.json") 4

        Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $LocalPackage -CompressionLevel Optimal -Force
        Require-File $LocalPackage "update package"

        $Hash = (Get-FileHash -LiteralPath $LocalPackage -Algorithm SHA256).Hash.ToLowerInvariant()
        $Size = (Get-Item -LiteralPath $LocalPackage).Length

        # Save versioned package so previous versions remain available for rollback
        $VersionedPackageName = "DocumentRegistry_{0}_update.zip" -f $PublishVersion
        $VersionedPackageTarget = Join-Path $Destination $VersionedPackageName
        $VersionedUploading = Join-Path $Destination ($VersionedPackageName + ".uploading")
        Remove-Item $VersionedUploading -Force -ErrorAction SilentlyContinue
        Copy-Item $LocalPackage $VersionedUploading -Force
        Move-Item $VersionedUploading $VersionedPackageTarget -Force

        # Also keep legacy filename updated for backward compatibility
        $LegacyPackageName = "DocumentRegistry_update.zip"
        $LegacyPackageTarget = Join-Path $Destination $LegacyPackageName
        Copy-Item $VersionedPackageTarget $LegacyPackageTarget -Force

        $ManifestTarget = Write-PublicUpdateManifest $Destination $PublishVersion $Notes $PackageFileId

        Write-Host ""
        Write-Host "PUBLISH OK" -ForegroundColor Green
        Write-Host "Update folder: $Destination"
        Write-Host "Version: $PublishVersion"
        Write-Host "Package: $VersionedPackageName"
        Write-Host "SHA-256: $Hash"
        if ([string]::IsNullOrWhiteSpace($PackageFileId)) {
            Write-Host "Next: upload $VersionedPackageName to Google Drive (do NOT delete old ZIP packages on Drive!), copy its file ID, then run:" -ForegroundColor Yellow
            Write-Host ("  packaging\build.ps1 -Version {0} -ManifestOnly -PublishTo `"{1}`" -PackageFileId `"<GOOGLE_DRIVE_FILE_ID>`" -ReleaseNotes `"{2}`"" -f $PublishVersion, $Destination, $Notes)
        }
        else {
            Write-Host "manifest.json includes package_file_id and rollback versions history, ready to upload to Google Drive." -ForegroundColor Green
        }
    }
    finally {
        Remove-Item $PublishTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Assert-AppVersion $Version

if ($ManifestOnly) {
    if ([string]::IsNullOrWhiteSpace($PublishTo)) {
        throw "-ManifestOnly requires -PublishTo <local publish folder>"
    }
    if ([string]::IsNullOrWhiteSpace($PackageFileId)) {
        throw "-ManifestOnly requires -PackageFileId <Google Drive file ID>"
    }
    $ManifestPath = Write-PublicUpdateManifest $PublishTo $Version $ReleaseNotes $PackageFileId
    Write-Host ""
    Write-Host "MANIFEST OK" -ForegroundColor Green
    Write-Host "Final manifest: $ManifestPath"
    Write-Host "Upload this file as a NEW VERSION of the existing Google Drive manifest.json; do not delete the old Drive file."
    exit 0
}

if ($PublishExisting) {
    if ([string]::IsNullOrWhiteSpace($PublishTo)) {
        throw "-PublishExisting requires -PublishTo <Google Drive synced folder>"
    }
    Require-File $ReleaseZip "existing release ZIP"
    $ExtractTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("DocumentRegistry_release_" + [guid]::NewGuid().ToString("N"))
    try {
        Expand-Archive -LiteralPath $ReleaseZip -DestinationPath $ExtractTemp -Force
        $ExistingPortable = Join-Path $ExtractTemp "DocumentRegistry"
        Publish-UpdatePackage $ExistingPortable $PublishTo $Version $ReleaseNotes
    }
    finally {
        Remove-Item $ExtractTemp -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

# Remove obsolete build helpers and old informational text files from earlier
# development packages. The final build flow uses this script only.
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

Write-Host "[1/7] Verifying project..."
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

$GoldDataset = Join-Path $Root "gold\gold_dataset.json"
$GoldBaseline = Join-Path $Root "gold\baseline.json"
if ((Test-Path -LiteralPath $GoldDataset -PathType Leaf) -and (Test-Path -LiteralPath $GoldBaseline -PathType Leaf)) {
    Write-Host "[1b/7] Running Gold Dataset parser regression gate..."
    Push-Location $Root
    try {
        & $Python "tools\gold_regression_gate.py" `
            "--gold" $GoldDataset `
            "--baseline" $GoldBaseline `
            "--cache" (Join-Path $Root "data\parser_cache") `
            "--report" (Join-Path $BuildDir "gold_gate.json")
        if ($LASTEXITCODE -ne 0) {
            throw "Gold Dataset regression gate failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Host "[1b/7] Gold Dataset gate skipped (gold_dataset.json + baseline.json not configured)."
}

Write-Host "[2/7] Cleaning previous build output..."
Remove-Item $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $DistDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $ReleaseDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[3/7] Building Windows portable application and updater..."
& $Python -m PyInstaller --noconfirm --clean $SpecFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Require-File (Join-Path $PortableDir "DocumentRegistry.exe") "frozen executable"
Require-File $UpdaterBuildExe "frozen updater"

$ModelSource = Join-Path $Root "models"
Copy-Item $ModelSource (Join-Path $PortableDir "models") -Recurse -Force

$UpdaterDir = Join-Path $PortableDir "updater"
New-Item -ItemType Directory -Path $UpdaterDir -Force | Out-Null
Copy-Item $UpdaterBuildExe (Join-Path $UpdaterDir "DocumentRegistryUpdater.exe") -Force

New-Item -ItemType Directory -Path (Join-Path $PortableDir "data") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $PortableDir "logs") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $PortableDir "backups") -Force | Out-Null
New-Item -ItemType File -Path (Join-Path $PortableDir "portable.flag") -Force | Out-Null

$VersionPayload = [ordered]@{
    version = $Version
    update_format_version = 1
    database_schema = $DatabaseSchema
}
Write-JsonUtf8NoBom $VersionPayload (Join-Path $PortableDir "version.json") 3

Assert-PortableRuntimeClean $PortableDir

Write-Host "[4/7] Running frozen application self-check..."
$ExePath = Join-Path $PortableDir "DocumentRegistry.exe"
Invoke-FrozenSelfCheck $ExePath $PortableDir "Frozen self-check" @("--self-check") (Join-Path $BuildDir "frozen_self_check.json")

Write-Host "[5/7] Initializing frozen OCR runtime..."
Invoke-FrozenSelfCheck $ExePath $PortableDir "Frozen OCR self-check" @("--ocr-self-check") (Join-Path $BuildDir "frozen_ocr_self_check.json")

$NerModelProbe = Join-Path $PortableDir "models\ner\rubert_tiny_news\model_int8.onnx"
if (Test-Path -LiteralPath $NerModelProbe -PathType Leaf) {
    Write-Host "[5b/7] Initializing frozen ML NER runtime..."
    Invoke-FrozenSelfCheck $ExePath $PortableDir "Frozen NER self-check" @("--ner-self-check") (Join-Path $BuildDir "frozen_ner_self_check.json")
}
else {
    Write-Host "[5b/7] ML NER model not installed; frozen NER self-check skipped."
}

Write-Host "[6/7] Running updater self-check..."
$UpdaterPath = Join-Path $UpdaterDir "DocumentRegistryUpdater.exe"
Invoke-FrozenSelfCheck $UpdaterPath $PortableDir "Updater self-check" @("--self-check")

Write-Host "[7/7] Creating final ZIP..."
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
New-ReleaseZipWithRetry $PortableDir $ReleaseZip
Require-File $ReleaseZip "release ZIP"

if (-not [string]::IsNullOrWhiteSpace($PublishTo)) {
    Publish-UpdatePackage $PortableDir $PublishTo $Version $ReleaseNotes
}

# build\ and dist\ are temporary. Keep them only when build/publish fails so they
# can be inspected. Once all requested work succeeds, remove both directories.
Write-Host "[cleanup] Removing temporary build and dist folders..."
foreach ($TempDir in @($BuildDir, $DistDir)) {
    if (Test-Path -LiteralPath $TempDir) {
        for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
            try {
                Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction Stop
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
if ([string]::IsNullOrWhiteSpace($PublishTo)) {
    Write-Host "After testing, publish this exact release with:"
    Write-Host ("  packaging\build.ps1 -Version {0} -PublishExisting -PublishTo `"<Google Drive folder>`"" -f $Version)
}
