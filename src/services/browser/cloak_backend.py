"""Optional CloakBrowser backend for persistent account profiles."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .cloak_proxy import CloakProxyBridge


class CloakBrowserBackend:
    def __init__(self, *, headless: bool = True, stealth_args: bool = False, **kwargs: Any):
        self.options = {"headless": headless, "stealth_args": stealth_args, **kwargs}
        self._proxy_bridges: dict[int, CloakProxyBridge] = {}

    async def open(self, profile_dir: Path, *, proxy_url: str | None = None) -> Any:
        return await self._open(profile_dir, proxy_url=proxy_url)

    async def open_background(self, profile_dir: Path, *, proxy_url: str | None = None) -> Any:
        return await self._open(
            profile_dir,
            proxy_url=proxy_url,
            display=os.environ.get("LAY4FLOW_PROFILE_BACKGROUND_DISPLAY"),
            # Background maintenance must never inherit the interactive
            # presentation setting.  Windows has no isolated DISPLAY, so a
            # headed backend here would flash a browser for every request.
            headless=True,
        )

    async def open_interactive(self, profile_dir: Path, *, proxy_url: str | None = None) -> Any:
        # The management viewer is a real local window on Windows. Background
        # maintenance may stay headless; this scoped override does not change
        # the profile identity digest (headless is presentation-only).
        return await self._open(profile_dir, proxy_url=proxy_url, headless=False)

    async def _open(
        self,
        profile_dir: Path,
        *,
        proxy_url: str | None = None,
        display: str | None = None,
        headless: bool | None = None,
    ) -> Any:
        try:
            import cloakbrowser
        except ImportError as error:
            raise RuntimeError("CloakBrowser is not installed") from error
        options = self._launch_options(profile_dir, proxy_url)
        if headless is not None:
            options["headless"] = headless
        self._check_launch_settings(profile_dir, options)
        if self._session_restore_enabled(profile_dir):
            self._prepare_restore_preferences(profile_dir)
        # Keep headed Profile identity unchanged; background windows use a
        # separate desktop that is never exposed through the viewer's VNC.
        if display:
            options["env"] = {**os.environ, **options.get("env", {}), "DISPLAY": display}
        bridge = None
        proxy = options.get("proxy")
        if isinstance(proxy, str):
            parsed = urlsplit(proxy)
            if (
                parsed.scheme in {"socks5", "socks5h"}
                or parsed.username is not None
                or os.getenv("LAY4FLOW_HOST_PROXY", "").strip()
            ):
                # Cloak passes these proxies only through Chromium's command
                # line, leaving context.request on a different exit. A local
                # HTTP bridge gives both clients Playwright's proxy settings.
                # The stable Profile hash above still binds the real proxy.
                bridge = await CloakProxyBridge.start(proxy)
                options["proxy"] = bridge.url
        elif os.getenv("LAY4FLOW_HOST_PROXY", "").strip():
            # The host transport is added after validating the stable Profile
            # options, so toggling a local VPN does not change its identity.
            bridge = await CloakProxyBridge.start(
                os.environ["LAY4FLOW_HOST_PROXY"].strip(), host_proxy=""
            )
            options["proxy"] = bridge.url
        try:
            context = await cloakbrowser.launch_persistent_context_async(
                str(profile_dir), **options
            )
        except BaseException:
            if bridge is not None:
                await bridge.close()
            raise
        if bridge is not None:
            self._proxy_bridges[id(context)] = bridge
        return context

    def _launch_options(self, profile_dir: Path, proxy_url: str | None) -> dict[str, Any]:
        options = dict(self.options)
        if proxy_url is not None:
            # Chromium accepts socks5:// here; socks5h:// is the HTTP client's
            # remote-DNS spelling and is rejected by Chromium navigation.
            browser_proxy = proxy_url or ""
            if browser_proxy.startswith("socks5h://"):
                browser_proxy = "socks5://" + browser_proxy[len("socks5h://") :]
            options["proxy"] = browser_proxy or None
        args = list(options.pop("args", ()))
        if any(arg == "--fingerprint" or arg.startswith("--fingerprint=") for arg in args):
            raise ValueError("fingerprint seed is owned by the profile manager")
        for flag in (
            "--disable-breakpad",
            "--disable-crash-reporter",
            # Chromium 146 blocks third-party cookies by default (Tracking
            # Protection / 3PCD). Google's Flow login spans accounts.google.com
            # and flow.google.com, so blocking cross-site cookies drops the
            # session on the next flow_bootstrap probe ("login_required").
            "--disable-features=TrackingProtection3pcd",
        ):
            if flag not in args:
                args.append(flag)
        if self._session_restore_enabled(profile_dir) and "--restore-last-session" not in args:
            args.append("--restore-last-session")
        args.append(f"--fingerprint={self._profile_seed(profile_dir)}")
        options["args"] = args
        return options

    @staticmethod
    def _session_restore_enabled(profile_dir: Path) -> bool:
        marker = profile_dir / "session_restore.json"
        if marker.is_symlink():
            raise ValueError("Profile session restoration path is invalid")
        try:
            values = json.loads(marker.read_text(encoding="utf-8"))
            return (
                isinstance(values, dict)
                and type(values.get("version")) is int
                and values["version"] == 1
            )
        except (OSError, ValueError, AttributeError):
            return False

    @staticmethod
    def _prepare_restore_preferences(profile_dir: Path) -> None:
        """Preserve session cookies without inventing their server-side expiry."""
        preference = profile_dir / "Default" / "Preferences"
        if preference.is_symlink() or preference.parent.is_symlink():
            raise ValueError("Profile preferences path is invalid")
        if preference.exists():
            values = json.loads(preference.read_text(encoding="utf-8"))
            if not isinstance(values, dict) or not isinstance(values.get("session", {}), dict):
                raise ValueError("Profile preferences are invalid")
        else:
            values = {}
        values.setdefault("session", {})["restore_on_startup"] = 1
        preference.parent.mkdir(parents=True, exist_ok=True)
        temporary = preference.with_suffix(".restore.tmp")
        if temporary.is_symlink():
            raise ValueError("Profile preferences path is invalid")
        temporary.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        temporary.replace(preference)

    @staticmethod
    def prepare_session_restore(profile_dir: Path) -> None:
        """Opt a new, closed Profile into persistent browser session restoration.

        Existing Profiles keep their exact launch digest. Only Profiles that
        already opted in can call this again after their first browser launch.
        """
        profile_dir = Path(profile_dir)
        metadata = profile_dir / "metadata.json"
        if metadata.is_symlink() or profile_dir.is_symlink():
            raise ValueError("Profile metadata path is invalid")
        metadata_values = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(metadata_values, dict):
            raise ValueError("Profile metadata is invalid")
        CloakBrowserBackend._profile_seed(profile_dir)
        marker = profile_dir / "session_restore.json"
        temporary = marker.with_suffix(".restore.tmp")
        if marker.is_symlink() or temporary.is_symlink():
            raise ValueError("Profile session restoration path is invalid")
        if (
            profile_dir / "launch_settings.sha256"
        ).exists() and not CloakBrowserBackend._session_restore_enabled(profile_dir):
            raise ValueError("Existing Profile session restoration requires explicit migration")
        CloakBrowserBackend._prepare_restore_preferences(profile_dir)
        temporary.write_text(json.dumps({"version": 1}), encoding="utf-8")
        temporary.replace(marker)

    def migrate_proxy_settings(
        self, staged_profile: Path, *, old_proxy_url: str, new_proxy_url: str
    ) -> None:
        """Change only a staged copy's explicit proxy binding after checking its old hash."""
        if not (staged_profile / "launch_settings.sha256").is_file():
            raise ValueError("Existing Profile launch settings are required")
        self._check_launch_settings(
            staged_profile, self._launch_options(staged_profile, old_proxy_url)
        )
        options = self._launch_options(staged_profile, new_proxy_url)
        serialized = json.dumps(
            {key: value for key, value in options.items() if key != "headless"},
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary = staged_profile / "launch_settings.sha256.tmp"
        temporary.write_text(
            hashlib.sha256(serialized.encode("utf-8")).hexdigest(), encoding="utf-8"
        )
        temporary.replace(staged_profile / "launch_settings.sha256")

    async def close(self, context: Any) -> None:
        await context.close()
        # Retain the bridge when Chromium's close fails, just like the Profile
        # manager retains its lock/context so that closing can be retried.
        bridge = self._proxy_bridges.get(id(context))
        if bridge is not None:
            await bridge.close()
            self._proxy_bridges.pop(id(context), None)

    @staticmethod
    def _profile_seed(profile_dir: Path) -> int:
        metadata = profile_dir / "metadata.json"
        try:
            value = json.loads(metadata.read_text(encoding="utf-8")).get("fingerprint_seed")
            if isinstance(value, int) and not isinstance(value, bool) and 10000 <= value <= 99999:
                return value
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        raise RuntimeError("profile fingerprint seed is not initialized")

    @staticmethod
    def _check_launch_settings(profile_dir: Path, options: dict[str, Any]) -> None:
        def digest(values: dict[str, Any]) -> str:
            serialized = json.dumps(values, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

        # Showing an existing browser on the debugging desktop changes only its
        # presentation. Keep identity, proxy and every other launch option fixed.
        canonical = digest({key: value for key, value in options.items() if key != "headless"})
        settings = profile_dir / "launch_settings.sha256"
        try:
            with settings.open("x", encoding="utf-8") as output:
                output.write(canonical)
        except FileExistsError:
            accepted = {canonical, digest(options)}
            if isinstance(options.get("headless"), bool):
                accepted.add(digest({**options, "headless": not options["headless"]}))
            # Migrate only the client's socks5h spelling for this exact same
            # SOCKS server. Every other option, address and credential remains
            # bound to the original Profile summary.
            proxy = options.get("proxy")
            if isinstance(proxy, str) and proxy.startswith("socks5://"):
                legacy = {**options, "proxy": "socks5h://" + proxy[len("socks5://") :]}
                accepted.add(
                    digest({key: value for key, value in legacy.items() if key != "headless"})
                )
                accepted.add(digest(legacy))
                if isinstance(legacy.get("headless"), bool):
                    accepted.add(digest({**legacy, "headless": not legacy["headless"]}))
            stored = settings.read_text(encoding="utf-8")
            if stored not in accepted:
                raise ValueError("profile launch settings changed; explicit migration required")
            if stored != canonical:
                settings.write_text(canonical, encoding="utf-8")
