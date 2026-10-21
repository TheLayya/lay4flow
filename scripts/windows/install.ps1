[CmdletBinding()]
param(
  [string]$BundleRoot = $PSScriptRoot,
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "lay4flow-remote"),
  [int]$Port = 38000,
  [switch]$NoBrowser,
  [switch]$NoShortcuts,
  [string]$ProjectName = "lay4flow-remote"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Stop-WithMessage([string]$Message) {
  throw $Message
}

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    Stop-WithMessage "Required command '$Name' was not found. Install Docker Desktop for Windows, then run this installer again."
  }
}

function Get-ComposeCommand {
  $null = & docker compose version 2>$null
  if ($LASTEXITCODE -eq 0) { return @("docker", "compose") }
  Stop-WithMessage "Docker Compose was not found. Install Docker Desktop for Windows and enable the WSL 2 engine."
}

function Invoke-Compose([string[]]$Compose, [string[]]$Arguments) {
  $command = $Compose[0]
  $args = @()
  if ($Compose.Count -gt 1) { $args += $Compose[1..($Compose.Count - 1)] }
  $args += $Arguments
  & $command @args
  if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed with exit code $LASTEXITCODE." }
}

function Test-DockerReady {
  & docker info *> $null
  if ($LASTEXITCODE -ne 0) { return $false }
  $osType = (& docker info --format '{{.OSType}}' 2>$null).Trim()
  return $LASTEXITCODE -eq 0 -and $osType -eq "linux"
}

function Wait-Docker([int]$Seconds = 120) {
  if (Test-DockerReady) { return }
  $desktop = @(
    (Join-Path ${env:ProgramFiles} "Docker\Docker\Docker Desktop.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
    (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
  ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
  if ($desktop) {
    Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
  }
  $deadline = (Get-Date).AddSeconds($Seconds)
  do {
    Start-Sleep -Seconds 2
    if (Test-DockerReady) { return }
    Write-Host "Waiting for Docker Desktop..."
  } while ((Get-Date) -lt $deadline)
  Stop-WithMessage "Docker Desktop did not become ready within $Seconds seconds. Start it manually and retry."
}

function Assert-ArchiveSafe([string]$Archive) {
  $listing = & tar -tzf $Archive
  if ($LASTEXITCODE -ne 0) { Stop-WithMessage "The runtime archive could not be read." }
  foreach ($entry in $listing) {
    $name = ([string]$entry).Trim().Replace('/', '\')
    if ([IO.Path]::IsPathRooted($name) -or $name -match '(^|\\)\.\.(\\|$)' -or $name -match '^[A-Za-z]:') {
      Stop-WithMessage "The runtime archive contains an unsafe path: $entry"
    }
    if ($name -eq ".env" -or $name -eq "config\setting.toml" -or $name -match '^(data|tmp|output|outputs|\.git)\\') {
      Stop-WithMessage "The runtime archive contains mutable or private state: $entry"
    }
  }
  $details = & tar -tvzf $Archive
  if ($LASTEXITCODE -ne 0) { Stop-WithMessage "The runtime archive details could not be read." }
  foreach ($entry in $details) {
    if (-not ([string]$entry).StartsWith("-")) {
      Stop-WithMessage "The runtime archive must contain regular files only; links and special entries are rejected."
    }
  }
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkingDirectory) {
  $shell = New-Object -ComObject WScript.Shell
  if (Test-Path -LiteralPath $Path) {
    $existing = $shell.CreateShortcut($Path)
    if ($existing.WorkingDirectory -and [IO.Path]::GetFullPath($existing.WorkingDirectory).TrimEnd('\') -ne $WorkingDirectory.TrimEnd('\')) {
      Write-Warning "A desktop shortcut at '$Path' belongs to another installation and was preserved."
      return
    }
  }
  $shortcut = $shell.CreateShortcut($Path)
  $shortcut.TargetPath = $Target
  $shortcut.Arguments = $Arguments
  $shortcut.WorkingDirectory = $WorkingDirectory
  $shortcut.Description = "lay4flow-remote"
  $shortcut.Save()
}

if ($Port -lt 1 -or $Port -gt 65535) { Stop-WithMessage "Port must be between 1 and 65535." }
if (-not [Environment]::Is64BitOperatingSystem) { Stop-WithMessage "Windows x64 is required for this bundle." }
if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]{0,62}$') { Stop-WithMessage "ProjectName must use lowercase letters, numbers, hyphens or underscores." }
$BundleRoot = (Resolve-Path -LiteralPath $BundleRoot).Path
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host "Install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/"
  if (-not $NoBrowser) { Start-Process "https://docs.docker.com/desktop/setup/install/windows-install/" }
  Stop-WithMessage "Install Docker Desktop, enable the WSL 2 engine with Linux containers, then run install.cmd again."
}
Require-Command "tar"
$archive = Get-ChildItem -LiteralPath (Join-Path $BundleRoot "runtime") -Filter "*-runtime.tar.gz" -File | Select-Object -First 1
if (-not $archive) { Stop-WithMessage "No runtime archive was found under '$BundleRoot\runtime'." }
$checksumFile = Join-Path $BundleRoot "runtime\SHA256SUMS"
if (-not (Test-Path -LiteralPath $checksumFile)) { Stop-WithMessage "Runtime checksum file is missing." }
$checksumLine = Get-Content -LiteralPath $checksumFile | Where-Object { $_ -match ('^([a-fA-F0-9]{64})\s+\*?' + [regex]::Escape($archive.Name) + '$') } | Select-Object -First 1
if (-not $checksumLine) { Stop-WithMessage "The runtime checksum entry is missing or malformed." }
$expected = ($checksumLine -split '\s+')[0]
$actual = (Get-FileHash -LiteralPath $archive.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected.ToLowerInvariant()) { Stop-WithMessage "Runtime archive checksum verification failed." }
Assert-ArchiveSafe $archive.FullName
Wait-Docker
$compose = Get-ComposeCommand

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$installEnv = Join-Path $InstallDir ".env"
if ((Test-Path -LiteralPath $installEnv) -and -not $PSBoundParameters.ContainsKey("ProjectName")) {
  $projectMatch = Select-String -LiteralPath $installEnv -Pattern '^COMPOSE_PROJECT_NAME=([a-z0-9][a-z0-9_-]{0,62})$' | Select-Object -First 1
  if ($projectMatch) { $ProjectName = $projectMatch.Matches[0].Groups[1].Value }
}
$ownedContainers = @(& docker ps -aq --filter "label=com.docker.compose.project=$ProjectName")
if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Docker container ownership inspection failed." }
foreach ($id in $ownedContainers) {
  if (-not $id) { continue }
  $inspection = @(& docker inspect $id | ConvertFrom-Json)
  $owner = [string]$inspection[0].Config.Labels.'com.docker.compose.project.working_dir'
  if ($LASTEXITCODE -ne 0 -or -not $owner) { Stop-WithMessage "The Compose project $ProjectName already exists; choose another ProjectName." }
  if ([IO.Path]::GetFullPath($owner).TrimEnd('\') -ne $InstallDir.TrimEnd('\')) {
    Stop-WithMessage "Compose project '$ProjectName' belongs to '$owner'. Choose a different ProjectName or use that existing installation."
  }
}
if (Test-Path -LiteralPath (Join-Path $InstallDir "docker-compose.yml")) {
  Write-Host "An existing installation was found at $InstallDir. Its files and data will be preserved."
  & (Join-Path $BundleRoot "windows\start.ps1") -InstallDir $InstallDir -Port $Port -ProjectName $ProjectName -NoBrowser:$NoBrowser
  exit 0
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$stage = Join-Path ([IO.Path]::GetTempPath()) ("lay4flow-runtime-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
try {
  & tar -xzf $archive.FullName -C $stage
  if ($LASTEXITCODE -ne 0) { throw "Runtime archive extraction failed." }
  $sourceRoot = $stage
  if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot "VERSION"))) { throw "Runtime archive is missing VERSION." }
  $existingConfig = Join-Path $InstallDir "config\setting.toml"
  $existingEnv = Join-Path $InstallDir ".env"
  Get-ChildItem -LiteralPath $sourceRoot -Force -File -Recurse | ForEach-Object {
    $relative = $_.FullName.Substring($sourceRoot.Length + 1)
    if ($relative -eq ".env" -or $relative -eq "config\setting.toml" -or $relative -match '^(data|tmp)\\') {
      throw "Runtime archive contains a mutable state file: $relative"
    }
    $destination = Join-Path $InstallDir $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "config"), (Join-Path $InstallDir "data"), (Join-Path $InstallDir "tmp") | Out-Null
  if (-not (Test-Path -LiteralPath $existingConfig)) {
    Copy-Item -LiteralPath (Join-Path $InstallDir "config\setting_example.toml") -Destination $existingConfig
  }
  if (-not (Test-Path -LiteralPath $existingEnv)) {
    @("LAY4FLOW_REMOTE_PORT=$Port", "COMPOSE_PROJECT_NAME=$ProjectName") | Set-Content -LiteralPath $existingEnv -Encoding ascii
  }
  $bundleWindows = Join-Path $BundleRoot "windows"
  $windowsDir = Join-Path $InstallDir ".lay4flow-windows"
  New-Item -ItemType Directory -Force -Path $windowsDir | Out-Null
  Get-ChildItem -LiteralPath $bundleWindows -File | Copy-Item -Destination $windowsDir -Force
  $envPath = Join-Path $InstallDir ".env"
  $envText = if (Test-Path -LiteralPath $envPath) { Get-Content -LiteralPath $envPath -Raw } else { "" }
  if ($envText -notmatch '(?m)^LAY4FLOW_REMOTE_PORT=') {
    Add-Content -LiteralPath $envPath -Value "LAY4FLOW_REMOTE_PORT=$Port"
  }
  if ($envText -notmatch '(?m)^COMPOSE_PROJECT_NAME=') {
    Add-Content -LiteralPath $envPath -Value "COMPOSE_PROJECT_NAME=$ProjectName"
  }
  Invoke-Compose $compose @("--project-directory", $InstallDir, "--env-file", $existingEnv, "-p", $ProjectName, "-f", (Join-Path $InstallDir "docker-compose.yml"), "up", "-d", "--build")
  & (Join-Path $windowsDir "start.ps1") -InstallDir $InstallDir -Port $Port -ProjectName $ProjectName -AlreadyStarted -NoBrowser
  if (-not $NoShortcuts) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $ps = (Get-Command powershell.exe).Source
    $shortcutName = if ($ProjectName -eq "lay4flow-remote") { "lay4flow-remote" } else { "lay4flow-remote ($ProjectName)" }
    New-Shortcut (Join-Path $desktop "$shortcutName.lnk") $ps "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$windowsDir\start.ps1`" -InstallDir `"$InstallDir`" -Port $Port -ProjectName $ProjectName" $InstallDir
    New-Shortcut (Join-Path $desktop "$shortcutName update.lnk") $ps "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$windowsDir\update.ps1`" -InstallDir `"$InstallDir`"" $InstallDir
  }
  Write-Host "lay4flow-remote is installed at $InstallDir"
  if (-not $NoBrowser) {
    & (Join-Path $windowsDir "start.ps1") -InstallDir $InstallDir -Port $Port -ProjectName $ProjectName -AlreadyStarted
  }
} finally {
  Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
