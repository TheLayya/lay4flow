[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'lay4flow-remote'),
  [int]$Port = 38000,
  [switch]$NoBrowser
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
if ($Port -lt 1 -or $Port -gt 65535) { throw 'The native backend port is invalid.' }
if (-not (Test-Path -LiteralPath $python)) { throw "Bundled Python runtime is missing: $python" }
if (-not (Test-Path -LiteralPath $service)) { throw "Native backend launcher is missing: $service" }
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

function Get-OwnedProcess($State) {
  if (-not $State.PSObject.Properties['pid'] -or -not $State.PSObject.Properties['creation_ticks']) { return $null }
  try {
    $taskProcessId = [int]$State.pid
    $value = Get-CimInstance Win32_Process -Filter "ProcessId=$taskProcessId"
    if (-not $value -or -not $value.ExecutablePath -or -not $value.CommandLine) { return $null }
    if ([IO.Path]::GetFullPath([string]$value.ExecutablePath) -ne $python) { return $null }
    if ($value.CommandLine.IndexOf($service, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or $value.CommandLine.IndexOf($InstallDir, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $null }
    $process = Get-Process -Id $taskProcessId -ErrorAction Stop
    if ([string]$process.StartTime.ToUniversalTime().Ticks -ne [string]$State.creation_ticks) { return $null }
    return $process
  } catch { return $null }
}
function Test-NativeHealth {
  try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
    return $status.backend_running -eq $true -and $status.version -eq $expectedVersion
  } catch { return $false }
}

$hashAlgorithm = [Security.Cryptography.SHA256]::Create()
try { $key = ([BitConverter]::ToString($hashAlgorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallDir.ToUpperInvariant())))).Replace('-', '').Substring(0, 24) } finally { $hashAlgorithm.Dispose() }
$mutex = [Threading.Mutex]::new($false, ('Local\lay4flow-native-start-' + $key))
$locked = $false
try {
  try { $locked = $mutex.WaitOne(120000) } catch [Threading.AbandonedMutexException] { $locked = $true }
  if (-not $locked) { throw 'Another launch is still starting the native backend.' }
  if (Test-Path -LiteralPath $statePath) {
    try {
      $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
      $owned = Get-OwnedProcess $saved
    } catch { $owned = $null }
    if ($owned) {
      $Port = [int]$saved.port
      if (Test-NativeHealth) { Write-Host "lay4flow-remote native backend is ready: http://127.0.0.1:$Port/login"; return }
      throw "The existing native backend is still running but is not ready. See $logPath"
    }
    Remove-Item -LiteralPath $statePath -Force
  }
  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
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
