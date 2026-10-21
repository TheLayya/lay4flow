"""Contract checks for the native Windows shell and installer allow-list."""

import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts.package_windows import package_windows

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop" / "runtime" / "windows-x64"


class DesktopContractTests(unittest.TestCase):
    def test_host_validates_a_loopback_startup_without_opening_a_window(self):
        executable = DESKTOP / "Lay4Flow.Desktop.exe"
        if sys.platform != "win32" or not executable.exists():
            self.skipTest("The native shell is exercised in Windows packaging CI")
        with tempfile.TemporaryDirectory(prefix="lay4flow-desktop-options-") as directory:
            output = Path(directory) / "options.json"
            valid = subprocess.run(
                [
                    str(executable),
                    "--validate-options-json",
                    "--url",
                    "http://127.0.0.1:38177/login",
                    "--no-start",
                    "--user-data-dir",
                    str(Path(directory) / "profile"),
                    "--ready-file",
                    str(output),
                    "--remote-debugging-port",
                    "38178",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["valid"])
            invalid = subprocess.run(
                [str(executable), "--validate-options-json", "--url", "https://example.com/"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(invalid.returncode, 2)

    def test_windows_package_contains_desktop_runtime_and_rejects_private_files(self):
        with tempfile.TemporaryDirectory(prefix="lay4flow-desktop-package-") as directory:
            fixture = Path(directory)
            runtime_source = fixture / "runtime-source"
            runtime_source.mkdir()
            (runtime_source / "VERSION").write_text("fixture\n", encoding="ascii")
            runtime_archive = fixture / "fixture-runtime.tar.gz"
            with tarfile.open(runtime_archive, "w:gz") as archive:
                archive.add(runtime_source / "VERSION", arcname="VERSION")
            desktop = fixture / "desktop"
            desktop.mkdir()
            for name in (
                "Lay4Flow.Desktop.exe",
                "Lay4Flow.Desktop.dll",
                "Lay4Flow.Desktop.deps.json",
                "Lay4Flow.Desktop.runtimeconfig.json",
            ):
                (desktop / name).write_bytes(b"fixture")
            output = fixture / "windows.zip"
            manifest = package_windows(ROOT, output, runtime_archive, desktop)
            self.assertTrue(manifest["desktop"])
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
            prefix = f"lay4flow-remote-{manifest['version']}-windows/"
            self.assertIn(prefix + "desktop/windows-x64/Lay4Flow.Desktop.exe", names)
            self.assertIn(prefix + "desktop/windows-x64/Lay4Flow.Desktop.deps.json", names)
            self.assertNotIn(prefix + "desktop/windows-x64/.env", names)
            private = desktop / "profiles"
            private.mkdir()
            (private / "cookies.json").write_text("secret", encoding="utf-8")
            with self.assertRaises(ValueError):
                package_windows(ROOT, fixture / "private.zip", runtime_archive, desktop)


if __name__ == "__main__":
    unittest.main()
