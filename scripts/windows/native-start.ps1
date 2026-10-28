[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'lay4flow-remote'),
  [int]$Port = 38000,
  [switch]$NoBrowser,
  [switch]$AllowMaintenance
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$backend = Join-Path $InstallDir '.lay4flow-windows\backend\windows-x64'
$python = Join-Path $backend 'python.exe'
$service = Join-Path $InstallDir '.lay4flow-windows\native-service.py'
$stateDir = Join-Path $InstallDir 'data'
$statePath = Join-Path $stateDir 'native-service.json'
$logPath = Join-Path $stateDir 'native-service.log'
$stopPath = Join-Path $stateDir 'native-service.stop'
$expectedVersion = (Get-Content -LiteralPath (Join-Path $InstallDir 'VERSION') -Raw).Trim()
. (Join-Path $PSScriptRoot 'native-process.ps1')
if ($Port -lt 1 -or $Port -gt 65535) { throw 'The native backend port is invalid.' }
if (-not (Test-Path -LiteralPath $python)) { throw "Bundled Python runtime is missing: $python" }
if (-not (Test-Path -LiteralPath $service)) { throw "Native backend launcher is missing: $service" }
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

function Test-NativeHealth {
  try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
    return $status.backend_running -eq $true -and $status.version -eq $expectedVersion
  } catch { return $false }
}

$mutex = Get-NativeMutex $InstallDir
$locked = $false
try {
  try { $locked = $mutex.WaitOne(120000) } catch [Threading.AbandonedMutexException] { $locked = $true }
  if (-not $locked) { throw 'Another launch is still starting the native backend.' }
  if (-not $AllowMaintenance -and (Test-Path -LiteralPath (Join-Path $stateDir 'update-maintenance'))) {
    throw 'The application is being updated. Its backend will restart automatically when installation completes.'
  }
  $owned = @(Get-NativeOwnedProcesses $InstallDir)
  if ($owned.Count -gt 1) { throw 'More than one owned native backend is running. Close this installation before retrying.' }
  if ($owned.Count -eq 1) {
    $Port = $owned[0].Port
    @{pid=$owned[0].Process.Id; port=$Port; creation_ticks=$owned[0].CreationTicks; backend=$backend} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    if (Test-NativeHealth) { Write-Host "lay4flow-remote native backend is ready: http://127.0.0.1:$Port/login"; return }
    throw "The existing native backend is still running but is not ready. See $logPath"
  }
  if (Test-Path -LiteralPath $statePath) {
    Remove-Item -LiteralPath $statePath -Force
  }
  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
  $listener.ExclusiveAddressUse = $true
  try { $listener.Start() } catch { throw "The local port $Port belongs to another process. Choose an available local port." } finally { $listener.Stop() }
  $env:LAY4FLOW_NATIVE_RUNTIME = '1'
  $env:LAY4FLOW_PROFILE_VIEWER_ENABLED = '1'
  $env:LAY4FLOW_PROFILE_PRESENTATION = 'native'
  $env:CLOAKBROWSER_AUTO_UPDATE = 'false'
  $env:CLOAKBROWSER_BINARY_PATH = (Join-Path $backend 'cloakbrowser\chrome.exe')
  Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
  $arguments = '"' + $service + '" --root "' + $InstallDir + '" --port ' + $Port + ' --stop-file "' + $stopPath + '" --log-file "' + $logPath + '"'
  $info = [Diagnostics.ProcessStartInfo]::new()
  $info.FileName = $python
  $info.Arguments = $arguments
  $info.WorkingDirectory = $InstallDir
  $info.UseShellExecute = $true
  $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
  $process = [Diagnostics.Process]::Start($info)
  @{pid=$process.Id; port=$Port; creation_ticks=[string]$process.StartTime.ToUniversalTime().Ticks; backend=$backend} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
  $deadline = (Get-Date).AddSeconds(120)
  while ((Get-Date) -lt $deadline) {
    $process.Refresh()
    if ($process.HasExited) { throw "Native backend exited with code $($process.ExitCode). See $logPath" }
    if (Test-NativeHealth) { Write-Host "lay4flow-remote native backend is ready: http://127.0.0.1:$Port/login"; return }
    Start-Sleep -Milliseconds 500
  }
  throw "Native backend did not become ready within 120 seconds. See $logPath"
} finally {
  if ($locked) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
}
