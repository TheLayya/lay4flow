[CmdletBinding()]
param(
  [string]$Dotnet,
  [string]$OutputDir
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDir) { $OutputDir = Join-Path $root "desktop\runtime\windows-x64" }
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
if (-not $Dotnet) {
  $command = Get-Command dotnet -ErrorAction SilentlyContinue
  if ($command -and ((& $command.Source --list-sdks) -match '^10\.')) { $Dotnet = $command.Source }
  $cached = Join-Path $env:LOCALAPPDATA "TkCRM-Dev\dotnet\dotnet.exe"
  if (-not $Dotnet -and (Test-Path -LiteralPath $cached)) { $Dotnet = $cached }
}
if (-not $Dotnet -or -not ((& $Dotnet --list-sdks) -match '^10\.')) { throw ".NET 10 SDK is required; pass -Dotnet with its full path." }
$version = (Get-Content -LiteralPath (Join-Path $root "VERSION") -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION must contain a stable semantic version" }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
& $Dotnet publish (Join-Path $root "desktop\Lay4Flow.Desktop\Lay4Flow.Desktop.csproj") -c Release -r win-x64 --self-contained true -p:Version=$version -o $OutputDir
if ($LASTEXITCODE -ne 0) { throw "Desktop publish failed ($LASTEXITCODE)" }
$forbidden = Get-ChildItem -LiteralPath $OutputDir -Recurse -File | Where-Object { $_.Name -eq '.env' -or $_.Extension -in '.db', '.sqlite', '.sqlite3' }
if ($forbidden) { throw "Desktop output contains runtime state" }
Write-Host "Desktop app: $(Join-Path $OutputDir 'Lay4Flow.Desktop.exe')"
