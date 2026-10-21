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

    def test_powershell_scripts_parse(self):
        powershell = next(
            (candidate for candidate in ("powershell.exe", "pwsh") if shutil.which(candidate)), None
        )
        if powershell is None:
            self.skipTest("Native PowerShell is checked by the Windows CI job")
        script = """
$errors = @()
foreach ($file in Get-ChildItem scripts/windows/*.ps1) {
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


if __name__ == "__main__":
    unittest.main()
