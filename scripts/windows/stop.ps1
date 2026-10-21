[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "lay4flow-remote"),
  [string]$ProjectName = "lay4flow-remote"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$compose = Join-Path $InstallDir "docker-compose.yml"
if (-not (Test-Path -LiteralPath $compose)) { throw "lay4flow-remote is not installed at $InstallDir." }
$envPath = Join-Path $InstallDir ".env"
if ((Test-Path -LiteralPath $envPath) -and -not $PSBoundParameters.ContainsKey("ProjectName")) {
  $projectMatch = Select-String -LiteralPath $envPath -Pattern '^COMPOSE_PROJECT_NAME=([a-z0-9][a-z0-9_-]{0,62})$' | Select-Object -First 1
  if ($projectMatch) { $ProjectName = $projectMatch.Matches[0].Groups[1].Value }
}
$ownedContainers = @(& docker ps -aq --filter "label=com.docker.compose.project=$ProjectName")
if ($LASTEXITCODE -ne 0) { throw "Docker container ownership inspection failed." }
foreach ($id in $ownedContainers) {
  if (-not $id) { continue }
  $labels = (& docker inspect --format '{{json .Config.Labels}}' $id | ConvertFrom-Json)
  $owner = [string]$labels.'com.docker.compose.project.working_dir'
  if ($LASTEXITCODE -ne 0 -or -not $owner -or [IO.Path]::GetFullPath($owner).TrimEnd('\') -ne $InstallDir.TrimEnd('\')) {
    throw "Compose project '$ProjectName' belongs to another installation."
  }
}
& docker compose --project-directory $InstallDir --env-file $envPath -p $ProjectName -f $compose stop
if ($LASTEXITCODE -ne 0) { throw "Docker Compose stop failed." }
Write-Host "lay4flow-remote stopped. Data and configuration were preserved."
