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
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$compose = Join-Path $InstallDir "docker-compose.yml"
if (-not (Test-Path -LiteralPath $compose)) { throw "lay4flow-remote is not installed at $InstallDir." }
if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]{0,62}$') { throw "ProjectName is invalid." }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is required. Install it and retry." }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running. Start it and retry." }
$osType = (& docker info --format '{{.OSType}}' 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $osType -ne "linux") { throw "Docker Desktop must use Linux containers (WSL 2 engine)." }
$portValue = $Port
$envPath = Join-Path $InstallDir ".env"
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
  $inspection = @(& docker inspect $id | ConvertFrom-Json)
  $owner = [string]$inspection[0].Config.Labels.'com.docker.compose.project.working_dir'
  if ($LASTEXITCODE -ne 0 -or -not $owner -or [IO.Path]::GetFullPath($owner).TrimEnd('\') -ne $InstallDir.TrimEnd('\')) {
    throw "Compose project '$ProjectName' belongs to another installation. Use its original directory or choose another ProjectName."
  }
}
if (-not $AlreadyStarted) {
  & docker compose --project-directory $InstallDir --env-file $envPath -p $ProjectName -f $compose up -d
  if ($LASTEXITCODE -ne 0) { throw "Docker Compose could not start lay4flow-remote." }
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
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$portValue/login" }
