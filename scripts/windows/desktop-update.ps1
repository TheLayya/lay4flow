[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstallDir,
  [ValidateSet("Prepare", "Apply")][string]$Phase = "Prepare",
  [string]$Version,
  [int]$ParentProcessId = 0
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$stateDir = Join-Path $InstallDir ".lay4flow-updates"
$candidateFile = Join-Path $stateDir "desktop-candidate.json"
$nativeDir = Join-Path $InstallDir ".lay4flow-windows\desktop"
$desktopExe = Join-Path $nativeDir "Lay4Flow.Desktop.exe"
$logFile = Join-Path $stateDir "desktop-update.log"
$statusFile = Join-Path $stateDir "desktop-update-status.json"

function Write-UpdateLog([string]$Message) {
  Add-Content -LiteralPath $logFile -Value ("[{0:O}] {1}" -f [DateTimeOffset]::Now, $Message) -Encoding utf8
  Write-Host $Message
}

function Save-UpdateStatus([string]$State, [string]$Message) {
  [IO.File]::WriteAllText($statusFile, (@{ state = $State; message = $Message } | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
}

function Start-VerifiedDesktop([string]$ExpectedVersion) {
  $ready = Join-Path $stateDir ("desktop-ready-" + [Guid]::NewGuid().ToString('N') + '.json')
  $port = 38000
  $envPath = Join-Path $InstallDir '.env'
  if (Test-Path -LiteralPath $envPath) {
    $portLine = Select-String -LiteralPath $envPath -Pattern '^LAY4FLOW_REMOTE_PORT=(\d+)$' | Select-Object -First 1
    if ($portLine) { $port = [int]$portLine.Matches[0].Groups[1].Value }
  }
  $started = Start-Process -FilePath $desktopExe -ArgumentList @('--install-dir', ('"' + $InstallDir + '"'), '--ready-file', ('"' + $ready + '"')) -WorkingDirectory $InstallDir -PassThru
  try {
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
      if (Test-Path -LiteralPath $ready) {
        try {
          $actual = Get-Content -LiteralPath $ready -Raw -Encoding utf8 | ConvertFrom-Json
          $uri = [Uri]$actual.url
          if ([int]$actual.pid -eq $started.Id -and [string]$actual.version -eq $ExpectedVersion -and [long]$actual.window_handle -gt 0 -and $uri.Scheme -eq 'http' -and $uri.IsLoopback -and $uri.Port -eq $port) { return }
        } catch { }
      }
      if ($started.HasExited) { throw 'The updated desktop exited before it was ready' }
      Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The updated desktop did not confirm its version and window within 90 seconds'
  } catch {
    if (-not $started.HasExited) {
      $started.Kill()
      $started.WaitForExit(10000) | Out-Null
    }
    throw
  } finally {
    if (Test-Path -LiteralPath $ready) { Remove-Item -LiteralPath $ready -Force }
  }
}

function Get-VerifiedDesktopCandidate([string]$TargetVersion) {
  if ($TargetVersion -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid desktop release version" }
  $assetName = "lay4flow-remote-$TargetVersion-windows-x64-setup.exe"
  $base = "https://github.com/TheLayya/lay4flow/releases/download/v$TargetVersion"
  $stage = Join-Path $stateDir ("desktop-" + $TargetVersion + "-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $stage | Out-Null
  $setupPath = Join-Path $stage $assetName
  $checksumsPath = Join-Path $stage "SHA256SUMS"
  Write-UpdateLog "Downloading desktop release $TargetVersion..."
  Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile $checksumsPath -UseBasicParsing -TimeoutSec 120
  $entries = @(Get-Content -LiteralPath $checksumsPath | Where-Object { $_ -match ('^([a-fA-F0-9]{64})\s+\*?' + [Regex]::Escape($assetName) + '$') })
  if ($entries.Count -ne 1) { throw "The desktop setup checksum entry is missing or ambiguous" }
  $expectedHash = ($entries[0] -split '\s+')[0].ToLowerInvariant()
  Invoke-WebRequest -Uri "$base/$assetName" -OutFile $setupPath -UseBasicParsing -TimeoutSec 600
  if ((Get-Item -LiteralPath $setupPath).Length -gt 300MB) { throw "The desktop setup exceeds the permitted package size" }
  if ((Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) { throw "Desktop setup checksum verification failed" }
  Write-UpdateLog "Desktop setup verified."
  return @{ version = $TargetVersion; setup = $setupPath; sha256 = $expectedHash }
}

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
try {
  if ($Phase -eq "Prepare") {
    if (-not (Test-Path -LiteralPath $desktopExe)) { throw "This installation has no desktop runtime" }
    if (-not $Version) {
      $release = Invoke-RestMethod -Uri "https://api.github.com/repos/TheLayya/lay4flow/releases/latest" -Headers @{ "User-Agent" = "lay4flow-remote-desktop-updater" } -TimeoutSec 60
      $Version = ([string]$release.tag_name).TrimStart('v')
    }
    $candidate = Get-VerifiedDesktopCandidate $Version
    Write-UpdateLog "Updating the service; the current window stays open during preparation."
    $runtimeUpdater = Join-Path $InstallDir ".lay4flow-windows\update.ps1"
    if (-not (Test-Path -LiteralPath $runtimeUpdater)) { throw "The Docker updater is missing from this installation" }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $runtimeUpdater -InstallDir $InstallDir -Version $Version
    if ($LASTEXITCODE -ne 0) { throw "Service update failed; the desktop runtime was preserved" }
    [IO.File]::WriteAllText($candidateFile, ($candidate | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Write-UpdateLog "The desktop update is ready to apply."
    exit 0
  }

  if ($ParentProcessId -le 0) { throw "Desktop update requires the originating process ID" }
  $candidate = Get-Content -LiteralPath $candidateFile -Raw -Encoding utf8 | ConvertFrom-Json
  $setup = [IO.Path]::GetFullPath([string]$candidate.setup)
  $allowedRoot = [IO.Path]::GetFullPath($stateDir).TrimEnd('\') + '\'
  if (-not $setup.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Desktop setup is outside its staging directory" }
  if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $candidate.sha256) { throw "Staged desktop setup changed after verification" }
  $process = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue
  if ($process -and ([IO.Path]::GetFullPath($process.Path) -ine $desktopExe)) { throw "The originating process belongs to a different application" }
  if ($process) {
    Write-UpdateLog "Waiting for the desktop window to close..."
    if (-not $process.WaitForExit(30000)) { throw "The desktop window did not close; update was left staged" }
  }
  $backup = Join-Path (Split-Path -Parent $setup) "previous-desktop"
  New-Item -ItemType Directory -Path $backup | Out-Null
  Get-ChildItem -LiteralPath $nativeDir -Force | Copy-Item -Destination $backup -Recurse -Force
  $setupProcess = $null
  try {
    Write-UpdateLog "Applying desktop update $($candidate.version)..."
    $installLog = Join-Path $stateDir "desktop-setup.log"
    $setupArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/DIR="' + $InstallDir + '"'), ('/LOG="' + $installLog + '"'))
    $setupProcess = Start-Process -FilePath $setup -ArgumentList $setupArguments -WindowStyle Hidden -PassThru -Wait
    if ($setupProcess.ExitCode -ne 0) { throw "Desktop installer failed (exit $($setupProcess.ExitCode)); see $installLog" }
    Start-VerifiedDesktop ([string]$candidate.version)
    Remove-Item -LiteralPath $candidateFile -Force
    Write-UpdateLog "Desktop and service update completed."
    Save-UpdateStatus "completed" "Desktop and service update completed."
  } catch {
    # Only restore this installation's native files.  Account and service data
    # are owned by the runtime updater and remain in their original directories.
    $restoreRoot = [IO.Path]::GetFullPath($nativeDir).TrimEnd('\')
    $expectedRoot = [IO.Path]::GetFullPath((Join-Path $InstallDir '.lay4flow-windows\desktop')).TrimEnd('\')
    if ($restoreRoot -ine $expectedRoot -or -not $restoreRoot.StartsWith($InstallDir.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
      throw 'Desktop restoration path is outside the selected installation'
    }
    if ((Test-Path -LiteralPath $nativeDir) -and ((Get-Item -LiteralPath $nativeDir).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw 'Desktop restoration does not follow a linked directory'
    }
    if (Test-Path -LiteralPath $nativeDir) {
      Get-ChildItem -LiteralPath $nativeDir -Force | Remove-Item -Recurse -Force
    } else { New-Item -ItemType Directory -Path $nativeDir -Force | Out-Null }
    Get-ChildItem -LiteralPath $backup -Force | Copy-Item -Destination $nativeDir -Recurse -Force
    Write-UpdateLog ("Desktop update failed; the previous desktop was restored. " + $_.Exception.Message)
    Save-UpdateStatus "failed" ("Desktop update failed; the previous desktop was restored. " + $_.Exception.Message)
    throw
  } finally {
    if ((Test-Path -LiteralPath $candidateFile) -and (Test-Path -LiteralPath $desktopExe)) {
      Start-Process -FilePath $desktopExe -ArgumentList @('--install-dir', ('"' + $InstallDir + '"')) -WorkingDirectory $InstallDir
    }
  }
} catch {
  Write-UpdateLog $_.Exception.Message
  Save-UpdateStatus "failed" $_.Exception.Message
  throw
}
