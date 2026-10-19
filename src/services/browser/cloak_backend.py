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
        )

    async def open_interactive(self, profile_dir: Path, *, proxy_url: str | None = None) -> Any:
        return await self._open(profile_dir, proxy_url=proxy_url)

    async def _open(
        self, profile_dir: Path, *, proxy_url: str | None = None, display: str | None = None
    ) -> Any:
        try:
            import cloakbrowser
        except ImportError as error:
            raise RuntimeError("CloakBrowser is not installed") from error
        options = self._launch_options(profile_dir, proxy_url)
        self._check_launch_settings(profile_dir, options)
        # Keep headed Profile identity unchanged; background windows use a
        # separate desktop that is never exposed through the viewer's VNC.
        if display:
            options["env"] = {**os.environ, **options.get("env", {}), "DISPLAY": display}
        bridge = None
        proxy = options.get("proxy")
        if isinstance(proxy, str):
            parsed = urlsplit(proxy)
            if parsed.scheme in {"socks5", "socks5h"} or parsed.username is not None:
                # Cloak passes these proxies only through Chromium's command
                # line, leaving context.request on a different exit. A local
                # HTTP bridge gives both clients Playwright's proxy settings.
                # The stable Profile hash above still binds the real proxy.
                bridge = await CloakProxyBridge.start(proxy)
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
        for flag in ("--disable-breakpad", "--disable-crash-reporter"):
            if flag not in args:
                args.append(flag)
        args.append(f"--fingerprint={self._profile_seed(profile_dir)}")
        options["args"] = args
        return options

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
