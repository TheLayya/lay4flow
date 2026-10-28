import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts import update_runtime


class UpdateRuntimeTests(unittest.TestCase):
    def test_login_extension_assets_are_public_but_extra_extension_files_are_not(self):
        for name in update_runtime.EXTENSION_FILES:
            with self.subTest(name=name):
                self.assertTrue(update_runtime.public_path(name))
        for name in (
            "account_import_extension/cookies.json",
            "account_import_extension/accounts.json",
            "account_import_extension/extra.js",
            "account_import_extension/session.private.json",
        ):
            with self.subTest(name=name):
                self.assertFalse(update_runtime.public_path(name))

    def test_apply_and_rollback_include_login_extension_public_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, candidate, backup = base / "installed", base / "candidate", base / "backup"
            for target in (root, candidate):
                target.mkdir()
                (target / "VERSION").write_text("fixture", encoding="utf-8")
                (target / "account_import_extension").mkdir()
            name = "account_import_extension/background.js"
            (root / name).write_text("old worker", encoding="utf-8")
            (candidate / name).write_text("new worker", encoding="utf-8")
            (candidate / "account_import_extension/cookies.json").write_text(
                "private synthetic fixture", encoding="utf-8"
            )
            update_runtime.snapshot(root, backup, candidate, "")
            update_runtime.apply(root, candidate)
            self.assertEqual((root / name).read_text(encoding="utf-8"), "new worker")
            self.assertFalse((root / "account_import_extension/cookies.json").exists())
            update_runtime.restore(root, backup)
            self.assertEqual((root / name).read_text(encoding="utf-8"), "old worker")

    def make_archive(self, root: Path, *, unsafe: str | None = None) -> tuple[Path, Path]:
        archive = root / "lay4flow-remote-1.2.0-runtime.tar.gz"
        required = {
            "VERSION": b"1.2.0\n",
            "Dockerfile": b"FROM scratch\n",
            "docker-compose.yml": b"services: {}\n",
            "main.py": b"print('ok')\n",
            "requirements.txt": b"\n",
            "src/main.py": b"API_VERSION = '1.2.0'\n",
        }
        if unsafe:
            required[unsafe] = b"private"
        with tarfile.open(archive, "w:gz") as bundle:
            for name, content in required.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksums = root / "SHA256SUMS"
        checksums.write_text(f"{digest}  *{archive.name}\n", encoding="utf-8")
        return archive, checksums

    def test_prepare_verifies_archive_and_builds_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, checksums = self.make_archive(root)
            inventory = update_runtime.prepare(archive, checksums, root / "candidate", "1.2.0")
            self.assertEqual(inventory["version"], "1.2.0")
            self.assertEqual((root / "candidate/VERSION").read_text(), "1.2.0\n")

    def test_prepare_rejects_bad_checksum_before_unpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, checksums = self.make_archive(root)
            checksums.write_text("0" * 64 + "  *" + archive.name + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                update_runtime.prepare(archive, checksums, root / "candidate", "1.2.0")
            self.assertFalse((root / "candidate").exists())

    def test_prepare_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, checksums = self.make_archive(root, unsafe="../escape.txt")
            with self.assertRaises(ValueError):
                update_runtime.prepare(archive, checksums, root / "candidate", "1.2.0")

    def test_prepare_rejects_private_runtime_and_links(self):
        for name in ("data/flow.db", "config/setting.toml", ".env"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive, checksums = self.make_archive(root, unsafe=name)
                with self.assertRaises(ValueError):
                    update_runtime.prepare(archive, checksums, root / "candidate", "1.2.0")

    def test_snapshot_prunes_private_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VERSION").write_text("1.1.0\n", encoding="utf-8")
            private = root / ".lay4flow-updates/backups/deep"
            private.mkdir(parents=True)
            (private / "VERSION").write_text("private", encoding="utf-8")
            files = [name for _, name in update_runtime.public_files(root)]
            self.assertEqual(files, ["VERSION"])

    def test_snapshot_and_restore_keep_private_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True)
            (root / "src/app.py").write_text("old", encoding="utf-8")
            (root / "VERSION").write_text("1.1.0\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=keep\n", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config/setting.toml").write_text("token='keep'\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data/state.txt").write_text("old", encoding="utf-8")
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "VERSION").write_text("1.2.0\n", encoding="utf-8")
            (candidate / "src").mkdir()
            (candidate / "src/app.py").write_text("new", encoding="utf-8")
            backup = root / "backup"
            update_runtime.snapshot(root, backup, candidate, "old:image")
            update_runtime.apply(root, candidate)
            update_runtime.set_image(root, "new:image")
            self.assertEqual(
                (root / ".env").read_text(), "SECRET=keep\nLAY4FLOW_REMOTE_IMAGE=new:image\n"
            )
            update_runtime.restore(root, backup, restore_data=True)
            self.assertEqual((root / "src/app.py").read_text(), "old")
            self.assertEqual((root / "data/state.txt").read_text(), "old")
            self.assertIn("SECRET=keep", (root / ".env").read_text())

    def test_idle_rejects_active_studio_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data/studio-jobs.json").write_text(
                '[{"items":[{"status":"generating"}]}]', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "generation jobs"):
                update_runtime.assert_idle(root)

    def test_apply_rejects_parent_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, outside, candidate = base / "root", base / "outside", base / "candidate"
            for path in (root, outside, candidate / "src"):
                path.mkdir(parents=True)
            try:
                (root / "src").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation requires Windows Developer Mode")
            (candidate / "src/app.py").write_text("fixture", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "symlink"):
                update_runtime.apply(root, candidate)
            self.assertFalse((outside / "app.py").exists())

    def test_rollback_retains_update_entrypoint_for_legacy_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VERSION").write_text("1.1.0\n", encoding="utf-8")
            candidate = root / "candidate"
            (candidate / "scripts").mkdir(parents=True)
            (candidate / "scripts/update.sh").write_text("fixture", encoding="utf-8")
            backup = root / "backup"
            update_runtime.snapshot(root, backup, candidate, "old:image")
            update_runtime.apply(root, candidate)
            update_runtime.restore(root, backup)
            self.assertEqual((root / "scripts/update.sh").read_text(), "fixture")


if __name__ == "__main__":
    unittest.main()
