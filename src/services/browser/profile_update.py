"""Replace an existing Profile login after one isolated, fixed-exit validation."""

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from .profiles import ProfileManager, _ProfileFileLock
from .proxy_switch import _copy_profile

_logger = logging.getLogger(__name__)


async def update_profile_login(manager, account_key, proxy_url, verifier, validate, commit):
    """Preserve the old login until validated files and the DB update both succeed.

    ``validate(staged_manager, context)`` applies captured credentials and checks
    their identity once. ``commit(updates)`` must atomically compare/update the
    account, return False or raise on rejection, and not raise after committing.
    Cancellation after commit begins waits for its final result so the database
    and Profile cannot end up on different versions.
    """
    normalized = manager._normalize_account_key(account_key)
    account_lock = await manager._account_lock(normalized)
    if account_lock.locked():
        raise ValueError("账号浏览器或登录态正在使用，请结束后再导入")
    async with account_lock:
        if normalized in manager._failed_handles:
            raise ValueError("账号浏览器尚未完成关闭，请检查服务状态")
        await manager._drop_warm_locked(normalized)
        spec = await manager.spec(account_key, existing_only=True)
        root = manager.root_dir.resolve()
        if spec.profile_dir.is_symlink() or spec.profile_dir.resolve().parent != root:
            raise ValueError("Profile 路径异常，现有登录资料已保留")
        await manager._reserve_capacity()
        original_lock = _ProfileFileLock(spec.profile_dir / ".profile.lock", timeout=0)
        try:
            await original_lock.acquire()
            stage_root = Path(tempfile.mkdtemp(prefix=".login-update-", dir=root))
            if stage_root.is_symlink() or stage_root.resolve().parent != root:
                raise ValueError("Profile 暂存路径异常")
        except BaseException:
            original_lock.release()
            await manager._release_capacity()
            raise

        staged = stage_root / normalized
        backup = stage_root / "original"
        saved = []
        installed = []
        committed = False
        cleanup_allowed = True
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

            # Check the existing seed/proxy/options; no proxy migration or seed
            # allocation is permitted when replacing the same account login.
            backend = manager.backend
            backend._check_launch_settings(staged, backend._launch_options(staged, proxy_url))
            # Reauthentication explicitly opts the closed copy into restoring
            # session cookies. Prove the original identity/options first; only
            # this private copy may acquire the additional restore flag.
            if not backend._session_restore_enabled(staged):
                (staged / "launch_settings.sha256").unlink()
                backend.prepare_session_restore(staged)
                backend._check_launch_settings(staged, backend._launch_options(staged, proxy_url))
            async with stage_manager.credential_session(
                account_key, verifier, proxy_url=proxy_url, existing_only=True
            ) as context:
                updates = await validate(stage_manager, context)

            checked = await stage_manager.spec(account_key, existing_only=True)
            if checked.fingerprint_seed != spec.fingerprint_seed:
                raise ValueError("Profile 身份已变化，现有登录资料已保留")
            backend._check_launch_settings(staged, backend._launch_options(staged, proxy_url))
            # This only updates closed metadata; it never launches a second
            # browser or repeats the account validation.
            await stage_manager.mark_verified(account_key)
            metadata = json.loads((staged / "metadata.json").read_text(encoding="utf-8"))
            metadata["profile_dir"] = str(spec.profile_dir)
            manager._write_metadata_file(staged / "metadata.json", metadata)

            # The original lock inode stays owned throughout replacement and
            # rollback. Both directories are verified descendants of root.
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
                result = await asyncio.shield(writing)
            except asyncio.CancelledError:
                result = await manager._wait_uninterrupted(writing)
            if result is False:
                raise ValueError("账号配置已变化，现有登录资料已保留")
            committed = True
            return updates
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
            if not stage_manager._failed_handles and cleanup_allowed:
                try:
                    # Recheck the resolved deletion target, including on Windows.
                    if stage_root.is_symlink() or stage_root.resolve().parent != root:
                        raise ValueError("Profile cleanup target changed")
                    shutil.rmtree(stage_root)
                except (OSError, ValueError):
                    _logger.warning("Private Profile login-update cleanup incomplete")
            else:
                # Reuse the existing retained-stage recovery ownership. A live
                # staged Chromium must block another copy of the same identity.
                manager._proxy_switch_failed_stages.append(stage_manager)
                for failed in stage_manager._failed_handles.values():
                    manager._failed_handles[normalized] = failed
