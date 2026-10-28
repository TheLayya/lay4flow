import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_account_import_extension import (
    build_extension,
    normalize_origin,
    render_manifest,
)


class AccountImportExtensionBuilderTests(unittest.TestCase):
    def test_normalizes_http_https_host_and_port(self):
        self.assertEqual(normalize_origin("https://EXAMPLE.test/"), "https://example.test")
        self.assertEqual(normalize_origin("https://example.test:443"), "https://example.test")
        self.assertEqual(normalize_origin("http://154.40.58.46:38000"), "http://154.40.58.46:38000")
        self.assertEqual(normalize_origin("https://[::1]:8443"), "https://[::1]:8443")

    def test_rejects_non_origin_values(self):
        for value in (
            "",
            "ftp://example.test",
            "https://example.test/path",
            "https://example.test/?token=x",
            "https://example.test/#fragment",
            "https://user:pass@example.test",
            "https://*.example.test",
            r"https://example.test\\evil",
            "https://example.test:",
            "https://example.test:0",
            "https://example.test:65536",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_origin(value)

    def test_static_manifest_has_only_exact_management_origin(self):
        manifest = render_manifest("http://198.51.100.7:38000")
        self.assertEqual(manifest["version"], "1.8.0")
        self.assertEqual(manifest["name"], "lay4flow-remote OAuth Account Import")
        self.assertIn("lay4flow", manifest["description"])
        self.assertNotIn("optional_host_permissions", manifest)
        self.assertNotIn("scripting", manifest["permissions"])
        self.assertIn("http://198.51.100.7:38000/*", manifest["host_permissions"])
        self.assertEqual(manifest["content_scripts"][0]["matches"], ["http://198.51.100.7:38000/*"])
        self.assertTrue(any(host.endswith("google.com/*") for host in manifest["host_permissions"]))
        flow_scripts = [
            entry for entry in manifest["content_scripts"] if "flow_identity.js" in entry["js"]
        ]
        self.assertEqual(len(flow_scripts), 1)
        self.assertEqual(flow_scripts[0]["matches"], ["https://flow.google.com/*"])

    def test_zip_contains_rendered_manifest_and_runtime_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "lay4flow-account-import.zip"
            build_extension("https://flow.example.test:8443", output)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))
                background = archive.read("background.js").decode("utf-8")
            self.assertEqual(
                names,
                {
                    "manifest.json",
                    "background.js",
                    "content.js",
                    "flow_identity.js",
                    "README.md",
                    "LICENSE",
                    "NOTICE",
                },
            )
            self.assertIn("https://flow.example.test:8443/*", manifest["host_permissions"])
            self.assertNotIn("manifest.template.json", names)
            self.assertNotIn("chrome.permissions.request", background)
            self.assertNotIn("chrome.scripting.registerContentScripts", background)

    def test_cookie_permissions_cover_both_schemes_without_all_site_access(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "lay4flow-account-import.zip"
            build_extension("https://manage.example.test", output)
            with zipfile.ZipFile(output) as archive:
                packaged = json.loads(archive.read("manifest.json"))
        extension = Path(__file__).resolve().parents[1] / "account_import_extension"
        manifests = [packaged] + [
            json.loads((extension / filename).read_text(encoding="utf-8"))
            for filename in ("manifest.json", "manifest.template.json")
        ]
        for manifest in manifests:
            with self.subTest(name=manifest["name"], hosts=manifest["host_permissions"]):
                hosts = manifest["host_permissions"]
                self.assertIn("http://*.google.com/*", hosts)
                self.assertIn("https://*.google.com/*", hosts)
                self.assertFalse(
                    {"<all_urls>", "*://*/*", "http://*/*", "https://*/*"}.intersection(hosts)
                )


if __name__ == "__main__":
    unittest.main()
