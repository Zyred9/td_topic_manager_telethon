"""asyncio 后台任务跟踪与优雅关闭(参考 baihui TaskTracker)。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Set

logger = logging.getLogger(__name__)


class TaskTracker:
    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task[Any]] = set()

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        async def _wrapped() -> Any:
            try:
                return await coro
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("后台任务异常: %s", exc)
                raise

        task = asyncio.create_task(_wrapped())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel_all(self, timeout: float = 15.0) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for t in tasks:
            if not t.done():
                t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("后台任务取消超时,强制结束")


# 全局单例
task_tracker = TaskTracker()
