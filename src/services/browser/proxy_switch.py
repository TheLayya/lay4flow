"""Validate an intentional proxy change on a closed copy before replacing account state."""

import asyncio
import json
import logging
import shutil
import stat
import tempfile
from pathlib import Path

from .profiles import ProfileManager, _ProfileFileLock

_logger = logging.getLogger(__name__)


def _copy_profile(source: Path, target: Path) -> None:
    """Never follow a Profile link into unrelated data while making the private stage."""
    for path in (source, *source.rglob("*")):
        attributes = path.lstat()
        if stat.S_ISLNK(attributes.st_mode) or (
            getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("Profile links require manual migration")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".profile.lock"))


async def switch_profile_proxy(
    manager, account_key, old_proxy_url, new_proxy_url, verifier, validate, commit
):
    """Keep original ownership until stage validation, close and DB compare-and-swap finish."""
    normalized = manager._normalize_account_key(account_key)
    account_lock = await manager._account_lock(normalized)
    if account_lock.locked():
        raise ValueError("账号浏览器或登录态正在使用，请结束后再切换代理")
    async with account_lock:
        if normalized in manager._failed_handles:
            raise ValueError("账号浏览器尚未完成关闭，请检查服务状态")
        await manager._drop_warm_locked(normalized)
        spec = await manager.spec(account_key, existing_only=True)
        await manager._reserve_capacity()
        original_lock = _ProfileFileLock(spec.profile_dir / ".profile.lock", timeout=0)
        try:
            await original_lock.acquire()
            stage_root = Path(tempfile.mkdtemp(prefix=".proxy-switch-", dir=manager.root_dir))
        except BaseException:
            original_lock.release()
            await manager._release_capacity()
            raise
        staged = stage_root / normalized
        backup = stage_root / "original"
        saved = []
        installed = []
        cleanup_allowed = True
        committed = False
        stage_manager = ProfileManager(stage_root, manager.backend)
        try:
            backup.mkdir()
            copying = asyncio.create_task(
                asyncio.to_thread(_copy_profile, spec.profile_dir, staged)
            )
            try:
                await asyncio.shield(copying)
            except asyncio.CancelledError:
                await manager._wait_uninterrupted(copying)
                raise
            migrate = getattr(manager.backend, "migrate_proxy_settings", None)
            if not callable(migrate):
                raise ValueError("浏览器后端尚未支持代理切换")
            migrate(staged, old_proxy_url=old_proxy_url, new_proxy_url=new_proxy_url)
            async with stage_manager.credential_session(
                account_key, verifier, proxy_url=new_proxy_url, existing_only=True
            ) as context:
                updates = await validate(stage_manager, context)
            # Prove the renewed state survives a cold close/reopen on the new exit.
            async with stage_manager.credential_session(
                account_key, verifier, proxy_url=new_proxy_url, existing_only=True
            ) as context:
                updates = await validate(stage_manager, context)
            metadata = json.loads((staged / "metadata.json").read_text(encoding="utf-8"))
            metadata["profile_dir"] = str(spec.profile_dir)
            manager._write_metadata_file(staged / "metadata.json", metadata)
            # Keep the original .profile.lock inode open while moving closed state.
            for child in spec.profile_dir.iterdir():
                if child.name != ".profile.lock":
                    child.replace(backup / child.name)
                    saved.append(child.name)
            for child in staged.iterdir():
                if child.name != ".profile.lock":
                    child.replace(spec.profile_dir / child.name)
                    installed.append(child.name)
            writing = asyncio.create_task(commit(updates))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                await manager._wait_uninterrupted(writing)
            committed = True
        except BaseException:
            try:
                if not committed:
                    for name in reversed(installed):
                        (spec.profile_dir / name).replace(staged / name)
                    for name in reversed(saved):
                        (backup / name).replace(spec.profile_dir / name)
            except BaseException:
                cleanup_allowed = False
                manager._failed_handles[normalized] = None
                raise
            raise
        finally:
            original_lock.release()
            await manager._release_capacity()
            # A failed close retains its stage and ownership; never erase live Chromium files.
            if not stage_manager._failed_handles and cleanup_allowed:
                try:
                    shutil.rmtree(stage_root)
                except OSError:
                    _logger.warning("Private proxy-switch cleanup incomplete")
            else:
                manager._proxy_switch_failed_stages.append(stage_manager)
                # A still-live staged Chromium has the same account identity.
                # Block a second original-Profile launch until service recovery.
                for failed in stage_manager._failed_handles.values():
                    manager._failed_handles[normalized] = failed
