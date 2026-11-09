"""Recover closed Profile caches without opening a browser or touching login state."""

import asyncio
import logging

_logger = logging.getLogger(__name__)


class ProfileCacheMaintenance:
    def __init__(self, manager, *, interval=3600):
        self.manager = manager
        self.interval = interval
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()
        self.last_result = None

    def start(self):
        if (
            self.manager is not None
            and not self._closed
            and (self._task is None or self._task.done())
        ):
            self._task = asyncio.create_task(self._run())

    async def tick(self):
        if self._closed or self.manager is None or self._lock.locked():
            return
        async with self._lock:
            self.last_result = await self.manager.prune_idle_caches()
            return self.last_result

    async def _run(self):
        while not self._closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Cache failures must not change a login state or kill the service.
                _logger.warning("Idle Profile cache recovery incomplete")
            await asyncio.sleep(self.interval)

    async def close(self):
        self._closed = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        cleanup = asyncio.gather(task, return_exceptions=True)
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
            cleanup.result()
            raise
