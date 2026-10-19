"""Authentication rejects unconfigured credentials without changing stored secrets."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.security import HTTPAuthorizationCredentials

from src.core import auth
from src.core.config import UNCONFIGURED_ADMIN_PASSWORD, UNCONFIGURED_API_KEY


class DefaultCredentialTests(unittest.TestCase):
    def test_default_admin_credentials_are_verified_without_enabling_api_placeholder(self):
        with patch.object(
            auth,
            "config",
            SimpleNamespace(
                api_key=UNCONFIGURED_API_KEY, admin_username="admin", admin_password="admin"
            ),
        ):
            self.assertTrue(auth.AuthManager.verify_admin("admin", "admin"))
            self.assertFalse(auth.AuthManager.verify_api_key(UNCONFIGURED_API_KEY))

    def test_matching_unconfigured_credentials_are_rejected(self):
        for api_key, password in (
            (UNCONFIGURED_API_KEY, UNCONFIGURED_ADMIN_PASSWORD),
            ("", ""),
            (None, None),
        ):
            with (
                self.subTest(api_key=api_key),
                patch.object(
                    auth,
                    "config",
                    SimpleNamespace(
                        api_key=api_key, admin_username="admin", admin_password=password
                    ),
                ),
            ):
                self.assertFalse(auth.AuthManager.verify_api_key(api_key))
                self.assertFalse(auth.AuthManager.verify_admin("admin", password))

    def test_configured_credentials_are_verified(self):
        with patch.object(
            auth,
            "config",
            SimpleNamespace(
                api_key="synthetic-key", admin_username="owner", admin_password="synthetic-password"
            ),
        ):
            self.assertTrue(auth.AuthManager.verify_api_key("synthetic-key"))
            self.assertFalse(auth.AuthManager.verify_api_key("wrong"))
            self.assertTrue(auth.AuthManager.verify_admin("owner", "synthetic-password"))
            self.assertFalse(auth.AuthManager.verify_admin("wrong", "synthetic-password"))
            self.assertFalse(auth.AuthManager.verify_admin("owner", "wrong"))

    def test_empty_admin_identity_is_rejected(self):
        with patch.object(
            auth,
            "config",
            SimpleNamespace(api_key="synthetic-key", admin_username="", admin_password="password"),
        ):
            self.assertFalse(auth.AuthManager.verify_admin("", "password"))

    def test_openai_and_gemini_auth_dependencies_accept_their_documented_inputs(self):
        async def check():
            with patch.object(
                auth,
                "config",
                SimpleNamespace(
                    api_key="synthetic-key", admin_username="owner", admin_password="password"
                ),
            ):
                bearer = await auth.verify_openai_api_key(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials="synthetic-key")
                )
                google_header = await auth.verify_api_key_flexible(
                    credentials=None, x_goog_api_key="synthetic-key", key=None
                )
                query_key = await auth.verify_api_key_flexible(
                    credentials=None, x_goog_api_key=None, key="synthetic-key"
                )
                return bearer, google_header, query_key

        self.assertEqual(asyncio.run(check()), ("synthetic-key",) * 3)
