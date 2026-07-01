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

from telethon import TelegramClient, errors, events

from config.constants import AccountStatus
from core import update_router
from helpers.dead_account import is_dead_error
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
    # 单个号启动连接超时(秒),防某个号卡死拖垮整个启动
    _CONNECT_TIMEOUT = 15.0

    async def startup(self) -> None:
        """服务启动:重置遗留状态,再把"已登录(3)"和"需重新登录(5)"的号都试连。

        协议号 session 是已登录态凭证,connect 即生效不需验证码,所以状态 5 的号
        若只是上次临时连接失败被误标,本次能连上就自动恢复成状态 3。
        连接结果分三类:
          - session 已失效/被作废(未授权 / AuthKeyDuplicated):终态无用 → 判死进死号列表;
          - 超时 / 其它未知异常:可能临时故障 → 维持 NEED_RELOGIN(5)留主列表,下次再试;
          - 连上且已授权:恢复为已登录(3)。
        """
        await run_db(account_repo.reset_all_status_on_startup)
        accounts = await run_db(
            account_repo.find_by_statuses,
            [int(AccountStatus.LOGGED_IN), int(AccountStatus.NEED_RELOGIN)],
        )
        logger.info("启动全起:发现 %d 个小号待试连(含需重新登录的)", len(accounts))
        for acc in accounts:
            try:
                await asyncio.wait_for(self._connect_account(acc), timeout=self._CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("全起小号超时(%.0fs),跳过 phone=%s", self._CONNECT_TIMEOUT, acc.phone)
                await run_db(account_repo.update_status, acc.phone, int(AccountStatus.NEED_RELOGIN))
            except errors.AuthKeyDuplicatedError as exc:
                # session 在多个 IP 同时使用被 TG 作废,是明确终态失效、无法恢复,
                # 该号对运营已无用 → 判死进死号列表(不是临时故障,不刷堆栈)。
                logger.warning(
                    "小号 %s 的 session 已被 Telegram 作废(同一 session 在多个 IP 使用),判定死号",
                    acc.phone,
                )
                await run_db(account_repo.mark_dead, acc.phone,
                             type(exc).__name__, "启动连接时 session 被作废(多 IP 撞车)")
            except Exception as exc:
                # connect()/get_me() 直接抛出的终态死号异常(UnauthorizedError 系等)会落到
                # 这里:该号已不可恢复,判死进死号列表,不再降级为 NEED_RELOGIN 赖在主列表。
                # (此时号尚未进池,直接 mark_dead 即可,无需出池。)
                if is_dead_error(exc):
                    logger.warning("全起小号死号异常 phone=%s reason=%s", acc.phone, type(exc).__name__)
                    await run_db(account_repo.mark_dead, acc.phone, type(exc).__name__, str(exc))
                else:
                    logger.exception("全起小号失败 phone=%s", acc.phone)
                    await run_db(account_repo.update_status, acc.phone, int(AccountStatus.NEED_RELOGIN))

    async def _connect_account(self, account) -> None:
        client = telethon_factory.build_client_for_account(account)
        registered = False
        try:
            await client.connect()
            if not await client.is_user_authorized():
                # 启动全起时 session 已失效:authkey 不被服务端承认,该号对运营已无用
                # (协议号无法靠验证码重登,手机号也需重新走登录流程)。直接判死进死号
                # 列表,由运营复活或彻底删除,不再赖在主列表里反复点「重新登录」也救不活。
                logger.warning("小号 %s 会话已失效,判定死号", account.phone)
                await run_db(account_repo.mark_dead, account.phone,
                             "SessionInvalid", "启动连接时 session 已失效(未授权)")
                return
            me = await client.get_me()
            self._register_events(account.phone, client)
            self._clients[account.phone] = client
            registered = True  # 已进池,交给池管理,后续不在此 disconnect
            await run_db(
                account_repo.mark_logged_in,
                account.phone,
                getattr(me, "id", None),
                getattr(me, "first_name", None),
                getattr(me, "last_name", None),
                getattr(me, "username", None),
            )
            logger.info("小号 %s 重连成功 tg_user_id=%s", account.phone, getattr(me, "id", None))
        finally:
            # 未进池的 client(失效/异常)一律断开,防半开连接泄漏
            if not registered:
                try:
                    await client.disconnect()
                except Exception:
                    pass

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

    # 单个 client disconnect 超时(秒):防 Telethon 半开连接阻塞拖住整个服务关闭
    _DISCONNECT_TIMEOUT = 5.0

    async def shutdown(self) -> None:
        """服务关闭:断开所有 client。逐个加超时,半开连接超时即放弃(进程退出由 OS 回收)。"""
        phones = list(self._clients.keys())
        for phone in phones:
            try:
                await asyncio.wait_for(self.remove(phone), timeout=self._DISCONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("断开 client 超时(%.0fs),跳过 phone=%s", self._DISCONNECT_TIMEOUT, phone)
            except Exception as exc:
                logger.warning("断开 client 异常 phone=%s", phone, exc_info=exc)
        logger.info("已断开全部 %d 个小号 client", len(phones))


# 全局单例
client_manager = ClientManager()
