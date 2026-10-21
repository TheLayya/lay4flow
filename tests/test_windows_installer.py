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
