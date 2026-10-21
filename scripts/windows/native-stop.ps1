[CmdletBinding()]
param([string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'lay4flow-remote'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$statePath = Join-Path $InstallDir 'data\native-service.json'
if (-not (Test-Path -LiteralPath $statePath)) { return }
try { $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { throw 'The native backend state is unreadable. Its processes were preserved.' }
if (-not $saved.PSObject.Properties['pid'] -or -not $saved.PSObject.Properties['creation_ticks']) { throw 'The native backend process identity is missing. Its processes were preserved.' }
$taskProcessId = [int]$saved.pid
$taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$taskProcessId"
if (-not $taskProcess) { Remove-Item -LiteralPath $statePath -Force; return }
$python = Join-Path $InstallDir '.lay4flow-windows\backend\windows-x64\python.exe'
$service = Join-Path $InstallDir '.lay4flow-windows\native-service.py'
if (-not $taskProcess.ExecutablePath -or -not $taskProcess.CommandLine -or [IO.Path]::GetFullPath([string]$taskProcess.ExecutablePath) -ne $python -or $taskProcess.CommandLine.IndexOf($service, [StringComparison]::OrdinalIgnoreCase) -lt 0) { throw 'The saved PID belongs to another process. It was preserved.' }
$process = Get-Process -Id $taskProcessId -ErrorAction Stop
if ([string]$process.StartTime.ToUniversalTime().Ticks -ne [string]$saved.creation_ticks) { throw 'The saved PID has been reused. It was preserved.' }
New-Item -ItemType File -Force -Path (Join-Path $InstallDir 'data\native-service.stop') | Out-Null
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
  $process.Refresh()
  if ($process.HasExited) {
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    Write-Host 'Native backend stopped after closing account Profiles and background workers.'
    return
  }
  Start-Sleep -Milliseconds 250
}
throw 'The native backend is still closing. Its process and Profile state were preserved; retry after active work completes.'
