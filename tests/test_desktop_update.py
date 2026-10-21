"""Exercise desktop update verification without fetching or applying a release."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Native Windows update helper")
class DesktopUpdateTests(unittest.TestCase):
    def run_script(self, source):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required")
        with tempfile.TemporaryDirectory(prefix="lay4flow-desktop-update-test-") as directory:
            fixture = Path(directory)
            script = fixture / "verify.ps1"
            script.write_text(source, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-RepoRoot",
                    str(ROOT),
                    "-Fixture",
                    str(fixture),
                ],
                capture_output=True,
                text=True,
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_checksum_failure_leaves_desktop_untouched(self):
        self.run_script(
            r"""param([string]$RepoRoot, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$stateDir = Join-Path $Fixture 'updates'
New-Item -ItemType Directory -Path $stateDir | Out-Null
$script:Mode = 'valid'
$script:Payload = [Text.Encoding]::ASCII.GetBytes('synthetic setup asset')
$hasher = [Security.Cryptography.SHA256]::Create()
$script:Hash = ([BitConverter]::ToString($hasher.ComputeHash($script:Payload))).Replace('-', '').ToLowerInvariant()
$hasher.Dispose()
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/desktop-update.ps1'), [ref]$tokens, [ref]$errors)
$function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-VerifiedDesktopCandidate'}, $false)
Invoke-Expression $function.Extent.Text
function Write-UpdateLog([string]$Message) { }
function Invoke-WebRequest {
  param([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing, [int]$TimeoutSec)
  if ($Uri -notlike 'https://github.com/TheLayya/lay4flow/releases/download/v1.3.1/*') { throw 'Updater used an unexpected download origin.' }
  if ($Uri.EndsWith('SHA256SUMS')) {
    $digest = if ($script:Mode -eq 'bad') { ('0' * 64) } else { $script:Hash }
    $entry = $digest + '  lay4flow-remote-1.3.1-windows-x64-setup.exe'
    if ($script:Mode -eq 'duplicate') { $entry = $entry + "`n" + $entry }
    [IO.File]::WriteAllText($OutFile, $entry)
  } else { [IO.File]::WriteAllBytes($OutFile, $script:Payload) }
}
$candidate = Get-VerifiedDesktopCandidate '1.3.1'
if ($candidate.sha256 -ne $script:Hash -or -not (Test-Path -LiteralPath $candidate.setup)) { throw 'Verified candidate was not retained.' }
foreach ($mode in @('bad', 'duplicate')) {
  $script:Mode = $mode
  $failed = $false
  try { Get-VerifiedDesktopCandidate '1.3.1' | Out-Null }
  catch {
    if ($_.Exception.Message -notmatch 'checksum') { throw }
    $failed = $true
  }
  if (-not $failed) { throw "Rejected checksum case was accepted: $mode" }
}
$invalid = $false
try { Get-VerifiedDesktopCandidate '1.3.1/../../other' | Out-Null }
catch { $invalid = $true }
if (-not $invalid) { throw 'Invalid release identity was accepted.' }
if (Test-Path -LiteralPath (Join-Path $Fixture '.lay4flow-windows')) { throw 'Native installation was touched by verification.' }
"""
        )

    def test_only_owned_legacy_update_shortcut_is_removed(self):
        self.run_script(
            r"""param([string]$RepoRoot, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $Fixture 'owned-install'
New-Item -ItemType Directory -Path $InstallDir | Out-Null
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/install.ps1'), [ref]$tokens, [ref]$errors)
$function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Remove-LegacyUpdateShortcut'}, $false)
Invoke-Expression $function.Extent.Text
$shell = New-Object -ComObject WScript.Shell
foreach ($owned in @($true, $false)) {
  $path = Join-Path $Fixture 'legacy update.lnk'
  $shortcut = $shell.CreateShortcut($path)
  $shortcut.TargetPath = (Get-Command powershell.exe).Source
  $shortcut.WorkingDirectory = if ($owned) { $InstallDir } else { $RepoRoot }
  $shortcut.Save()
  Remove-LegacyUpdateShortcut $path
  if ((Test-Path -LiteralPath $path) -eq $owned) { throw 'Shortcut ownership was not preserved.' }
}
"""
        )

    def test_apply_failure_restores_its_native_runtime_and_restarts(self):
        self.run_script(
            r"""param([string]$RepoRoot, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $Fixture 'installation'
$stateDir = Join-Path $InstallDir '.lay4flow-updates'
$nativeDir = Join-Path $InstallDir '.lay4flow-windows\desktop'
$stage = Join-Path $stateDir 'desktop-1.3.1-fixture'
New-Item -ItemType Directory -Path $nativeDir, $stage -Force | Out-Null
$desktopExe = Join-Path $nativeDir 'Lay4Flow.Desktop.exe'
[IO.File]::WriteAllText($desktopExe, 'previous runtime')
$settings = Join-Path $InstallDir '.env'
[IO.File]::WriteAllText($settings, 'LAY4FLOW_REMOTE_PORT=38000')
$setup = Join-Path $stage 'lay4flow-remote-1.3.1-windows-x64-setup.exe'
[IO.File]::WriteAllText($setup, 'synthetic setup')
$candidateFile = Join-Path $stateDir 'desktop-candidate.json'
@{version='1.3.1';setup=$setup;sha256=(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()} | ConvertTo-Json | Set-Content -LiteralPath $candidateFile -Encoding utf8
$global:DesktopRestarted = $false
function Get-Process { param([int]$Id, [string]$ErrorAction) return $null }
function Start-Process {
  param([string]$FilePath, [string[]]$ArgumentList, [string]$WindowStyle, [switch]$PassThru, [switch]$Wait, [string]$WorkingDirectory)
  if ($FilePath -eq $setup) {
    if (($ArgumentList -join ' ') -notmatch '/DIR="') { throw 'Installer did not preserve the installation directory.' }
    [IO.File]::WriteAllText($desktopExe, 'broken runtime')
    [IO.File]::WriteAllText((Join-Path $nativeDir 'new-file.dll'), 'candidate file')
    return [PSCustomObject]@{ExitCode=7}
  }
  if ($FilePath -eq $desktopExe) { $global:DesktopRestarted = $true; return }
  throw 'Unexpected native process.'
}
$failed = $false
try {
  & (Join-Path $RepoRoot 'scripts/windows/desktop-update.ps1') -InstallDir $InstallDir -Phase Apply -ParentProcessId 123456
} catch {
  if ($_.Exception.Message -notmatch 'exit 7') { throw }
  $failed = $true
}
if (-not $failed -or -not $global:DesktopRestarted) { throw 'Failed setup did not restore and restart the retained application.' }
if ([IO.File]::ReadAllText($desktopExe) -ne 'previous runtime') { throw 'Native backup was not restored.' }
if (Test-Path -LiteralPath (Join-Path $nativeDir 'new-file.dll')) { throw 'Candidate native files survived rollback.' }
if ([IO.File]::ReadAllText($settings) -ne 'LAY4FLOW_REMOTE_PORT=38000') { throw 'Installation configuration changed.' }
$status = Get-Content -LiteralPath (Join-Path $stateDir 'desktop-update-status.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($status.state -ne 'failed') { throw 'The update failure was not persisted.' }
if (-not (Test-Path -LiteralPath $candidateFile)) { throw 'A failed candidate was reported as applied.' }
"""
        )

    def test_apply_success_clears_candidate_and_records_status(self):
        self.run_script(
            r"""param([string]$RepoRoot, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $Fixture 'installation'
$stateDir = Join-Path $InstallDir '.lay4flow-updates'
$nativeDir = Join-Path $InstallDir '.lay4flow-windows\desktop'
$stage = Join-Path $stateDir 'desktop-1.3.1-fixture'
New-Item -ItemType Directory -Path $nativeDir, $stage -Force | Out-Null
$desktopExe = Join-Path $nativeDir 'Lay4Flow.Desktop.exe'
[IO.File]::WriteAllText($desktopExe, 'previous runtime')
$setup = Join-Path $stage 'lay4flow-remote-1.3.1-windows-x64-setup.exe'
[IO.File]::WriteAllText($setup, 'synthetic setup')
$candidateFile = Join-Path $stateDir 'desktop-candidate.json'
@{version='1.3.1';setup=$setup;sha256=(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()} | ConvertTo-Json | Set-Content -LiteralPath $candidateFile -Encoding utf8
$global:DesktopRestarted = $false
function Get-Process { param([int]$Id, [string]$ErrorAction) return $null }
function Start-Process {
  param([string]$FilePath, [string[]]$ArgumentList, [string]$WindowStyle, [switch]$PassThru, [switch]$Wait, [string]$WorkingDirectory)
  if ($FilePath -eq $setup) {
    [IO.File]::WriteAllText($desktopExe, 'updated runtime')
    return [PSCustomObject]@{ExitCode=0}
  }
  if ($FilePath -eq $desktopExe) {
    $global:DesktopRestarted = $true
    $readyIndex = [Array]::IndexOf($ArgumentList, '--ready-file')
    if ($readyIndex -lt 0) { throw 'Updated desktop did not request readiness verification.' }
    $ready = $ArgumentList[$readyIndex + 1].Trim('"')
    @{pid=987654;version='1.3.1';url='http://127.0.0.1:38000/login';window_handle=1234} | ConvertTo-Json | Set-Content -LiteralPath $ready -Encoding utf8
    return [PSCustomObject]@{Id=987654;HasExited=$false}
  }
  throw 'Unexpected native process.'
}
& (Join-Path $RepoRoot 'scripts/windows/desktop-update.ps1') -InstallDir $InstallDir -Phase Apply -ParentProcessId 123456
if (-not $global:DesktopRestarted) { throw 'Successful update did not restart the desktop.' }
if (Test-Path -LiteralPath $candidateFile) { throw 'Successful update retained the candidate marker.' }
if ([IO.File]::ReadAllText($desktopExe) -ne 'updated runtime') { throw 'Successful installer did not leave the new runtime.' }
$status = Get-Content -LiteralPath (Join-Path $stateDir 'desktop-update-status.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($status.state -ne 'completed') { throw 'Successful update did not persist completed status.' }
"""
        )

    def test_ready_version_mismatch_fails_before_success(self):
        self.run_script(
            r"""param([string]$RepoRoot, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $Fixture 'installation'
$stateDir = Join-Path $InstallDir '.lay4flow-updates'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$desktopExe = Join-Path $InstallDir 'Lay4Flow.Desktop.exe'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/desktop-update.ps1'), [ref]$tokens, [ref]$errors)
$function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-VerifiedDesktop'}, $false)
Invoke-Expression $function.Extent.Text
function Start-Process {
  param([string]$FilePath, [string[]]$ArgumentList, [string]$WorkingDirectory, [switch]$PassThru)
  $ready = $ArgumentList[[Array]::IndexOf($ArgumentList, '--ready-file') + 1].Trim('"')
  @{pid=987654;version='1.3.0';url='http://127.0.0.1:38000/login';window_handle=1234} | ConvertTo-Json | Set-Content -LiteralPath $ready -Encoding utf8
  return [PSCustomObject]@{Id=987654;HasExited=$true}
}
$failed = $false
try { Start-VerifiedDesktop '1.3.1' }
catch {
  if ($_.Exception.Message -notmatch 'before it was ready') { throw }
  $failed = $true
}
if (-not $failed) { throw 'Wrong native version was considered a completed update.' }
if (@(Get-ChildItem -LiteralPath $stateDir -Filter 'desktop-ready-*').Count) { throw 'Readiness marker was retained.' }
"""
        )


if __name__ == "__main__":
    unittest.main()
