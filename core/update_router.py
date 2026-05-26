"""新消息事件分发。

ClientManager 给每个 client 注册 NewMessage 事件 → dispatch(phone, event)。
分发到已注册的 listener(关键字回复 M3、AI 应答真人 M4)。
listener 形如 async def(phone: str, event) -> None,异常隔离不互相影响。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List

logger = logging.getLogger(__name__)

# listener 签名:async (phone, telethon NewMessage event)
Listener = Callable[[str, object], Awaitable[None]]

_listeners: List[Listener] = []


def register(listener: Listener) -> None:
    if listener not in _listeners:
        _listeners.append(listener)


async def dispatch(phone: str, event: object) -> None:
    """并行喂给所有 listener,单个异常不影响其他。"""
    if not _listeners:
        return
    results = await asyncio.gather(
        *(_safe_call(listener, phone, event) for listener in _listeners),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            logger.exception("listener 处理消息异常: %s", r)


async def _safe_call(listener: Listener, phone: str, event: object) -> None:
    try:
        await listener(phone, event)
    except Exception:
        logger.exception("listener %s 异常 phone=%s", getattr(listener, "__name__", listener), phone)
