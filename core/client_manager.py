"""TelegramClient 客户端池(单进程多 client,共享主 event loop)。

职责:
- 维护 phone -> TelegramClient
- 启动全起:扫已登录小号逐个 connect + 授权校验,注册 NewMessage 事件
- 失效处理:连接断开/未授权 → status=5 需重新登录,移出池
- 给 message_sender / 各 service 提供就绪 client
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from telethon import TelegramClient, events

from config.constants import AccountStatus
from core import update_router
from infra import telethon_factory
from infra.db import run_db
from repositories import account_repo

logger = logging.getLogger(__name__)


class ClientManager:
    def __init__(self) -> None:
        self._clients: Dict[str, TelegramClient] = {}
        self._lock = asyncio.Lock()

    # ---------- 查询 ----------
    def get_client(self, phone: str) -> Optional[TelegramClient]:
        return self._clients.get(phone)

    def get_ready_client(self, phone: str) -> Optional[TelegramClient]:
        """返回已连接的 client,否则 None。"""
        client = self._clients.get(phone)
        if client is not None and client.is_connected():
            return client
        return None

    def is_logged_in(self, phone: str) -> bool:
        return phone in self._clients

    def all_phones(self) -> list[str]:
        return list(self._clients.keys())

    # ---------- 全起 ----------
    async def startup(self) -> None:
        """服务启动:重置遗留状态,再把已登录(status=3)的号重连验证。"""
        await run_db(account_repo.reset_all_status_on_startup)
        accounts = await run_db(account_repo.find_by_status, int(AccountStatus.LOGGED_IN))
        logger.info("启动全起:发现 %d 个已登录小号待重连", len(accounts))
        for acc in accounts:
            try:
                await self._connect_account(acc)
            except Exception:
                logger.exception("全起小号失败 phone=%s", acc.phone)
                await run_db(account_repo.update_status, acc.phone, int(AccountStatus.NEED_RELOGIN))

    async def _connect_account(self, account) -> None:
        client = telethon_factory.build_client_for_account(account)
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("小号 %s 会话已失效,需重新登录", account.phone)
            await client.disconnect()
            await run_db(account_repo.update_status, account.phone, int(AccountStatus.NEED_RELOGIN))
            return
        me = await client.get_me()
        self._register_events(account.phone, client)
        self._clients[account.phone] = client
        await run_db(
            account_repo.mark_logged_in,
            account.phone,
            getattr(me, "id", None),
            getattr(me, "first_name", None),
            getattr(me, "last_name", None),
            getattr(me, "username", None),
        )
        logger.info("小号 %s 重连成功 tg_user_id=%s", account.phone, getattr(me, "id", None))

    # ---------- 注册 / 注销 ----------
    def _register_events(self, phone: str, client: TelegramClient) -> None:
        @client.on(events.NewMessage)
        async def _on_new_message(event):  # noqa: ANN001
            await update_router.dispatch(phone, event)

    async def register_ready_client(self, phone: str, client: TelegramClient) -> None:
        """登录服务(协议号/手机号)完成授权后,把 client 交给池统一持有。"""
        async with self._lock:
            old = self._clients.get(phone)
            if old is not None and old is not client:
                try:
                    await old.disconnect()
                except Exception:
                    pass
            self._register_events(phone, client)
            self._clients[phone] = client

    async def remove(self, phone: str) -> None:
        """移出池并断开(删除小号/失效时)。"""
        async with self._lock:
            client = self._clients.pop(phone, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.warning("断开 client 失败 phone=%s", phone)

    async def shutdown(self) -> None:
        """服务关闭:断开所有 client。"""
        phones = list(self._clients.keys())
        for phone in phones:
            await self.remove(phone)
        logger.info("已断开全部 %d 个小号 client", len(phones))


# 全局单例
client_manager = ClientManager()
