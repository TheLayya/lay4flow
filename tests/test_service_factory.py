"""The service graph shares request policy without starting any network work."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.database import Database
from src.services.factory import create_services


class ServiceFactoryTests(unittest.TestCase):
    def test_shared_services_and_independent_application_instances(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.services.generation_handler.FileCache"),
        ):
            db = Database(str(Path(directory) / "unused.db"))
            first = create_services(db)
            second = create_services(db)
            self.assertIs(first.flow_client.db, db)
            self.assertIs(first.flow_client.proxy_manager, first.proxy_manager)
            self.assertFalse(hasattr(first.flow_client, "protocol_captcha_service"))
            self.assertIs(first.token_manager.flow_client, first.flow_client)
            self.assertFalse(hasattr(first.token_manager, "browser_recovery"))
            self.assertFalse(hasattr(first.token_manager, "server_browser_login"))
            self.assertIs(first.load_balancer.concurrency_manager, first.concurrency_manager)
            self.assertIs(first.generation_handler.flow_client, first.flow_client)
            self.assertIs(first.generation_handler.load_balancer, first.load_balancer)
            self.assertIsNot(first.model_gateway, second.model_gateway)
            self.assertIs(first.vids_dispatcher.db, db)
            self.assertIs(first.vids_runtime.dispatcher, first.vids_dispatcher)
            self.assertIs(first.vids_runtime.proxy_manager, first.proxy_manager)
            self.assertFalse(first.vids_runtime._started)
            self.assertEqual(first.vids_runtime._gateways, [])
            self.assertIsNot(first.vids_runtime, second.vids_runtime)
            self.assertIsNot(first.vids_dispatcher, second.vids_dispatcher)
            self.assertIsNot(first.flow_client, second.flow_client)
            self.assertIsNot(first.concurrency_manager, second.concurrency_manager)
            self.assertFalse(Path(db.db_path).exists())

    def test_browser_profile_refresh_is_opt_in_and_wired(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("src.services.generation_handler.FileCache"),
        ):
            from src.services.factory import config

            original = config._config.get("browser_profile")
            config._config["browser_profile"] = {
                "enabled": True,
                "root": str(Path(directory) / "profiles"),
                "headless": True,
                "max_concurrent": 1,
            }
            db = Database(str(Path(directory) / "unused.db"))
            try:
                with patch.dict(sys.modules, {"cloakbrowser": object()}):
                    services = create_services(db)
                self.assertIsNotNone(services.token_manager.profile_refresher)
                self.assertEqual(
                    services.token_manager.profile_refresher.manager.root_dir,
                    Path(directory) / "profiles",
                )
            finally:
                if original is None:
                    config._config.pop("browser_profile", None)
                else:
                    config._config["browser_profile"] = original

    def test_browser_profile_default_root_is_persistent_data_directory(self):
        from src.services.factory import config

        original = config._config.get("browser_profile")
        try:
            config._config["browser_profile"] = {}
            self.assertEqual(config.browser_profile_root, "data/browser_profiles")
        finally:
            if original is None:
                config._config.pop("browser_profile", None)
            else:
                config._config["browser_profile"] = original
