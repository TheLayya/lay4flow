"""The public runtime release stays runnable and excludes private state."""

import re
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.package_release import ROOT, TOP_LEVEL, package_release, runtime_files


class ReleasePackageTests(unittest.TestCase):
    def test_ci_checks_built_frontend_and_isolated_linux_before_packaging(self):
        workflow = (ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8")
        smoke = "docker run --rm --network none lay4flow-remote:ci python scripts/smoke_package.py"
        package = 'python scripts/package_release.py "$RUNNER_TEMP/lay4flow-remote-runtime.tar.gz"'
        for command in ("npm --prefix frontend test", "npm --prefix frontend run build", package):
            self.assertIn(command, workflow)
        self.assertLess(workflow.index(smoke), workflow.index(package))

    def test_docker_is_pinned_and_does_not_expose_vnc(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("cloakhq/cloakbrowser:latest@sha256:", dockerfile)
        self.assertIn("x11vnc novnc", dockerfile)
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:${LAY4FLOW_REMOTE_PORT:-38000}:8000", compose)
        self.assertNotIn(":5900", compose)
        self.assertIn("create_host_path: false", compose)
        self.assertIn("read_only: true", compose)
        rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for rule in (
            "data",
            "tmp",
            "config/*",
            "!config/setting_example.toml",
            "!LICENSE",
            "!NOTICE",
        ):
            self.assertIn(rule, rules)

    def test_example_credentials_and_remote_mode(self):
        example = tomllib.loads((ROOT / "config/setting_example.toml").read_text(encoding="utf-8"))
        self.assertEqual(example["global"]["api_key"], "__configure_api_key__")
        self.assertEqual(example["global"]["admin_username"], "admin")
        self.assertEqual(example["global"]["admin_password"], "admin")
        self.assertEqual(example["captcha"]["captcha_method"], "remote_browser")
        self.assertEqual(example["captcha"]["remote_browser_base_url"], "https://cap.tk218.top")
        self.assertEqual(example["captcha"]["remote_browser_api_key"], "")
        self.assertTrue(example["browser_profile"]["enabled"])
        self.assertFalse(example["browser_profile"]["headless"])
        self.assertFalse(example["browser_profile"]["headless"])

    def test_release_has_current_modules_and_built_frontend(self):
        files = {path.relative_to(ROOT).as_posix() for path in runtime_files(ROOT)}
        for name in (
            "main.py",
            "LICENSE",
            "NOTICE",
            "Dockerfile",
            "docker-compose.yml",
            "src/services/flow_client.py",
            "src/services/browser/profile_session_refresher.py",
            "src/api/admin_routes/profile_transfer.py",
            "static/app/index.html",
            "static/js/model_catalog.js",
            "docs/API.md",
            "docs/REMOTE_BROWSER_FINGERPRINT.md",
            "docs/images/qq-group.jpg",
            "docs/screenshots/login.webp",
            "docs/screenshots/session-transfer.webp",
        ):
            self.assertIn(name, files)
        self.assertTrue(any(name.startswith("static/app/assets/") for name in files))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        images = set(re.findall(r"!\[[^\]]*\]\((docs/screenshots/[^)]+)\)", readme))
        images.update(re.findall(r'<img src="(docs/screenshots/[^\"]+)"', readme))
        self.assertTrue(images, "README must include product screenshots")
        self.assertTrue(images <= files, "Runtime README images must ship with the archive")
        for name in files:
            self.assertFalse(
                name.startswith(("data/", "tmp/", "frontend/", "tests/", "docs/evidence/"))
            )
        self.assertNotIn("config/setting.toml", files)
        self.assertNotIn(".env", files)
        self.assertFalse(any("protocol_captcha" in name for name in files))

    def test_archive_excludes_nested_state_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            for name in (*TOP_LEVEL, "src/module.py", "static/app/index.html", "docker/start.sh"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic", encoding="utf-8")
            for name in (
                "src/tmp/state.py",
                "src/secret.local.py",
                "src/session.private.py",
                "static/app/profile.session.json",
                "static/app/accounts.json",
                "docs/evidence/private.json",
                "data/accounts.db",
                ".env",
                "config/setting.toml",
                "src/cookies.json",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private fixture", encoding="utf-8")
            output = Path(directory) / "release.tar.gz"
            manifest = package_release(root, output)
            with tarfile.open(output) as archive:
                names = set(archive.getnames())
                self.assertTrue(all(member.isfile() for member in archive.getmembers()))
            self.assertEqual(names, set(manifest["files"]))
            self.assertEqual(
                names,
                set(TOP_LEVEL) | {"src/module.py", "static/app/index.html", "docker/start.sh"},
            )
            self.assertEqual(manifest["project"], "lay4flow-remote")
            self.assertEqual(len(manifest["sha256"]), 64)

    def test_missing_required_file_stops_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                list(runtime_files(Path(directory)))

    def test_public_docs_and_packaging_use_current_product_name(self):
        retired_brand = "flow" + "2api"
        public_files = [ROOT / name for name in TOP_LEVEL if name.endswith(".md")]
        public_files.extend((ROOT / "Dockerfile", ROOT / "docker-compose.yml"))
        for path in public_files:
            with self.subTest(path=path.name):
                self.assertNotIn(retired_brand, path.read_text(encoding="utf-8").lower())
        self.assertIn("TheSmallHanCat", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("TheSmallHanCat", (ROOT / "NOTICE").read_text(encoding="utf-8"))
