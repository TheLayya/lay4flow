[CmdletBinding()]
param(
  [string]$BundleRoot = $PSScriptRoot,
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "lay4flow-remote"),
  [int]$Port = 38000,
  [switch]$NoBrowser,
  [switch]$NoShortcuts,
  [string]$ProjectName = "lay4flow-remote",
  [switch]$ResolveConflicts,
  [string]$LogPath = (Join-Path ([IO.Path]::GetTempPath()) "lay4flow-remote-install.log")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$script:InstallLogPath = [IO.Path]::GetFullPath($LogPath)
$script:LastCommand = ""
$script:InstallPathValidated = $false

function Write-InstallLog([string]$Message) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"), $Message
  try {
    $parent = Split-Path -Parent $script:InstallLogPath
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Add-Content -LiteralPath $script:InstallLogPath -Value $line -Encoding utf8
  } catch {
    # Logging must never hide the original installation error.
  }
  Write-Host $Message
}

function Copy-InstallLogToTarget {
  try {
    if (-not $script:InstallPathValidated -or -not $InstallDir -or -not (Test-Path -LiteralPath $InstallDir -PathType Container)) { return }
    $target = Join-Path ([IO.Path]::GetFullPath($InstallDir)) ".lay4flow-install.log"
    $parent = Split-Path -Parent $target
    if (Test-Path -LiteralPath $script:InstallLogPath) {
      Copy-Item -LiteralPath $script:InstallLogPath -Destination $target -Force
    }
  } catch {
    # The temp log remains the authoritative diagnostic when the target is unavailable.
  }
}

function Write-InstallError([System.Management.Automation.ErrorRecord]$ErrorRecord) {
  $message = @(
    $ErrorRecord.Exception.Message,
    "Command: $script:LastCommand",
    "Location: $($ErrorRecord.InvocationInfo.PositionMessage)",
    "Details:",
    ($ErrorRecord | Out-String)
  ) -join [Environment]::NewLine
  try {
    Add-Content -LiteralPath $script:InstallLogPath -Value $message -Encoding utf8
    Set-Content -LiteralPath ($script:InstallLogPath + ".error.txt") -Value $message -Encoding utf8
  } catch {
    Write-Host "Could not write the installation diagnostic log: $($_.Exception.Message)"
  }
  Copy-InstallLogToTarget
  Write-Host ("ERROR: $($ErrorRecord.Exception.Message) Diagnostic log: $script:InstallLogPath")
}

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
  $display = $command + " " + ($args -join " ")
  $script:LastCommand = $display
  Write-InstallLog "Running: $display"
  # Docker writes normal build progress to stderr. Windows PowerShell must use
  # the native process exit code, rather than treating progress as a failure.
  $savedErrorAction = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & $command @args 2>&1 | ForEach-Object { Write-InstallLog ([string]$_) }
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $savedErrorAction
  }
  Write-InstallLog "Command exit code: $exitCode"
  if ($exitCode -ne 0) { throw "Docker Compose failed with exit code $exitCode. See $script:InstallLogPath." }
}

function Test-DockerReady {
  $savedErrorAction = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    $osType = [string](& docker info --format '{{.OSType}}' 2>$null)
    return $LASTEXITCODE -eq 0 -and $osType.Trim() -eq "linux"
  } finally {
    $ErrorActionPreference = $savedErrorAction
  }
}

function Get-ProjectOwner([string]$Name) {
  $script:LastCommand = "docker ps -aq --filter label=com.docker.compose.project=$Name"
  $savedErrorAction = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $ids = @(& docker ps -aq --filter "label=com.docker.compose.project=$Name" 2>&1)
    $listExit = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $savedErrorAction
  }
  if ($listExit -ne 0) {
    Write-InstallLog (($ids | Out-String).Trim())
    Stop-WithMessage "Docker container ownership inspection failed (exit code $listExit). See $script:InstallLogPath."
  }
  foreach ($id in $ids) {
    if (-not $id) { continue }
    $script:LastCommand = "docker inspect --format {{json .Config.Labels}} $id"
    try {
      $ErrorActionPreference = "Continue"
      $rawLabels = @(& docker inspect --format '{{json .Config.Labels}}' $id 2>&1)
      $inspectExit = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $savedErrorAction
    }
    if ($inspectExit -ne 0) {
      Write-InstallLog (($rawLabels | Out-String).Trim())
      Stop-WithMessage "Docker ownership inspection failed for container '$id' (exit code $inspectExit). See $script:InstallLogPath."
    }
    try {
      $labels = (($rawLabels -join "`n") | ConvertFrom-Json)
      $ownerProperty = $labels.PSObject.Properties['com.docker.compose.project.working_dir']
      $owner = if ($ownerProperty) { [string]$ownerProperty.Value } else { "" }
    } catch {
      Write-InstallLog (($rawLabels | Out-String).Trim())
      Stop-WithMessage "Docker returned malformed ownership metadata for container '$id'. See $script:InstallLogPath."
    }
    if (-not $owner) {
      Stop-WithMessage "Compose project '$Name' already exists but has no working directory label. Choose -ProjectName lay4flow-remote-new. See $script:InstallLogPath."
    }
    if ([IO.Path]::GetFullPath($owner).TrimEnd('\') -ine $InstallDir.TrimEnd('\')) { return $owner }
  }
  return ""
}

function Test-PortAvailable([int]$Value) {
  $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Value)
  try {
    $listener.Server.ExclusiveAddressUse = $true
    $listener.Start()
    return $true
  } catch {
    return $false
  } finally {
    $listener.Stop()
  }
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
  $icon = Join-Path $WorkingDirectory ".lay4flow-windows\tkcrm.ico"
  if (Test-Path -LiteralPath $icon) { $shortcut.IconLocation = "$icon,0" }
  $shortcut.Description = "lay4flow-remote"
  $shortcut.Save()
}

function Copy-LauncherAssets([string]$Bundle, [string]$Target) {
  $windowsDir = Join-Path $Target ".lay4flow-windows"
  New-Item -ItemType Directory -Force -Path $windowsDir | Out-Null
  Get-ChildItem -LiteralPath (Join-Path $Bundle "windows") -File | Copy-Item -Destination $windowsDir -Force
  $desktopSource = Join-Path $Bundle "desktop\windows-x64"
  if (Test-Path -LiteralPath $desktopSource) {
    $desktopTarget = Join-Path $windowsDir "desktop"
    New-Item -ItemType Directory -Force -Path $desktopTarget | Out-Null
    Get-ChildItem -LiteralPath $desktopSource -Force | Copy-Item -Destination $desktopTarget -Recurse -Force
  }
  return $windowsDir
}

function Sync-HostNetwork([string]$WindowsDir) {
  $networkScript = Join-Path $WindowsDir "network.ps1"
  if (-not (Test-Path -LiteralPath $networkScript)) { return }
  try { & $networkScript -InstallDir $InstallDir | Out-Null }
  catch { Write-InstallLog ("Host proxy discovery needs attention: " + $_.Exception.Message) }
}

function New-ApplicationShortcuts([string]$WindowsDir) {
  $desktop = [Environment]::GetFolderPath("Desktop")
  $ps = (Get-Command powershell.exe).Source
  $shortcutName = if ($ProjectName -eq "lay4flow-remote") { "lay4flow-remote" } else { "lay4flow-remote ($ProjectName)" }
  $desktopExe = Join-Path $WindowsDir "desktop\Lay4Flow.Desktop.exe"
  if (Test-Path -LiteralPath $desktopExe) {
    New-Shortcut (Join-Path $desktop "$shortcutName.lnk") $desktopExe "--install-dir `"$InstallDir`"" $InstallDir
  } else {
    New-Shortcut (Join-Path $desktop "$shortcutName.lnk") $ps "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$WindowsDir\start.ps1`" -InstallDir `"$InstallDir`" -Port $Port -ProjectName $ProjectName" $InstallDir
  }
  # Updates are exposed in the desktop app's update log.  Keep the desktop
  # clean and remove the shortcut created by older bundles for this install.
  Remove-LegacyUpdateShortcut (Join-Path $desktop "$shortcutName update.lnk")
}

function Remove-LegacyUpdateShortcut([string]$Path) {
  if (Test-Path -LiteralPath $Path) {
    $shell = New-Object -ComObject WScript.Shell
    $existing = $shell.CreateShortcut($Path)
    if ($existing.WorkingDirectory -and [IO.Path]::GetFullPath($existing.WorkingDirectory).TrimEnd('\') -eq $InstallDir.TrimEnd('\')) {
      Remove-Item -LiteralPath $Path -Force
    }
  }
}

function Get-ApplicationShortcutName {
  if ($ProjectName -eq "lay4flow-remote") { return "lay4flow-remote" }
  return "lay4flow-remote ($ProjectName)"
}

try {
$logParent = Split-Path -Parent $script:InstallLogPath
if ($logParent) { New-Item -ItemType Directory -Force -Path $logParent | Out-Null }
Set-Content -LiteralPath $script:InstallLogPath -Value "lay4flow-remote installer diagnostic" -Encoding utf8
if (Test-Path -LiteralPath ($script:InstallLogPath + ".error.txt")) {
  Remove-Item -LiteralPath ($script:InstallLogPath + ".error.txt") -Force
}
Write-InstallLog "Starting lay4flow-remote installation. BundleRoot='$BundleRoot'; InstallDir='$InstallDir'; ProjectName='$ProjectName'; Port=$Port."
if ($Port -lt 1 -or $Port -gt 65535) { Stop-WithMessage "Port must be between 1 and 65535." }
if (-not [Environment]::Is64BitOperatingSystem) { Stop-WithMessage "Windows x64 is required for this bundle." }
if ($ProjectName -cnotmatch '^[a-z0-9][a-z0-9_-]{0,62}$') { Stop-WithMessage "ProjectName must use lowercase letters, numbers, hyphens or underscores." }
if (-not $BundleRoot) { $BundleRoot = $PSScriptRoot }
$BundleRoot = (Resolve-Path -LiteralPath $BundleRoot).Path
Write-InstallLog "Resolved BundleRoot='$BundleRoot'."
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
Write-InstallLog "Verified runtime archive '$($archive.Name)' ($actual)."
Assert-ArchiveSafe $archive.FullName
Wait-Docker
$compose = Get-ComposeCommand
Write-InstallLog "Docker Desktop is ready and Compose is available."

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$protectedRoots = @($env:WINDIR, [IO.Path]::GetPathRoot($InstallDir))
foreach ($protectedRoot in $protectedRoots) {
  if ($protectedRoot -and $InstallDir.TrimEnd('\') -eq ([IO.Path]::GetFullPath($protectedRoot)).TrimEnd('\')) {
    Stop-WithMessage "Choose an application folder as InstallDir; system and drive roots are not installation targets."
  }
}
$script:InstallPathValidated = $true
$installEnv = Join-Path $InstallDir ".env"
if (Test-Path -LiteralPath $installEnv) {
  $portMatch = Select-String -LiteralPath $installEnv -Pattern '^LAY4FLOW_REMOTE_PORT=(\d+)' | Select-Object -First 1
  if ($portMatch) { $Port = [int]$portMatch.Matches[0].Groups[1].Value }
}
if ((Test-Path -LiteralPath $installEnv) -and -not $PSBoundParameters.ContainsKey("ProjectName")) {
  $projectMatch = Select-String -LiteralPath $installEnv -Pattern '^COMPOSE_PROJECT_NAME=([a-z0-9][a-z0-9_-]{0,62})$' | Select-Object -First 1
  if ($projectMatch) { $ProjectName = $projectMatch.Matches[0].Groups[1].Value }
}
$projectOwner = Get-ProjectOwner $ProjectName
if ($projectOwner) {
  if ($ResolveConflicts -and -not $PSBoundParameters.ContainsKey("ProjectName") -and -not (Test-Path -LiteralPath $installEnv)) {
    $hashAlgorithm = [Security.Cryptography.SHA256]::Create()
    try {
      $pathHash = ([BitConverter]::ToString($hashAlgorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallDir.ToLowerInvariant())))).Replace('-', '').ToLowerInvariant().Substring(0, 8)
    } finally {
      $hashAlgorithm.Dispose()
    }
    Write-InstallLog "Compose project '$ProjectName' is already used by '$projectOwner'; preserving it and selecting an independent project."
    $ProjectName = "lay4flow-remote-$pathHash"
    $projectOwner = Get-ProjectOwner $ProjectName
  }
  if ($projectOwner) {
    Stop-WithMessage "Compose project '$ProjectName' belongs to '$projectOwner'. This installer targets '$InstallDir'. Choose -ProjectName lay4flow-remote-new or install into '$projectOwner'. See $script:InstallLogPath."
  }
}
if (-not (Test-Path -LiteralPath $installEnv)) {
  if (-not (Test-PortAvailable $Port)) {
    if (-not $ResolveConflicts -or $PSBoundParameters.ContainsKey("Port")) {
      Stop-WithMessage "Port $Port is already in use. Choose an unused -Port, for example -Port 38002. See $script:InstallLogPath."
    }
    $originalPort = $Port
    while ($Port -le 65535 -and -not (Test-PortAvailable $Port)) { $Port++ }
    if ($Port -gt 65535) { Stop-WithMessage "No available local port was found from $originalPort through 65535." }
    Write-InstallLog "Port $originalPort is already in use; selecting available port $Port."
  }
}
Write-InstallLog "Installation target: '$InstallDir'; Compose project: '$ProjectName'; port: $Port."
if (Test-Path -LiteralPath (Join-Path $InstallDir "docker-compose.yml")) {
  Write-Host "An existing installation was found at $InstallDir. Its files and data will be preserved."
  # Refresh the launcher assets so reinstalling a bundle also updates the shortcut icon.
  $windowsDir = Copy-LauncherAssets $BundleRoot $InstallDir
  Remove-LegacyUpdateShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "$(Get-ApplicationShortcutName) update.lnk")
  if (-not $NoShortcuts) { New-ApplicationShortcuts $windowsDir }
  & (Join-Path $BundleRoot "windows\start.ps1") -InstallDir $InstallDir -Port $Port -ProjectName $ProjectName -NoBrowser:$NoBrowser
  Write-InstallLog "Existing installation was started successfully."
  Copy-InstallLogToTarget
  return
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
  $windowsDir = Copy-LauncherAssets $BundleRoot $InstallDir
  $envPath = Join-Path $InstallDir ".env"
  $envText = if (Test-Path -LiteralPath $envPath) { Get-Content -LiteralPath $envPath -Raw } else { "" }
  if ($envText -notmatch '(?m)^LAY4FLOW_REMOTE_PORT=') {
    Add-Content -LiteralPath $envPath -Value "LAY4FLOW_REMOTE_PORT=$Port"
  }
  if ($envText -notmatch '(?m)^COMPOSE_PROJECT_NAME=') {
    Add-Content -LiteralPath $envPath -Value "COMPOSE_PROJECT_NAME=$ProjectName"
  }
  Sync-HostNetwork $windowsDir
  Invoke-Compose $compose @("--project-directory", $InstallDir, "--env-file", $existingEnv, "-p", $ProjectName, "-f", (Join-Path $InstallDir "docker-compose.yml"), "up", "-d", "--build")
  & (Join-Path $windowsDir "start.ps1") -InstallDir $InstallDir -Port $Port -ProjectName $ProjectName -AlreadyStarted -NoBrowser
  Remove-LegacyUpdateShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "$(Get-ApplicationShortcutName) update.lnk")
  if (-not $NoShortcuts) { New-ApplicationShortcuts $windowsDir }
  Write-Host "lay4flow-remote is installed at $InstallDir"
  Write-InstallLog "lay4flow-remote is installed at $InstallDir."
  Copy-InstallLogToTarget
  if (-not $NoBrowser) {
    & (Join-Path $windowsDir "start.ps1") -InstallDir $InstallDir -Port $Port -ProjectName $ProjectName -AlreadyStarted
  }
} finally {
  Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
} catch {
  Write-InstallError $_
  throw
}
