"""Bounded Flow workspace probes without identity or generation claims."""

import asyncio
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from .environment import EnvironmentSnapshot, EnvironmentSnapshotStore


@dataclass(frozen=True)
class ProfileVerification:
    workspace_visible: bool
    reason: str


@dataclass(frozen=True)
class AccountVerification:
    email_matches: bool
    session_valid: bool
    credits: int
    reason: str


class AccountProbe(Protocol):
    async def verify(self, expected_email: str) -> Any: ...


class ProfileVerifier:
    def __init__(self, *, timeout_seconds: float = 30, poll_seconds: float = 0.5):
        if not all(math.isfinite(value) and value > 0 for value in (timeout_seconds, poll_seconds)):
            raise ValueError("probe timing must be positive and finite")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    async def environment_snapshot(self, context: Any) -> EnvironmentSnapshot:
        page = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                page = await context.new_page()
                await page.goto("about:blank", wait_until="domcontentloaded")
                value = await page.evaluate(
                    """() => ({
                    userAgent: navigator.userAgent,
                    language: navigator.language,
                    languages: navigator.languages,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    platform: navigator.platform,
                    width: window.innerWidth || screen.width,
                    height: window.innerHeight || screen.height
                    })"""
                )
                return EnvironmentSnapshot.from_probe(value)
        finally:
            if page is not None:
                await asyncio.wait_for(page.close(), timeout=self.timeout_seconds)

    async def verify_environment(
        self, context: Any, profile_dir: str, *, allow_viewport_change: bool = False
    ) -> str:
        snapshot = await self.environment_snapshot(context)
        store = EnvironmentSnapshotStore(profile_dir)
        if allow_viewport_change and store.path.exists():
            stored = store.path.read_text(encoding="utf-8")
            baseline = EnvironmentSnapshot.from_json(stored)
            identity_fields = (
                "user_agent",
                "language",
                "languages",
                "timezone",
                "platform",
            )
            if any(
                getattr(baseline, field) != getattr(snapshot, field) for field in identity_fields
            ):
                return "environment_changed"
        elif not store.check_or_record(snapshot):
            return "environment_changed"
        return "environment_stable"

    async def verify_ready(self, context: Any, profile_dir: str) -> ProfileVerification:
        try:
            environment = await self.verify_environment(context, profile_dir)
        except TimeoutError:
            return ProfileVerification(False, "environment_timeout")
        except Exception:
            return ProfileVerification(False, "environment_probe_error")
        if environment != "environment_stable":
            return ProfileVerification(False, environment)
        return await self.verify(context)

    async def verify_account(
        self, expected_email: str, account_probe: AccountProbe
    ) -> AccountVerification:
        try:
            value = await account_probe.verify(expected_email)
            if not isinstance(value, dict):
                return AccountVerification(False, False, 0, "account_probe_error")
            probe_reason = str(value.get("reason") or "").strip()
            if probe_reason:
                return AccountVerification(False, False, 0, probe_reason)
            email = str(value.get("email") or "").strip()
            session_valid = value.get("session_valid") is True
            credits = value.get("credits", 0)
            if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
                return AccountVerification(False, session_valid, 0, "credits_invalid")
            if not email or email.casefold() != expected_email.strip().casefold():
                return AccountVerification(False, session_valid, credits, "identity_mismatch")
            if not session_valid:
                return AccountVerification(True, False, credits, "session_invalid")
            return AccountVerification(True, True, credits, "account_ready")
        except Exception:
            return AccountVerification(False, False, 0, "account_probe_error")

    async def verify(self, context: Any) -> ProfileVerification:
        page = None
        result = ProfileVerification(False, "probe_error")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                page = await context.new_page()
                await page.goto("https://flow.google.com/", wait_until="domcontentloaded")
                while True:
                    snapshot = await page.evaluate(
                        "() => ({url: location.href, ready: document.readyState, "
                        "text: document.body ? document.body.innerText : ''})"
                    )
                    observed = self._classify(snapshot)
                    if observed is not None:
                        result = observed
                        break
                    await asyncio.sleep(self.poll_seconds)
        except TimeoutError:
            result = ProfileVerification(False, "timeout")
        except Exception as error:
            if type(error).__name__ == "TimeoutError":
                result = ProfileVerification(False, "timeout")
            else:
                result = ProfileVerification(False, "probe_error")
        finally:
            if page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=self.timeout_seconds)
                except Exception:
                    result = ProfileVerification(False, "cleanup_error")
        return result

    @staticmethod
    def _classify(snapshot: dict) -> ProfileVerification | None:
        address = urlsplit(snapshot.get("url", ""))
        if address.hostname == "accounts.google.com":
            return ProfileVerification(False, "login_required")
        if address.scheme != "https" or address.hostname != "flow.google.com":
            return ProfileVerification(False, "unexpected_page")
        if snapshot.get("ready") != "complete":
            return None
        text = snapshot.get("text", "")
        if not text.strip():
            return None
        if address.path.rstrip("/") == "/about":
            return ProfileVerification(False, "public_page")
        if any(marker in text.casefold() for marker in ("已退出账号", "选择账号", "sign in")):
            return ProfileVerification(False, "login_required")
        if address.path != "/":
            return None
        icons = {line.strip().casefold() for line in text.splitlines()}
        has_projects = {"edit", "delete"}.issubset(icons)
        has_create = "新建项目" in text or re.search(r"\bnew project\b", text, re.I)
        if has_projects or has_create:
            return ProfileVerification(True, "workspace_visible")
        return None
