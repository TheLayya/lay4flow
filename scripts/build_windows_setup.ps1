[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$BundleZip,
  [Parameter(Mandatory = $true)][string]$OutputDir,
  [string]$Iscc = (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = (Get-Content -LiteralPath (Join-Path $root "VERSION") -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid VERSION" }
if (-not (Test-Path -LiteralPath $Iscc)) { throw "Inno Setup 6 is required to build setup.exe: $Iscc" }
$BundleZip = (Resolve-Path -LiteralPath $BundleZip).Path
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$output = Join-Path $OutputDir "lay4flow-remote-$version-windows-x64-setup.exe"
if (Test-Path -LiteralPath $output) { throw "Setup output already exists; use a fresh output directory" }
$stage = Join-Path ([IO.Path]::GetTempPath()) ("lay4flow-setup-" + [guid]::NewGuid().ToString("N"))
try {
  Expand-Archive -LiteralPath $BundleZip -DestinationPath $stage
  $bundle = Join-Path $stage "lay4flow-remote-$version-windows"
  if (-not (Test-Path -LiteralPath (Join-Path $bundle "install.ps1"))) { throw "Windows bundle entrypoint is missing" }
  & $Iscc "/DAppVersion=$version" "/DBundleDir=$bundle" "/DOutputDir=$OutputDir" (Join-Path $PSScriptRoot "windows\installer.iss")
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed: $LASTEXITCODE" }
  $hash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
  Add-Content -LiteralPath (Join-Path $OutputDir "SHA256SUMS") -Encoding ascii -Value "$hash  $([IO.Path]::GetFileName($output))"
  $manifestPath = Join-Path $OutputDir "build-manifest.json"
  if (Test-Path -LiteralPath $manifestPath) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $manifest.assets += [pscustomobject]@{ name = [IO.Path]::GetFileName($output); bytes = (Get-Item -LiteralPath $output).Length; sha256 = $hash }
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
  }
  Write-Host $output
} finally {
  if ($stage -and (Test-Path -LiteralPath $stage)) {
    $resolvedStage = [IO.Path]::GetFullPath($stage)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolvedStage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Invalid temporary path" }
    Remove-Item -LiteralPath $resolvedStage -Recurse -Force
  }
}
