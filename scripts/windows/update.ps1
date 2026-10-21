[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "lay4flow-remote"),
  [string]$Version,
  [switch]$Rollback
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if (Test-Path -LiteralPath (Join-Path $InstallDir '.lay4flow-windows\backend\windows-x64\python.exe')) {
  if ($Rollback) { throw 'Restore the required native backup from the installation update history.' }
  $desktop = Join-Path $InstallDir '.lay4flow-windows\desktop\Lay4Flow.Desktop.exe'
  Start-Process -FilePath $desktop -ArgumentList @('--install-dir', ('"' + $InstallDir + '"')) -WorkingDirectory $InstallDir
  Write-Host 'Open the bottom-left version number, check updates, then select Update now.'
  exit 0
}
$updater = Join-Path $InstallDir "scripts\update.ps1"
if (-not (Test-Path -LiteralPath $updater)) {
  throw "The Docker updater is missing from this installation. Reinstall from a current Windows bundle."
}
$args = @("-InstallDir", $InstallDir)
if ($Version) { $args += @("-Version", $Version) }
if ($Rollback) { $args += "-Rollback" }
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $updater @args
if ($LASTEXITCODE -ne 0) { throw "Docker update failed. The updater reports the installation was rolled back when possible." }
