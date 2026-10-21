[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "lay4flow-remote"),
  [int]$Port = 38000,
  [string]$ProjectName = "lay4flow-remote",
  [switch]$AlreadyStarted,
  [switch]$NoBrowser
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$compose = Join-Path $InstallDir "docker-compose.yml"
if (-not (Test-Path -LiteralPath $compose)) { throw "lay4flow-remote is not installed at $InstallDir." }
if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]{0,62}$') { throw "ProjectName is invalid." }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is required. Install it and retry." }
$savedErrorAction = $ErrorActionPreference
try {
  $ErrorActionPreference = "Continue"
  & docker info *> $null
  $infoExit = $LASTEXITCODE
  $osType = [string](& docker info --format '{{.OSType}}' 2>$null)
  $osTypeExit = $LASTEXITCODE
} finally {
  $ErrorActionPreference = $savedErrorAction
}
if ($infoExit -ne 0) {
  $dockerDesktop = @(
    (Join-Path ${env:ProgramFiles} "Docker\Docker\Docker Desktop.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
    (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
  if ($dockerDesktop) { Start-Process -FilePath $dockerDesktop -WindowStyle Hidden | Out-Null }
  $dockerDeadline = (Get-Date).AddSeconds(120)
  do {
    Start-Sleep -Seconds 2
    try {
      $ErrorActionPreference = "Continue"
      & docker info *> $null
      $infoExit = $LASTEXITCODE
      if ($infoExit -eq 0) {
        $osType = [string](& docker info --format '{{.OSType}}' 2>$null)
        $osTypeExit = $LASTEXITCODE
        break
      }
    } finally { $ErrorActionPreference = $savedErrorAction }
  } while ((Get-Date) -lt $dockerDeadline)
}
if ($infoExit -ne 0) { throw "Docker Desktop did not become ready within 120 seconds. Start it and retry." }
if ($osTypeExit -ne 0 -or $osType.Trim() -ne "linux") { throw "Docker Desktop must use Linux containers (WSL 2 engine)." }
$portValue = $Port
$envPath = Join-Path $InstallDir ".env"
$networkScript = Join-Path $PSScriptRoot "network.ps1"
if (Test-Path -LiteralPath $networkScript) {
  try { & $networkScript -InstallDir $InstallDir | Out-Null }
  catch { Write-Warning ("Host proxy discovery needs attention: " + $_.Exception.Message) }
}
if (Test-Path -LiteralPath $envPath) {
  $match = Select-String -LiteralPath $envPath -Pattern '^LAY4FLOW_REMOTE_PORT=(\d+)' | Select-Object -First 1
  if ($match) { $portValue = [int]$match.Matches[0].Groups[1].Value }
  if (-not $PSBoundParameters.ContainsKey("ProjectName")) {
    $projectMatch = Select-String -LiteralPath $envPath -Pattern '^COMPOSE_PROJECT_NAME=([a-z0-9][a-z0-9_-]{0,62})$' | Select-Object -First 1
    if ($projectMatch) { $ProjectName = $projectMatch.Matches[0].Groups[1].Value }
  }
}
$ownedContainers = @(& docker ps -aq --filter "label=com.docker.compose.project=$ProjectName")
if ($LASTEXITCODE -ne 0) { throw "Docker container ownership inspection failed." }
foreach ($id in $ownedContainers) {
  if (-not $id) { continue }
  $labels = (& docker inspect --format '{{json .Config.Labels}}' $id | ConvertFrom-Json)
  $owner = [string]$labels.'com.docker.compose.project.working_dir'
  if ($LASTEXITCODE -ne 0 -or -not $owner -or [IO.Path]::GetFullPath($owner).TrimEnd('\') -ne $InstallDir.TrimEnd('\')) {
    throw "Compose project '$ProjectName' belongs to another installation. Use its original directory or choose another ProjectName."
  }
}
if (-not $AlreadyStarted) {
  try {
    $ErrorActionPreference = "Continue"
    & docker compose --project-directory $InstallDir --env-file $envPath -p $ProjectName -f $compose up -d
    $composeExit = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $savedErrorAction
  }
  if ($composeExit -ne 0) { throw "Docker Compose could not start lay4flow-remote (exit code $composeExit)." }
}
$expectedVersion = (Get-Content -LiteralPath (Join-Path $InstallDir "VERSION") -Raw).Trim()
$deadline = (Get-Date).AddSeconds(120)
$healthy = $false
while ((Get-Date) -lt $deadline) {
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$portValue/health" -UseBasicParsing -TimeoutSec 5
    $status = $response.Content | ConvertFrom-Json
    if ($response.StatusCode -eq 200 -and $status.backend_running -eq $true -and $status.version -eq $expectedVersion) {
      $healthy = $true
      break
    }
  } catch { }
  Start-Sleep -Seconds 2
}
if (-not $healthy) { throw "lay4flow-remote did not become ready at http://127.0.0.1:$portValue/health." }
Write-Host "lay4flow-remote is ready: http://127.0.0.1:$portValue/login"
if (-not $NoBrowser) {
  $desktopExe = Join-Path $InstallDir ".lay4flow-windows\desktop\Lay4Flow.Desktop.exe"
  if (Test-Path -LiteralPath $desktopExe) {
    Start-Process -FilePath $desktopExe -ArgumentList @("--install-dir", ('"' + $InstallDir + '"')) -WorkingDirectory $InstallDir
  } else {
    Start-Process "http://127.0.0.1:$portValue/login"
  }
}
