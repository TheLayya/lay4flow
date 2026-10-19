"""Concurrency manager for token-based rate limiting"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional

from ..core.logger import debug_logger


class ConcurrencyManager:
    """Manages concurrent request limits for each token"""

    def __init__(self):
        """Initialize concurrency manager"""
        # token_id -> max concurrency limit (only stores >0 values, missing means unlimited)
        self._image_limits: Dict[int, int] = {}
        self._video_limits: Dict[int, int] = {}
        # token_id -> current in-flight requests
        self._image_inflight: Dict[int, int] = {}
        self._video_inflight: Dict[int, int] = {}
        self._lock = asyncio.Lock()  # Protect concurrent access
        self._maintenance: set[int] = set()
        # Conditions exist only while callers wait; unrelated accounts are not woken.
        self._slot_conditions: dict[tuple[str, int], tuple[asyncio.Condition, int]] = {}

    def _notify_slot_waiters(self, kind: str, token_id: int):
        """Called with _lock held after capacity or account membership changes."""
        entry = self._slot_conditions.get((kind, token_id))
        if entry is not None:
            entry[0].notify_all()

    async def initialize(self, tokens: list):
        """
        Initialize concurrency counters from token list

        Args:
            tokens: List of Token objects with image_concurrency and video_concurrency fields
        """
        async with self._lock:
            self._image_limits.clear()
            self._video_limits.clear()

            # A new manager starts empty. Reloading limits must retain live leases.
            token_ids = {token.id for token in tokens}
            for inflight in (self._image_inflight, self._video_inflight):
                for token_id in list(inflight):
                    if token_id not in token_ids:
                        inflight.pop(token_id)

            for token in tokens:
                self._image_inflight.setdefault(token.id, 0)
                self._video_inflight.setdefault(token.id, 0)

                if token.image_concurrency and token.image_concurrency > 0:
                    self._image_limits[token.id] = token.image_concurrency
                if token.video_concurrency and token.video_concurrency > 0:
                    self._video_limits[token.id] = token.video_concurrency

            for condition, _ in self._slot_conditions.values():
                condition.notify_all()

            debug_logger.log_info(f"Concurrency manager initialized with {len(tokens)} tokens")

    async def can_use_image(self, token_id: int) -> bool:
        """
        Check if token can be used for image generation

        Args:
            token_id: Token ID

        Returns:
            True if token has available image concurrency, False if concurrency is 0
        """
        async with self._lock:
            if token_id in self._maintenance:
                return False
            limit = self._image_limits.get(token_id)
            # Missing limit means unlimited (-1)
            if limit is None:
                return True

            inflight = self._image_inflight.get(token_id, 0)
            if inflight >= limit:
                debug_logger.log_info(
                    f"Token {token_id} image concurrency exhausted (inflight: {inflight}/{limit})"
                )
                return False

            return True

    async def can_use_video(self, token_id: int) -> bool:
        """
        Check if token can be used for video generation

        Args:
            token_id: Token ID

        Returns:
            True if token has available video concurrency, False if concurrency is 0
        """
        async with self._lock:
            if token_id in self._maintenance:
                return False
            limit = self._video_limits.get(token_id)
            # Missing limit means unlimited (-1)
            if limit is None:
                return True

            inflight = self._video_inflight.get(token_id, 0)
            if inflight >= limit:
                debug_logger.log_info(
                    f"Token {token_id} video concurrency exhausted (inflight: {inflight}/{limit})"
                )
                return False

            return True

    async def acquire_image(self, token_id: int) -> bool:
        """
        Acquire image concurrency slot

        Args:
            token_id: Token ID

        Returns:
            True if acquired, False if not available
        """
        async with self._lock:
            if token_id in self._maintenance:
                return False
            limit = self._image_limits.get(token_id)
            inflight = self._image_inflight.get(token_id, 0)

            if limit is not None and inflight >= limit:
                return False

            new_inflight = inflight + 1
            self._image_inflight[token_id] = new_inflight
            if limit is None:
                debug_logger.log_info(
                    f"Token {token_id} acquired image slot (inflight: {new_inflight}, limit: unlimited)"
                )
            else:
                debug_logger.log_info(
                    f"Token {token_id} acquired image slot (inflight: {new_inflight}/{limit})"
                )
            return True

    async def wait_acquire_image(self, token_id: int, timeout_seconds: float) -> tuple[bool, int]:
        """Wait for image capacity, notified when that account releases a slot."""
        return await self._wait_acquire("image", token_id, timeout_seconds)

    async def wait_acquire_video(self, token_id: int, timeout_seconds: float) -> tuple[bool, int]:
        """Wait for video capacity, independently of image capacity."""
        return await self._wait_acquire("video", token_id, timeout_seconds)

    async def _wait_acquire(
        self, kind: str, token_id: int, timeout_seconds: float
    ) -> tuple[bool, int]:
        wait_started = time.monotonic()
        timeout_seconds = max(1.0, float(timeout_seconds or 1.0))
        limits = self._image_limits if kind == "image" else self._video_limits
        inflight = self._image_inflight if kind == "image" else self._video_inflight
        key = (kind, token_id)
        async with self._lock:
            if token_id not in inflight:
                return False, int((time.monotonic() - wait_started) * 1000)
            condition, count = self._slot_conditions.get(key, (asyncio.Condition(self._lock), 0))
            self._slot_conditions[key] = (condition, count + 1)
            try:
                async with asyncio.timeout(timeout_seconds):
                    await condition.wait_for(
                        lambda: (
                            token_id not in inflight
                            or (
                                token_id not in self._maintenance
                                and (
                                    token_id not in limits or inflight[token_id] < limits[token_id]
                                )
                            )
                        )
                    )
                if token_id not in inflight:
                    return False, int((time.monotonic() - wait_started) * 1000)
                inflight[token_id] += 1
                return True, int((time.monotonic() - wait_started) * 1000)
            except TimeoutError:
                return False, int((time.monotonic() - wait_started) * 1000)
            finally:
                # No await after increment: cancellation cannot strand a lease
                # between acquisition and handing it back to the caller.
                _, count = self._slot_conditions[key]
                if count == 1:
                    self._slot_conditions.pop(key)
                else:
                    self._slot_conditions[key] = (condition, count - 1)

    async def acquire_video(self, token_id: int) -> bool:
        """
        Acquire video concurrency slot

        Args:
            token_id: Token ID

        Returns:
            True if acquired, False if not available
        """
        async with self._lock:
            if token_id in self._maintenance:
                return False
            limit = self._video_limits.get(token_id)
            inflight = self._video_inflight.get(token_id, 0)

            if limit is not None and inflight >= limit:
                return False

            new_inflight = inflight + 1
            self._video_inflight[token_id] = new_inflight
            if limit is None:
                debug_logger.log_info(
                    f"Token {token_id} acquired video slot (inflight: {new_inflight}, limit: unlimited)"
                )
            else:
                debug_logger.log_info(
                    f"Token {token_id} acquired video slot (inflight: {new_inflight}/{limit})"
                )
            return True

    async def release_image(self, token_id: int):
        """
        Release image concurrency slot

        Args:
            token_id: Token ID
        """
        async with self._lock:
            inflight = self._image_inflight.get(token_id, 0)
            if inflight <= 0:
                debug_logger.log_warning(f"Token {token_id} release_image called with inflight=0")
                return

            new_inflight = inflight - 1
            self._image_inflight[token_id] = new_inflight
            self._notify_slot_waiters("image", token_id)
            limit = self._image_limits.get(token_id)
            if limit is None:
                debug_logger.log_info(
                    f"Token {token_id} released image slot (inflight: {new_inflight}, limit: unlimited)"
                )
            else:
                debug_logger.log_info(
                    f"Token {token_id} released image slot (inflight: {new_inflight}/{limit})"
                )

    async def release_video(self, token_id: int):
        """
        Release video concurrency slot

        Args:
            token_id: Token ID
        """
        async with self._lock:
            inflight = self._video_inflight.get(token_id, 0)
            if inflight <= 0:
                debug_logger.log_warning(f"Token {token_id} release_video called with inflight=0")
                return

            new_inflight = inflight - 1
            self._video_inflight[token_id] = new_inflight
            self._notify_slot_waiters("video", token_id)
            limit = self._video_limits.get(token_id)
            if limit is None:
                debug_logger.log_info(
                    f"Token {token_id} released video slot (inflight: {new_inflight}, limit: unlimited)"
                )
            else:
                debug_logger.log_info(
                    f"Token {token_id} released video slot (inflight: {new_inflight}/{limit})"
                )

    async def get_image_remaining(self, token_id: int) -> Optional[int]:
        """
        Get remaining image concurrency for token

        Args:
            token_id: Token ID

        Returns:
            Remaining count or None if no limit
        """
        async with self._lock:
            limit = self._image_limits.get(token_id)
            if limit is None:
                return None
            inflight = self._image_inflight.get(token_id, 0)
            return max(0, limit - inflight)

    async def get_video_remaining(self, token_id: int) -> Optional[int]:
        """
        Get remaining video concurrency for token

        Args:
            token_id: Token ID

        Returns:
            Remaining count or None if no limit
        """
        async with self._lock:
            limit = self._video_limits.get(token_id)
            if limit is None:
                return None
            inflight = self._video_inflight.get(token_id, 0)
            return max(0, limit - inflight)

    async def get_image_inflight(self, token_id: int) -> int:
        """Get current in-flight image request count for token"""
        async with self._lock:
            return self._image_inflight.get(token_id, 0)

    async def get_video_inflight(self, token_id: int) -> int:
        """Get current in-flight video request count for token"""
        async with self._lock:
            return self._video_inflight.get(token_id, 0)

    async def snapshot(self) -> dict[int, tuple[int, int]]:
        """Return one consistent, lock-bounded view of per-token in-flight work.

        Runtime metrics and schedulers should use this instead of taking the
        same global lock once per account.  The returned mapping is detached
        from the manager and is safe to retain for the duration of a request.
        """
        async with self._lock:
            token_ids = set(self._image_inflight) | set(self._video_inflight)
            return {
                token_id: (
                    int(self._image_inflight.get(token_id, 0)),
                    int(self._video_inflight.get(token_id, 0)),
                )
                for token_id in token_ids
            }

    @asynccontextmanager
    async def account_maintenance(self, token_id: int):
        """Exclude new leases while an idle account changes its proxy."""
        async with self._lock:
            if (
                token_id in self._maintenance
                or self._image_inflight.get(token_id, 0)
                or self._video_inflight.get(token_id, 0)
            ):
                raise ValueError(
                    "Account is busy; retry the proxy switch after current work finishes"
                )
            self._maintenance.add(token_id)
        try:
            yield
        finally:
            async with self._lock:
                self._maintenance.discard(token_id)
                self._notify_slot_waiters("image", token_id)
                self._notify_slot_waiters("video", token_id)

    async def reset_token(
        self, token_id: int, image_concurrency: int = -1, video_concurrency: int = -1
    ):
        """
        Reset concurrency counters for a token

        Args:
            token_id: Token ID
            image_concurrency: New image concurrency limit (-1 for no limit)
            video_concurrency: New video concurrency limit (-1 for no limit)
        """
        async with self._lock:
            if image_concurrency > 0:
                self._image_limits[token_id] = image_concurrency
            elif token_id in self._image_limits:
                del self._image_limits[token_id]

            if video_concurrency > 0:
                self._video_limits[token_id] = video_concurrency
            elif token_id in self._video_limits:
                del self._video_limits[token_id]

            # 重置时确保存在 in-flight 计数字段
            self._image_inflight.setdefault(token_id, 0)
            self._video_inflight.setdefault(token_id, 0)

            self._notify_slot_waiters("image", token_id)
            self._notify_slot_waiters("video", token_id)

            debug_logger.log_info(
                f"Token {token_id} concurrency reset (image: {image_concurrency}, video: {video_concurrency})"
            )

    async def remove_token(self, token_id: int):
        """Remove all concurrency state for a deleted token."""
        async with self._lock:
            self._image_limits.pop(token_id, None)
            self._video_limits.pop(token_id, None)
            self._image_inflight.pop(token_id, None)
            self._video_inflight.pop(token_id, None)
            self._notify_slot_waiters("image", token_id)
            self._notify_slot_waiters("video", token_id)
            debug_logger.log_info(f"Token {token_id} concurrency state removed")
