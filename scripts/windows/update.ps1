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
$updater = Join-Path $InstallDir "scripts\update.ps1"
if (-not (Test-Path -LiteralPath $updater)) {
  throw "The Docker updater is missing from this installation. Reinstall from a current Windows bundle."
}
$args = @("-InstallDir", $InstallDir)
if ($Version) { $args += @("-Version", $Version) }
if ($Rollback) { $args += "-Rollback" }
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $updater @args
if ($LASTEXITCODE -ne 0) { throw "Docker update failed. The updater reports the installation was rolled back when possible." }
