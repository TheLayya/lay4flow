"""Keep Windows upgrade backups usable with ordinary Chromium Profile paths."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def extended(path: Path) -> Path:
    return Path("\\\\?\\" + str(path.resolve()))


@unittest.skipUnless(sys.platform == "win32", "Windows long-path upgrade integration")
class WindowsUpdateLongPathTests(unittest.TestCase):
    def test_snapshot_and_restore_preserve_deep_browser_profile(self):
        with tempfile.TemporaryDirectory(prefix="l4f-", ignore_cleanup_errors=True) as temporary:
            base = Path(temporary)
            # Regular rmtree cannot remove >260-char descendants with
            # LongPathsEnabled=0, including when an earlier assertion fails.
            self.addCleanup(shutil.rmtree, extended(base), ignore_errors=True)
            root, stage = base / "installed", base / "stage"
            candidate = stage / "candidate"
            candidate.mkdir(parents=True)
            (candidate / "VERSION").write_text("1.5.3\n", encoding="utf-8")
            root.mkdir()
            (root / "VERSION").write_text("1.5.2\n", encoding="utf-8")
            relative = (
                Path("browser_profiles")
                / ("profile-pending-" + "a" * 60)
                / "Default"
                / "Segmentation Platform"
                / "SignalStorageConfigDB"
                / "MANIFEST-000001"
            )
            profile = root / "data" / relative
            profile.parent.mkdir(parents=True)
            profile.write_text("synthetic saved profile", encoding="utf-8")
            backup = root / ".lay4flow-updates" / ("native-" + "b" * 32)
            saved = backup / "persistent/data" / relative
            self.assertLess(len(str(profile)), 260)
            self.assertGreater(len(str(saved)), 280)

            def run(action: str, *options: str) -> None:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/update_runtime.py"),
                        action,
                        "--root",
                        str(root),
                        "--stage",
                        str(stage),
                        "--backup",
                        str(backup),
                        *options,
                    ],
                    capture_output=True,
                    text=True,
                    errors="replace",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            run("snapshot")
            self.assertEqual(extended(saved).read_text(), "synthetic saved profile")
            metadata = json.loads((backup / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["version"], "1.5.2")
            profile.write_text("synthetic changed profile", encoding="utf-8")
            run("restore", "--restore-data")
            self.assertEqual(profile.read_text(), "synthetic saved profile")
            self.assertEqual(
                extended(backup / "failed-data" / relative).read_text(),
                "synthetic changed profile",
            )
            shutil.rmtree(extended(base))


if __name__ == "__main__":
    unittest.main()
