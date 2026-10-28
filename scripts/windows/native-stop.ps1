[CmdletBinding()]
param([string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'lay4flow-remote'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
. (Join-Path $PSScriptRoot 'native-process.ps1')
$statePath = Join-Path $InstallDir 'data\native-service.json'
$mutex = Get-NativeMutex $InstallDir
$locked = $false
try {
  try { $locked = $mutex.WaitOne(120000) } catch [Threading.AbandonedMutexException] { $locked = $true }
  if (-not $locked) { throw 'Another launch is still starting the native backend.' }
  $owned = @(Get-NativeOwnedProcesses $InstallDir)
  if (-not $owned.Count) {
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    return
  }
  New-Item -ItemType File -Force -Path (Join-Path $InstallDir 'data\native-service.stop') | Out-Null
  $deadline = (Get-Date).AddSeconds(90)
  while ((Get-Date) -lt $deadline) {
    $running = @()
    foreach ($item in $owned) {
      $item.Process.Refresh()
      if (-not $item.Process.HasExited) { $running += $item }
    }
    if (-not $running.Count) {
      if (@(Get-NativeOwnedProcesses $InstallDir).Count) { throw 'The native backend restarted while it was stopping. Retry after closing this installation.' }
      Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
      Write-Host 'Native backend stopped after closing account Profiles and background workers.'
      return
    }
    Start-Sleep -Milliseconds 250
  }
  throw 'The native backend is still closing. Its process and Profile state were preserved; retry after active work completes.'
} finally {
  if ($locked) { $mutex.ReleaseMutex() }
  $mutex.Dispose()
}
