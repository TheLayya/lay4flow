"""Exercise the shipped Windows entrypoints without touching a user's Docker project."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WindowsBundleTests(unittest.TestCase):
    def test_existing_install_is_version_gated_before_launcher_refresh(self):
        source = (ROOT / "scripts/windows/install.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-BundleVersion", source)
        self.assertIn("Get-InstalledVersion", source)
        self.assertIn("Invoke-BundledRuntimeUpdate", source)
        self.assertIn("older than the installed version", source)
        self.assertIn("-SourceArchive", source)
        self.assertIn("-ChecksumFile", source)
        upgrade_section = source.split(
            'if (Test-Path -LiteralPath (Join-Path $InstallDir "docker-compose.yml")) {', 1
        )[1]
        self.assertLess(
            upgrade_section.index("Invoke-BundledRuntimeUpdate"),
            upgrade_section.index("Copy-LauncherAssets"),
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows bundled runtime upgrade integration")
    def test_bundled_updater_receives_archive_checksum_and_native_progress(self):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required")
        with tempfile.TemporaryDirectory(prefix="lay4flow-bundle-upgrade-") as directory:
            fixture = Path(directory)
            script = fixture / "verify.ps1"
            script.write_text(
                r"""param([string]$RepoRoot, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$InstallDir = Join-Path $Fixture 'installed'
$source = Join-Path $Fixture 'source'
New-Item -ItemType Directory -Path $InstallDir, (Join-Path $source 'scripts') -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $InstallDir 'VERSION'), '1.3.0')
[IO.File]::WriteAllText((Join-Path $source 'VERSION'), '1.4.0')
$updater = @'
param([string]$InstallDir,[string]$Version,[string]$SourceArchive,[string]$ChecksumFile)
if ($Version -ne '1.4.0' -or -not (Test-Path -LiteralPath $SourceArchive) -or -not (Test-Path -LiteralPath $ChecksumFile)) { exit 8 }
[Console]::Error.WriteLine('ordinary candidate build progress')
[IO.File]::WriteAllText((Join-Path $InstallDir 'VERSION'), $Version)
exit 0
'@
[IO.File]::WriteAllText((Join-Path $source 'scripts\update.ps1'), $updater)
[IO.File]::WriteAllText((Join-Path $source 'scripts\update_runtime.py'), '# fixture')
$archive = Join-Path $Fixture 'runtime.tar.gz'
& tar -czf $archive -C $source VERSION scripts/update.ps1 scripts/update_runtime.py
if ($LASTEXITCODE -ne 0) { throw 'Fixture archive creation failed.' }
$checksum = Join-Path $Fixture 'SHA256SUMS'
[IO.File]::WriteAllText($checksum, 'fixture checksum')
$script:InstallLogPath = Join-Path $Fixture 'install.log'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/install.ps1'), [ref]$tokens, [ref]$errors)
foreach ($name in @('Write-InstallLog','Stop-WithMessage','Get-BundleVersion','Get-InstalledVersion','Invoke-BundledRuntimeUpdate')) {
  $definition = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $false)
  Invoke-Expression $definition.Extent.Text
}
if ((Get-BundleVersion $archive) -ne '1.4.0') { throw 'Bundle version was not read from the runtime archive.' }
if ((Get-InstalledVersion $InstallDir) -ne '1.3.0') { throw 'Existing version was not retained before upgrade.' }
Invoke-BundledRuntimeUpdate $archive $checksum '1.4.0'
if ((Get-InstalledVersion $InstallDir) -ne '1.4.0') { throw 'Bundled runtime updater did not run.' }
if ((Get-Content -LiteralPath $script:InstallLogPath -Raw) -notmatch 'ordinary candidate build progress') { throw 'Native update progress was lost.' }
""",
                encoding="utf-8-sig",
            )
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

    def test_bundle_has_checksum_and_native_entrypoints(self):
        with tempfile.TemporaryDirectory(prefix="lay4flow-windows-package-check-") as directory:
            output = Path(directory) / "lay4flow-windows.zip"
            result = subprocess.run(
                [sys.executable, "scripts/package_windows.py", str(output)],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["version"], (ROOT / "VERSION").read_text().strip())
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                prefix = f"lay4flow-remote-{manifest['version']}-windows/"
                for entry in (
                    "install.cmd",
                    "install.ps1",
                    "README.md",
                    "start.cmd",
                    "stop.cmd",
                    "uninstall.cmd",
                    "update.cmd",
                ):
                    self.assertIn(prefix + entry, names)
                checksum = (
                    archive.read(prefix + "runtime/SHA256SUMS").decode("ascii").strip().split()
                )
                runtime = [name for name in names if name.endswith("-runtime.tar.gz")]
                self.assertEqual(len(runtime), 1)
                self.assertEqual(checksum[1], runtime[0].rsplit("/", 1)[-1])
                self.assertEqual(hashlib.sha256(archive.read(runtime[0])).hexdigest(), checksum[0])
                self.assertFalse(
                    any(name.endswith(("/.env", "/flow.db", "/setting.toml")) for name in names)
                )
                icon = prefix + "windows/tkcrm.ico"
                self.assertIn(icon, names)
                source_icon = ROOT / "scripts/windows/tkcrm.ico"
                self.assertEqual(archive.read(icon), source_icon.read_bytes())
                self.assertEqual(
                    hashlib.sha256(archive.read(icon)).hexdigest(),
                    "f7110df60bbf05a070524ac2ca720818b84b0f61baa27c175218b53e18a10c86",
                )
                self.assertIn("IconLocation", (ROOT / "scripts/windows/install.ps1").read_text())

    def test_powershell_scripts_parse(self):
        powershell = next(
            (candidate for candidate in ("powershell.exe", "pwsh") if shutil.which(candidate)), None
        )
        if powershell is None:
            self.skipTest("Native PowerShell is checked by the Windows CI job")
        script = """
$errors = @()
foreach ($file in @(Get-ChildItem scripts/windows/*.ps1) + @(Get-Item scripts/build_windows_setup.ps1)) {
  $tokens = $null; $parseErrors = $null
  [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
  $errors += $parseErrors
}
if ($errors.Count) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }
"""
        subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell diagnostic integration")
    def test_failure_log_contains_current_error_and_does_not_create_installation(self):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required")
        with tempfile.TemporaryDirectory(prefix="lay4flow-install-error-") as directory:
            fixture = Path(directory)
            log = fixture / "install.log"
            target = fixture / "untouched-install"
            command = [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts/windows/install.ps1"),
                "-InstallDir",
                str(target),
                "-LogPath",
                str(log),
                "-NoBrowser",
                "-NoShortcuts",
            ]
            first = subprocess.run(
                command + ["-Port", "0"], capture_output=True, text=True, errors="replace"
            )
            self.assertNotEqual(first.returncode, 0)
            error_file = Path(str(log) + ".error.txt")
            self.assertTrue(error_file.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(
                error_file.read_text(encoding="utf-8-sig").splitlines()[0],
                "Port must be between 1 and 65535.",
            )
            second = subprocess.run(
                command + ["-ProjectName", "INVALID"],
                capture_output=True,
                text=True,
                errors="replace",
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertTrue(
                error_file.read_text(encoding="utf-8-sig").startswith("ProjectName must use ")
            )
            self.assertNotIn("Port must be between", log.read_text(encoding="utf-8-sig"))
            self.assertFalse(target.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows native stderr and ownership integration")
    def test_native_progress_uses_exit_status_and_project_owner_is_preserved(self):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required")
        with tempfile.TemporaryDirectory(prefix="lay4flow-install-native-") as directory:
            fixture = Path(directory)
            script = fixture / "check.ps1"
            script.write_text(
                r"""param([string]$RepoRoot, [string]$Fixture)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:InstallLogPath = Join-Path $Fixture 'install.log'
$script:LastCommand = ''
$InstallDir = Join-Path $Fixture 'new-install'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/install.ps1'), [ref]$tokens, [ref]$errors)
foreach ($name in @('Write-InstallLog', 'Invoke-Compose', 'Stop-WithMessage', 'Get-ProjectOwner', 'Test-PortAvailable')) {
  $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $false)
  Invoke-Expression $definition.Extent.Text
}
$native = (Get-Command powershell.exe).Source
Invoke-Compose @($native) @('-NoLogo', '-NoProfile', '-Command', "[Console]::Error.WriteLine('ordinary build progress'); exit 0")
$failureSeen = $false
try {
  Invoke-Compose @($native) @('-NoLogo', '-NoProfile', '-Command', "[Console]::Error.WriteLine('synthetic build failure'); exit 7")
} catch {
  if ($_.Exception.Message -notmatch 'exit code 7') { throw }
  $failureSeen = $true
}
if (-not $failureSeen) { throw 'A real native command failure was ignored.' }
$log = Get-Content -LiteralPath $script:InstallLogPath -Raw -Encoding utf8
if ($log -notmatch 'ordinary build progress' -or $log -notmatch 'Command exit code: 0' -or $log -notmatch 'Command exit code: 7') { throw 'Native progress and exit status were not retained.' }
$script:MockOwner = Join-Path $Fixture 'existing-install'
function docker {
  param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
  $global:LASTEXITCODE = 0
  if ($Arguments[0] -eq 'ps') { return 'fixture-container' }
  if ($Arguments[0] -eq 'inspect') {
    return (@{'com.docker.compose.project.working_dir' = $script:MockOwner} | ConvertTo-Json -Compress)
  }
  throw 'Unexpected mock Docker command.'
}
if ((Get-ProjectOwner 'lay4flow-remote') -ne $script:MockOwner) { throw 'The foreign Compose installation was not identified.' }
$script:MockOwner = $InstallDir
if (Get-ProjectOwner 'lay4flow-remote') { throw 'The same installation was incorrectly treated as foreign.' }
$listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
try {
  $listener.Start()
  $occupied = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
  if (Test-PortAvailable $occupied) { throw 'An occupied port was reported as available.' }
} finally { $listener.Stop() }
Write-Output 'Native Docker progress, exit status and ownership checks passed.'
""",
                encoding="utf-8-sig",
            )
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
                cwd=ROOT,
                capture_output=True,
                text=True,
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(sys.platform == "win32", "Windows Shell shortcut integration")
    def test_shortcut_icon_is_saved_and_refreshed_without_changing_foreign_shortcuts(self):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell is required")
        with tempfile.TemporaryDirectory(prefix="lay4flow-shortcut-check-") as directory:
            fixture = Path(directory)
            windows = fixture / ".lay4flow-windows"
            windows.mkdir()
            shutil.copyfile(ROOT / "scripts/windows/tkcrm.ico", windows / "tkcrm.ico")
            script = fixture / "check.ps1"
            script.write_text(
                r"""param([string]$RepoRoot, [string]$Fixture)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/install.ps1'), [ref]$tokens, [ref]$errors)
$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'New-Shortcut' }, $false)
Invoke-Expression $definition.Extent.Text
$target = (Get-Command powershell.exe).Source
$path = Join-Path $Fixture 'lay4flow-remote.lnk'
New-Shortcut $path $target '-NoLogo' $Fixture
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
$expectedIcon = (Join-Path $Fixture '.lay4flow-windows\tkcrm.ico') + ',0'
if ($shortcut.IconLocation -ne $expectedIcon) { throw "Shortcut icon was not persisted: $($shortcut.IconLocation)" }
$shortcut.IconLocation = "$target,0"
$shortcut.Save()
New-Shortcut $path $target '-NoLogo' $Fixture
if ($shell.CreateShortcut($path).IconLocation -ne $expectedIcon) { throw 'Reinstall did not refresh the icon.' }
$foreign = Join-Path $Fixture 'foreign.lnk'
$shortcut = $shell.CreateShortcut($foreign)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.IconLocation = "$target,0"
$shortcut.Save()
New-Shortcut $foreign $target '-NoLogo' $Fixture
$preserved = $shell.CreateShortcut($foreign)
if ($preserved.WorkingDirectory -ne $RepoRoot -or $preserved.IconLocation -ne "$target,0") { throw 'A foreign shortcut was changed.' }
Write-Output 'Windows shortcut icon checks passed.'
""",
                encoding="utf-8-sig",
            )
            subprocess.run(
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
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
