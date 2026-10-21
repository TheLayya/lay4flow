[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [string]$Version,
    [string]$SourceArchive,
    [string]$ChecksumFile,
    [ValidateRange(2, 600)][int]$HealthTimeoutSeconds = 120,
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
$StateDir = Join-Path $InstallDir ".lay4flow-updates"
$Marker = Join-Path $InstallDir "data/update-maintenance"
$OriginalImageEnv = $env:LAY4FLOW_REMOTE_IMAGE
$Stage = $null
$Lock = $null
$Maintenance = $false
$Stopped = $false

function Docker([string[]]$Arguments) {
    & docker.exe --context $script:Context @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE): $($Arguments[0])" }
}

function Compose([string[]]$Arguments) {
    $composeOptions = @($script:ComposeArgs)
    $override = Join-Path $StateDir "image-override.json"
    if (Test-Path -LiteralPath $override) { $composeOptions += @("-f", $override) }
    Docker ($composeOptions + $Arguments)
}

function Helper([string[]]$Arguments) {
    Docker (@("run", "--rm", "--entrypoint", "python", "--mount", "type=bind,source=$InstallDir,target=/deploy", "--mount", "type=bind,source=$Stage,target=/update", $script:HelperImage, "/update/update_runtime.py") + $Arguments)
}

function Health([string]$ExpectedVersion = "") {
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
    do {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$script:Port/health" -TimeoutSec 8
            if ($health.backend_running -eq $true -and (-not $ExpectedVersion -or $health.version -eq $ExpectedVersion)) { return $health }
        } catch { }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Health/version check failed at port $script:Port (expected $ExpectedVersion)"
}

function Smoke {
    $page = Invoke-WebRequest -Uri "http://127.0.0.1:$script:Port/" -TimeoutSec 10 -UseBasicParsing
    if ($page.StatusCode -ne 200 -or $page.Content -notmatch '<html') { throw "Management page smoke check failed" }
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$script:Port/v1/models" -TimeoutSec 10 -UseBasicParsing | Out-Null
        throw "Public model API unexpectedly skipped authentication"
    } catch [System.Net.WebException] {
        if ([int]$_.Exception.Response.StatusCode -ne 401) { throw }
    }
}

function Restore([string]$BackupPath, [bool]$RestoreData) {
    $metadata = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $InstallDir "$BackupPath/metadata.json") | ConvertFrom-Json
    Compose @("stop", "--timeout", "120", "app") | Out-Host
    $args = @("restore", "--backup", "/deploy/$BackupPath")
    if ($RestoreData) { $args += "--restore-data" }
    Helper $args | Out-Host
    if ($metadata.image_id -and $metadata.image_id -ne $metadata.image) {
        Docker @("image", "tag", $metadata.image_id, $metadata.image) | Out-Host
    }
    $env:LAY4FLOW_REMOTE_IMAGE = $metadata.image
    Compose @("up", "-d", "--no-build", "--force-recreate", "app") | Out-Host
    Health $metadata.health_version | Out-Null
    Smoke
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "docker-compose.yml"))) { throw "The selected folder has no docker-compose.yml" }
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    try { $Lock = [IO.File]::Open((Join-Path $StateDir "update.lock"), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw "Another update is already running in $InstallDir" }
    $Context = (& docker.exe context show).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Context) { throw "Docker context is unavailable" }
    $ProjectName = "lay4flow-remote"
    if (Test-Path -LiteralPath (Join-Path $InstallDir ".env")) {
        foreach ($line in Get-Content -Encoding UTF8 -LiteralPath (Join-Path $InstallDir ".env")) {
            if ($line -match '^\s*COMPOSE_PROJECT_NAME\s*=\s*([a-z0-9_-]+)\s*$') { $ProjectName = $Matches[1] }
        }
    }
    $ComposeArgs = @("compose", "--project-name", $ProjectName, "--project-directory", $InstallDir, "-f", (Join-Path $InstallDir "docker-compose.yml"))
    if (Test-Path -LiteralPath (Join-Path $InstallDir ".env")) { $ComposeArgs += @("--env-file", (Join-Path $InstallDir ".env")) }
    Compose @("config", "--quiet") | Out-Host
    $container = @(Compose @("ps", "-q", "app")) | Select-Object -First 1
    if (-not $container) { throw "Start the existing installation before updating" }
    $HelperImage = @(Docker @("inspect", "--format", "{{.Image}}", $container)) | Select-Object -First 1
    if (-not $HelperImage) { throw "The current container image is unavailable" }
    $CurrentImage = @(Compose @("config", "--images")) | Select-Object -First 1
    if (-not $CurrentImage) { throw "The current Compose image is unavailable" }
    $labels = (@(Docker @("inspect", "--format", '{{json .Config.Labels}}', $container)) | Select-Object -First 1) | ConvertFrom-Json
    $owner = $labels.'com.docker.compose.project.working_dir'
    if (-not $owner -or ([IO.Path]::GetFullPath($owner) -ne $InstallDir)) { throw "Compose project belongs to another install directory: $owner" }
    $Port = 38000
    $portLine = if (Test-Path -LiteralPath (Join-Path $InstallDir ".env")) { Get-Content -Encoding UTF8 -LiteralPath (Join-Path $InstallDir ".env") | Where-Object { $_ -match '^\s*LAY4FLOW_REMOTE_PORT\s*=' } | Select-Object -First 1 }
    if ($portLine -match '=\s*(\d+)') { $Port = [int]$Matches[1] }
    $Stage = Join-Path ([IO.Path]::GetTempPath()) ("lay4flow-update-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $Stage | Out-Null
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "update_runtime.py") -Destination (Join-Path $Stage "update_runtime.py")

    if ($Rollback) {
        $latest = Get-ChildItem -LiteralPath (Join-Path $StateDir "backups") -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "metadata.json") } | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $latest) { throw "No update backup exists" }
        $BackupPath = ".lay4flow-updates/backups/$($latest.Name)"
    } else {
        if (-not $Version) {
            $release = Invoke-RestMethod -Uri "https://api.github.com/repos/TheLayya/lay4flow/releases/latest" -Headers @{ "User-Agent" = "lay4flow-remote-updater" }
            $Version = [string]$release.tag_name
        }
        $Version = $Version.TrimStart("v")
        if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$') { throw "Invalid release version" }
        $base = "https://github.com/TheLayya/lay4flow/releases/download/v$Version"
        if ($SourceArchive) { Copy-Item -LiteralPath $SourceArchive -Destination (Join-Path $Stage "runtime.tar.gz") }
        else { Invoke-WebRequest -Uri "$base/lay4flow-remote-$Version-runtime.tar.gz" -OutFile (Join-Path $Stage "runtime.tar.gz") -UseBasicParsing }
        if ($ChecksumFile) { Copy-Item -LiteralPath $ChecksumFile -Destination (Join-Path $Stage "SHA256SUMS") }
        else { Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile (Join-Path $Stage "SHA256SUMS") -UseBasicParsing }
        Helper @("prepare", "--version", $Version) | Out-Host
        $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss-fff")
        $CandidateImage = "lay4flow-remote:update-$Version-$stamp"
        Write-Host "Building verified candidate while the current service keeps running..."
        Docker @("build", "--build-arg", "APP_VERSION=$Version", "--tag", $CandidateImage, (Join-Path $Stage "candidate")) | Out-Host
        $BackupPath = ".lay4flow-updates/backups/$stamp"
    }

    if (Test-Path -LiteralPath $Marker) { throw "An update maintenance marker already exists: $Marker" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Marker) | Out-Null
    [IO.File]::WriteAllText($Marker, "host updater " + [DateTime]::UtcNow.ToString("o"))
    $Maintenance = $true
    Start-Sleep -Seconds 1
    $health = Health
    if ($health.update_active_requests -gt 0 -or $health.update_pending_imports -gt 0) { throw "Active requests or Profile imports remain; retry after they finish" }
    Helper @("idle") | Out-Host

    if ($Rollback) {
        Restore $BackupPath $false
        Write-Host "Rollback completed; current account data was kept."
    } else {
        Write-Host "Switching app container; a brief service interruption is expected."
        Compose @("stop", "--timeout", "120", "app") | Out-Host
        $Stopped = $true
        Helper @("snapshot", "--backup", "/deploy/$BackupPath", "--image", $CurrentImage, "--image-id", $HelperImage) | Out-Host
        $metadataFile = Join-Path $InstallDir "$BackupPath/metadata.json"
        $savedMetadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $metadataFile | ConvertFrom-Json
        $savedMetadata.health_version = [string]$health.version
        [IO.File]::WriteAllText($metadataFile, ($savedMetadata | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))
        try {
            Helper @("apply") | Out-Host
            Helper @("set-image", "--image", $CandidateImage) | Out-Host
            $env:LAY4FLOW_REMOTE_IMAGE = $CandidateImage
            Compose @("up", "-d", "--no-build", "--force-recreate", "app") | Out-Host
            Health $Version | Out-Null
            Smoke
            $Stopped = $false
            Set-Content -LiteralPath (Join-Path $StateDir "last-success.txt") -Value "$Version`n$BackupPath" -Encoding UTF8
            Write-Host "Update complete: lay4flow-remote $Version"
        } catch {
            $failure = $_
            Write-Warning "Candidate failed; restoring the previous image, files and pre-update data snapshot."
            Restore $BackupPath $true
            $Stopped = $false
            throw $failure
        }
    }
} finally {
    if ($Maintenance -and (Test-Path -LiteralPath $Marker)) { Remove-Item -LiteralPath $Marker -Force }
    if ($Stopped) { try { Compose @("up", "-d", "--no-build", "app") | Out-Host } catch { Write-Warning "Start the retained installation with docker compose up -d app." } }
    $env:LAY4FLOW_REMOTE_IMAGE = $OriginalImageEnv
    if ($Lock) { $Lock.Dispose() }
    if ($Stage -and (Test-Path -LiteralPath $Stage)) { Remove-Item -LiteralPath $Stage -Recurse -Force }
}
