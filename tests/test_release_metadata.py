"""Release information stays local until an explicit, bounded online check."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.admin_routes import settings


class ReleaseMetadataTests(unittest.IsolatedAsyncioTestCase):
    def test_local_changelog_has_a_version_and_no_network_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/CHANGELOG.md").write_text(
                "# 更新记录\n\n## 1.2.0 — 2026-10-21\n\n- 图片与视频批量生成。\n- 一键安装。\n",
                encoding="utf-8",
            )
            with (
                patch.object(settings, "PROJECT_ROOT", root),
                patch.object(settings.urllib.request, "urlopen") as network,
            ):
                result = settings._read_release_metadata()
        self.assertEqual(result["version"], settings.VERSION)
        self.assertEqual(result["release_date"], "2026-10-21")
        self.assertEqual(result["entries"][0]["items"], ["图片与视频批量生成。", "一键安装。"])
        network.assert_not_called()

    async def test_remote_check_handles_unpublished_offline_and_newer_versions(self):
        unpublished = urllib.error.HTTPError("synthetic", 404, "not found", {}, None)
        with patch.object(settings.urllib.request, "urlopen", side_effect=unpublished):
            result = await settings._check_latest_release()
        self.assertEqual(result["status"], "unpublished")
        self.assertIn("尚未发布", result["message"])
        with patch.object(settings.urllib.request, "urlopen", side_effect=OSError("offline")):
            result = await settings._check_latest_release()
        self.assertEqual(result["status"], "unavailable")

        for tag, expected in (("v9.0.0", "update_available"), ("v0.1.0", "up_to_date")):
            body = {
                "tag_name": tag,
                "html_url": settings.RELEASE_PAGE_URL + "/tag/" + tag,
                "name": "合成发布",
            }
            with patch.object(
                settings.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(body).encode()),
            ) as request:
                result = await settings._check_latest_release()
            self.assertEqual(result["status"], expected)
            self.assertEqual(request.call_args.kwargs["timeout"], 8)
            self.assertEqual(request.call_args.args[0].full_url, settings.RELEASE_API_URL)

    def test_release_link_and_version_are_limited_to_the_expected_repository(self):
        base = {"tag_name": "v1.2.0", "html_url": settings.RELEASE_PAGE_URL + "/tag/v1.2.0"}
        self.assertEqual(settings._valid_release_payload(base)["version"], "1.2.0")
        for invalid in (
            {**base, "html_url": "https://example.test/releases/tag/v1.2.0"},
            {**base, "tag_name": "main"},
            {**base, "tag_name": "1.2.0-beta"},
            {**base, "draft": True},
            {**base, "prerelease": True},
        ):
            self.assertIsNone(settings._valid_release_payload(invalid))


class ReleaseMetadataRouteTests(unittest.TestCase):
    def test_health_exposes_drain_counts_and_marks_database_failure_not_ready(self):
        app = FastAPI()
        health = AsyncMock(return_value={"backend_running": True, "ready": True})
        router, _ = settings.create_settings_router(Mock(), Mock(), health, Mock(), Mock())
        app.include_router(router)
        with tempfile.TemporaryDirectory() as directory, TestClient(app) as client:
            marker = Path(directory) / "maintenance"
            marker.touch()
            app.state.update_activity = SimpleNamespace(active_requests=3, marker=marker)
            app.state.token_manager = SimpleNamespace(
                _profile_import_jobs=SimpleNamespace(_pending=lambda: 2)
            )
            payload = client.get("/health").json()
            self.assertEqual(payload["version"], settings.VERSION)
            self.assertEqual(payload["update_active_requests"], 3)
            self.assertEqual(payload["update_pending_imports"], 2)
            self.assertTrue(payload["update_in_progress"])
            health.side_effect = OSError("Synthetic database problem")
            payload = client.get("/health").json()
            self.assertFalse(payload["ready"])
            self.assertFalse(payload["backend_running"])

    def test_local_metadata_requires_admin_session_and_remote_result_is_cached(self):
        def denied():
            raise HTTPException(status_code=401, detail="Synthetic expired session")

        app = FastAPI()
        router, _ = settings.create_settings_router(Mock(), Mock(), Mock(), denied, Mock())
        app.include_router(router)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/admin/release").status_code, 401)
            self.assertEqual(client.get("/api/admin/release/check").status_code, 401)
            app.dependency_overrides[denied] = lambda: "synthetic-session"
            with patch.object(settings.urllib.request, "urlopen") as network:
                response = client.get("/api/admin/release")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["version"], settings.VERSION)
            network.assert_not_called()
            with patch.object(
                settings,
                "_check_latest_release",
                new=AsyncMock(return_value={"success": True, "status": "up_to_date"}),
            ) as checker:
                self.assertEqual(client.get("/api/admin/release/check").status_code, 200)
                self.assertEqual(client.get("/api/admin/release/check").status_code, 200)
            self.assertEqual(checker.await_count, 1)

    def test_docs_route_has_an_explicit_public_file_allowlist(self):
        app = FastAPI()
        router, _ = settings.create_settings_router(Mock(), Mock(), Mock(), Mock(), Mock())
        app.include_router(router)
        with tempfile.TemporaryDirectory() as directory, TestClient(app) as client:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs/WINDOWS_INSTALL.md").write_text("安装指引", encoding="utf-8")
            with patch.object(settings, "PROJECT_ROOT", root):
                response = client.get("/docs/WINDOWS_INSTALL.md")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, "安装指引")
                self.assertEqual(client.get("/docs/setting.toml").status_code, 404)
                self.assertEqual(client.get("/docs/DOCKER_UPDATE.md").status_code, 404)


if __name__ == "__main__":
    unittest.main()
